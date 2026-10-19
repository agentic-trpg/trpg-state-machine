"""SQLite-owned provider fixtures; production adapters declare these same owner seams."""

import json
from contextlib import contextmanager
from dataclasses import asdict, replace

from trpg_state_machine.environment_commands import source_from_data
from trpg_state_machine.environment_versions import (
    EnvironmentInputWatch,
    VersionedEnvironmentEffectsProvider,
    VersionedProtectionProvider,
)
from trpg_state_machine.object_protection import (
    CurrentProtectionProperties,
    PrecipitationProtection,
    RainProtection,
    WindBoundary,
    WindProtection,
)


@contextmanager
def write(sm):
    scope = sm._store._scope
    if scope is not None:
        sm._store.require_write()
        yield
    else:
        with sm._transaction(write=True):
            yield


class Owner:
    def __init__(self, sm, sid, column):
        self.sm, self.sid, self.column = sm, sid, column
        self.reads = 0

    def raw(self, sid=None):
        row = self.sm._conn.execute(
            f"SELECT {self.column} FROM fixture_environment_inputs "
            "WHERE session_id=? AND name='owner'",
            (sid or self.sid,),
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def put(self, data):
        with write(self.sm):
            self.sm._conn.execute(
                f"UPDATE fixture_environment_inputs SET {self.column}=? "
                "WHERE session_id=? AND name='owner'",
                (json.dumps(data), self.sid),
            )


class ProtectionOwner(Owner):
    @property
    def current(self):
        data = self.raw()
        if data is None:
            return None
        rain, wind, precipitation = data["rain"], data["wind"], data["precipitation"]
        return CurrentProtectionProperties(
            RainProtection(tuple(map(tuple, rain["local_cells"]))) if rain is not None else None,
            WindProtection(
                tuple(
                    WindBoundary(**{**b, "local_cell": tuple(b["local_cell"])})
                    for b in wind["boundaries"]
                )
            )
            if wind is not None
            else None,
            data["base_height_ft"],
            PrecipitationProtection(
                tuple(map(tuple, precipitation["local_cells"])), tuple(precipitation["kinds"])
            )
            if precipitation is not None
            else None,
        )

    @current.setter
    def current(self, value):
        self.put(asdict(value))

    def current_protection(self, sid, oid, package, *, candidate):
        self.reads += 1
        return self.current


class EffectsOwner(Owner):
    @property
    def current(self):
        data = self.raw()
        return tuple(source_from_data(v) for v in data) if data is not None else None

    @current.setter
    def current(self, value):
        self.put([asdict(s) for s in value] if value is not None else None)

    def environment_sources(self, sid, scene_id, package):
        self.reads += 1
        data = self.raw(sid)
        return tuple(source_from_data(v) for v in data) if data is not None else None


def providers(sm, sid, package, context, initial, oid):
    with sm._transaction(write=True):
        sm._conn.execute(
            "CREATE TABLE IF NOT EXISTS fixture_environment_inputs ("
            "session_id TEXT NOT NULL,name TEXT NOT NULL,effects_json TEXT,"
            "protection_json TEXT,PRIMARY KEY(session_id,name))"
        )
        sm._conn.execute(
            "INSERT INTO fixture_environment_inputs VALUES (?,'owner','[]',?)",
            (sid, json.dumps(asdict(initial))),
        )
    wo = ProtectionOwner(sm, sid, "protection_json")
    effects = EffectsOwner(sm, sid, "effects_json")
    p = VersionedProtectionProvider(
        "fixture.wo/1",
        wo,
        (oid,),
        (
            EnvironmentInputWatch(
                "fixture_environment_inputs", "name", "owner", ("protection_json",), "protection"
            ),
        ),
    )
    e = VersionedEnvironmentEffectsProvider(
        "fixture.effects/1",
        effects,
        (
            EnvironmentInputWatch(
                "fixture_environment_inputs", "name", "owner", ("effects_json",), "effects_json"
            ),
        ),
    )
    sm._objects.spatial.protection_provider = p
    sm.environment.effects_provider = e
    sm.environment.bind_providers(
        sid, package, context=replace(context, can_manage_environment=True)
    )
    return wo, effects
