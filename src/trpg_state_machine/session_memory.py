"""Bounded, transaction-versioned runtime reads over the domain-owned records.

The persistent connection remains private to SqliteStore.  Domain SQL is an
internal compatibility interface: loaded equality ranges are served by indexed
RAM relations, not executed again.  Other queries have dependency-tracked,
bounded results.  TEMP change triggers include cascades and derived-table writes.
Candidates contain sparse row changes; old rows and returned inputs are never
mutated. Publication updates private indices under the gate after durable COMMIT.
"""

import json
import re
import sqlite3
from collections import OrderedDict
from copy import deepcopy
from itertools import combinations

from .errors import StateMachineError


class Rows:
    """Detached cursor interface. SQL cursors never outlive their reading scope."""

    def __init__(self, rows=(), *, rowcount=-1, lastrowid=None, description=None):
        self.rows = iter(rows)
        self.rowcount, self.lastrowid, self.description = rowcount, lastrowid, description
        self.arraysize = 1

    def __iter__(self):
        return self.rows

    def fetchone(self):
        return next(self.rows, None)

    def fetchall(self):
        return list(self.rows)

    def fetchmany(self, size=None):
        result = []
        for _ in range(self.arraysize if size is None else size):
            row = self.fetchone()
            if row is None:
                break
            result.append(row)
        return result

    def close(self):
        self.rows = iter(())


class Record:
    """sqlite.Row-compatible immutable scalar record, including positional reads."""

    __slots__ = ("_names", "_values")

    def __init__(self, names, values):
        object.__setattr__(self, "_names", tuple(names))
        object.__setattr__(self, "_values", tuple(values))

    def __setattr__(self, name, value):
        raise AttributeError("Runtime records are immutable")

    def __delattr__(self, name):
        raise AttributeError("Runtime records are immutable")

    def __getitem__(self, key):
        if isinstance(key, str):
            key = self._names.index(key)
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._names

    def __deepcopy__(self, memo):
        return self


class Range:
    """Private mutable index; only detached Record values leave the runtime.

    Candidates are sparse change lists. Publication updates dictionary entries
    and index buckets, without copying/scanning a loaded Scene/Session relation.
    """

    def __init__(self, table, predicates, rows, primary, stage=0):
        self.table, self.predicates, self.primary, self.stage = table, predicates, primary, stage
        self.rows, self.indices = {}, {}
        self.size = 0
        for row in rows:
            self.apply(None, row)

    def matches(self, row):
        return all(row[name] == value for name, value in self.predicates)

    def identity(self, row):
        return tuple(row[name] for name in self.primary)

    def apply(self, old, new):
        if old is not None:
            identity = self.identity(old)
            before = self.rows.pop(identity, None)
            if before is not None:
                self.size -= len(repr(tuple(before)).encode("utf-8")) + 128
                for name, buckets in self.indices.items():
                    bucket = buckets[before[name]]
                    bucket.discard(identity)
                    if not bucket:
                        del buckets[before[name]]
        if new is not None and self.matches(new):
            identity = self.identity(new)
            self.rows[identity] = new
            self.size += len(repr(tuple(new)).encode("utf-8")) + 128
            for name in new.keys():  # noqa: SIM118
                if name.endswith("_id") or name in ("domain", "kind", "cell"):
                    self.indices.setdefault(name, {}).setdefault(new[name], set()).add(identity)

    def read(self, predicates, changes, stats):
        buckets = [
            self.indices[name].get(value, set())
            for name, value in predicates
            if name in self.indices
        ]
        identities = min(buckets, key=len) if buckets else self.rows
        result = {}
        for identity in identities:
            row = self.rows[identity]
            stats["ram_rows"] += 1
            if all(row[name] == value for name, value in predicates):
                result[identity] = row
        for table, old, new in changes[self.stage :]:
            if table != self.table:
                continue
            if old is not None:
                result.pop(self.identity(old), None)
            if new is not None and all(new[name] == value for name, value in predicates):
                result[self.identity(new)] = new
        return tuple(result.values())


def _simple(sql, parameters, columns):
    """Recognize a deliberately closed subset; never guess SQL semantics.

    NULL equality, expressions, collations, joins and aggregates stay with SQLite.
    Equality ranges include all columns, allowing different projections to share
    the same facts without reloading or parsing them.
    """
    match = re.fullmatch(
        r"SELECT\s+([\w *,]+)\s+FROM\s+(\w+)\s+WHERE\s+(.+?)"
        r"(?:\s+ORDER\s+BY\s+([\w ,]+))?\s*;?",
        sql.strip(),
        re.I,
    )
    if match is None or not isinstance(parameters, (tuple, list)):
        return None
    projection, table, where, order = match.groups()
    if table not in columns:
        return None
    selected = (
        tuple(columns[table])
        if projection.strip() == "*"
        else tuple(v.strip() for v in projection.split(","))
    )
    ordering = tuple(v.strip() for v in order.split(",")) if order else ()
    if any(v not in columns[table] for v in (*selected, *ordering)):
        return None
    predicates, position = [], 0
    for term in re.split(r"\s+AND\s+", where, flags=re.I):
        m = re.fullmatch(r"(\w+)\s*(=\s*(\?|'.*?'|\d+)|IS\s+NULL)", term.strip(), re.I)
        if not m or m[1] not in columns[table]:
            return None
        value = m[3]
        if value == "?":
            if position >= len(parameters) or parameters[position] is None:
                return None
            value = parameters[position]
            position += 1
        elif value is not None:
            value = value[1:-1] if value.startswith("'") else int(value)
        predicates.append((m[1], value))
    if position != len(parameters):
        return None
    return table, tuple(sorted(predicates)), selected, ordering


class SessionMemory:
    def __init__(self, store, *, capacity=2048, byte_limit=32 * 1024 * 1024):
        self.store = store
        self.capacity, self.byte_limit = capacity, byte_limit
        self.columns, self.primary = {}, {}
        self.values = OrderedDict()
        self.range_keys = {}
        self.range_shapes = {}
        self.dependency_shapes = {}
        self.projection_shapes = {}
        self.sizes, self.bytes = {}, 0
        self.versions = {}
        self.collectors = []
        self.paused = False
        self.paused_sessions = store._coordinator.paused_sessions
        self.generation = 0
        self.recovery_generations = {}
        self.recovery_generation = store._coordinator.recovery_generation
        self.stats = {
            "hits": 0,
            "cold_queries": 0,
            "cold_rows": 0,
            "publications": 0,
            "ram_rows": 0,
            "published_rows": 0,
            "parsed_hits": 0,
            "parsed_builds": 0,
            "snapshot_builds": 0,
            "commit_ns": 0,
            "publication_ns": 0,
            "write_scope_ns": 0,
            "json_parses": 0,
            "model_parses": 0,
            "instance_hits": 0,
            "instance_builds": 0,
        }
        self.install()

    def install(self):
        conn = self.store._conn
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        names = {row[0] for row in tables}
        self.columns = {k: v for k, v in self.columns.items() if k in names}
        self.primary = {k: v for k, v in self.primary.items() if k in names}
        conn.create_function("session_memory_change", -1, self.changed)
        for (table,) in tables:
            info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            names = tuple(row[1] for row in info)
            self.columns[table] = names
            self.primary[table] = tuple(
                row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]
            )
            # TEMP triggers have connection lifetime, never alter the saved schema,
            # and also observe writes performed by persistent integrity triggers.
            for kind in ("INSERT", "UPDATE", "DELETE"):
                args = [f"'{table}'", f"'{kind}'"]
                for prefix in ("OLD", "NEW"):
                    args.extend(
                        "NULL"
                        if (kind == "INSERT" and prefix == "OLD")
                        or (kind == "DELETE" and prefix == "NEW")
                        else f'{prefix}."{name}"'
                        for name in names
                    )
                conn.execute(
                    f'CREATE TEMP TRIGGER IF NOT EXISTS "ram_{table}_{kind}" '
                    f'AFTER {kind} ON main."{table}" BEGIN '
                    f"SELECT session_memory_change({','.join(args)}); END"
                )

    def clear(self):
        self.values.clear()
        self.range_keys.clear()
        self.range_shapes.clear()
        self.sizes.clear()
        self.bytes = 0
        self.versions.clear()
        self.dependency_shapes.clear()
        self.projection_shapes.clear()
        self.generation += 1

    def guard(self, sid=None):
        if self.paused or sid in self.paused_sessions:
            raise StateMachineError(
                "session_reconciliation_required",
                "Persistent commit needs reconciliation; current RAM is unavailable",
            )

    def release(self, sid):
        for key, item in tuple(self.values.items()):
            if isinstance(item, Range):
                owns = dict(item.predicates).get("session_id") in (sid, None)
            else:
                dependencies = item[1] if key[0] == "parsed" else item[2]
                owns = (key[0] == "instance" and key[1] == sid) or any(
                    session in (sid, None) for _, session in dependencies
                )
                if key[0] == "parsed" and not dependencies:
                    owns = _references(key[3], sid)
            if owns:
                self.forget(key)
        self.versions = {dep: version for dep, version in self.versions.items() if dep[1] != sid}

    def pause(self, scope):
        if scope.memory_sessions:
            self.paused_sessions.update(scope.memory_sessions)
            coordinator = self.store._coordinator
            for sid in scope.memory_sessions:
                coordinator.recovery_generations[sid] = (
                    coordinator.recovery_generations.get(sid, 0) + 1
                )
        else:
            self.paused = True
            self.store._coordinator.recovery_generation += 1

    def sync_recovery(self):
        """Invalidate on this connection's own thread, including partial publications.

        data_version alone is insufficient: a failed publisher may already have
        observed the committed database version before another context recovers.
        """
        coordinator = self.store._coordinator
        if self.recovery_generation != coordinator.recovery_generation:
            self.clear()
            if self.store._grid_maps is not None:
                self.store._grid_maps.clear()
            for listener in self.store._recovery_listeners:
                listener(None)
            self.recovery_generation = coordinator.recovery_generation
        for sid, generation in coordinator.recovery_generations.items():
            if self.recovery_generations.get(sid, 0) != generation:
                self.release(sid)
                if self.store._grid_maps is not None:
                    self.store._grid_maps.release(sid)
                for listener in self.store._recovery_listeners:
                    listener(sid)
                self.recovery_generations[sid] = generation

    def dependency(self, table, sid):
        self.guard(sid)
        if sid is not None and self.store._scope is not None:
            self.store._scope.memory_sessions.add(sid)
        dependency = (table, sid)
        for collector in self.collectors:
            collector.add(dependency)
        return dependency

    def range_dependency(self, table, predicates):
        sid = dict(predicates).get("session_id")
        shape = tuple(name for name, _ in predicates)
        self.dependency_shapes.setdefault(table, set()).add(shape)
        tag = f"{table}#{predicates!r}"
        dependency = self.dependency(tag, sid)
        scope = self.store._scope
        scope.memory_versions.setdefault(dependency, 0)
        return dependency

    def projection_dependency(self, table, predicates, columns):
        """A relation projection changes only with membership or selected columns."""
        shape = tuple(name for name, _ in predicates)
        columns = tuple(sorted(columns))
        self.projection_shapes.setdefault(table, set()).add((shape, columns))
        tag = f"{table}@{(predicates, columns)!r}"
        dependency = self.dependency(tag, dict(predicates).get("session_id"))
        self.store._scope.memory_versions.setdefault(dependency, 0)
        return dependency

    def scene_association(self, table, sid, scene):
        dependency = self.dependency(f"{table}~scene:{scene!r}", sid)
        self.store._scope.memory_versions.setdefault(dependency, 0)
        return dependency

    def _scene_for(self, row):
        """Resolve only a changed row's position chain; no JSON or entity scan."""
        if row is None:
            return None
        sid, seen = row["session_id"], set()
        while row is not None and row["object_id"] not in seen:
            seen.add(row["object_id"])
            if row["scene_id"] is not None:
                return row["scene_id"]
            if row["carrier_id"] is not None:
                actor = self.select(
                    "SELECT current_scene_id FROM actors WHERE session_id=? AND actor_id=?",
                    (sid, row["carrier_id"]),
                ).fetchone()
                return actor[0] if actor else None
            row = (
                self.select(
                    "SELECT * FROM objects WHERE session_id=? AND object_id=?",
                    (sid, row["container_id"]),
                ).fetchone()
                if row["container_id"]
                else None
            )
        return None

    def _association_changes(self, table, old, new):
        impacted = set()
        if table == "objects":
            columns = ("scene_id", "container_id", "carrier_id", "object_id")
            if old is not None and new is not None and all(old[c] == new[c] for c in columns):
                return
            for row in (old, new):
                scene = self._scene_for(row)
                if scene is not None:
                    impacted.update(
                        (t, row["session_id"], scene)
                        for t in ("object_lights", "object_mechanisms")
                    )
        elif table == "actors":
            if (
                old is not None
                and new is not None
                and old["current_scene_id"] == new["current_scene_id"]
            ):
                return
            for row in (old, new):
                if row is not None:
                    impacted.update(
                        (t, row["session_id"], row["current_scene_id"])
                        for t in ("object_lights", "object_mechanisms")
                    )
        elif table in ("object_lights", "object_mechanisms"):
            if old is not None and new is not None and old["object_id"] == new["object_id"]:
                return
            for row in (old, new):
                if row is not None:
                    obj = self.select(
                        "SELECT * FROM objects WHERE session_id=? AND object_id=?",
                        (row["session_id"], row["object_id"]),
                    ).fetchone()
                    scene = self._scene_for(obj)
                    if scene is not None:
                        impacted.add((table, row["session_id"], scene))
        scope = self.store._scope
        for capability, sid, scene in impacted:
            dependency = (f"{capability}~scene:{scene!r}", sid)
            if dependency in scope.memory_versions:
                scope.memory_versions[dependency] += 1

    def version(self, dependency):
        scope = self.store._scope
        versions = scope.memory_versions if scope is not None else self.versions
        table, sid = dependency
        return (self.generation, versions.get((table, sid), 0))

    def get(self, key):
        scope = self.store._scope
        if key in scope.memory_pending:
            return scope.memory_pending[key]
        if key in self.values:
            self.values.move_to_end(key)
            return self.values[key]
        return None

    def put(self, key, value):
        self.store._scope.memory_pending[key] = value

    def json(self, encoded):
        """Content-keyed parsed fact input; callers receive an isolated value."""
        if self.store._scope is None:
            with self.store.transaction():
                return self.json(encoded)
        key = ("json", encoded)
        cached = self.get(key)
        if cached is None:
            value = json.loads(encoded)
            self.stats["json_parses"] += 1
            dependencies = tuple(
                ("__scope__", sid) for sid in sorted(self.store._scope.memory_sessions)
            )
            cached = (value, None, dependencies, ())
            self.put(key, cached)
        return deepcopy(cached[0])

    def model_json(self, schema, encoded):
        """Reuse pure schema decoding; domain/OCC/binding checks still run.

        The encoded content and schema identity fully determine this value. The
        returned model is detached, including nested fields; callers can prepare
        new candidates without changing any prior input.
        """
        if self.store._scope is None:
            with self.store.transaction():
                return self.model_json(schema, encoded)
        key = ("model-json", schema, encoded)
        cached = self.get(key)
        if cached is None:
            validate = getattr(schema, "model_validate_json", None) or schema.validate_json
            value = validate(encoded)
            self.stats["model_parses"] += 1
            dependencies = tuple(
                ("__scope__", sid) for sid in sorted(self.store._scope.memory_sessions)
            )
            cached = (value, None, dependencies, ())
            self.put(key, cached)
        return deepcopy(cached[0])

    def remember(self, key, value):
        # Bound both entry count and scalar payload; no world/history preload.
        size = value.size if isinstance(value, Range) else len(repr(value).encode("utf-8"))
        size += len(repr(key).encode("utf-8"))
        if key in self.values:
            self.forget(key)
        if size <= self.byte_limit:
            self.values[key], self.sizes[key] = value, size
            if isinstance(value, Range):
                self.range_keys.setdefault(value.table, set()).add(key)
                shape = tuple(name for name, _ in key[2])
                shapes = self.range_shapes.setdefault(value.table, {})
                shapes[shape] = shapes.get(shape, 0) + 1
            self.bytes += size
        while len(self.values) > self.capacity or self.bytes > self.byte_limit:
            self.forget(next(iter(self.values)))

    def forget(self, key):
        self.values.pop(key)
        self.bytes -= self.sizes.pop(key)
        if key[0] == "range":
            self.range_keys[key[1]].discard(key)
            shape = tuple(name for name, _ in key[2])
            count = self.range_shapes[key[1]][shape] - 1
            if count:
                self.range_shapes[key[1]][shape] = count
            else:
                del self.range_shapes[key[1]][shape]

    def trim_versions(self):
        if len(self.versions) <= self.capacity * 32:
            return
        alive = set()
        for key, value in self.values.items():
            if isinstance(value, Range):
                tag = f"{value.table}#{value.predicates!r}"
                alive.add((tag, dict(value.predicates).get("session_id")))
            else:
                alive.update(value[1] if key[0] == "parsed" else value[2])
        self.versions = {d: self.versions.get(d, 0) for d in alive}
        if len(self.versions) > self.capacity * 32:
            self.clear()

    def invalidate_table(self, table):
        """Controlled REPLACE compatibility: SQLite may omit delete callbacks."""
        scope = self.store._scope
        for key in tuple(self.range_keys.get(table, ())):
            self.forget(key)
        scope.memory_pending = {
            key: value
            for key, value in scope.memory_pending.items()
            if not (key[0] == "range" and key[1] == table)
        }
        for dependency in scope.memory_versions:
            name = dependency[0]
            if (
                name == table
                or name.startswith((table + "#", table + ":", table + "@"))
                or table in ("objects", "actors")
                and name.startswith(("object_lights~scene:", "object_mechanisms~scene:"))
            ):
                scope.memory_versions[dependency] += 1

    def changed(self, table, kind, *values):
        scope = self.store._scope
        if scope is None:
            # Raw private connection maintenance cannot leave apparently valid RAM.
            self.clear()
            return 0
        count = len(self.columns[table])
        old = None if kind == "INSERT" else Record(self.columns[table], values[:count])
        new = None if kind == "DELETE" else Record(self.columns[table], values[count:])
        if kind == "UPDATE" and values[:count] == values[count:]:
            return 0
        self._association_changes(table, old, new)
        sessions = {
            row["session_id"] if "session_id" in self.columns[table] else None
            for row in (old, new)
            if row is not None
        }
        for sid in sessions:
            if sid is not None:
                scope.memory_sessions.add(sid)
            dep = (table, sid)
            scope.memory_versions[dep] = scope.memory_versions.get(dep, 0) + 1
        if None not in sessions:
            dep = (table, None)
            scope.memory_versions[dep] = scope.memory_versions.get(dep, 0) + 1
        primary = self.primary[table]
        if old is None or new is None or any(old[k] != new[k] for k in primary):
            for sid in sessions | {None}:
                dep = (table + ":membership", sid)
                scope.memory_versions[dep] = scope.memory_versions.get(dep, 0) + 1
        scope.memory_changes.append((table, old, new))
        affected = set()
        for shape in self.dependency_shapes.get(table, ()):
            for row in (old, new):
                if row is not None:
                    predicates = tuple((name, row[name]) for name in shape)
                    tag = f"{table}#{predicates!r}"
                    affected.add((tag, dict(predicates).get("session_id")))
        for dependency in affected:
            if dependency in scope.memory_versions:
                scope.memory_versions[dependency] += 1
        for shape, columns in self.projection_shapes.get(table, ()):
            if (
                old is not None
                and new is not None
                and all(old[c] == new[c] for c in (*shape, *columns))
            ):
                continue
            affected = {
                tuple((name, row[name]) for name in shape) for row in (old, new) if row is not None
            }
            for predicates in affected:
                dependency = (
                    f"{table}@{(predicates, columns)!r}",
                    dict(predicates).get("session_id"),
                )
                if dependency in scope.memory_versions:
                    scope.memory_versions[dependency] += 1
        return 0

    def select(self, sql, parameters):
        self.guard(_session_parameter(sql, parameters))
        simple = _simple(
            sql, parameters, {t: c for t, c in self.columns.items() if self.primary[t]}
        )
        if simple:
            table, predicates, selected, ordering = simple
            sid = dict(predicates).get("session_id")
            self.projection_dependency(table, predicates, (*selected, *ordering))
            key = ("range", table, predicates)
            relation = self.get(key)
            if relation is None and len(predicates) <= 8:
                # Reuse a loaded wider range through identity/relationship buckets.
                # Looking up query shapes does not enumerate unrelated entities.
                for size in range(len(predicates) - 1, 0, -1):
                    for subset in combinations(predicates, size):
                        wider = self.get(("range", table, subset))
                        extra = dict(set(predicates) - set(subset))
                        if wider is not None and any(c in wider.indices for c in extra):
                            relation = wider
                            break
                    if relation is not None:
                        break
            if relation is None:
                where = " AND ".join(f'"{name}" IS ?' for name, _ in predicates)
                cursor = self.store._conn.execute(
                    f'SELECT * FROM "{table}" WHERE {where}', tuple(v for _, v in predicates)
                )
                rows = tuple(cursor.fetchall())
                relation = Range(
                    table,
                    predicates,
                    rows,
                    self.primary[table],
                    len(self.store._scope.memory_changes),
                )
                self.put(key, relation)
                self.stats["cold_queries"] += 1
                self.stats["cold_rows"] += len(rows)
            else:
                self.stats["hits"] += 1
            rows = relation.read(predicates, self.store._scope.memory_changes, self.stats)
            if ordering:
                rows = sorted(
                    rows, key=lambda row: tuple((row[c] is not None, row[c]) for c in ordering)
                )
            return Rows(
                tuple(Record(selected, tuple(row[c] for c in selected)) for row in rows),
                description=tuple((c, None, None, None, None, None, None) for c in selected),
            )
        key = ("query", sql, tuple(parameters))
        cached = self.get(key)
        if cached is not None:
            rows, description, dependencies, stamps = cached
            if stamps == tuple(self.version(d) for d in dependencies):
                for table, sid in dependencies:
                    self.dependency(table, sid)
                self.stats["hits"] += 1
                return Rows(rows, description=description)
        tables = set()

        def authorize(action, table, column, database, trigger):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and table in self.columns:
                tables.add(table)
            return sqlite3.SQLITE_OK

        # Statement caching must not suppress dependency collection on a miss.
        self.store._conn.set_authorizer(authorize)
        try:
            cursor = self.store._conn.execute(sql, parameters)
            description = cursor.description
            names = tuple(c[0] for c in description)
            rows = tuple(Record(names, row) for row in cursor.fetchall())
        finally:
            self.store._conn.set_authorizer(None)
        sid = _session_parameter(sql, parameters)
        dependencies = tuple(sorted((table, sid) for table in tables))
        point = _query_range(sql, parameters, self.columns)
        if point is not None and tables == {point[0]}:
            dependencies = (self.range_dependency(point[0], point[1]),)
        if not dependencies:
            dependencies = tuple(
                ("__scope__", s) for s in sorted(self.store._scope.memory_sessions)
            )
        for table, session in dependencies:
            self.dependency(table, session)
        for row in rows:
            if "session_id" in row.keys():  # noqa: SIM118
                self.guard(row["session_id"])
        self.stats["cold_queries"] += 1
        self.stats["cold_rows"] += len(rows)
        self.put(
            key, (rows, description, dependencies, tuple(self.version(d) for d in dependencies))
        )
        return Rows(rows, description=description)

    def publish(self, scope):
        """No SQL, domain work or callbacks during current-fact publication."""
        # Resolve changed query shapes by their indexed predicate values. The
        # number of entities in a loaded range never affects publication work.
        shapes = {table: set(signatures) for table, signatures in self.range_shapes.items()}
        for key in scope.memory_pending:
            if key[0] == "range":
                shapes.setdefault(key[1], set()).add(tuple(name for name, _ in key[2]))
        changed = {}
        for number, (table, old, new) in enumerate(scope.memory_changes):
            for shape in shapes.get(table, ()):
                for row in (old, new):
                    if row is not None:
                        key = ("range", table, tuple((name, row[name]) for name in shape))
                        relation = self.get(key)
                        if relation is not None and number >= relation.stage:
                            changed.setdefault(key, {})[number] = (old, new)
        for key, edits in changed.items():
            relation = self.get(key)
            for old, new in edits.values():
                relation.apply(old, new)
                self.stats["published_rows"] += 1
            relation.stage = 0
            scope.memory_pending[key] = relation
        for key, value in scope.memory_pending.items():
            if isinstance(value, Range):
                value.stage = 0
            self.remember(key, value)
        self.versions = scope.memory_versions
        self.trim_versions()
        self.stats["publications"] += int(bool(scope.memory_changes))

    def accept_read(self, scope):
        for key, value in scope.memory_pending.items():
            if isinstance(value, Range):
                value.stage = 0
            self.remember(key, value)
        self.versions = scope.memory_versions
        self.trim_versions()


def _session_parameter(sql, parameters):
    match = re.search(r"\bsession_id\s*=\s*\?", sql, re.I)
    if match is None or not isinstance(parameters, (tuple, list)):
        return None
    index = sql[: match.end()].count("?") - 1
    return parameters[index] if index < len(parameters) else None


def _references(value, sid):
    if isinstance(value, (tuple, frozenset)):
        return any(_references(v, sid) for v in value)
    return value == sid


def _query_range(sql, parameters, columns):
    """Track a scalar/expression projection by its exact single-table range."""
    match = re.fullmatch(
        r"SELECT\s+.+?\s+FROM\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?\s+WHERE\s+(.+)",
        sql.strip(),
        re.I | re.S,
    )
    if match is None or match[1] not in columns:
        return None
    table, alias, where = match.groups()
    if alias:
        where = re.sub(rf"\b{re.escape(alias)}\.", "", where)
    start = sql.upper().rfind("WHERE")
    offset = sql[:start].count("?")
    simple = _simple(f"SELECT * FROM {table} WHERE {where}", parameters[offset:], columns)
    return simple[:2] if simple is not None else None


class RuntimeConnection:
    """Internal record access, including all legacy domain callers and writes."""

    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        return getattr(self.store._conn, name)

    def execute(self, sql, parameters=()):
        store = self.store
        store.ensure_open()
        operation = sql.lstrip().split(None, 1)[0].upper()
        if operation in {"BEGIN", "COMMIT", "END"} or (
            operation == "ROLLBACK" and not re.match(r"ROLLBACK\s+TO\b", sql, re.I)
        ):
            raise StateMachineError("invalid_transaction", "Transaction control belongs to Store")
        if operation in {"SELECT", "WITH"} and store._scope is None:
            with store.transaction():
                return store.memory.select(sql, parameters)
        if operation in {"SELECT", "WITH"}:
            store.memory.guard()
            return store.memory.select(sql, parameters)
        if (
            operation in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}
            and store._scope is None
        ):
            try:
                with store.transaction(write=True):
                    return self.execute(sql, parameters)
            except StateMachineError as exc:
                # Preserve the internal raw-record interface's SQLite constraint
                # errors. Public domain APIs already own explicit Store scopes.
                if exc.code == "storage_error" and isinstance(exc.__cause__, sqlite3.Error):
                    raise exc.__cause__ from None
                raise
        if operation in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            store.require_write()
            store.begin_persistence()
            replacement = re.match(
                r"(?:INSERT\s+OR\s+REPLACE\s+INTO|REPLACE\s+INTO)\s+(\w+)", sql, re.I
            )
            if replacement:
                store.memory.invalidate_table(replacement[1])
        scope = store._scope
        if operation == "SAVEPOINT":
            scope.memory_savepoints.append(
                (
                    sql.split()[-1],
                    dict(scope.memory_pending),
                    dict(scope.memory_versions),
                    len(scope.memory_changes),
                )
            )
        cursor = store._conn.execute(sql, parameters)
        if operation == "ROLLBACK" and " TO " in f" {sql.upper()} ":
            name = sql.split()[-1]
            i = next(
                i
                for i in range(len(scope.memory_savepoints) - 1, -1, -1)
                if scope.memory_savepoints[i][0] == name
            )
            _, pending, versions, changes = scope.memory_savepoints[i]
            scope.memory_pending, scope.memory_versions = dict(pending), dict(versions)
            del scope.memory_changes[changes:]
            del scope.memory_savepoints[i + 1 :]
            scope.read_memo.clear()
        elif operation == "RELEASE":
            name = sql.split()[-1]
            i = next(
                i
                for i in range(len(scope.memory_savepoints) - 1, -1, -1)
                if scope.memory_savepoints[i][0] == name
            )
            del scope.memory_savepoints[i:]
        elif operation in {"CREATE", "DROP", "ALTER"}:
            store.memory.clear()
            if re.match(r"(?:CREATE|DROP|ALTER)\s+(?:TEMP\s+)?TABLE\b", sql, re.I):
                store.memory.install()
        return cursor

    def commit(self):
        raise StateMachineError("invalid_transaction", "Commit belongs to the outer scope")

    def rollback(self):
        raise StateMachineError("invalid_transaction", "Rollback belongs to the outer scope")

    def executemany(self, sql, parameters):
        count = 0
        if self.store._scope is None:
            with self.store.transaction(write=True):
                return self.executemany(sql, parameters)
        for values in parameters:
            count += self.execute(sql, values).rowcount
        return Rows(rowcount=count)
