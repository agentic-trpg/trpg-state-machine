"""File-backed SQLite lifecycle and one explicit shared transaction scope."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import get_ident
from time import monotonic, sleep

from .errors import StateMachineError
from .storage_migrations import migrate_perception_queues


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
        try:
            self._conn = sqlite3.connect(str(database), isolation_level=None, timeout=5.0)
        except sqlite3.Error as exc:
            raise StateMachineError("storage_error", "Cannot open SQLite database") from exc
        self._conn.row_factory = sqlite3.Row
        try:
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

    def _migrate(self):
        scripts = [Path(__file__).with_name("_schema.sql")] + [
            Path(__file__).with_name(f"_migration_{v}.sql") for v in range(2, 30)
        ]
        steps = [_migration_statements(path.read_text("utf-8")) for path in scripts]
        # Migration 4 rebuilds a referenced table. Disable FK enforcement only on
        # this unopened runtime connection, then validate all references before commit.
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            # Read AFTER acquiring the cross-process SQLite migration write lock.
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(30):
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
            for target in range(version + 1, 30):
                for statement in steps[target - 1]:
                    self._conn.execute(statement)
                if self._conn.execute("PRAGMA user_version").fetchone()[0] != target:
                    raise StateMachineError("storage_error", "Non-contiguous schema migration")
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
        return self._conn

    def ensure_open(self):
        if self._closed:
            raise StateMachineError("closed", "SQLite store is closed")
        if get_ident() != self._thread:
            raise StateMachineError("invalid_transaction", "Connection belongs to another thread")

    def require_write(self):
        self.ensure_open()
        if self._scope is None or not self._scope.write or not self._scope.active:
            raise StateMachineError("invalid_transaction", "An active write scope is required")

    def close(self):
        self.ensure_open()
        if self._scope is not None:
            raise StateMachineError("invalid_transaction", "Cannot close an active transaction")
        self._conn.close()
        self._closed = True

    def savepoint(self):
        self.require_write()
        return self._scope.savepoint()

    @contextmanager
    def transaction(self, *, write=False):
        self.ensure_open()
        if self._scope is not None:
            raise StateMachineError("invalid_transaction", "Use a savepoint within a transaction")
        scope = UnitOfWork(self, write)
        try:
            self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            self._scope = scope
            try:
                yield scope
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            finally:
                scope.active = False
                self._scope = None
        except sqlite3.Error as exc:
            scope.active = False
            raise StateMachineError("storage_error", "SQLite transaction failed") from exc


class UnitOfWork:
    """A scope handle expires on exit; it never owns a second connection."""

    def __init__(self, store, write):
        self._store = store
        self.write = write
        self.active = True

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
            raise
        finally:
            connection.execute("RELEASE domain_effects")
