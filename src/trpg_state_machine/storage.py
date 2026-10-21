"""File-backed SQLite lifecycle and one explicit shared transaction scope."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, get_ident
from time import monotonic, perf_counter_ns, sleep
from weakref import WeakValueDictionary

from .errors import StateMachineError
from .storage_migrations import (
    migrate_perception_queues,
    reconcile_schema34,
    validate_environment_table,
    validate_environment_versions,
)


class _Coordinator:
    def __init__(self):
        self.lock = RLock()
        self.paused_sessions = set()
        self.recovery_generations = {}
        self.recovery_generation = 0


_coordinators = WeakValueDictionary()
_coordinators_lock = RLock()


def _migration_statements(script: str) -> tuple[str, ...]:
    """Read our bundled SQL without executescript's implicit transaction commits."""
    statements, pending = [], ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            normalized = " ".join(pending.split()).upper()
            if normalized not in (
                "BEGIN IMMEDIATE;",
                "COMMIT;",
                "PRAGMA FOREIGN_KEYS = OFF;",
                "PRAGMA FOREIGN_KEYS = ON;",
            ):
                statements.append(pending)
            pending = ""
    if pending.strip():
        raise StateMachineError("storage_error", "Incomplete bundled migration SQL")
    return tuple(statements)


class SqliteStore:
    def __init__(self, database: str | Path):
        if str(database) == ":memory:":
            raise StateMachineError(
                "unsupported_storage", "A local file database with WAL is required"
            )
        self._closed = False
        self._thread = get_ident()
        self._scope = None
        self._grid_maps = None
        self._recovery_listeners = []
        with _coordinators_lock:
            identity = str(Path(database).resolve()).casefold()
            coordinator = _coordinators.get(identity)
            if coordinator is None:
                coordinator = _Coordinator()
                _coordinators[identity] = coordinator
            self._coordinator = coordinator
        try:
            self._conn = sqlite3.connect(str(database), isolation_level=None, timeout=5.0)
        except sqlite3.Error as exc:
            raise StateMachineError("storage_error", "Cannot open SQLite database") from exc
        self._conn.row_factory = sqlite3.Row
        from .environment_versions import protection_key, source_key, wind_key
        from .runtime_integrity import spatial_cells

        try:
            for name, function in (
                ("runtime_spatial_cells", spatial_cells),
                ("env_source_key", source_key),
                ("env_protection_key", protection_key),
                ("env_wind_key", wind_key),
            ):
                self._conn.create_function(
                    name, 5 if name == "runtime_spatial_cells" else 1, function, deterministic=True
                )
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
            # Journal-mode changes cannot run in the migration transaction. SQLite
            # can report BUSY immediately when simultaneous openers change the mode.
            deadline = monotonic() + 5.0
            while True:
                try:
                    mode = self._conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                    break
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY or monotonic() >= deadline:
                        raise
                    sleep(0.01)
            if mode != "wal":
                raise StateMachineError("unsupported_storage", "Database must support WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
            if not self._conn.execute("PRAGMA foreign_keys").fetchone()[0]:
                raise StateMachineError("storage_error", "Foreign keys must be enabled")
            if self._conn.execute("PRAGMA foreign_key_check").fetchone():
                raise StateMachineError("storage_error", "Database contains invalid references")
        except sqlite3.Error as exc:
            self._conn.close()
            self._closed = True
            raise StateMachineError("storage_error", "Cannot initialize SQLite database") from exc
        except BaseException:
            self._conn.close()
            self._closed = True
            raise
        from .session_memory import RuntimeConnection, SessionMemory

        self.memory = SessionMemory(self)
        self._runtime_connection = RuntimeConnection(self)
        self._data_version = self._conn.execute("PRAGMA data_version").fetchone()[0]

    def _migrate(self):
        scripts = [Path(__file__).with_name("_schema.sql")] + [
            Path(__file__).with_name(f"_migration_{v}.sql") for v in range(2, 41)
        ]
        steps = [_migration_statements(path.read_text("utf-8")) for path in scripts]
        # Migration 4 rebuilds a referenced table. Disable FK enforcement only on
        # this unopened runtime connection, then validate all references before commit.
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # Read AFTER acquiring the cross-process SQLite migration write lock.
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(41):
                raise StateMachineError(
                    "unsupported_storage_schema", f"Unsupported storage version: {version}"
                )
            if (
                version == 0
                and self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchone()
            ):
                raise StateMachineError(
                    "unsupported_storage_schema", "Unversioned nonempty database"
                )
            validate_environment_table(self._conn, required=version >= 35)
            validate_environment_versions(self._conn, required=version >= 36)
            if version >= 34:
                reconcile_schema34(self._conn, version, steps[33])
            for target in range(version + 1, 41):
                for statement in steps[target - 1]:
                    self._conn.execute(statement)
                if self._conn.execute("PRAGMA user_version").fetchone()[0] != target:
                    raise StateMachineError("storage_error", "Non-contiguous schema migration")
            if version < 40:
                from .runtime_integrity import certify_scene

                for row in self._conn.execute(
                    "SELECT session_id,scene_id FROM scene_instances"
                ).fetchall():
                    certify_scene(self._conn, row[0], row[1])
            # Derived lookups/certificates are only trustworthy with every
            # transaction-maintenance trigger installed, including on reopen.
            import re

            for statement in steps[39]:
                declaration = re.search(
                    r"CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX|TRIGGER)\s+(\w+)",
                    statement,
                    re.IGNORECASE,
                )
                if declaration is None:
                    continue
                kind, name = declaration.groups()
                stored = self._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                    (kind.lower(), name),
                ).fetchone()
                if stored is None and kind.lower() == "index" and name == "spatial_object_lookup":
                    # Backward-compatible Schema40 index addendum. Install only
                    # while opening under the migration write lock, never during
                    # an ordinary command. Existing facts/readers remain valid.
                    self._conn.execute(statement)
                    stored = self._conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
                    ).fetchone()
                expected = statement[declaration.start() :].strip().rstrip(";")
                if stored is None or " ".join(stored[0].split()) != " ".join(expected.split()):
                    raise StateMachineError(
                        "unsupported_storage_schema", "Runtime integrity maintenance is incomplete"
                    )
            reconcile_schema34(self._conn, 35, steps[33])
            validate_environment_versions(self._conn, required=True)
            if version < 5:
                # The locked source version proves ALL existing events predate
                # perception, even if a user command used the legacy source sentinel.
                self._conn.execute(
                    "INSERT INTO legacy_perception_boundaries "
                    "SELECT session_id,event_seq,'legacy_event_evidence_unavailable' "
                    "FROM sessions WHERE event_seq>0 "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "through_event_seq=excluded.through_event_seq"
                )
                self._conn.execute(
                    "INSERT INTO projection_cursors "
                    "SELECT session_id,'perception',through_event_seq "
                    "FROM legacy_perception_boundaries WHERE true "
                    "ON CONFLICT(session_id,consumer) DO UPDATE SET event_seq=excluded.event_seq"
                )
            if version < 19:
                migrate_perception_queues(self._conn)
            if self._conn.execute("PRAGMA foreign_key_check").fetchone():
                raise StateMachineError("storage_error", "Migration contains invalid references")
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON")

    @property
    def connection(self):
        self.ensure_open()
        return self._runtime_connection

    def ensure_open(self):
        if self._closed:
            raise StateMachineError("closed", "SQLite store is closed")
        if get_ident() != self._thread:
            raise StateMachineError("invalid_transaction", "Connection belongs to another thread")

    def require_write(self):
        self.ensure_open()
        if self._scope is None or not self._scope.write or not self._scope.active:
            raise StateMachineError("invalid_transaction", "An active write scope is required")

    @property
    def derived_read_scope(self):
        """Unmanaged SQL scopes and write candidates cannot use committed caches."""
        self.ensure_open()
        scope = self._scope
        return scope if scope is not None and scope.active and not scope.write else None

    def close(self):
        self.ensure_open()
        if self._scope is not None:
            raise StateMachineError("invalid_transaction", "Cannot close an active transaction")
        self._conn.close()
        if self._grid_maps is not None:
            self._grid_maps.clear()
        self.memory.clear()
        self._closed = True

    def savepoint(self):
        self.require_write()
        return self._scope.savepoint()

    @contextmanager
    def transaction(self, *, write=False):
        # Readers acquire the gate when fixing their current version. Existing
        # read scopes may finish against the explicitly pinned older snapshot.
        gate = self._coordinator.lock
        gate.acquire()
        held = True
        try:
            with self._transaction_scope(write=write) as scope:
                if not write:
                    gate.release()
                    held = False
                yield scope
        finally:
            if held:
                gate.release()

    @contextmanager
    def _transaction_scope(self, *, write=False):
        self.ensure_open()
        if self._scope is not None:
            raise StateMachineError("invalid_transaction", "Use a savepoint within a transaction")
        scope = UnitOfWork(self, write)
        started = perf_counter_ns()
        committed = False
        try:
            self.memory.guard()
            self.memory.sync_recovery()
            # Preparation reads one fixed RAM/SQLite snapshot. The database write
            # lock is acquired only at the first incremental persistence statement.
            self._conn.execute("BEGIN")
            # Pin the SQLite reading snapshot before accepting any RAM range.
            # This metadata fence never rereads loaded domain facts.
            self._conn.execute("PRAGMA schema_version").fetchone()
            data_version = self._conn.execute("PRAGMA data_version").fetchone()[0]
            if data_version != self._data_version:
                self.memory.clear()
                self._data_version = data_version
            scope.memory_versions = dict(self.memory.versions)
            scope.recovery_generations = dict(self._coordinator.recovery_generations)
            scope.recovery_generation = self._coordinator.recovery_generation
            self._scope = scope
            try:
                yield scope
                with self._coordinator.lock:
                    try:
                        commit_started = perf_counter_ns()
                        if write:
                            self._commit()
                        else:
                            self._conn.commit()
                        committed = write
                        if write:
                            self.memory.stats["commit_ns"] += perf_counter_ns() - commit_started
                    except BaseException:
                        if write and not self._conn.in_transaction:
                            self.memory.pause(scope)
                            raise StateMachineError(
                                "commit_outcome_unknown",
                                "Reconcile the original command/work key before retrying",
                            ) from None
                        raise
                    latest = self._conn.execute("PRAGMA data_version").fetchone()[0]
                    recovered = (
                        scope.recovery_generation != self._coordinator.recovery_generation
                        or any(
                            scope.recovery_generations.get(sid, 0)
                            != self._coordinator.recovery_generations.get(sid, 0)
                            for sid in scope.memory_sessions
                        )
                    )
                    if latest != self._data_version or recovered:
                        # An explicitly older reader may return its fixed input,
                        # but cannot publish it into current facts/derived caches.
                        if latest != self._data_version or (
                            scope.recovery_generation != self._coordinator.recovery_generation
                        ):
                            self.memory.clear()
                        else:
                            for sid in scope.memory_sessions:
                                if scope.recovery_generations.get(sid, 0) != (
                                    self._coordinator.recovery_generations.get(sid, 0)
                                ):
                                    self.memory.release(sid)
                        self._data_version = latest
                        scope.derived_pending.clear()
                        if write:
                            raise StateMachineError(
                                "external_write_during_publication", "Reconcile external writer"
                            )
                    else:
                        if write:
                            publish_started = perf_counter_ns()
                            self.memory.publish(scope)
                            self.memory.stats["publication_ns"] += (
                                perf_counter_ns() - publish_started
                            )
                            self.memory.stats["write_scope_ns"] += perf_counter_ns() - started
                        else:
                            self.memory.accept_read(scope)
                        for (cache, generation), pending in scope.derived_pending.items():
                            cache.publish(generation, pending)
            except BaseException:
                if committed:
                    self.memory.pause(scope)
                    raise StateMachineError(
                        "committed_publication_pending",
                        "SQLite committed; reconcile RAM before serving current state",
                    ) from None
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise
            finally:
                scope.read_memo.clear()
                scope.derived_pending.clear()
                scope.active = False
                self._scope = None
        except sqlite3.Error as exc:
            scope.active = False
            raise StateMachineError("storage_error", "SQLite transaction failed") from exc

    def _commit(self):
        """Single durable boundary, also the fault-injection seam."""
        self._conn.commit()

    def begin_persistence(self):
        """Leave the preparation snapshot, lock/recheck, then persist the delta.

        No writes have happened at this transition. SQL savepoints are recreated;
        their sparse RAM candidates remain isolated. A foreign writer invalidates
        the entire prepared read scope, never splices newer SQL into older RAM.
        """
        self.require_write()
        scope = self._scope
        if scope.persisting:
            return
        self._conn.rollback()
        self._conn.execute("BEGIN IMMEDIATE")
        self._conn.execute("PRAGMA schema_version").fetchone()
        latest = self._conn.execute("PRAGMA data_version").fetchone()[0]
        for name, *_ in scope.memory_savepoints:
            self._conn.execute(f"SAVEPOINT {name}")
        if latest != self._data_version:
            self.memory.pause(scope)
            raise StateMachineError(
                "session_reconciliation_required", "Prepared facts changed before persistence"
            )
        scope.persisting = True

    @contextmanager
    def reconciliation(self, sid=None):
        """Stop current service while the caller validates persisted instances.

        The caller must check original stable receipts and existing recovery
        boundaries before reopening. Failure leaves the runtime paused.
        """
        self.ensure_open()
        if self._scope is not None:
            raise StateMachineError("invalid_transaction", "Drain the active scope first")
        # Validation temporarily permits this caller's persisted reads. Keep the
        # gate until acceptance so another connection cannot enter current service
        # while the shared paused marker is temporarily removed.
        with self._coordinator.lock:
            coordinator = self._coordinator
            if sid is None:
                coordinator.recovery_generation += 1
            else:
                coordinator.recovery_generations[sid] = (
                    coordinator.recovery_generations.get(sid, 0) + 1
                )
            self.memory.sync_recovery()
            self.memory.paused = False
            if sid is not None:
                self.memory.paused_sessions.discard(sid)
            try:
                yield
            except BaseException:
                if sid is None:
                    self.memory.paused = True
                else:
                    self.memory.paused_sessions.add(sid)
                raise

    def release_memory(self, sid=None):
        self.ensure_open()
        if self._scope is not None:
            raise StateMachineError("invalid_transaction", "Cannot release an active scope")
        if sid is None:
            self.memory.clear()
        else:
            self.memory.release(sid)
        if self._grid_maps is not None:
            if sid is None:
                self._grid_maps.clear()
            else:
                self._grid_maps.release(sid)


class UnitOfWork:
    """A scope handle expires on exit; it never owns a second connection."""

    def __init__(self, store, write):
        self._store = store
        self.write = write
        self.active = True
        self.derived_pending = {}
        self.read_memo = {}
        self.read_stage = -1
        self.memory_pending = {}
        self.memory_versions = {}
        self.memory_changes = []
        self.memory_savepoints = []
        self.memory_sessions = set()
        self.persisting = False

    def _connection(self):
        self._store.ensure_open()
        if not self.active or self._store._scope is not self:
            raise StateMachineError("invalid_transaction", "Transaction scope has ended")
        return self._store.connection

    def execute(self, sql, parameters=()):
        """Write in this scope, returning no cursor or connection that can escape."""
        connection = self._connection()
        self._store.require_write()
        statement = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if statement not in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            raise StateMachineError(
                "invalid_transaction", "Transaction control belongs to the outer scope"
            )
        return connection.execute(sql, parameters).rowcount

    def fetchone(self, sql, parameters=()):
        """A detached row, never a reusable SQLite cursor."""
        connection = self._connection()
        if not sql.lstrip().upper().startswith("SELECT "):
            raise StateMachineError("invalid_transaction", "Expected a read query")
        return connection.execute(sql, parameters).fetchone()

    @contextmanager
    def savepoint(self):
        connection = self._connection()
        self._store.require_write()
        # SQLite nests savepoints bearing the same name; RELEASE targets the latest.
        connection.execute("SAVEPOINT domain_effects")
        try:
            yield self
        except BaseException:
            connection.execute("ROLLBACK TO domain_effects")
            self.read_memo.clear()
            raise
        finally:
            connection.execute("RELEASE domain_effects")
