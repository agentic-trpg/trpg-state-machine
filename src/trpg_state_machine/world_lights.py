"""Ordinary scenario emitters; no fuel, magic, sunlight or effect lifecycle."""

from dataclasses import asdict, dataclass

from dnd5e_engine.spatial import cell_id, parse_cell

from ._execution import _execution_data
from .errors import StateMachineError
from .runtime_instances import instance_read, resolve_field_batch
from .world_commands import SetLightEnabled

_RANK = {"dark": 0, "dim": 1, "bright": 2}
_MAX_QUERY_CELLS = 256


@dataclass(frozen=True, slots=True)
class LightGeometry:
    """Share prepared immutable light blockers and uncertain topology inputs."""

    candidates: frozenset
    topology: object

    def __iter__(self):
        """Iterate the immutable light geometry's candidate and topology inputs."""
        yield self.candidates
        yield self.topology

    def __deepcopy__(self, memo):
        """Share immutable light geometry when deep-copied."""
        return self


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
        geometry = self.geometry(sid, scene, package) if self.sources(sid, scene, package) else None
        return self.project(sid, scene, package, raw, geometry=geometry)

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
        """Prepare the base grid and evaluate candidate light contributions for cache misses."""
        prepared = self.h.grid.reader.current_grid(sid, scene, package)
        sources = self.sources(sid, scene, package)
        if not sources:
            lighting = prepared.data["lighting"]
            default = prepared.data["default_lighting"]
            return {cell: (lighting.get(cell, default), False, ()) for cell in cells}
        by_cell = self._sources_for_cells(prepared.topology, sources, cells)
        geometry = self.geometry(sid, scene, package) if any(by_cell.values()) else None
        return {
            cell: (
                self._illuminate_cell(
                    prepared.topology, geometry.topology, geometry.candidates, by_cell[cell], cell
                )
                if by_cell.get(cell)
                else (prepared.topology.light_on_cell(cell), False, ())
            )
            for cell in cells
        }

    @staticmethod
    def _sources_for_cells(topology, sources, cells):
        """Traverse scene emitters once and assign them to reachable selected cells.

        Unknown or out-of-bounds origins still participate in every requested cell.
        Actual cover, sight lines and transmission are resolved during composition.
        """
        coordinates = {cell: parse_cell(cell) for cell in cells}
        columns, rows = zip(*coordinates.values(), strict=True)
        left, right = min(columns), max(columns)
        top, bottom = min(rows), max(rows)
        by_cell = {cell: [] for cell in cells}
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
    def _illuminate_cell(topology, uncertain_geometry, candidates, sources, cell):
        """Compose light for a cell using the shared single-cell and map rules."""
        level = topology.light_on_cell(cell)
        contributions, unknown = [], False
        for source in sources:
            origin, mode = source["cell"], source["mode"]
            distance = topology.distance_ft(origin, cell) if origin is not None else None
            if distance is None:
                unknown = True
                continue
            if distance > mode.dim_outer_radius_ft:
                continue
            if (
                not topology.has_line_of_sight(origin, cell)
                or topology.cover_between(origin, cell) == "total"
            ):
                continue
            if (
                source["transmission"] == "unknown"
                or cell in candidates
                or not uncertain_geometry.has_line_of_sight(origin, cell)
            ):
                unknown = True
                continue
            value = (
                "bright"
                if mode.bright_radius_ft is not None and distance <= mode.bright_radius_ft
                else "dim"
            )
            if _RANK[value] > _RANK[level]:
                level = value
            contributions.append(source["object_id"])
        return level, unknown, tuple(contributions)

    @instance_read("scene", "lighting", "geometry", maintained="light_candidates")
    def geometry(self, sid, scene, package):
        """Prepare light-blocking geometry from the current map."""
        raw = self.h.grid.reader.current_grid(sid, scene, package)
        return self._geometry(sid, scene, package, raw)

    @instance_read("scene", "lighting", "candidates", maintained="light_candidates")
    def _candidates(self, sid, scene, package):
        """Ground-object uncertainty, maintained separately from source membership."""
        candidates = self.h.objects.spatial.movement_cells(sid, scene, package)
        actual = self.h.grid.reader.scene_object_occupants(sid, scene, package)
        candidates.update(
            cell
            for oid, cell in actual
            if cell is not None and self.h.facts.instances.legacy_object(sid, oid).blocks_passage
        )
        return frozenset(candidates)

    def _geometry(self, sid, scene, package, raw):
        """Combine ground/shared-door blockers into uncertain light geometry topology."""
        candidates = set(self._candidates(sid, scene, package))
        for passage in self.h.facts.instances.passages(sid, scene_id=scene):
            if passage.door_object_id and self.h._door_closed(sid, passage, package):
                candidates.update(
                    e.threshold_cell for e in passage.endpoints if e.scene_id == scene
                )
        topology = self.h.grid.reader.topology_for_grid(sid, scene, raw)
        uncertain_geometry = topology.with_blocked(candidates)
        return LightGeometry(frozenset(candidates), uncertain_geometry)

    def project(self, sid, scene, package, raw, *, cells=None, geometry=None):
        """Project current sources on a supplied fixed map.

        Keep base overrides and defaults. With no explicit cell selection, add only
        cells within source reach, or every cell if any source location is unknown.
        """
        from .grid_maps import PreparedGrid

        topology = self.h.grid.reader.topology_for_grid(sid, scene, raw)
        supplied = raw
        raw = raw.copy_data() if isinstance(raw, PreparedGrid) else raw
        grid = {**raw, "lighting": dict(raw["lighting"])}
        sources = self.sources(sid, scene, package)
        if not sources:
            return grid, (), {}
        candidates, uncertain_geometry = (
            self._geometry(sid, scene, package, supplied) if geometry is None else geometry
        )
        selected = (
            cells if cells is not None else self._affected_light_cells(topology, raw, sources)
        )
        uncertain, evidence = self._project_cells(
            topology, uncertain_geometry, candidates, sources, selected, grid["lighting"]
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
        if fixed_grid is None:
            grid = self.h.grid.reader.current_grid(sid, scene, package)
            candidates, uncertain_geometry = self.geometry(sid, scene, package)
        else:
            grid = fixed_grid
            candidates, uncertain_geometry = self._geometry(sid, scene, package, grid)
        cells = self._affected_light_cells(grid.topology, grid.data, sources)
        levels = {}
        uncertain, evidence = self._project_cells(
            grid.topology, uncertain_geometry, candidates, sources, cells, levels
        )
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
            covered_until = 0
            for start, stop in sorted(row_ranges[row]):
                yield from (cell_id(col, row) for col in range(max(start, covered_until), stop))
                covered_until = max(covered_until, stop)

    def _project_cells(self, topology, uncertain_geometry, candidates, sources, cells, levels):
        """Overlay levels and return unknown cells and evidence in the supplied cell order."""
        uncertain, evidence = [], {}
        for cell in cells:
            level, unknown, contributions = self._illuminate_cell(
                topology, uncertain_geometry, candidates, sources, cell
            )
            if unknown:
                uncertain.append(cell)
            levels[cell] = level
            if contributions:
                evidence[cell] = list(contributions)
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

    def query_area(self, sid, actor, cells, package, *, context):
        """Return illumination records in caller order after authorizing every cell.

        Resolve current values in one read scope. Disclose each source only if the
        actor can access it under the current object visibility rules.
        """
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
                level, uncertain, sources = values[cell]
                visible = []
                for oid in sources:
                    if oid not in visibility:
                        try:
                            h.objects._visible(sid, actor, oid, package)
                            visibility[oid] = True
                        except StateMachineError:
                            visibility[oid] = False
                    if visibility[oid]:
                        visible.append(oid)
                records.append(
                    {
                        "cell": cell,
                        "illumination": None if uncertain else level,
                        "status": "unknown" if uncertain else "known",
                        "contributing_sources": tuple(visible),
                        "sunlight": prepared.data["sunlight"],
                    }
                )
            return tuple(records)
