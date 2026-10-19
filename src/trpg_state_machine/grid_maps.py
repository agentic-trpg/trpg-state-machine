"""Bounded Grid runtime representations, rebuilt from transaction-visible content.

SQLite remains authoritative. There is deliberately no cached "current map"
pointer: every live lookup first reads its exact encoded record in the caller's
transaction. Candidate/rolled-back/older-reader records cannot alias newer data.
"""

import json
from collections import OrderedDict
from dataclasses import dataclass
from types import MappingProxyType

from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene

from .package import canonical_json


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


class ReadOnlyTopology(GridTopology):
    """Pinned RE algorithms with detached, recursively read-only backing inputs.

    RE's only nested model input here is WallSegment (four coordinates). Its
    geometry algorithms only read these attributes; no RE rule is reimplemented.
    """

    def __init__(self, scene):
        super().__init__(scene)
        self._walls = tuple(_Wall(w.x1, w.y1, w.x2, w.y2) for w in self._walls)
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


@dataclass(frozen=True, slots=True)
class PreparedScene:
    data: object
    grid: PreparedGrid | None

    def copy_data(self):
        return _thaw(self.data)


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
        encoded = canonical_json(raw)
        return self._resolve(
            "grids", (sid, scene_id, source, encoded), lambda: PreparedGrid.build(raw)
        )

    def scene(self, sid, scene_id, *, spatial_only=False):
        row = self.store.connection.execute(
            "SELECT state_json,spatial_enabled FROM mechanical_scenes "
            "WHERE session_id=? AND scene_id=?",
            (sid, scene_id),
        ).fetchone()
        if row is None or spatial_only and not row[1]:
            return None
        encoded = row[0]

        def build():
            data = json.loads(encoded)
            grid = (
                self.for_grid(sid, scene_id, data["grid"]) if data.get("grid") is not None else None
            )
            return PreparedScene(_freeze(data), grid)

        return self._resolve("records", (sid, scene_id, "live", encoded), build)

    def template(self, sid, scene_id, encoded):
        return self._resolve(
            "grids",
            (sid, scene_id, "template", encoded),
            lambda: PreparedGrid.build(json.loads(encoded)),
        )
