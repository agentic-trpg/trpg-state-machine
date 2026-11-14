"""Fresh schema setup, migration locking, and interrupted setup rollback."""

import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from test_mechanics import mechanical_package as mechanical_package

from trpg_state_machine import (
    StateMachine,
)

ROOT = Path(__file__).resolve().parents[1]


def stored_rows(database):
    with sqlite3.connect(database) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {
            name: (
                [r[1] for r in conn.execute(f"PRAGMA table_xinfo({name})")],
                set(conn.execute(f"SELECT * FROM {name}").fetchall()),
            )
            for (name,) in tables
        }


def assert_preserved(database, original):
    with sqlite3.connect(database) as conn:
        for table, (columns, rows) in original.items():
            after = set(conn.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall())
            assert rows <= after, table
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 44
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def assert_original_rows_equal(original, current, *, excluded=()):
    """Check unchanged columns and rows across a current-schema operation."""
    for table, (columns, rows) in original.items():
        if table in excluded:
            continue
        current_columns, current_rows = current[table]
        indices = [current_columns.index(column) for column in columns]
        projected = {tuple(row[i] for i in indices) for row in current_rows}
        assert projected == rows, table


def test_concurrent_fresh_initialization(tmp_path, monkeypatch):
    database, original = tmp_path / "empty.db", {}
    barrier = Barrier(4)
    connect = sqlite3.connect
    version_reads = []

    class TracedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if (
                sql == "PRAGMA user_version"
                and not getattr(self, "read_only_probe", False)
                and not getattr(self, "read_version", False)
            ):
                version_reads.append(self.in_transaction)
                self.read_version = True
            return super().execute(sql, *args)

    def traced_connect(*args, **kwargs):
        conn = connect(*args, factory=TracedConnection, **kwargs)
        conn.read_only_probe = kwargs.get("uri", False) and "mode=ro" in str(args[0])
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    def open_runtime(_):
        barrier.wait(timeout=10)
        with StateMachine(database) as sm:
            return tuple(
                sm._conn.execute(f"PRAGMA {p}").fetchone()[0]
                for p in ("user_version", "foreign_keys", "journal_mode")
            )

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert list(workers.map(open_runtime, range(4))) == [(44, 1, "wal")] * 4
    assert len(version_reads) >= 4 and all(version_reads)
    monkeypatch.undo()
    assert_preserved(database, original)


def test_process_interruption_rolls_back_fresh_schema_setup(tmp_path):
    database, original = tmp_path / "empty.db", {}
    # Kill after real DDL changes in fresh schema setup.
    script = """
import os, sqlite3, sys
sys.path.insert(0, sys.argv[1])
original = sqlite3.connect
class CrashConnection(sqlite3.Connection):
    def execute(self, sql, *args):
        result = super().execute(sql, *args)
        if sql.strip().upper() == 'DROP TABLE RNG_STATES;':
            os._exit(73)
        return result
sqlite3.connect = lambda *a, **k: original(*a, factory=CrashConnection, **k)
from trpg_state_machine import StateMachine
StateMachine(sys.argv[2])
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "src"), str(database)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True,
    )
    assert process.returncode == 73, process.stderr
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert stored_rows(database) == original
    with StateMachine(database):
        pass
    assert_preserved(database, original)
