"""Real Schema33/main34/ENV34 runtimes and the combined atomic Schema35 upgrade."""

import sqlite3
from pathlib import Path

import pytest
from test_migrations import stored_rows

from trpg_state_machine import (
    StateMachine,
    StateMachineError,
)

ROOT = Path(__file__).resolve().parents[1]
HISTORY = {"33": "9ce60cb", "main34": "afe6871", "env34": "06c7af8"}


def snapshot(db):
    with sqlite3.connect(db) as connection:
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name"
            ).fetchall(),
            stored_rows(db),
        )


def assert_current(sm):
    assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 53
    assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert sm._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert sm._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert sm._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_new_database_upgrade_and_failure_are_atomic(tmp_path, monkeypatch):
    db = tmp_path / "fresh.db"
    read = Path.read_text

    def broken(path, *args, **kwargs):
        sql = read(path, *args, **kwargs)
        return (
            sql.replace("PRAGMA user_version = 35;", "INVALID MIGRATION;")
            if path.name == "_migration_35.sql"
            else sql
        )

    monkeypatch.setattr(Path, "read_text", broken)
    with pytest.raises(StateMachineError) as error:
        StateMachine(db)
    assert error.value.code == "storage_error"
    assert snapshot(db) == (0, [], {})
    monkeypatch.setattr(Path, "read_text", read)
    with StateMachine(db) as sm:
        assert_current(sm)
        assert sm._conn.execute("SELECT * FROM scene_environments").fetchall() == []
        assert sm._conn.execute("SELECT * FROM mechanism_activations").fetchall() == []
