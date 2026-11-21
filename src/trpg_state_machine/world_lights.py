"""Compose objective light sources in the requested scene region."""

from dataclasses import asdict, dataclass
from itertools import chain
from math import ceil, floor
from typing import TYPE_CHECKING, NamedTuple, NotRequired, TypedDict

from dnd5e_engine.spatial import cell_id, parse_cell

from ._execution import _execution_data
from .errors import StateMachineError
from .light_fuel import LightFuel
from .light_geometry import (
    Point,
    Polygon,
    Segment,
    clip_to_bounds,
    clip_to_cone,
    lit_polygons,
    shadow_intersects_interior,
)
from .runtime_instances import instance_read, resolve_field, resolve_field_batch
from .world_commands import SetLightEnabled
from .world_daylight import SceneAmbientSource, scene_ambient_source
from .world_package import LightMode

if TYPE_CHECKING:
    from .world_magic import MagicLightSource

_RANK = {"dark": 0, "dim": 1, "bright": 2}
_MAX_QUERY_CELLS = 256


class _EmitterSource(TypedDict):
    """Retain the current position, range and transmission of a point emitter."""

    object_id: str
    cell: str | None
    mode: LightMode
    transmission: str
    direction: NotRequired[str]


@dataclass(frozen=True, slots=True)
class _SceneLightSources:
    """Share prepared emitters and area-wide sources between light projections."""

    emitters: tuple[_EmitterSource, ...]
    ambient: SceneAmbientSource | None
    magic: tuple["MagicLightSource", ...]


class CellLight(NamedTuple):
    """Retain a cell's scalar light and clipped drawing shapes in one immutable result."""

    level: str
    unknown: bool = False
    sources: tuple[str, ...] = ()
    base: str = "dark"
    dim: tuple[Polygon, ...] = ()
    bright: tuple[Polygon, ...] = ()
    sunlight: bool = False
    magical_darkness: bool = False

    @classmethod
    def ambient(cls, level, *, sunlight=False):
        """Use the map's base grade when no local source contributes."""
        return cls(level, base=level, sunlight=sunlight)

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
    def sources(self, sid, scene, package) -> list[_EmitterSource]:
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
            source: _EmitterSource = {
                "object_id": oid,
                "cell": location["cell"],
                "transmission": transmission,
                "mode": mode,
            }
            if c.emission_shape == "grid_cone_static_los":
                source["direction"] = self.h.facts.object(sid, oid)["orientation"]
            result.append(source)
        return result

    def _scene_sources(self, sid, scene, package, ambient, *, cells=None):
        """Read the effective World light sources for one scene version."""
        return _SceneLightSources(
            tuple(self.sources(sid, scene, package)),
            ambient,
            self.h.magic.active_magic_sources(sid, scene, package, cells=cells),
        )

    @staticmethod
    def _projection_key(package, ambient):
        """Fix the effective phase for light caches that otherwise depend on scene facts."""
        return (("package", package.package_digest), ambient.phase if ambient else None)

    def current_project(self, sid, scene, package):
        """Build the current grid projection using the current phase and scene facts."""
        with self.h.grid.read_scope():
            prepared = self.h.grid.reader.current_grid(sid, scene, package)
            ambient = scene_ambient_source(self.h, sid, scene, prepared)
            return self._cached_projection(sid, scene, package, prepared, ambient)

    def _cached_projection(self, sid, scene, package, prepared, ambient):
        """Use the same fixed projection for map and topology consumers."""
        return resolve_field(
            self.h.store,
            "scene",
            sid,
            scene,
            "lighting",
            "projection",
            self._projection_key(package, ambient),
            lambda: self._project_grid(sid, scene, package, prepared, ambient),
            maintained="light_result",
        )

    def current_topology(self, sid, scene, package):
        """Reuse base topology with current illumination overlaid."""
        with self.h.grid.read_scope():
            return self._current_topology(sid, scene, package)

    def _current_topology(self, sid, scene, package):
        """Resolve topology and its current ambient phase inside one scope."""
        prepared = self.h.grid.reader.current_grid(sid, scene, package)
        ambient = scene_ambient_source(self.h, sid, scene, prepared)

        def overlay():
            """Preserve the fixed topology and set its effective base grade."""
            if (
                ambient is None
                and not self.sources(sid, scene, package)
                and not self.h.magic.active_magic_sources(sid, scene, package)
            ):
                return prepared.topology
            grid, _, _ = self._cached_projection(sid, scene, package, prepared, ambient)
            return prepared.topology.with_lighting(
                grid["lighting"],
                default_lighting=grid["default_lighting"],
                sunlight=grid["sunlight"],
                sunlight_overrides=grid.get("sunlight_overrides", {}),
                magical_darkness_cells=grid.get("magical_darkness_cells", ()),
            )

        return resolve_field(
            self.h.store,
            "scene",
            sid,
            scene,
            "lighting",
            "topology",
            self._projection_key(package, ambient),
            overlay,
            maintained="light_result",
        )

    def current_cells(self, sid, scene, package, cells):
        """Resolve trusted in-bounds cells through the Scene's maintained per-cell field.

        Reuse available cells and prepare one source selection and geometry for the misses.
        """
        selected = tuple(dict.fromkeys(cells))
        if not selected:
            return {}
        with self.h.grid.read_scope():
            return self._current_cells(sid, scene, package, selected)

    def _current_cells(self, sid, scene, package, selected):
        """Keep phase, grid and cached light in the same read or mutation stage."""
        prepared = self.h.grid.reader.current_grid(sid, scene, package)
        ambient = scene_ambient_source(self.h, sid, scene, prepared)
        key = self._projection_key(package, ambient)
        parameters = {cell: ((key, cell), ()) for cell in selected}

        def compute_missing(missing):
            """Build only the scalar entries that were absent or invalidated."""
            cells = tuple(point[0][1] for point in missing)
            levels = self._compute_cells(sid, scene, package, cells, prepared, ambient)
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

    def _compute_cells(self, sid, scene, package, cells, prepared, ambient):
        """Use current map facts and compute only missing cell light results."""
        sources = self._scene_sources(sid, scene, package, ambient, cells=cells)
        return self._evaluate_cells(sid, scene, package, prepared, sources, cells)

    @staticmethod
    def _ambient_cell(grid, ambient, cell):
        """Resolve dynamic outdoor light relative to fixed map overrides."""
        if ambient is not None and ambient.covers(cell) and cell not in grid.data["lighting"]:
            return CellLight.ambient(ambient.level, sunlight=ambient.sunlight)
        return CellLight.ambient(
            grid.topology.light_on_cell(cell), sunlight=grid.topology.sunlight_on_cell(cell)
        )

    def _evaluate_cells(self, sid, scene, package, grid, sources, cells):
        """Share one regional blocker read and one geometric result per relevant source."""
        topology = grid.topology
        magic_at = {}
        if sources.magic:
            cells = tuple(dict.fromkeys(cells))
            selected = frozenset(cells)
            for effect in sources.magic:
                for covered in (effect.cells, effect.dim_cells):
                    for cell in selected.intersection(covered):
                        magic_at.setdefault(cell, []).append(effect)
        masked = {
            cell
            for cell, effects in magic_at.items()
            if any(
                effect.spec.kind == "magical_darkness" and not effect.unknown for effect in effects
            )
        }
        if not sources.emitters:
            return {
                cell: self._combine_magic(
                    self._ambient_cell(grid, sources.ambient, cell),
                    cell,
                    magic_at.get(cell, ()),
                    cell in masked,
                )
                for cell in cells
            }
        by_cell = self._sources_for_cells(topology, sources.emitters, cells)
        for cell in masked:
            by_cell[cell] = []
        coverage, uncertain = self._coverage(sid, scene, package, grid, by_cell)
        values = {}
        for cell, applicable in by_cell.items():
            result = self._ambient_cell(grid, sources.ambient, cell)
            if applicable:
                result = self._illuminate_cell(
                    topology, coverage, uncertain, applicable, cell, base_light=result
                )
            values[cell] = self._combine_magic(result, cell, magic_at.get(cell, ()), cell in masked)
        return values

    @staticmethod
    def _combine_magic(result, cell, effects, masked):
        """Combine already projected spell footprints with ordinary light and ambient light."""
        if not effects:
            return result
        if any(effect.unknown and effect.spec.kind == "magical_darkness" for effect in effects):
            return CellLight("dark", unknown=True, magical_darkness=True)
        if masked:
            result = CellLight.ambient("dark")._replace(magical_darkness=True)
        if any(effect.unknown for effect in effects):
            result = result._replace(unknown=True)
        grade, sunlight = "dark", result.sunlight
        object_ids = list(result.sources)
        for effect in effects:
            if effect.spec.kind != "light" or effect.unknown:
                continue
            primary = cell in effect.cells
            value = effect.spec.light if primary else "dim"
            if _RANK[value] > _RANK[grade]:
                grade = value
            sunlight |= effect.spec.sunlight if primary else effect.spec.sunlight_in_dim
            if effect.item_id is not None and effect.item_id not in object_ids:
                object_ids.append(effect.item_id)
        if _RANK[grade] <= _RANK[result.base]:
            return result._replace(sunlight=sunlight, sources=tuple(object_ids))
        x, y = parse_cell(cell)
        polygon = (((x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1)),)
        return result._replace(
            level=max(grade, result.level, key=_RANK.__getitem__),
            sunlight=sunlight,
            magical_darkness=False,
            sources=tuple(object_ids),
            dim=polygon if grade == "dim" else (),
            bright=polygon if grade == "bright" else result.bright,
        )

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
            radius = LightService._candidate_radius(source, topology.cell_size_ft)
            if not (left - radius <= x <= right + radius and top - radius <= y <= bottom + radius):
                continue
            for cell, (col, row) in coordinates.items():
                if max(abs(col - x), abs(row - y)) <= radius and LightService._in_cone(
                    source, (x, y), col, row, topology.cell_size_ft
                ):
                    by_cell[cell].append(source)
        return by_cell

    @staticmethod
    def _candidate_radius(source, cell_size):
        """Select cells in the grid radius or a conservative cone envelope."""
        outer = source["mode"].dim_outer_radius_ft
        return ceil(outer / cell_size) if source.get("direction") else outer // cell_size

    @staticmethod
    def _in_cone(source, origin, col, row, cell_size):
        """Confirm positive-area cone reach before reading the path's occluders."""
        direction = source.get("direction")
        if direction is None:
            return True
        x, y = origin
        cell = ((col, row), (col + 1, row), (col + 1, row + 1), (col, row + 1))
        length = source["mode"].dim_outer_radius_ft / cell_size
        return bool(clip_to_cone((cell,), (x + 0.5, y + 0.5), direction, length))

    @staticmethod
    def _illuminate_cell(topology, coverage, uncertain_cells, sources, cell, *, base_light):
        """Compose scalar levels and drawing pieces from the same local geometry."""
        base = level = base_light.level
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
            mode = source["mode"]
            if direction := source.get("direction"):
                bright_length = (mode.bright_radius_ft or 0) / topology.cell_size_ft
                dim_length = mode.dim_outer_radius_ft / topology.cell_size_ft
                bright_pieces = clip_to_cone(pieces, visible.origin, direction, bright_length)
                dim_pieces = clip_to_cone(
                    pieces, visible.origin, direction, dim_length, start=bright_length
                )
                pieces = (*bright_pieces, *dim_pieces)
            else:
                distance = topology.distance_ft(source["cell"], cell)
                is_bright = mode.bright_radius_ft is not None and distance <= mode.bright_radius_ft
                bright_pieces, dim_pieces = (pieces, ()) if is_bright else ((), pieces)
            if not pieces:
                continue
            if (
                source["transmission"] == "unknown"
                or cell in uncertain_cells
                or shadow_intersects_interior(visible.origin, pieces, visible.possible_edges)
            ):
                unknown = True
                continue
            value = "bright" if bright_pieces else "dim"
            if _RANK[value] > _RANK[level]:
                level = value
            if base != "bright":
                bright.extend(bright_pieces)
            if base == "dark":
                dim.extend(dim_pieces)
            contributions.append(oid)
        return CellLight(
            level,
            unknown,
            tuple(contributions),
            base,
            tuple(dim),
            tuple(bright),
            sunlight=base_light.sunlight,
        )

    @staticmethod
    def _apply_ambient(grid, ambient):
        """Represent base light in a mechanical or event grid without a full-map expansion."""
        if ambient is None:
            return
        overrides = grid["lighting"]
        sun = grid["sunlight"]
        sun_overrides = grid.get("sunlight_overrides", {})
        if ambient.cells is None:
            grid["default_lighting"] = ambient.level
            grid["sunlight"] = ambient.sunlight
            sun_overrides = {
                cell: value
                for cell in overrides
                if (value := sun_overrides.get(cell, sun)) != ambient.sunlight
            }
        else:
            base = grid["default_lighting"]
            for cell in ambient.cells:
                if cell not in overrides:
                    if ambient.sunlight == sun:
                        sun_overrides.pop(cell, None)
                    else:
                        sun_overrides[cell] = ambient.sunlight
                    if ambient.level != base:
                        overrides[cell] = ambient.level
        if sun_overrides:
            grid["sunlight_overrides"] = sun_overrides
        else:
            grid.pop("sunlight_overrides", None)

    def project(self, sid, scene, package, raw, *, cells=None):
        """Project current sources on a supplied fixed map.

        Keep base overrides and defaults. With no explicit cell selection, add only
        cells within source reach, or every cell if any source location is unknown.
        """
        from .grid_maps import PreparedGrid

        supplied = raw if isinstance(raw, PreparedGrid) else PreparedGrid.build(raw)
        with self.h.grid.read_scope():
            ambient = scene_ambient_source(self.h, sid, scene, supplied)
            return self._project_grid(sid, scene, package, supplied, ambient, cells=cells)

    def _project_grid(self, sid, scene, package, supplied, ambient, *, cells=None):
        """Compose explicit full-map or selected-cell output from the shared light sources."""
        grid = supplied.copy_data()
        if cells is not None:
            cells = tuple(cells)
        sources = self._scene_sources(sid, scene, package, ambient, cells=cells)
        self._apply_ambient(grid, sources.ambient)
        if not sources.emitters and not sources.magic:
            return grid, (), {}
        selected = cells if cells is not None else self._projection_cells(supplied, sources)
        uncertain, evidence = self._project_cells(
            sid, scene, package, supplied, sources, selected, grid["lighting"], grid
        )
        return grid, uncertain, evidence

    def event_overlay(self, sid, scene, package, *, fixed_grid=None, output_grid):
        """Capture stage-local light levels, uncertainty and sources for event evidence.

        Levels overlay the base grid's explicit lighting and default. Keep all cells
        in each source's reach, including cells whose light level equals the base.
        A source without an in-bounds origin can make any cell uncertain.
        """
        grid = fixed_grid or self.h.grid.reader.current_grid(sid, scene, package)
        ambient = scene_ambient_source(self.h, sid, scene, grid)
        sources = self._scene_sources(sid, scene, package, ambient)
        self._apply_ambient(output_grid, sources.ambient)
        if not sources.emitters and not sources.magic:
            return {}, (), {}
        cells = self._projection_cells(grid, sources)
        levels = {}
        uncertain, evidence = self._project_cells(
            sid, scene, package, grid, sources, cells, levels, output_grid
        )
        return levels, uncertain, evidence

    @staticmethod
    def _projection_cells(grid, sources):
        """Stream ordinary light cells and the cells explicitly covered by active magic."""
        ordinary = LightService._affected_light_cells(grid.topology, grid.data, sources.emitters)
        magical = {
            cell for effect in sources.magic for cell in chain(effect.cells, effect.dim_cells)
        }
        yield from ordinary
        yield from sorted(magical)

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
            radius = LightService._candidate_radius(source, topology.cell_size_ft)
            columns = (max(0, x - radius), min(width, x + radius + 1))
            for row in range(max(0, y - radius), min(height, y + radius + 1)):
                if not source.get("direction"):
                    row_ranges.setdefault(row, []).append(columns)
                else:
                    for col in range(*columns):
                        if LightService._in_cone(source, (x, y), col, row, topology.cell_size_ft):
                            row_ranges.setdefault(row, []).append((col, col + 1))
        for row in sorted(row_ranges):
            for start, stop in _merge_intervals(row_ranges[row]):
                yield from (cell_id(col, row) for col in range(start, stop))

    def _project_cells(self, sid, scene, package, grid, sources, cells, levels, output_grid):
        """Overlay levels and return unknown cells and evidence in the supplied cell order."""
        uncertain, evidence = [], {}
        sun = output_grid["sunlight"]
        sun_overrides = output_grid.get("sunlight_overrides", {})
        magical_darkness = set(output_grid.get("magical_darkness_cells", ()))
        for cell, result in self._evaluate_cells(sid, scene, package, grid, sources, cells).items():
            if result.unknown:
                uncertain.append(cell)
            levels[cell] = result.level
            if result.sunlight == sun:
                sun_overrides.pop(cell, None)
            else:
                sun_overrides[cell] = result.sunlight
            if result.magical_darkness:
                magical_darkness.add(cell)
            else:
                magical_darkness.discard(cell)
            if result.sources:
                evidence[cell] = list(result.sources)
        if sun_overrides:
            output_grid["sunlight_overrides"] = sun_overrides
        else:
            output_grid.pop("sunlight_overrides", None)
        if magical_darkness:
            output_grid["magical_darkness_cells"] = sorted(magical_darkness)
        else:
            output_grid.pop("magical_darkness_cells", None)
        return tuple(uncertain), evidence

    def validate_target(self, row, actor, op, package):
        """Admit the reachable emitter and return its current object and light facts."""
        h, sid = self.h, row["session_id"]
        h._world_operation_capability(row, op)
        h._world_actor_ready(sid, actor)
        position = h._public_scope(sid, actor, package)
        if (position.scene_id, position.cell) != (op.scene_id, op.expected_cell):
            raise StateMachineError("position_conflict", "Actor position changed")
        obj = h.objects.containers.reachable(sid, actor, op.object_id, package)
        c = self.config(sid, op.object_id)
        if c is None:
            raise StateMachineError(
                "unsupported_object_capability", "Object has no light capability"
            )
        if h.facts.object_hp(sid, op.object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed emitter is unusable")
        return obj, c

    def validate(self, row, actor, op, package):
        """Validate the requested state change and calculate a pending fuel transition."""
        _, c = self.validate_target(row, actor, op, package)
        if type(op) is not SetLightEnabled and op.mode not in {m.mode_id for m in c.modes}:
            raise StateMachineError("unsupported_light_mode", "Undeclared light mode")
        if type(op) is SetLightEnabled and c.fuel_seconds is not None:
            return self._plan_fuel(row["session_id"], op.object_id, op.enabled)
        return None

    def _plan_fuel(self, sid, oid, enabled):
        """Plan pausing or resuming a metered lamp's remaining fuel."""
        fuel = self.h.facts.light_fuel(sid, oid)
        if fuel.expires_tick is not None:
            if enabled:
                return None
            tick = self.h.facts.time_state(sid)[1]
            return LightFuel(remaining_seconds=max(0, fuel.expires_tick - tick))
        if fuel.remaining_seconds is None:
            raise StateMachineError("incomplete_session", "Metered light is missing its fuel")
        if not enabled:
            return None
        if fuel.remaining_seconds == 0:
            raise StateMachineError("light_fuel_exhausted", "Light has no fuel remaining")
        return LightFuel(expires_tick=self.h.time.seconds_deadline(sid, fuel.remaining_seconds))

    def operate(self, row, actor, op, package, *, origin=None):
        """Apply admitted light enabled/mode changes and capture their events."""
        h, sid = self.h, row["session_id"]
        fuel = None
        if origin is None:
            fuel = self.validate(row, actor, op, package)
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
        if origin is not None and c.fuel_seconds is not None:
            fuel = self._plan_fuel(sid, op.object_id, enabled)
        payload = {
            **asdict(op),
            "actor_id": actor,
            "enabled": enabled,
            "mode": mode,
            **({"execution": _execution_data(None, origin)} if origin else {}),
        }
        return self._write_light_change(sid, package, "object.light_changed", payload, fuel)

    def expire(self, sid, package, oid, mode, deadline):
        """Extinguish the due emitter while applying the world-time boundary."""
        payload = {
            "scene_id": self.h.facts.placement(sid, oid)["scene_id"],
            "object_id": oid,
            "enabled": False,
            "mode": mode,
            "boundary_tick": deadline,
        }
        return self._write_light_change(
            sid, package, "object.light_expired", payload, LightFuel(remaining_seconds=0)
        )

    def _write_light_change(self, sid, package, event, payload, fuel):
        """Capture both stages around the shared object light mutation."""
        h, oid = self.h, payload["object_id"]
        before = h._capture_event(
            sid, package, event, payload, phase="world.before_light_update", source_actor_id=oid
        )
        h.facts.set_light_state(sid, oid, payload["enabled"], payload["mode"], fuel=fuel)
        after = h._capture_event(
            sid, package, event, payload, phase="world.after_light_update", source_actor_id=oid
        )
        return [before, after]

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
                    "sunlight": result.sunlight,
                }
                if result.magical_darkness and not result.unknown:
                    record["magical_darkness"] = True
                if include_geometry:
                    record["geometry"] = (
                        None
                        if result.unknown
                        else {"base": result.base, "dim": result.dim, "bright": result.bright}
                    )
                records.append(record)
            return tuple(records)
