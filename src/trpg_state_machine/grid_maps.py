"""Bounded Grid representations over consistent RAM/candidate fact records.

Exact content selects immutable maps; the runtime record reader resolves loaded
facts without SQL. Candidate/rolled-back/older-reader inputs cannot alias newer
data. Grid geometry remains separate from base-fact ownership and publication.
"""

import json
from collections import ChainMap, OrderedDict
from collections.abc import Mapping, Set
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType

from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene

from .package import canonical_json
from .runtime_instances import resolve_field


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, set):
        return frozenset(value)
    return value


def _thaw(value):
    if isinstance(value, MappingProxyType):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class _Wall:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True, slots=True)
class _WallIndex:
    """Immutable bounding hierarchy; candidates still use pinned RE geometry.

    Inclusive bounds retain endpoint touches and degenerate/fractional walls.
    No raster expansion of long walls, or all-coordinate-pair result cache.
    """

    bounds: tuple
    walls: tuple = ()
    children: tuple = ()

    @classmethod
    def build(cls, walls):
        if not walls:
            return None
        bounds = (
            min(min(w.x1, w.x2) for w in walls),
            min(min(w.y1, w.y2) for w in walls),
            max(max(w.x1, w.x2) for w in walls),
            max(max(w.y1, w.y2) for w in walls),
        )
        if len(walls) <= 8:
            return cls(bounds, tuple(walls))
        axis = bounds[2] - bounds[0] >= bounds[3] - bounds[1]
        ordered = sorted(walls, key=lambda w: w.x1 + w.x2 if axis else w.y1 + w.y2)
        middle = len(ordered) // 2
        return cls(bounds, children=(cls.build(ordered[:middle]), cls.build(ordered[middle:])))

    def query(self, bounds):
        x1, y1, x2, y2 = bounds
        a, b, c, d = self.bounds
        if c < x1 or x2 < a or d < y1 or y2 < b:
            return
        for wall in self.walls:
            if not (
                max(wall.x1, wall.x2) < x1
                or min(wall.x1, wall.x2) > x2
                or max(wall.y1, wall.y2) < y1
                or min(wall.y1, wall.y2) > y2
            ):
                yield wall
        for child in self.children:
            yield from child.query(bounds)


@dataclass(frozen=True, slots=True)
class _BlockedOverlay(Set):
    """Membership shares the immutable base; constructing it visits only extras."""

    base: Set
    extra: frozenset

    def __contains__(self, cell):
        return cell in self.extra or cell in self.base

    def __iter__(self):
        yield from self.base
        yield from (cell for cell in self.extra if cell not in self.base)

    def __len__(self):
        return len(self.base) + sum(cell not in self.base for cell in self.extra)

    def __bool__(self):
        return bool(self.extra) or bool(self.base)

    @classmethod
    def _from_iterable(cls, values):
        return frozenset(values)


@dataclass(frozen=True, slots=True)
class _CoverEndpoint(Mapping):
    base: Mapping
    cell: str
    degree: str

    def __getitem__(self, key):
        return self.degree if key == self.cell else self.base[key]

    def __iter__(self):
        yield from self.base
        if self.cell not in self.base:
            yield self.cell

    def __len__(self):
        return len(self.base) + int(self.cell not in self.base)


class ReadOnlyTopology(GridTopology):
    """Pinned RE algorithms with detached, recursively read-only backing inputs.

    RE's only nested model input here is WallSegment (four coordinates). Its
    geometry algorithms only read these attributes; no RE rule is reimplemented.
    """

    def __init__(self, scene):
        super().__init__(scene)
        self._walls = tuple(_Wall(w.x1, w.y1, w.x2, w.y2) for w in self._walls)
        self._wall_index = (
            _WallIndex.build(self._walls)
            if all(isfinite(v) for w in self._walls for v in (w.x1, w.y1, w.x2, w.y2))
            else None  # Preserve pinned RE behavior for nonfinite legacy input.
        )
        self.grid_edge_walls = all(
            all(float(v).is_integer() for v in (w.x1, w.y1, w.x2, w.y2))
            and (w.x1 == w.x2 or w.y1 == w.y2)
            for w in self._walls
        )
        for name, value in tuple(vars(self).items()):
            object.__setattr__(self, name, _freeze(value))
        object.__setattr__(self, "_readonly", True)

    def __setattr__(self, name, value):
        if getattr(self, "_readonly", False):
            raise AttributeError("Prepared topology is read-only")
        object.__setattr__(self, name, value)

    def __getattribute__(self, name):
        value = super().__getattribute__(name)
        if name == "__dict__" and getattr(self, "_readonly", False):
            return MappingProxyType(value)
        return value

    def __delattr__(self, name):
        raise AttributeError("Prepared topology is read-only")

    def __deepcopy__(self, memo):
        return self

    def with_lighting(self, lighting):
        """Immutable query overlay; share parsed geometry without rebuilding a map."""
        result = object.__new__(type(self))
        for name, value in vars(self).items():
            object.__setattr__(result, name, value)
        object.__setattr__(
            result, "_lighting", MappingProxyType(ChainMap(_freeze(lighting), self._lighting))
        )
        return result

    def with_blocked(self, cells):
        result = object.__new__(type(self))
        for name, value in vars(self).items():
            object.__setattr__(result, name, value)
        object.__setattr__(result, "_blocked", _BlockedOverlay(self._blocked, frozenset(cells)))
        return result

    def with_cover(self, cells):
        """Replace only the cover channel; callers prepare independent-source maxima."""
        result = object.__new__(type(self))
        for name, value in vars(self).items():
            object.__setattr__(result, name, value)
        object.__setattr__(result, "_cover_cells", MappingProxyType(dict(cells)))
        return result

    def with_cover_at(self, cell, degree):
        result = object.__new__(type(self))
        for name, value in vars(self).items():
            object.__setattr__(result, name, value)
        object.__setattr__(result, "_cover_cells", _CoverEndpoint(self._cover_cells, cell, degree))
        return result

    def _ray_topology(self, a, b):
        if self._wall_index is None:
            return self
        ax, ay = map(int, a.split(","))
        bx, by = map(int, b.split(","))
        walls = tuple(
            self._wall_index.query(
                (min(ax, bx) + 0.5, min(ay, by) + 0.5, max(ax, bx) + 0.5, max(ay, by) + 0.5)
            )
        )
        result = object.__new__(type(self))
        for name, value in vars(self).items():
            object.__setattr__(result, name, value)
        object.__setattr__(result, "_walls", walls)
        return result

    def _step_is_legal(self, a, b):
        return GridTopology._step_is_legal(self._ray_topology(a, b), a, b)

    def has_line_of_sight(self, a, b):
        if not self._in_bounds(a) or not self._in_bounds(b):
            return False
        return GridTopology.has_line_of_sight(self._ray_topology(a, b), a, b)


@dataclass(frozen=True, slots=True)
class PreparedGrid:
    grid_json: str
    data: object
    topology: ReadOnlyTopology

    @classmethod
    def build(cls, raw):
        # Serialization detaches nested inputs as well as supplying an exact key.
        return cls(canonical_json(raw), _freeze(raw), ReadOnlyTopology(GridScene(**raw)))

    def copy_data(self):
        return _thaw(self.data)

    def __deepcopy__(self, memo):
        return self


@dataclass(frozen=True, slots=True)
class PreparedScene:
    data: object
    grid: PreparedGrid | None

    def copy_data(self):
        return _thaw(self.data)

    def __deepcopy__(self, memo):
        return self


class GridMaps:
    """One pool per Store: 64 entries and 8 MiB of encoded keys per partition.

    Immutable values need no commit publication: only the exact visible encoded
    content can select them, even within unmanaged SQL/savepoint scopes. Retained
    Views own their snapshots independently of LRU eviction or Store close.
    """

    capacity = 64
    byte_limit = 8 * 1024 * 1024

    def __init__(self, store):
        self.store = store
        self._records = OrderedDict()
        self._grids = OrderedDict()
        self._sizes = {"records": 0, "grids": 0}

    def clear(self):
        self._records.clear()
        self._grids.clear()
        self._sizes = {"records": 0, "grids": 0}

    def release(self, sid):
        for name, values in (("records", self._records), ("grids", self._grids)):
            for key in tuple(values):
                if key[0] == sid:
                    self._sizes[name] -= values.pop(key)[1]

    def _resolve(self, name, key, compute):
        self.store.ensure_open()
        values = self._records if name == "records" else self._grids
        if key in values:
            values.move_to_end(key)
            return values[key][0]
        result = compute()
        size = len(key[-1].encode("utf-8"))
        if size <= self.byte_limit:
            values[key] = result, size
            self._sizes[name] += size
            while len(values) > self.capacity or self._sizes[name] > self.byte_limit:
                _, (_, old_size) = values.popitem(last=False)
                self._sizes[name] -= old_size
        return result

    def for_grid(self, sid, scene_id, raw, *, source="live"):
        if isinstance(raw, PreparedGrid):
            return raw
        encoded = canonical_json(raw)
        return self._resolve(
            "grids", (sid, scene_id, source, encoded), lambda: PreparedGrid.build(raw)
        )

    def scene(self, sid, scene_id, *, spatial_only=False):
        stored, enabled = resolve_field(
            self.store,
            "scene",
            sid,
            scene_id,
            "spatial",
            "record",
            (),
            lambda: self._scene(sid, scene_id),
        )
        return None if spatial_only and not enabled else stored

    def _scene(self, sid, scene_id):
        row = self.store.connection.execute(
            "SELECT state_json,spatial_enabled FROM mechanical_scenes "
            "WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        ).fetchone()
        if row is None:
            return None, False
        encoded = row[0]

        def build():
            data = json.loads(encoded)
            grid = (
                self.for_grid(sid, scene_id, data["grid"]) if data.get("grid") is not None else None
            )
            return PreparedScene(_freeze(data), grid)

        return self._resolve("records", (sid, scene_id, "live", encoded), build), bool(row[1])

    def mechanical_grid(self, sid, scene_id):
        """Validate the complete sensory schema once per visible map dependency.

        GridScene's movement defaults never supply missing sensory fields.
        Returned models are detached by the existing instance field boundary.
        """
        from dnd5e_engine.evaluation_state import MechanicalGrid

        def build():
            stored = self.scene(sid, scene_id)
            if stored is None or stored.data["scene_id"] != scene_id or stored.grid is None:
                return None
            return MechanicalGrid.model_validate(stored.grid.copy_data())

        return resolve_field(
            self.store,
            "scene",
            sid,
            scene_id,
            "spatial",
            "sensory",
            MechanicalGrid,
            build,
        )

    def template(self, sid, scene_id, encoded):
        return self._resolve(
            "grids",
            (sid, scene_id, "template", encoded),
            lambda: PreparedGrid.build(json.loads(encoded)),
        )
