"""Internal current-space reads. No position authority or mechanical settlement.

Views are detached, immutable inputs for trusted components, never actor catalogues.
RE owns geometry. Exploration's conservative occupancy policy stays separate from
static geometry, vision and cover, and must not be supplied as a combat Grid.
"""

import json
from contextlib import contextmanager
from dataclasses import InitVar, dataclass
from functools import wraps
from math import isfinite

from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene

from .errors import StateMachineError
from .grid_maps import PreparedGrid, ReadOnlyTopology
from .scoped_reads import scoped_read
from .world_spatial import ObjectSpatialProjection


def cell_in_bounds(topology, cell):
    """Canonical stored coordinates; bounds are distinct from walkability."""
    try:
        return (
            type(cell) is str
            and canonical_cell_id(cell) == cell
            and topology.distance_ft(cell, cell) is not None
        )
    except ValueError:
        return False


def _read(method):
    @wraps(method)
    def read(self, *args, **kwargs):
        with self.read_scope():
            return method(self, *args, **kwargs)

    return read


@dataclass(frozen=True, slots=True)
class GridOccupant:
    kind: str
    entity_id: str
    cells: tuple[str, ...]


class _GridSnapshot:
    # Keep prepared implementation data out of dataclass fields/asdict/wire data.
    __slots__ = ("_map",)


@dataclass(frozen=True, slots=True)
class GridView(_GridSnapshot):
    scene_id: str
    world_version: int
    grid_json: str
    occupants: tuple[GridOccupant, ...]
    objects: tuple[tuple[str, ObjectSpatialProjection], ...]
    _prepared: InitVar[PreparedGrid | None] = None

    def __post_init__(self, _prepared):
        prepared = _prepared
        if prepared is None or prepared.grid_json != self.grid_json:
            prepared = PreparedGrid.build(json.loads(self.grid_json))
        object.__setattr__(self, "_map", prepared)

    @property
    def width(self):
        return self._map.data["width"]

    @property
    def height(self):
        return self._map.data["height"]

    @property
    def cell_size_ft(self):
        return self._map.topology.cell_size_ft

    def _base(self):
        return self._map.topology

    def normalize_cell(self, cell):
        try:
            if type(cell) is not str:
                raise ValueError("Expected a Cell string")
            cell = canonical_cell_id(cell)
        except ValueError as exc:
            raise StateMachineError("invalid_cell", "Malformed grid coordinate") from exc
        if not cell_in_bounds(self._base(), cell):
            raise StateMachineError("invalid_cell", "Cell exceeds Scene bounds")
        return cell

    def occupied(self, cells):
        """Entities intersecting a bounded set of cells, preserving kinds and footprints."""
        selected = {self.normalize_cell(c) for c in cells}
        return tuple(o for o in self.occupants if selected.intersection(o.cells))

    def entity_cells(self, kind, entity_id):
        for occupant in self.occupants:
            if (occupant.kind, occupant.entity_id) == (kind, entity_id):
                return occupant.cells
        raise StateMachineError("invalid_target", "Entity has no local grid placement")

    def distance_ft(self, a, b):
        """Geometric distance; neither path length nor movement cost."""
        return self._base().distance_ft(self.normalize_cell(a), self.normalize_cell(b))

    def is_adjacent(self, a, b):
        # RE is_adjacent also filters blocked destinations; this is geometry only.
        return self.distance_ft(a, b) == self.cell_size_ft

    def cells_within_range(self, origin, range_ft):
        origin = self._range_origin(origin, range_ft)
        topology = self._base()
        x0, y0 = map(int, origin.split(","))
        # The pinned square grid uses Chebyshev distance. Clamp before division
        # or conversion, including huge finite floats and arbitrary Python ints.
        extent = max(self.width, self.height)
        if range_ft >= extent * self.cell_size_ft:
            radius = extent
        elif type(range_ft) is float:
            numerator, denominator = range_ft.as_integer_ratio()
            radius = numerator // (denominator * self.cell_size_ft)
        else:
            radius = range_ft // self.cell_size_ft
        return tuple(
            f"{x},{y}"
            for y in range(max(0, y0 - radius), min(self.height, y0 + radius + 1))
            for x in range(max(0, x0 - radius), min(self.width, x0 + radius + 1))
            if topology.within_range(origin, f"{x},{y}", range_ft)
        )

    def _range_origin(self, origin, range_ft):
        if (
            type(range_ft) not in (int, float)
            or (type(range_ft) is float and not isfinite(range_ft))
            or range_ft < 0
        ):
            raise StateMachineError("invalid_query", "Expected finite nonnegative range")
        return self.normalize_cell(origin)

    def entities_within_range(self, origin, range_ft):
        origin = self._range_origin(origin, range_ft)
        topology = self._base()
        return tuple(
            occupant
            for occupant in self.occupants
            if any(topology.within_range(origin, cell, range_ft) for cell in occupant.cells)
        )

    def neighbors(self, cell):
        cell = self.normalize_cell(cell)
        x0, y0 = map(int, cell.split(","))
        return tuple(
            f"{x},{y}"
            for y in range(max(0, y0 - 1), min(self.height, y0 + 2))
            for x in range(max(0, x0 - 1), min(self.width, x0 + 2))
            if (x, y) != (x0, y0) and self._base().within_range(cell, f"{x},{y}", self.cell_size_ft)
        )

    def movement_blocking_cells(self):
        return tuple(
            sorted(
                set(self._map.data["blocked_cells"])
                | {c for _, p in self.objects for c in p.movement_blocking_cells}
            )
        )

    def difficult_terrain_cells(self):
        return tuple(
            sorted(
                set(self._map.data["difficult_terrain_cells"])
                | {c for _, p in self.objects for c in p.difficult_terrain_cells}
            )
        )

    def can_step(self, a, b):
        """Basic passage geometry, without creature-passage rules or movement payment."""
        a, b = self.normalize_cell(a), self.normalize_cell(b)
        raw = self._map.copy_data()
        raw["blocked_cells"] = list(self.movement_blocking_cells())
        topology = (
            self._base()
            if set(raw["blocked_cells"]) == set(self._map.data["blocked_cells"])
            else ReadOnlyTopology(GridScene(**raw))
        )
        return topology.is_valid_cell(a) and topology.edge_distance(a, b) is not None

    def has_line_of_sight(self, a, b):
        """Geometric occlusion only; no lighting, senses or perception authorization."""
        a, b = self.normalize_cell(a), self.normalize_cell(b)
        if any(p.vision_occlusion_cells for _, p in self.objects):
            raise StateMachineError(
                "unsupported_grid_vision", "RE has no independent dynamic vision channel"
            )
        return self._base().has_line_of_sight(a, b) and self.cover_between(a, b) != "total"

    def cover_between(self, a, b, *, creature_cover=False):
        a, b = self.normalize_cell(a), self.normalize_cell(b)
        base = self._base()
        if not base.has_line_of_sight(a, b):
            return "total"
        raw = self._map.copy_data()
        occupied = (
            tuple(c for o in self.occupants if o.kind == "actor" for c in o.cells)
            if creature_cover
            else ()
        )
        degrees = [base.cover_between(a, b, occupied_cells=occupied)]
        # Each object is an independent cover source; RE computes its ray contribution.
        for _, projection in self.objects:
            if projection.cover_cells:
                topology = GridTopology(
                    GridScene(**{**raw, "cover_cells": dict(projection.cover_cells)})
                )
                degrees.append(topology.cover_between(a, b))
        return max(degrees, key=("none", "half", "three_quarters", "total").index)

    def cells_in_template(self, origin, shape, size_ft, *, direction=None, width_ft=None):
        """RE's 2D raster template only, including its origin convention; no effect/LoS filter."""
        origin = self.normalize_cell(origin)
        if type(size_ft) is not int or size_ft < 0:
            raise StateMachineError("invalid_query", "Expected nonnegative integer template size")
        if width_ft is not None and (type(width_ft) is not int or width_ft <= 0):
            raise StateMachineError("invalid_query", "Expected positive integer template width")
        if direction is not None and (
            type(direction) is not tuple
            or len(direction) != 2
            or any(type(v) is not int for v in direction)
            or direction == (0, 0)
        ):
            raise StateMachineError("invalid_query", "Expected nonzero integer direction")
        try:
            return tuple(
                self._base().cells_in_template(
                    origin, shape, size_ft, direction=direction, width_ft=width_ft
                )
            )
        except ValueError as exc:
            raise StateMachineError("unsupported_grid_template", str(exc)) from exc


class GridReader:
    """Leaf reads only: depends on Store/WorldFacts, never OSS/Light/composition."""

    def __init__(self, store, facts):
        self.store = store
        self.facts = facts
        self.maps = facts.maps

    @contextmanager
    def read_scope(self):
        if self.store.connection.in_transaction:
            yield
        else:
            with self.store.transaction():
                yield

    @_read
    def prepared_scene(self, sid, scene_id, package):
        stored = self.maps.scene(sid, scene_id, spatial_only=True)
        if stored is not None:
            prepared = stored.grid
        else:
            initialized = self.store.connection.execute(
                "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, scene_id)
            ).fetchone()
            # Missing live grid is not permission to reset facts from a template.
            raw = None if initialized else self.facts.instances.scene(sid, scene_id).grid_json
            prepared = None if raw is None else self.maps.template(sid, scene_id, raw)
        if prepared is None:
            raise StateMachineError("spatial_unconfigured", "Fine spatial data is unavailable")
        return prepared

    @_read
    def scene_grid(self, sid, scene_id, package):
        return self.prepared_scene(sid, scene_id, package).copy_data()

    def topology_for_grid(self, sid, scene_id, raw):
        """Exact detached projection, including every supplied lighting/geometry input."""
        return self.maps.for_grid(sid, scene_id, raw).topology

    @_read
    def scene_occupants(self, sid, scene_id, package):
        initialized = self.store.connection.execute(
            "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, scene_id)
        ).fetchone()
        actors = [
            tuple(r)
            for r in self.store.connection.execute(
                "SELECT actor_id,cell FROM actors WHERE session_id=? AND current_scene_id=?",
                (sid, scene_id),
            )
        ]
        objects = [
            tuple(r)
            for r in self.store.connection.execute(
                "SELECT object_id,cell FROM objects WHERE session_id=? AND scene_id=?",
                (sid, scene_id),
            )
        ]
        if not initialized:
            pending_actors = self.facts.instances.scene(sid, scene_id).pending_actors
            existing = {
                r[0]
                for r in self.store.connection.execute(
                    "SELECT actor_id FROM actors WHERE session_id=? "
                    "AND actor_id IN (SELECT value FROM json_each(?))",
                    (sid, json.dumps([a for a, _ in pending_actors])),
                )
            }
            actors.extend(
                (actor_id, cell) for actor_id, cell in pending_actors if actor_id not in existing
            )
            pending_objects = self.facts.instances.scene(sid, scene_id).objects
            existing_objects = {
                r[0]
                for r in self.store.connection.execute(
                    "SELECT object_id FROM objects WHERE session_id=? "
                    "AND object_id IN (SELECT value FROM json_each(?))",
                    (sid, json.dumps([o.object_id for o in pending_objects])),
                )
            }
            objects.extend(
                (o.object_id, o.initial_cell)
                for o in pending_objects
                if o.object_id not in existing_objects
            )
        return sorted(actors), sorted(objects)


class GridService:
    """Trusted World composition. Callers retain Session/Package authorization.

    Every multi-read operation owns one read transaction or joins the caller's
    active scope. Only immutable, content-identified maps are retained; no Scene
    initialization, write or event, and never commits a caller's scope.
    """

    def __init__(self, handler):
        self.h = handler
        self.reader = GridReader(handler.store, handler.facts)

    def read_scope(self):
        return self.reader.read_scope()

    @staticmethod
    def package_topology(package, scene_id):
        """Static approved input only; Navigation must check map grants first."""
        raw = package.scene(scene_id).grid_json
        if raw is None:
            raise StateMachineError("spatial_unconfigured", "Scene has no grid")
        return GridTopology(GridScene(**json.loads(raw)))

    def prepared_package_topology(self, sid, package, scene_id):
        """Authorized Navigation's static map, separate from live Session facts."""
        raw = package.scene(scene_id).grid_json
        if raw is None:
            raise StateMachineError("spatial_unconfigured", "Scene has no grid")
        return self.reader.maps.template(sid, scene_id, raw).topology

    @_read
    @scoped_read
    def base_topology(self, sid, scene_id, package=None, *, with_extent=False):
        """Legacy static/Light authorized-visibility input; not the combined current view."""
        scene = self.reader.maps.scene(sid, scene_id, spatial_only=True)
        if scene is None or scene.grid is None:
            raise StateMachineError("spatial_unconfigured", "Scene has no fine spatial capability")
        prepared = scene.grid
        grid = prepared.copy_data()
        topology = prepared.topology
        if package is not None:
            grid, _, _ = self.h.objects.lights.project(sid, scene_id, package, grid)
            topology = self.reader.topology_for_grid(sid, scene_id, grid)
        return (topology, (grid["width"], grid["height"])) if with_extent else topology

    @_read
    def read_scene(self, sid, scene_id, package):
        row = self.h._conn.execute(
            "SELECT world_version FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        if row is None:
            raise StateMachineError("invalid_target", "Unknown Session")
        prepared = self.reader.prepared_scene(sid, scene_id, package)
        base = prepared.topology
        actors, _ = self.reader.scene_occupants(sid, scene_id, package)
        objects = tuple(
            sorted(self.h.objects.spatial.scene_contributions(sid, scene_id, package).items())
        )
        occupants = tuple(GridOccupant("actor", aid, (cell,)) for aid, cell in actors) + tuple(
            GridOccupant("object", oid, p.occupied_cells) for oid, p in objects
        )
        if any(not o.cells or any(not cell_in_bounds(base, c) for c in o.cells) for o in occupants):
            raise StateMachineError(
                "invalid_spatial_state", "Entity placement is missing or out of bounds"
            )
        return GridView(scene_id, row[0], prepared.grid_json, occupants, objects, prepared)

    @_read
    @scoped_read
    def local_topology(
        self,
        sid,
        scene_id,
        package,
        cells,
        *,
        exclude_actor=None,
        exclude_object=None,
        handled_thresholds=(),
    ):
        """Dynamic blockers for local edges only; includes diagonal corner cells.

        Consumers may test edges among the supplied cells, never use this as a
        complete map for path planning. The immutable base topology is reused.
        """
        proof = self.h._conn.execute(
            "SELECT complete FROM scene_integrity WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        ).fetchone()
        initialized = self.h._conn.execute(
            "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, scene_id)
        ).fetchone()
        if initialized and (proof is None or not proof[0]):
            raise StateMachineError(
                "incomplete_session", "Scene requires explicit integrity acceptance"
            )
        if not initialized:
            # Prediction for a genuinely absent destination uses the existing
            # complete scene input. No live instance can enter this fallback.
            return self.exploration_topology(
                sid,
                scene_id,
                package,
                exclude_actor=exclude_actor,
                exclude_object=exclude_object,
                handled_thresholds=handled_thresholds,
            )
        prepared = self.reader.prepared_scene(sid, scene_id, package)
        if any(not cell_in_bounds(prepared.topology, c) for c in cells):
            raise StateMachineError("invalid_spatial_state", "Local edge placement is incomplete")
        points = [tuple(map(int, c.split(","))) for c in cells]
        relevant = set(cells)
        for x, y in points:
            relevant.update(f"{x + dx},{y + dy}" for dx in (-1, 0, 1) for dy in (-1, 0, 1))
        encoded = json.dumps(sorted(relevant))
        passages = self.h.facts.instances.passages(sid, scene_id=scene_id)
        doors = json.dumps([p.door_object_id for p in passages if p.door_object_id])
        occupied = {
            r[0]
            for r in self.h._conn.execute(
                "SELECT cell FROM actors WHERE session_id=? AND current_scene_id=? "
                "AND actor_id IS NOT ? AND cell IN (SELECT value FROM json_each(?))",
                (sid, scene_id, exclude_actor, encoded),
            )
        }
        occupied.update(
            r[0]
            for r in self.h._conn.execute(
                "SELECT cell FROM object_spatial_cells WHERE session_id=? AND scene_id=? "
                "AND object_id IS NOT ? AND blocks=1 AND cell IN (SELECT value FROM json_each(?)) "
                "AND object_id NOT IN (SELECT value FROM json_each(?))",
                (sid, scene_id, exclude_object, encoded, doors),
            )
        )
        for passage in passages:
            if passage.door_object_id == exclude_object:
                continue
            endpoints = [
                e
                for e in passage.endpoints
                if e.scene_id == scene_id
                and e.threshold_cell in relevant
                and (passage.passage_id, e.threshold_cell) not in handled_thresholds
            ]
            if endpoints and passage.door_object_id and self.h._door_closed(sid, passage, package):
                occupied.update(e.threshold_cell for e in endpoints)
        raw = prepared.copy_data()
        return self.reader.topology_for_grid(
            sid,
            scene_id,
            {
                **raw,
                "blocked_cells": sorted(set(raw["blocked_cells"]) | occupied),
            },
        )

    @_read
    @scoped_read
    def exploration_topology(
        self,
        sid,
        scene_id,
        package,
        *,
        exclude_actor=None,
        exclude_object=None,
        handled_thresholds=(),
    ):
        """Existing conservative exploration/reach policy. NEVER a mechanical or vision grid.

        Creatures block exploration here by existing policy, not because they are
        walls. RE/Combat retains creature passage rules and all movement budgets.
        """
        prepared = self.reader.prepared_scene(sid, scene_id, package)
        raw = prepared.copy_data()
        actors, _ = self.reader.scene_occupants(sid, scene_id, package)
        projections = self.h.objects.spatial.scene_contributions(sid, scene_id, package)
        base = prepared.topology
        if any(not cell_in_bounds(base, cell) for _, cell in actors) or any(
            not p.occupied_cells or any(not cell_in_bounds(base, cell) for cell in p.occupied_cells)
            for p in projections.values()
        ):
            raise StateMachineError("invalid_spatial_state", "Current grid placement is incomplete")
        doors = {
            p.door_object_id
            for p in self.h.facts.instances.passages(sid, scene_id=scene_id)
            if p.door_object_id
        }
        cells = set(raw["blocked_cells"])
        cells.update(cell for aid, cell in actors if aid != exclude_actor and cell is not None)
        cells.update(
            cell
            for oid, p in projections.items()
            if oid != exclude_object and oid not in doors
            for cell in p.movement_blocking_cells
        )
        for passage in self.h.facts.instances.passages(sid, scene_id=scene_id):
            if passage.door_object_id and self.h._door_closed(sid, passage, package):
                cells.update(
                    e.threshold_cell
                    for e in passage.endpoints
                    if e.scene_id == scene_id
                    and (passage.passage_id, e.threshold_cell) not in handled_thresholds
                )
        return self.reader.topology_for_grid(sid, scene_id, {**raw, "blocked_cells": sorted(cells)})
