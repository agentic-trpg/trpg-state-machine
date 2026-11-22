"""Internal current-space reads. No position authority or mechanical settlement.

Views are detached, immutable inputs for trusted components, never actor catalogues.
RE owns geometry. Exploration's conservative occupancy policy stays separate from
static geometry, vision and cover, and must not be supplied as a combat Grid.
"""

import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import InitVar, dataclass
from functools import wraps
from math import isfinite
from types import MappingProxyType

from dnd5e_engine.spatial import GridTopology, _bresenham_cells, canonical_cell_id, parse_cell
from dnd5e_engine.specs import GridScene

from .errors import StateMachineError
from .grid_maps import ObserverTopology, PreparedGrid
from .instance_data import actor_light_opaque, passage_index
from .runtime_instances import instance_read
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
    """Wrap a grid method in a consistent read scope."""

    @wraps(method)
    def read(self, *args, **kwargs):
        """Call the wrapped grid method inside the grid read scope."""
        with self.read_scope():
            return method(self, *args, **kwargs)

    return read


@dataclass(frozen=True, slots=True)
class GridOccupant:
    """Associate an entity with its occupied scene cells and spatial kind."""

    kind: str
    entity_id: str
    cells: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LightOccluders:
    """Fixed local light geometry in cell units, with unknown legacy occupancy."""

    walls: tuple[tuple[float, float, float, float], ...] = ()
    opaque_cells: frozenset[str] = frozenset()
    uncertain_cells: frozenset[str] = frozenset()
    opaque_objects: tuple[tuple[str, tuple[str, ...]], ...] = ()
    opaque_actor_cells: frozenset[str] = frozenset()

    def solid_cells(self, *, exclude_object=None, origin=None):
        """Resolve static and fixture cells while excluding the specified source object."""
        return (self.opaque_cells - {origin}).union(
            cell
            for oid, footprint in self.opaque_objects
            if oid != exclude_object
            for cell in footprint
        )

    def __deepcopy__(self, memo):
        """Share immutable geometry across readers and savepoints."""
        return self


class _GridSnapshot:
    # Keep prepared implementation data out of dataclass fields/asdict/wire data.
    """Carry fixed map, occupants and object contributions for grid queries."""

    __slots__ = (
        "_map",
        "_by_cell",
        "_by_entity",
        "_movement",
        "_cover",
        "_creature_cover",
        "_vision",
    )


@dataclass(frozen=True, slots=True)
class GridView(_GridSnapshot):
    """Query a fixed grid snapshot using prepared occupancy and geometry indices."""

    scene_id: str
    world_version: int
    grid_json: str
    occupants: tuple[GridOccupant, ...]
    objects: tuple[tuple[str, ObjectSpatialProjection], ...]
    _prepared: InitVar[PreparedGrid | None] = None

    def __deepcopy__(self, memo):
        # Hidden read-only preparations need no reconstruction for a detached DTO.
        """Copy the fixed view while preserving immutable prepared input isolation."""
        result = object.__new__(type(self))
        memo[id(self)] = result
        for name in ("scene_id", "world_version", "grid_json", "occupants", "objects"):
            object.__setattr__(result, name, deepcopy(getattr(self, name), memo))
        for name in _GridSnapshot.__slots__:
            object.__setattr__(result, name, getattr(self, name))
        return result

    def __post_init__(self, _prepared):
        """Prepare occupancy, entity and cover query indices from the fixed map."""
        prepared = _prepared
        if prepared is None or prepared.grid_json != self.grid_json:
            prepared = PreparedGrid.build(json.loads(self.grid_json))
        object.__setattr__(self, "_map", prepared)
        cells, entities = {}, {}
        for index, occupant in enumerate(self.occupants):
            entities.setdefault((occupant.kind, occupant.entity_id), occupant.cells)
            for cell in occupant.cells:
                cells.setdefault(cell, set()).add(index)
        object.__setattr__(
            self, "_by_cell", MappingProxyType({c: tuple(v) for c, v in cells.items()})
        )
        object.__setattr__(self, "_by_entity", MappingProxyType(entities))
        object.__setattr__(
            self,
            "_movement",
            prepared.topology.with_blocked(
                c for _, p in self.objects for c in p.movement_blocking_cells
            ),
        )
        rank = ("none", "half", "three_quarters", "total").index
        cover = dict(prepared.data["cover_cells"])
        for _, projection in self.objects:
            for cell, degree in projection.cover_cells:
                if rank(degree) > rank(cover.get(cell, "none")):
                    cover[cell] = degree
        object.__setattr__(self, "_cover", prepared.topology.with_cover(cover))
        for occupant in self.occupants:
            if occupant.kind == "actor":
                for cell in occupant.cells:
                    if rank(cover.get(cell, "none")) < rank("half"):
                        cover[cell] = "half"
        object.__setattr__(self, "_creature_cover", prepared.topology.with_cover(cover))
        object.__setattr__(self, "_vision", any(p.vision_occlusion_cells for _, p in self.objects))

    @property
    def width(self):
        """Return map width in cells."""
        return self._map.data["width"]

    @property
    def height(self):
        """Return map height in cells."""
        return self._map.data["height"]

    @property
    def cell_size_ft(self):
        """Return cell edge length in feet."""
        return self._map.topology.cell_size_ft

    def _base(self):
        """Return the fixed base topology."""
        return self._map.topology

    def normalize_cell(self, cell):
        """Normalize a cell identity and check map bounds."""
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
        indices = {i for c in selected for i in self._by_cell.get(c, ())}
        return tuple(self.occupants[i] for i in sorted(indices))

    def entity_cells(self, kind, entity_id):
        """Read the fixed occupied cells for one entity."""
        try:
            return self._by_entity[kind, entity_id]
        except KeyError as exc:
            raise StateMachineError("invalid_target", "Entity has no local grid placement") from exc

    def distance_ft(self, a, b):
        """Geometric distance; neither path length nor movement cost."""
        return self._base().distance_ft(self.normalize_cell(a), self.normalize_cell(b))

    def is_adjacent(self, a, b):
        # RE is_adjacent also filters blocked destinations; this is geometry only.
        """Return whether the geometric distance qualifies as adjacent."""
        return self.distance_ft(a, b) == self.cell_size_ft

    def cells_within_range(self, origin, range_ft):
        """Enumerate cells within the requested geometric range."""
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
        """Validate the range and normalize its origin."""
        if (
            type(range_ft) not in (int, float)
            or (type(range_ft) is float and not isfinite(range_ft))
            or range_ft < 0
        ):
            raise StateMachineError("invalid_query", "Expected finite nonnegative range")
        return self.normalize_cell(origin)

    def entities_within_range(self, origin, range_ft):
        """Return entities with at least one occupied cell in range."""
        origin = self._range_origin(origin, range_ft)
        topology = self._base()
        return tuple(
            occupant
            for occupant in self.occupants
            if any(topology.within_range(origin, cell, range_ft) for cell in occupant.cells)
        )

    def neighbors(self, cell):
        """Enumerate neighboring cells within map bounds."""
        cell = self.normalize_cell(cell)
        x0, y0 = map(int, cell.split(","))
        return tuple(
            f"{x},{y}"
            for y in range(max(0, y0 - 1), min(self.height, y0 + 2))
            for x in range(max(0, x0 - 1), min(self.width, x0 + 2))
            if (x, y) != (x0, y0) and self._base().within_range(cell, f"{x},{y}", self.cell_size_ft)
        )

    def movement_blocking_cells(self):
        """Combine base and object movement-blocking cells."""
        return tuple(
            sorted(
                set(self._map.data["blocked_cells"])
                | {c for _, p in self.objects for c in p.movement_blocking_cells}
            )
        )

    def difficult_terrain_cells(self):
        """Combine base and object difficult terrain cells."""
        return tuple(
            sorted(
                set(self._map.data["difficult_terrain_cells"])
                | {c for _, p in self.objects for c in p.difficult_terrain_cells}
            )
        )

    def can_step(self, a, b):
        """Basic passage geometry, without creature-passage rules or movement payment."""
        a, b = self.normalize_cell(a), self.normalize_cell(b)
        topology = self._movement
        return topology.is_valid_cell(a) and topology.edge_distance(a, b) is not None

    def has_line_of_sight(self, a, b):
        """Geometric occlusion only; no lighting, senses or perception authorization."""
        a, b = self.normalize_cell(a), self.normalize_cell(b)
        if self._vision:
            raise StateMachineError(
                "unsupported_grid_vision", "RE has no independent dynamic vision channel"
            )
        return self._base().has_line_of_sight(a, b) and self.cover_between(a, b) != "total"

    def cover_between(self, a, b, *, creature_cover=False):
        """Combine base, object and optional creature cover."""
        a, b = self.normalize_cell(a), self.normalize_cell(b)
        base = self._base()
        if not base.has_line_of_sight(a, b):
            return "total"
        # Max across independent sources commutes with RE's max along the ray.
        # Creature endpoints must still be excluded, unlike explicit object cover.
        degree = self._cover.cover_between(a, b)
        if creature_cover:
            endpoint = self._creature_cover.with_cover_at(
                b, self._cover._cover_cells.get(b, "none")
            )
            degree = max(
                (degree, endpoint.cover_between(a, b)),
                key=("none", "half", "three_quarters", "total").index,
            )
        return degree

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
        """Bind grid facts and the shared map interface."""
        self.store = store
        self.facts = facts
        self.maps = facts.maps

    @contextmanager
    def read_scope(self):
        """Reuse the active read scope or open a read-only transaction when needed."""
        if self.store.connection.in_transaction:
            yield
        else:
            with self.store.transaction():
                yield

    @_read
    @instance_read("scene", "spatial", "map")
    def prepared_scene(self, sid, scene_id, package):
        """Prepare persisted scene maps, distinguishing predictions for uncreated scenes."""
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
        """Return a detached data copy of the prepared scene map."""
        return self.prepared_scene(sid, scene_id, package).copy_data()

    @_read
    @instance_read("scene", "spatial", "mechanical_map")
    def current_grid(self, sid, scene_id, package):
        """Mechanical-only maps need not grant fine spatial operations."""
        stored = self.maps.scene(sid, scene_id)
        if stored is not None and stored.grid is not None:
            return stored.grid
        return self.prepared_scene(sid, scene_id, package)

    def topology_for_grid(self, sid, scene_id, raw):
        """Exact detached projection, including every supplied lighting/geometry input."""
        return self.maps.for_grid(sid, scene_id, raw).topology

    @_read
    @instance_read("scene", "spatial", "object_positions")
    def scene_object_occupants(self, sid, scene_id, package):
        """Read scene object occupancy, including definitions not yet instantiated."""
        initialized = self.store.connection.execute(
            "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, scene_id)
        ).fetchone()
        objects = [
            tuple(r)
            for r in self.store.connection.execute(
                "SELECT object_id,cell FROM objects WHERE session_id=? AND scene_id=?",
                (sid, scene_id),
            )
        ]
        if not initialized:
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
        return sorted(objects)

    @_read
    @instance_read("scene", "spatial", "occupants")
    def scene_occupants(self, sid, scene_id, package):
        """Read character and object occupancy for the scene."""
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
        return sorted(actors), self.scene_object_occupants(sid, scene_id, package)

    @_read
    @instance_read("scene", "spatial", "cells", capacity=128)
    def cell_occupants(self, sid, scene_id, cell):
        """The loaded cell relation, backed by the existing position indices."""
        actors = tuple(
            r[0]
            for r in self.store.connection.execute(
                "SELECT actor_id FROM actors WHERE session_id=? AND current_scene_id=? AND cell=?",
                (sid, scene_id, cell),
            )
        )
        objects = tuple(
            r[0]
            for r in self.store.connection.execute(
                "SELECT object_id FROM object_spatial_cells "
                "WHERE session_id=? AND scene_id=? AND blocks=1 AND cell=?",
                (sid, scene_id, cell),
            )
        )
        return actors, objects

    def region_occupants(self, sid, scene_id, cells, *, include_actors=False, include_objects=True):
        """Read the requested occupant kinds in selected cells in the open scope.

        Return actor (id, cell) rows and object (id, cell, movement flag) rows.
        Only cells inside the caller's selection are included.
        """
        parameters = sid, scene_id, json.dumps(sorted(cells))
        actors, objects = (), ()
        if include_actors:
            actors = self.store.connection.execute(
                "SELECT actor_id,cell FROM actors WHERE session_id=? AND current_scene_id=? "
                "AND cell IN (SELECT value FROM json_each(?))",
                parameters,
            )
        if include_objects:
            objects = self.store.connection.execute(
                "SELECT object_id,cell,blocks FROM object_spatial_cells "
                "WHERE session_id=? AND scene_id=? AND cell IN (SELECT value FROM json_each(?))",
                parameters,
            )
        return actors, objects


class GridService:
    """Trusted World composition. Callers retain Session/Package authorization.

    Every multi-read operation owns one read transaction or joins the caller's
    active scope. Only immutable, content-identified maps are retained; no Scene
    initialization, write or event, and never commits a caller's scope.
    """

    def __init__(self, handler):
        """Bind the grid service and its reader."""
        self.h = handler
        self.reader = GridReader(handler.store, handler.facts)

    def read_scope(self):
        """Delegate consistent read scope management to the reader."""
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
    def base_topology(
        self, sid, scene_id, package=None, *, with_extent=False, cells=None, observer_cell=None
    ):
        """Read static topology or overlay current light on a whole map or selection."""
        prepared = self._sight_grid(sid, scene_id)
        topology = prepared.topology
        if package is not None:
            if cells is None:
                topology = self.h.objects.lights.current_topology(sid, scene_id, package)
            else:
                selected = tuple(
                    cell for cell in dict.fromkeys(cells) if cell_in_bounds(topology, cell)
                )
                topology = self._sight_lighting(
                    sid, scene_id, package, topology, selected, observer_cell
                )
        return (
            (topology, (prepared.data["width"], prepared.data["height"]))
            if with_extent
            else topology
        )

    def _sight_grid(self, sid, scene_id):
        """Read the active fine-spatial map without predicting uninstantiated facts."""
        scene = self.reader.maps.scene(sid, scene_id, spatial_only=True)
        if scene is None or scene.grid is None:
            raise StateMachineError("spatial_unconfigured", "Scene has no fine spatial capability")
        return scene.grid

    def _sight_lighting(self, sid, scene_id, package, topology, selected, observer_cell):
        """Overlay target light and any relevant magical darkness along their sight rays."""
        if not selected:
            return topology
        if cell_in_bounds(topology, observer_cell) and (
            self.h.magic.may_have_darkness(sid, scene_id)
            or self.h.magic.may_have_obscurement(sid, scene_id)
        ):
            origin = parse_cell(observer_cell)
            rays = (
                cell
                for target in selected
                for cell in _bresenham_cells(*origin, *parse_cell(target))
            )
            selected = tuple(dict.fromkeys((*selected, *rays)))
        levels = self.h.objects.lights.current_cells(sid, scene_id, package, selected)
        lit = topology.with_lighting(
            {cell: result.level for cell, result in levels.items()},
            sunlight_overrides={cell: result.sunlight for cell, result in levels.items()},
            magical_darkness_cells=(
                cell for cell, result in levels.items() if result.magical_darkness
            ),
        )
        return lit.with_obscurement(self.h.magic.obscuring_cells(sid, scene_id, package, selected))

    @_read
    @scoped_read
    def visibility_topology(self, sid, scene_id, package, observer_cell, cells):
        """Prepare ordinary-sight inputs for valid requested rays with no total cover."""
        prepared = self._sight_grid(sid, scene_id)
        topology = prepared.topology
        clear_rays = {}
        if cell_in_bounds(topology, observer_cell):
            for cell in dict.fromkeys(cells):
                if not cell_in_bounds(topology, cell) or not topology.has_line_of_sight(
                    observer_cell, cell
                ):
                    continue
                cover = topology.cover_between(observer_cell, cell)
                if cover != "total":
                    clear_rays[cell] = cover
        topology = self._sight_lighting(
            sid, scene_id, package, topology, tuple(clear_rays), observer_cell
        )
        return ObserverTopology(topology, observer_cell, clear_rays)

    @_read
    def read_scene(self, sid, scene_id, package):
        """Build a fixed grid view with scene occupants and spatial contributions."""
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
    def light_occluders(
        self, sid, scene_id, package, cells, *, fixed_grid=None, include_actors=False
    ):
        """Read static and current dynamic blockers touching conservative light corridors.

        The supplied prepared map can be a fixed stage input. Object footprint
        and door state use the caller's current transaction or candidate scope.
        Objective light can request opaque Actor cells. Spell delivery and
        overlap keep their separate rules blocking policy.
        """
        prepared = (
            self.reader.current_grid(sid, scene_id, package) if fixed_grid is None else fixed_grid
        )
        selected = frozenset(cell for cell in cells if cell_in_bounds(prepared.topology, cell))
        if not selected:
            return LightOccluders()
        if len(selected) <= 512:
            dynamic = self._cached_light_blockers(sid, scene_id, package, selected, include_actors)
        else:
            dynamic = self._dynamic_light_blockers(sid, scene_id, package, selected, include_actors)
        return LightOccluders(
            prepared.topology.light_walls(selected),
            dynamic.opaque_cells | prepared.topology.light_solid_cells(selected),
            dynamic.uncertain_cells,
            dynamic.opaque_objects,
            dynamic.opaque_actor_cells,
        )

    @instance_read("scene", "lighting", "candidates", capacity=8, maintained="light_candidates")
    def _cached_light_blockers(self, sid, scene_id, package, selected, include_actors):
        """Keep a small number of regions tied to the Scene's dynamic geometry version."""
        return self._dynamic_light_blockers(sid, scene_id, package, selected, include_actors)

    def _dynamic_light_blockers(self, sid, scene_id, package, selected, include_actors):
        """Resolve door, object and opt-in Actor geometry in the current scope."""
        opaque = set()
        uncertain, opaque_objects = set(), []
        passages = passage_index(self.h.store, sid)
        doors = {}
        for cell in selected:
            for oid in passages.doors_by_threshold.get((scene_id, cell), ()):
                doors.setdefault(oid, []).append(cell)
        spatial = self.h.objects.spatial
        for oid, thresholds in doors.items():
            if spatial.light_door_closed(sid, oid, package):
                opaque.update(thresholds)
        touching = {}
        actors, occupants = self.reader.region_occupants(
            sid, scene_id, selected, include_actors=include_actors
        )
        opaque_actor_cells = frozenset(
            cell for aid, cell in actors if actor_light_opaque(self.h.store, sid, aid)
        )
        for oid, cell, blocks in occupants:
            if oid not in passages.by_door:
                touching.setdefault(oid, {})[cell] = blocks
        for oid, footprint in sorted(touching.items()):
            declared, legacy_anchor = spatial.light_occlusion(sid, oid, package)
            if declared:
                opaque_objects.append((oid, tuple(sorted(footprint))))
            elif declared is None:
                uncertain.update(
                    c for c, blocks in footprint.items() if blocks or c == legacy_anchor
                )
        return LightOccluders(
            opaque_cells=frozenset(opaque),
            uncertain_cells=frozenset(uncertain),
            opaque_objects=tuple(opaque_objects),
            opaque_actor_cells=opaque_actor_cells,
        )

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
        passages = self.h.facts.instances.passages(sid, scene_id=scene_id)
        doors = {p.door_object_id for p in passages if p.door_object_id}
        occupied = set()
        for cell in sorted(relevant):
            actors, objects = self.reader.cell_occupants(sid, scene_id, cell)
            if any(aid != exclude_actor for aid in actors) or any(
                oid != exclude_object and oid not in doors for oid in objects
            ):
                occupied.add(cell)
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
        return prepared.topology.with_blocked(occupied)

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
        cells = set()
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
        return prepared.topology.with_blocked(cells)
