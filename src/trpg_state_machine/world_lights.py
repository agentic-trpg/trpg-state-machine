"""Ordinary scenario emitters; no fuel, magic, sunlight or effect lifecycle."""

from dataclasses import asdict, dataclass
from itertools import chain
from math import ceil, floor
from typing import NamedTuple

from dnd5e_engine.spatial import cell_id, parse_cell

from ._execution import _execution_data
from .errors import StateMachineError
from .light_geometry import (
    Point,
    Polygon,
    Segment,
    clip_to_bounds,
    lit_polygons,
    shadow_intersects_interior,
)
from .runtime_instances import instance_read, resolve_field_batch
from .world_commands import SetLightEnabled

_RANK = {"dark": 0, "dim": 1, "bright": 2}
_MAX_QUERY_CELLS = 256


class CellLight(NamedTuple):
    """Retain a cell's scalar light and clipped drawing shapes in one immutable result."""

    level: str
    unknown: bool = False
    sources: tuple[str, ...] = ()
    base: str = "dark"
    dim: tuple[Polygon, ...] = ()
    bright: tuple[Polygon, ...] = ()

    @classmethod
    def ambient(cls, level):
        """Use the map's base grade when no local source contributes."""
        return cls(level, base=level)

    def __deepcopy__(self, memo):
        """Share fixed lighting data, including immutable polygon vertices."""
        return self


@dataclass(frozen=True, slots=True)
class _SourceCoverage:
    """Hold local point-light coverage under definite and possible occlusion."""

    origin: Point
    lit: tuple[Polygon, ...]
    possible_edges: tuple[Segment, ...]


def _cell_edges(cells):
    """Emit the boundary segments of opaque unit cells."""
    boundary = set()
    for cell in cells:
        x, y = parse_cell(cell)
        corners = ((x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1))
        for start, end in zip(corners, (*corners[1:], corners[0]), strict=True):
            edge = tuple(sorted((start, end)))
            if edge in boundary:
                boundary.remove(edge)
            else:
                boundary.add(edge)
    return tuple(sorted(boundary))


def _segment_meets_bounds(segment, bounds):
    """Conservatively select segments that can cross the light path bounds."""
    (x1, y1), (x2, y2) = segment
    left, top, right, bottom = bounds
    return (
        min(x1, x2) <= right
        and max(x1, x2) >= left
        and min(y1, y2) <= bottom
        and max(y1, y2) >= top
    )


def _corridor_cells(scoped_targets):
    """Yield a sparse supercover of the source-to-target ground area.

    Merge adjacent targets into horizontal runs. Each run and its source form
    a convex corridor. Intersecting its outer rays with each grid row gives
    the candidate columns, without rasterizing the full bounding rectangle.
    """
    intervals = {}
    for origin, targets in scoped_targets:
        x, y = parse_cell(origin)
        ox, oy = x + 0.5, y + 0.5
        targets_by_row = {}
        for col, row in targets:
            targets_by_row.setdefault(row, []).append((col, col + 1))
        for target_row, target_columns in targets_by_row.items():
            for left, right in _merge_intervals(target_columns):
                corners = (
                    (left, target_row),
                    (right, target_row),
                    (left, target_row + 1),
                    (right, target_row + 1),
                )
                rays = tuple((ty, (tx - ox) / (ty - oy)) for tx, ty in corners)
                for row in range(min(y, target_row), max(y, target_row) + 1):
                    crossings = [
                        ox + slope * (edge_y - oy)
                        for ty, slope in rays
                        for edge_y in (row, row + 1)
                        if min(oy, ty) <= edge_y <= max(oy, ty)
                    ]
                    if row == y:
                        crossings.append(ox)
                    if row == target_row:
                        crossings.extend((left, right))
                    intervals.setdefault(row, []).append(
                        (floor(min(crossings)), ceil(max(crossings)))
                    )
    return {
        cell_id(x, y)
        for y, ranges in intervals.items()
        for start, stop in _merge_intervals(ranges)
        for x in range(start, stop)
    }


def _merge_intervals(intervals):
    """Yield disjoint runs from half-open intervals on one grid axis."""
    ordered = sorted(intervals)
    lower, upper = ordered[0]
    for start, stop in ordered[1:]:
        if start > upper:
            yield lower, upper
            lower, upper = start, stop
        else:
            upper = max(upper, stop)
    yield lower, upper


class LightService:
    """Resolve scoped light contributions and coordinate controlled light mutations."""

    def __init__(self, handler):
        """Bind the light service to World."""
        self.h = handler

    def config(self, sid, oid):
        """Read the object's current light-source capability."""
        d = self.h.facts.object_definition(sid, oid)
        return d.capability("light") if d else None

    @instance_read("scene", "lighting", "sources", maintained="light_sources")
    def sources(self, sid, scene, package):
        """Resolve actual scene lights, accounting for mode, damage and enclosing-container
        transmission.
        """
        result = []
        for oid in self.h.facts.scene_capability_ids(sid, scene, "object_lights"):
            light = self.h.facts.light_state(sid, oid)
            c = self.config(sid, oid)
            if not light["enabled"] or self.h.facts.object_hp(sid, oid) == 0:
                continue
            location = self.h.facts.placement(sid, oid)
            if location["scene_id"] != scene:
                continue
            transmission = "transmit"
            for parent in location["ancestry"]:
                config = self.h.objects.containers.config(sid, parent)
                obj = self.h.facts.object(sid, parent)
                if self.h.facts.object_hp(sid, parent) == 0:
                    continue  # The supported destroyed shell no longer encloses light.
                value = (
                    config.light_transmission_closed
                    if obj["state"] == "closed"
                    else config.light_transmission_open
                )
                if value == "block":
                    transmission = "block"
                    break
                if value == "unknown":
                    transmission = "unknown"
            if transmission == "block":
                continue
            mode = next(m for m in c.modes if m.mode_id == light["mode"])
            result.append(
                {
                    "object_id": oid,
                    "cell": location["cell"],
                    "transmission": transmission,
                    "mode": mode,
                }
            )
        return result

    @instance_read("scene", "lighting", "projection", maintained="light_result")
    def current_project(self, sid, scene, package):
        """Build the effective current grid, storing dynamic light only within source reach."""
        raw = self.h.grid.reader.current_grid(sid, scene, package)
        return self.project(sid, scene, package, raw)

    @instance_read("scene", "lighting", "topology", maintained="light_result")
    def current_topology(self, sid, scene, package):
        """Reuse base topology with current illumination overlaid."""
        prepared = self.h.grid.reader.current_grid(sid, scene, package)
        if not self.sources(sid, scene, package):
            return prepared.topology
        grid, _, _ = self.current_project(sid, scene, package)
        return prepared.topology.with_lighting(grid["lighting"])

    def current_cells(self, sid, scene, package, cells):
        """Resolve trusted in-bounds cells through the Scene's maintained per-cell field.

        Reuse available cells and prepare one source selection and geometry for the misses.
        """
        package_key = ("package", package.package_digest)
        parameters = {cell: ((package_key, cell), ()) for cell in cells}

        def compute_missing(missing):
            """Build only the scalar entries that were absent or invalidated."""
            levels = self._compute_cells(sid, scene, package, tuple(key[0][1] for key in missing))
            return {key: levels[key[0][1]] for key in missing}

        values = resolve_field_batch(
            self.h.store,
            "scene",
            sid,
            scene,
            "lighting",
            "cells",
            parameters.values(),
            compute_missing,
            capacity=128,
            maintained="light_result",
        )
        return {cell: values[key] for cell, key in parameters.items()}

    def _compute_cells(self, sid, scene, package, cells):
        """Use current map facts and compute only missing cell light results."""
        prepared = self.h.grid.reader.current_grid(sid, scene, package)
        sources = self.sources(sid, scene, package)
        return self._evaluate_cells(sid, scene, package, prepared, sources, cells)

    def _evaluate_cells(self, sid, scene, package, grid, sources, cells):
        """Share one regional blocker read and one geometric result per relevant source."""
        topology = grid.topology
        if not sources:
            return {cell: CellLight.ambient(topology.light_on_cell(cell)) for cell in cells}
        by_cell = self._sources_for_cells(topology, sources, cells)
        coverage, uncertain = self._coverage(sid, scene, package, grid, by_cell)
        return {
            cell: (
                self._illuminate_cell(topology, coverage, uncertain, applicable, cell)
                if applicable
                else CellLight.ambient(topology.light_on_cell(cell))
            )
            for cell, applicable in by_cell.items()
        }

    def _coverage(self, sid, scene, package, grid, by_cell):
        """Prepare visibility pieces over the union of possible source-to-target paths."""
        coordinates = {}
        origins = {}
        for cell, sources in by_cell.items():
            xy = parse_cell(cell) if sources else None
            for source in sources:
                oid = source["object_id"]
                if oid not in origins:
                    origin = source["cell"]
                    origins[oid] = (
                        origin
                        if origin is not None
                        and grid.topology.distance_ft(origin, origin) is not None
                        else None
                    )
                if (origin := origins[oid]) is not None:
                    if oid not in coordinates:
                        coordinates[oid] = (origin, [])
                    coordinates[oid][1].append(xy)
        if not coordinates:
            return {}, frozenset()

        scopes = {}
        for oid, (origin, points) in coordinates.items():
            columns, rows = zip(*points, strict=True)
            x, y = parse_cell(origin)
            target = (min(columns), min(rows), max(columns) + 1, max(rows) + 1)
            left, top, right, bottom = target
            scopes[oid] = (
                origin,
                target,
                (min(left, x), min(top, y), max(right, x + 1), max(bottom, y + 1)),
            )
        corridor_cells = _corridor_cells(coordinates.values())
        blockers = self.h.grid.light_occluders(sid, scene, package, corridor_cells, fixed_grid=grid)
        walls = tuple(((x1, y1), (x2, y2)) for x1, y1, x2, y2 in blockers.walls)
        opaque_objects = {oid: frozenset(cells) for oid, cells in blockers.opaque_objects}
        coverage = {}
        for oid, (origin, target, path) in scopes.items():
            x, y = parse_cell(origin)
            point = (x + 0.5, y + 0.5)
            solids = blockers.opaque_cells - {origin}
            for other, cells in opaque_objects.items():
                if other != oid:
                    solids |= cells
            definite = tuple(
                segment
                for segment in chain(walls, _cell_edges(solids))
                if _segment_meets_bounds(segment, path)
            )
            lit = lit_polygons(point, target, definite)
            possible_edges = tuple(
                segment
                for segment in _cell_edges(blockers.uncertain_cells - {origin})
                if _segment_meets_bounds(segment, path)
            )
            coverage[oid] = _SourceCoverage(point, lit, possible_edges)
        return coverage, blockers.uncertain_cells

    @staticmethod
    def _sources_for_cells(topology, sources, cells):
        """Traverse scene emitters once and assign them to reachable selected cells.

        Unknown or out-of-bounds origins still participate in every requested cell.
        Actual cover, sight lines and transmission are resolved during composition.
        """
        coordinates = {cell: parse_cell(cell) for cell in cells}
        if not coordinates:
            return {}
        columns, rows = zip(*coordinates.values(), strict=True)
        left, right = min(columns), max(columns)
        top, bottom = min(rows), max(rows)
        by_cell = {cell: [] for cell in coordinates}
        for source in sources:
            origin = source["cell"]
            if origin is None or topology.distance_ft(origin, origin) is None:
                for candidates in by_cell.values():
                    candidates.append(source)
                continue
            x, y = parse_cell(origin)
            # Match GridTopology's Chebyshev radius, including its outer boundary.
            radius = source["mode"].dim_outer_radius_ft // topology.cell_size_ft
            if not (left - radius <= x <= right + radius and top - radius <= y <= bottom + radius):
                continue
            for cell, (col, row) in coordinates.items():
                if max(abs(col - x), abs(row - y)) <= radius:
                    by_cell[cell].append(source)
        return by_cell

    @staticmethod
    def _illuminate_cell(topology, coverage, uncertain_cells, sources, cell):
        """Compose scalar levels and drawing pieces from the same local geometry."""
        base = level = topology.light_on_cell(cell)
        contributions, unknown = [], False
        dim, bright = [], []
        x, y = parse_cell(cell)
        bounds = (x, y, x + 1, y + 1)
        for source in sources:
            oid = source["object_id"]
            if oid not in coverage:
                unknown = True
                continue
            visible = coverage[oid]
            pieces = clip_to_bounds(visible.lit, bounds)
            if not pieces:
                continue
            if (
                source["transmission"] == "unknown"
                or cell in uncertain_cells
                or shadow_intersects_interior(visible.origin, pieces, visible.possible_edges)
            ):
                unknown = True
                continue
            mode = source["mode"]
            distance = topology.distance_ft(source["cell"], cell)
            value = (
                "bright"
                if mode.bright_radius_ft is not None and distance <= mode.bright_radius_ft
                else "dim"
            )
            if _RANK[value] > _RANK[level]:
                level = value
            if _RANK[value] > _RANK[base]:
                (bright if value == "bright" else dim).extend(pieces)
            contributions.append(oid)
        return CellLight(level, unknown, tuple(contributions), base, tuple(dim), tuple(bright))

    def project(self, sid, scene, package, raw, *, cells=None):
        """Project current sources on a supplied fixed map.

        Keep base overrides and defaults. With no explicit cell selection, add only
        cells within source reach, or every cell if any source location is unknown.
        """
        from .grid_maps import PreparedGrid

        supplied = raw if isinstance(raw, PreparedGrid) else PreparedGrid.build(raw)
        grid = supplied.copy_data()
        sources = self.sources(sid, scene, package)
        if not sources:
            return grid, (), {}
        selected = (
            cells
            if cells is not None
            else self._affected_light_cells(supplied.topology, supplied.data, sources)
        )
        uncertain, evidence = self._project_cells(
            sid, scene, package, supplied, sources, selected, grid["lighting"]
        )
        return grid, uncertain, evidence

    def event_overlay(self, sid, scene, package, *, fixed_grid=None):
        """Capture stage-local light levels, uncertainty and sources for event evidence.

        Levels overlay the base grid's explicit lighting and default. Keep all cells
        in each source's reach, including cells whose light level equals the base.
        A source without an in-bounds origin can make any cell uncertain.
        """
        sources = self.sources(sid, scene, package)
        if not sources:
            return {}, (), {}
        grid = fixed_grid or self.h.grid.reader.current_grid(sid, scene, package)
        cells = self._affected_light_cells(grid.topology, grid.data, sources)
        levels = {}
        uncertain, evidence = self._project_cells(sid, scene, package, grid, sources, cells, levels)
        return levels, uncertain, evidence

    @staticmethod
    def _affected_light_cells(topology, grid, sources):
        """Yield each possible affected cell once in row-major order."""
        width, height = grid["width"], grid["height"]
        origins = [source["cell"] for source in sources]
        if any(
            origin is None or topology.distance_ft(origin, origin) is None for origin in origins
        ):
            # Preserve cell-level uncertainty from unsupported source locations.
            yield from (cell_id(x, y) for y in range(height) for x in range(width))
            return

        row_ranges = {}
        for source, origin in zip(sources, origins, strict=True):
            x, y = parse_cell(origin)
            # The pinned GridTopology uses Chebyshev distance in feet.
            radius = source["mode"].dim_outer_radius_ft // topology.cell_size_ft
            columns = (max(0, x - radius), min(width, x + radius + 1))
            for row in range(max(0, y - radius), min(height, y + radius + 1)):
                row_ranges.setdefault(row, []).append(columns)
        for row in sorted(row_ranges):
            for start, stop in _merge_intervals(row_ranges[row]):
                yield from (cell_id(col, row) for col in range(start, stop))

    def _project_cells(self, sid, scene, package, grid, sources, cells, levels):
        """Overlay levels and return unknown cells and evidence in the supplied cell order."""
        uncertain, evidence = [], {}
        for cell, result in self._evaluate_cells(sid, scene, package, grid, sources, cells).items():
            if result.unknown:
                uncertain.append(cell)
            levels[cell] = result.level
            if result.sources:
                evidence[cell] = list(result.sources)
        return tuple(uncertain), evidence

    def validate(self, row, actor, op, package):
        """Validate actor, placement and capability for manual light operations."""
        h, sid = self.h, row["session_id"]
        h._world_operation_capability(row, op)
        h._world_actor_ready(sid, actor)
        position = h._public_scope(sid, actor, package)
        if (position.scene_id, position.cell) != (op.scene_id, op.expected_cell):
            raise StateMachineError("position_conflict", "Actor position changed")
        h.objects.containers.reachable(sid, actor, op.object_id, package)
        c = self.config(row["session_id"], op.object_id)
        if c is None:
            raise StateMachineError(
                "unsupported_object_capability", "Object has no light capability"
            )
        if h.facts.object_hp(sid, op.object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed emitter is unusable")
        if type(op) is not SetLightEnabled and op.mode not in {m.mode_id for m in c.modes}:
            raise StateMachineError("unsupported_light_mode", "Undeclared light mode")

    def operate(self, row, actor, op, package, *, origin=None):
        """Apply admitted light enabled/mode changes and capture their events."""
        h, sid = self.h, row["session_id"]
        if origin is None:
            self.validate(row, actor, op, package)
        else:
            h.objects.mechanisms.qualify_link(
                sid, origin, op.object_id, "set_light_enabled", op.enabled, package
            )
        c = self.config(row["session_id"], op.object_id)
        if c is None or h.facts.object_hp(sid, op.object_id) == 0:
            raise StateMachineError("unsupported_object_capability", "Emitter unavailable")
        old = h.facts.light_state(sid, op.object_id)
        enabled = op.enabled if type(op) is SetLightEnabled else old["enabled"]
        mode = old["mode"] if type(op) is SetLightEnabled else op.mode
        if mode not in {m.mode_id for m in c.modes}:
            raise StateMachineError("unsupported_light_mode", "Undeclared light mode")
        if old == {"enabled": enabled, "mode": mode}:
            return []
        payload = {
            **asdict(op),
            "actor_id": actor,
            "enabled": enabled,
            "mode": mode,
            **({"execution": _execution_data(None, origin)} if origin else {}),
        }
        events = [
            h._capture_event(
                sid,
                package,
                "object.light_changed",
                payload,
                phase="world.before_light_update",
                source_actor_id=op.object_id,
            )
        ]
        h.facts.set_light_state(sid, op.object_id, enabled, mode)
        events.append(
            h._capture_event(
                sid,
                package,
                "object.light_changed",
                payload,
                phase="world.after_light_update",
                source_actor_id=op.object_id,
            )
        )
        return events

    def query_area(self, sid, actor, cells, package, *, context, include_geometry=False):
        """Return illumination records in caller order after authorizing every cell.

        Resolve current values in one read scope. Disclose each source only if the
        actor can access it under the current object visibility rules. An opt-in
        geometry field includes cell-clipped bright and dim polygons in grid units,
        with no source identities. Apply the strongest grade over the base level.
        Unknown cells have no deterministic geometry.
        """
        if type(include_geometry) is not bool:
            raise StateMachineError("invalid_query", "Geometry selection must be boolean")
        if type(cells) not in (list, tuple) or not 1 <= len(cells) <= _MAX_QUERY_CELLS:
            raise StateMachineError("invalid_query", "Select between 1 and 256 grid cells")
        cells = tuple(cells)
        if any(type(cell) is not str or not 0 < len(cell) <= 128 for cell in cells):
            raise StateMachineError("invalid_query", "Cells must be bounded coordinate strings")
        h = self.h
        with h._transaction():
            position = h._spatial_reader(sid, actor, package, context)
            h._public_scope(sid, actor, package)
            prepared = h.grid.reader.prepared_scene(sid, position.scene_id, package)
            topology = prepared.topology
            for cell in dict.fromkeys(cells):
                try:
                    visible_cell = topology.has_line_of_sight(position.cell, cell)
                except (ValueError, TypeError) as exc:
                    raise StateMachineError("invalid_query", "Invalid grid coordinate") from exc
                if not visible_cell:
                    raise StateMachineError("invalid_target", "No authorized local position")
            values = self.current_cells(sid, position.scene_id, package, cells)
            visibility, records = {}, []
            for cell in cells:
                result = values[cell]
                visible = []
                for oid in result.sources:
                    if oid not in visibility:
                        try:
                            h.objects._visible(sid, actor, oid, package)
                            visibility[oid] = True
                        except StateMachineError:
                            visibility[oid] = False
                    if visibility[oid]:
                        visible.append(oid)
                record = {
                    "cell": cell,
                    "illumination": None if result.unknown else result.level,
                    "status": "unknown" if result.unknown else "known",
                    "contributing_sources": tuple(visible),
                    "sunlight": prepared.data["sunlight"],
                }
                if include_geometry:
                    record["geometry"] = (
                        None
                        if result.unknown
                        else {"base": result.base, "dim": result.dim, "bright": result.bright}
                    )
                records.append(record)
            return tuple(records)
