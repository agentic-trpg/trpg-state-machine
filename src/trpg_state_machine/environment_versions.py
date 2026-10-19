"""Authoritative input revisions and transactional provider dependency registration.

Subscriptions describe owner inputs, never copies of their values. SQLite triggers
advance the two revisions even for internal SQL writers, inside their savepoints.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

from .errors import StateMachineError
from .package import canonical_json


def source_key(raw):
    data = json.loads(raw) if raw else {}
    return canonical_json((data.get("base"), data.get("sources", [])))


def protection_key(raw):
    return canonical_json((json.loads(raw) if raw else {}).get("protection"))


def wind_key(raw):
    data = (json.loads(raw) if raw else {}) or {}
    sources = data if isinstance(data, list) else data.get("sources", [])
    # Full Wind values matter: speed-only changes can introduce/remove a tie
    # conflict with a source owned by ANOTHER provider and change the selected
    # incoming direction to/from unsupported. Conservatively invalidate on any
    # Wind value change, without reading other owners during the trigger.
    base = None if isinstance(data, list) else (data.get("base") or {}).get("wind")
    return canonical_json(
        (
            base,
            [
                (
                    s["source_id"],
                    s["conditions"]["wind"],
                    s.get("area"),
                    s.get("priority", 0),
                    s.get("observable", False),
                )
                for s in sorted(sources, key=lambda s: s["source_id"])
                if s["conditions"].get("wind") is not None
            ],
        )
    )


def _identifier(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value):
        raise ValueError("Expected a SQL identifier")
    return '"' + value + '"'


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@dataclass(frozen=True, slots=True)
class EnvironmentInputWatch:
    table: str
    key_column: str
    key: str
    columns: tuple[str, ...]
    kind: str  # source / wind_source / effects_json / protection
    local_object: bool = False
    scene_ids: tuple[str, ...] = ()

    def __post_init__(self):
        for name in (self.table, self.key_column, *self.columns):
            _identifier(name)
        if (
            type(self.columns) is not tuple
            or not self.columns
            or self.kind not in ("source", "wind_source", "effects_json", "protection")
            or self.kind == "effects_json"
            and len(self.columns) != 1
        ):
            raise ValueError("Expected explicit immutable owner input columns and impact")
        if type(self.scene_ids) is not tuple or any(type(s) is not str for s in self.scene_ids):
            raise ValueError("Expected immutable Scene scope")
        if type(self.key) is not str or not self.key or type(self.local_object) is not bool:
            raise ValueError("Expected explicit row identity and local-object flag")


@dataclass(frozen=True, slots=True)
class EnvironmentTimeWatch:
    boundaries: tuple[int, ...] = ()
    continuous: bool = False
    wind: bool = False
    scene_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if (
            type(self.boundaries) is not tuple
            or len(self.boundaries) > 256
            or any(type(t) is not int or t < 0 for t in self.boundaries)
            or type(self.continuous) is not bool
            or type(self.wind) is not bool
            or type(self.scene_ids) is not tuple
            or any(type(s) is not str for s in self.scene_ids)
        ):
            raise ValueError("Expected bounded explicit tick dependencies")


@dataclass(frozen=True, slots=True)
class VersionedEnvironmentEffectsProvider:
    """Reader uses ONLY the declared SQLite inputs and explicit tick dependencies.

    key identifies the owner's stable reading policy; change it when that policy
    changes. Unversioned external memory / I/O is not permitted in this adapter.
    Bind in a trusted write scope before use; registrations survive reopening.
    """

    key: str
    reader: object
    watches: tuple[EnvironmentInputWatch, ...] = ()
    time: EnvironmentTimeWatch = EnvironmentTimeWatch()
    _binding_descriptor: str | None = field(default=None, init=False, repr=False, compare=False)

    def environment_sources(self, sid, scene_id, package):
        return self.reader.environment_sources(sid, scene_id, package)


@dataclass(frozen=True, slots=True)
class VersionedProtectionProvider:
    """Only listed objects contribute; other objects explicitly contribute nothing.

    Pose, placement and component changes are automatically watched. Additional
    owner input tables (including actor/container dependencies) must be declared.
    """

    key: str
    reader: object
    object_ids: tuple[str, ...]
    watches: tuple[EnvironmentInputWatch, ...] = ()
    time: EnvironmentTimeWatch = EnvironmentTimeWatch()
    _binding_descriptor: str | None = field(default=None, init=False, repr=False, compare=False)

    def current_protection(self, sid, oid, package, *, candidate):
        from .object_protection import CurrentProtectionProperties, RainProtection, WindProtection

        if oid not in self.object_ids:
            return CurrentProtectionProperties(RainProtection(()), WindProtection(()))
        return self.reader.current_protection(sid, oid, package, candidate=candidate)


class EnvironmentVersions:
    def __init__(self, store):
        self.store = store

    @property
    def db(self):
        return self.store.connection

    def current(self, sid, scene):
        row = self.db.execute(
            "SELECT source_version,protection_version FROM scene_environment_versions "
            "WHERE session_id=? AND scene_id=?",
            (sid, scene),
        ).fetchone()
        return tuple(row) if row else (0, 0)

    def advance(self, sid, scene, *, source=False, protection=False):
        self.store.require_write()
        self.db.execute(
            "INSERT INTO scene_environment_versions VALUES (?,?,?,?) "
            "ON CONFLICT(session_id,scene_id) DO UPDATE SET "
            "source_version=source_version+excluded.source_version,"
            "protection_version=protection_version+excluded.protection_version",
            (sid, scene, int(source), int(protection)),
        )

    @staticmethod
    def descriptor(provider):
        if provider is None:
            return "none"
        if type(provider) not in (VersionedEnvironmentEffectsProvider, VersionedProtectionProvider):
            return None
        if provider._binding_descriptor is not None:
            return provider._binding_descriptor
        if (
            type(provider.key) is not str
            or not provider.key
            or len(provider.key) > 128
            or type(provider.watches) is not tuple
            or any(type(w) is not EnvironmentInputWatch for w in provider.watches)
            or type(provider.time) is not EnvironmentTimeWatch
        ):
            raise ValueError("Expected complete immutable provider dependency declaration")
        if type(provider) is VersionedProtectionProvider and (
            type(provider.object_ids) is not tuple
            or any(type(oid) is not str for oid in provider.object_ids)
        ):
            raise ValueError("Expected explicit protection object identities")
        data = (
            provider.key,
            [asdict(w) for w in provider.watches],
            asdict(provider.time),
            getattr(provider, "object_ids", ()),
        )
        descriptor = hashlib.sha256(canonical_json(data).encode()).hexdigest()
        object.__setattr__(provider, "_binding_descriptor", descriptor)
        return descriptor

    def cacheable(self, sid, scene, effects, protection):
        descriptors = tuple(self.descriptor(p) for p in (effects, protection))
        if None in descriptors or any(
            getattr(getattr(p, "reader", p), "environment_cacheable", True) is not True
            for p in (effects, protection)
        ):
            return False, descriptors
        rows = dict(
            self.db.execute(
                "SELECT role,descriptor FROM environment_provider_bindings "
                "WHERE session_id=? AND scene_id=?",
                (sid, scene),
            )
        )
        return all(
            rows.get(role, "none") == d
            for role, d in zip(("effects", "protection"), descriptors, strict=True)
        ), descriptors

    def bind(self, sid, scene, package, provider, role):
        with self.store.savepoint():
            self._bind(sid, scene, package, provider, role)

    def _bind(self, sid, scene, package, provider, role):
        self.store.require_write()
        expected = {
            "effects": VersionedEnvironmentEffectsProvider,
            "protection": VersionedProtectionProvider,
        }.get(role)
        if expected is None or provider is not None and type(provider) is not expected:
            raise ValueError("Provider type does not match its environment role")
        if provider is not None:
            if any((role == "protection") != (w.kind == "protection") for w in provider.watches):
                raise ValueError("Dependency impact does not match its provider role")
            if role == "protection":
                known = {o.object_id for s in package.scenes for o in s.objects}
                if not set(provider.object_ids) <= known:
                    raise ValueError("Protection dependency is outside the pinned Package")
        descriptor = self.descriptor(provider)
        if descriptor is None:
            raise StateMachineError(
                "unversioned_environment_provider", "Declare owner dependencies"
            )
        old = self.db.execute(
            "SELECT descriptor FROM environment_provider_bindings "
            "WHERE session_id=? AND scene_id=? AND role=?",
            (sid, scene, role),
        ).fetchone()
        if old is None and provider is None:
            return  # The default absence of a provider is already the current policy.
        if old and old[0] == descriptor:
            return
        prefix = (
            "env_provider_"
            + hashlib.sha256(canonical_json((sid, scene, role)).encode()).hexdigest()[:24]
            + "_"
        )
        for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall():
            if row[0].startswith(prefix):
                self.db.execute("DROP TRIGGER " + _identifier(row[0]))
        self.db.execute(
            "INSERT INTO environment_provider_bindings(session_id,scene_id,role,descriptor) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(session_id,scene_id,role) DO UPDATE SET "
            "descriptor=excluded.descriptor,manifest_json='{}'",
            (sid, scene, role, descriptor),
        )
        self.advance(sid, scene, source=role == "effects", protection=True)
        if provider is None:
            return
        watches = list(provider.watches)
        if role == "protection":
            for oid in provider.object_ids:
                watches.append(
                    EnvironmentInputWatch(
                        "objects",
                        "object_id",
                        oid,
                        ("scene_id", "cell", "orientation", "state", "carrier_id", "container_id"),
                        "protection",
                        True,
                    )
                )
                for table, columns in (
                    ("object_durability", ("current_hp",)),
                    ("object_openables", ("barred", "jammed")),
                    ("object_lights", ("enabled", "mode")),
                    ("object_mechanisms", ("status",)),
                ):
                    watches.append(
                        EnvironmentInputWatch(table, "object_id", oid, columns, "protection", True)
                    )
        for index, watch in enumerate(watches):
            self._watch(sid, scene, prefix + str(index), watch)
        self._time(sid, scene, prefix + "time", provider.time, role)
        manifest = {
            row[0]: row[1]
            for row in self.db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
            if row[0].startswith(prefix)
        }
        self.db.execute(
            "UPDATE environment_provider_bindings SET manifest_json=? "
            "WHERE session_id=? AND scene_id=? AND role=?",
            (canonical_json(manifest), sid, scene, role),
        )

    def _bump_sql(self, sid, scene, source, protection):
        return (
            "INSERT INTO scene_environment_versions VALUES ("
            + _literal(sid)
            + ","
            + _literal(scene)
            + ","
            + source
            + ","
            + protection
            + ") "
            "ON CONFLICT(session_id,scene_id) DO UPDATE SET "
            "source_version=source_version+excluded.source_version,"
            "protection_version=protection_version+excluded.protection_version;"
        )

    def _watch(self, sid, scene, name, watch):
        if watch.scene_ids and scene not in watch.scene_ids:
            return
        if watch.table in ("scene_environment_versions", "environment_provider_bindings"):
            raise ValueError("Version metadata is not a provider input")
        columns = {
            r[1] for r in self.db.execute("PRAGMA table_info(" + _identifier(watch.table) + ")")
        }
        if not {"session_id", watch.key_column, *watch.columns} <= columns:
            raise ValueError("Provider input table/columns are unavailable")
        if watch.local_object and "object_id" not in columns:
            raise ValueError("Local object inputs require object_id")
        for event in ("INSERT", "UPDATE", "DELETE"):
            refs = (
                ("OLD", "NEW") if event == "UPDATE" else ("OLD",) if event == "DELETE" else ("NEW",)
            )
            matches = []
            for ref in refs:
                match = (
                    f"{ref}.session_id={_literal(sid)} AND "
                    f"{ref}.{_identifier(watch.key_column)}={_literal(watch.key)}"
                )
                if watch.local_object:
                    match += (
                        f" AND {ref}.scene_id={_literal(scene)}"
                        if watch.table == "objects"
                        else " AND EXISTS (SELECT 1 FROM objects "
                        f"WHERE session_id={ref}.session_id "
                        f"AND object_id={ref}.object_id AND scene_id={_literal(scene)})"
                    )
                matches.append("(" + match + ")")
            when = " OR ".join(matches)
            if event == "UPDATE":
                changed = " OR ".join(
                    f"OLD.{_identifier(c)} IS NOT NEW.{_identifier(c)}"
                    for c in (*watch.columns, watch.key_column, "session_id")
                )
                when = "(" + when + ") AND (" + changed + ")"
            source = "0" if watch.kind == "protection" else "1"
            protection = "1" if watch.kind in ("protection", "wind_source") else "0"
            if watch.kind == "effects_json":
                column = _identifier(watch.columns[0])
                before = (
                    "NULL"
                    if event == "INSERT"
                    else ("CASE WHEN " + matches[0] + " THEN OLD." + column + " ELSE NULL END")
                )
                after = (
                    "NULL"
                    if event == "DELETE"
                    else ("CASE WHEN " + matches[-1] + " THEN NEW." + column + " ELSE NULL END")
                )
                protection = f"(env_wind_key({before}) IS NOT env_wind_key({after}))"
            self.db.execute(
                f"CREATE TRIGGER {_identifier(name + event)} AFTER {event} ON "
                f"{_identifier(watch.table)} WHEN {when} BEGIN "
                + self._bump_sql(sid, scene, source, protection)
                + " END"
            )

    def _time(self, sid, scene, name, watch, role):
        if watch.scene_ids and scene not in watch.scene_ids:
            return
        if not watch.continuous and not watch.boundaries:
            return
        crossed = (
            "1"
            if watch.continuous
            else " OR ".join(
                f"(min(OLD.game_tick,NEW.game_tick)<{t} AND {t}<=max(OLD.game_tick,NEW.game_tick))"
                for t in watch.boundaries
            )
        )
        self.db.execute(
            f"CREATE TRIGGER {_identifier(name)} AFTER UPDATE OF game_tick ON world_states "
            f"WHEN NEW.session_id={_literal(sid)} AND OLD.game_tick!=NEW.game_tick "
            f"AND ({crossed}) BEGIN "
            + self._bump_sql(
                sid,
                scene,
                "1" if role == "effects" else "0",
                "1" if role == "protection" or watch.wind else "0",
            )
            + " END"
        )
