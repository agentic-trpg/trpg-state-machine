"""Internal current-space reads. No position authority or mechanical settlement.

Views are detached, immutable inputs for trusted components, never actor catalogues.
RE owns geometry. Exploration's conservative occupancy policy stays separate from
static geometry, vision and cover, and must not be supplied as a combat Grid.
"""

import json
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from math import isfinite

from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene

from .errors import StateMachineError
from .package import canonical_json
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


@dataclass(frozen=True, slots=True)
class GridView:
    scene_id: str
    world_version: int
    grid_json: str
    occupants: tuple[GridOccupant, ...]
    objects: tuple[tuple[str, ObjectSpatialProjection], ...]

    @property
    def width(self):
        return json.loads(self.grid_json)["width"]

    @property
    def height(self):
        return json.loads(self.grid_json)["height"]

    @property
    def cell_size_ft(self):
        return json.loads(self.grid_json)["cell_size_ft"]

    def _base(self):
        return GridTopology(GridScene(**json.loads(self.grid_json)))

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
        if type(range_ft) not in (int, float) or not isfinite(range_ft) or range_ft < 0:
            raise StateMachineError("invalid_query", "Expected finite nonnegative range")
        origin = self.normalize_cell(origin)
        topology = self._base()
        return tuple(
            f"{x},{y}"
            for y in range(self.height)
            for x in range(self.width)
            if topology.within_range(origin, f"{x},{y}", range_ft)
        )

    def entities_within_range(self, origin, range_ft):
        return self.occupied(self.cells_within_range(origin, range_ft))

    def neighbors(self, cell):
        cell = self.normalize_cell(cell)
        return tuple(c for c in self.cells_within_range(cell, self.cell_size_ft) if c != cell)

    def movement_blocking_cells(self):
        return tuple(
            sorted(
                set(json.loads(self.grid_json)["blocked_cells"])
                | {c for _, p in self.objects for c in p.movement_blocking_cells}
            )
        )

    def difficult_terrain_cells(self):
        return tuple(
            sorted(
                set(json.loads(self.grid_json)["difficult_terrain_cells"])
                | {c for _, p in self.objects for c in p.difficult_terrain_cells}
            )
        )

    def can_step(self, a, b):
        """Basic passage geometry, without creature-passage rules or movement payment."""
        a, b = self.normalize_cell(a), self.normalize_cell(b)
        raw = json.loads(self.grid_json)
        raw["blocked_cells"] = list(self.movement_blocking_cells())
        topology = GridTopology(GridScene(**raw))
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
        raw = json.loads(self.grid_json)
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

    @contextmanager
    def read_scope(self):
        if self.store.connection.in_transaction:
            yield
        else:
            with self.store.transaction():
                yield

    @_read
    def scene_grid(self, sid, scene_id, package):
        stored = self.facts.spatial_scene(sid, scene_id)
        if stored is not None:
            raw = stored.get("grid")
        else:
            initialized = self.store.connection.execute(
                "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, scene_id)
            ).fetchone()
            # Missing live grid is not permission to reset facts from a template.
            raw = None if initialized else package.scene(scene_id).grid_json
            if raw is not None:
                raw = json.loads(raw)
        if raw is None:
            raise StateMachineError("spatial_unconfigured", "Fine spatial data is unavailable")
        return raw

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
            existing = {
                r[0]
                for r in self.store.connection.execute(
                    "SELECT actor_id FROM actors WHERE session_id=?", (sid,)
                )
            }
            actors.extend(
                (a.actor_id, a.initial_cell)
                for a in package.actors
                if a.initial_scene_id == scene_id and a.actor_id not in existing
            )
            existing_objects = {
                r[0]
                for r in self.store.connection.execute(
                    "SELECT object_id FROM objects WHERE session_id=?", (sid,)
                )
            }
            objects.extend(
                (o.object_id, o.initial_cell)
                for o in package.scene(scene_id).objects
                if o.object_id not in existing_objects
            )
        return sorted(actors), sorted(objects)


class GridService:
    """Trusted World composition. Callers retain Session/Package authorization.

    Every multi-read operation owns one read transaction or joins the caller's
    active scope. No cache, initialization, write or event; never commits a caller's scope.
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

    @_read
    def base_topology(self, sid, scene_id, package=None):
        """Legacy static/Light authorized-visibility input; not the combined current view."""
        scene = self.h.facts.spatial_scene(sid, scene_id)
        if scene is None or scene.get("grid") is None:
            raise StateMachineError("spatial_unconfigured", "Scene has no fine spatial capability")
        grid = scene["grid"]
        if package is not None:
            grid, _, _ = self.h.objects.lights.project(sid, scene_id, package, grid)
        return GridTopology(GridScene(**grid))

    @_read
    def read_scene(self, sid, scene_id, package):
        row = self.h._conn.execute(
            "SELECT world_version FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        if row is None:
            raise StateMachineError("invalid_target", "Unknown Session")
        raw = self.reader.scene_grid(sid, scene_id, package)
        base = GridTopology(GridScene(**raw))
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
        return GridView(scene_id, row[0], canonical_json(raw), occupants, objects)

    @_read
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
        raw = self.reader.scene_grid(sid, scene_id, package)
        actors, _ = self.reader.scene_occupants(sid, scene_id, package)
        projections = self.h.objects.spatial.scene_contributions(sid, scene_id, package)
        base = GridTopology(GridScene(**raw))
        if any(not cell_in_bounds(base, cell) for _, cell in actors) or any(
            not p.occupied_cells or any(not cell_in_bounds(base, cell) for cell in p.occupied_cells)
            for p in projections.values()
        ):
            raise StateMachineError("invalid_spatial_state", "Current grid placement is incomplete")
        doors = {p.door_object_id for p in package.passages if p.door_object_id}
        cells = set(raw["blocked_cells"])
        cells.update(cell for aid, cell in actors if aid != exclude_actor and cell is not None)
        cells.update(
            cell
            for oid, p in projections.items()
            if oid != exclude_object and oid not in doors
            for cell in p.movement_blocking_cells
        )
        for passage in package.passages:
            if passage.door_object_id and self.h._door_closed(sid, passage, package):
                cells.update(
                    e.threshold_cell
                    for e in passage.endpoints
                    if e.scene_id == scene_id
                    and (passage.passage_id, e.threshold_cell) not in handled_thresholds
                )
        return GridTopology(GridScene(**{**raw, "blocked_cells": sorted(cells)}))
