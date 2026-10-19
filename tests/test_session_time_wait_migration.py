"""Schema37-shaped persisted progress survives the additive wait contract migration."""

import sqlite3
from pathlib import Path

import pytest
from test_migrations import stored_rows
from test_session_time_coordination import (
    player_command,
    port,
    start,
    submit,
)
from test_session_time_coordination import (
    session as session,
)

from trpg_state_machine import AdvanceSessionTime, StateMachine, StateMachineError


@pytest.mark.parametrize("inject_failure", [False, True])
def test_schema38_preserves_all_old_rows_and_rolls_back_failed_ddl(
    session, monkeypatch, inject_failure
):
    sm, sid, package, context, clock, _ = session
    deadlines = port(session)
    deadlines.put(sid, "first", 18)
    start(session, 3, 2)
    clock.advance(2)
    sm.submit_command(player_command(session), package, context=context)
    submit(session, AdvanceSessionTime(60, "activity", budget=1), "partial")
    path = sm._conn.execute("PRAGMA database_list").fetchone()[2]
    sm.close()
    # Schema38 has no changes to any Schema37 table or index. Remove only its
    # empty new table to construct the exact previous schema, including real
    # commands' admissions, receipts, Outbox, running settings and pending work.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE session_time_waits")
        conn.execute("PRAGMA user_version=37")
    original = stored_rows(path)
    for name in ("session_time_runs", "session_time_progress", "time_command_admissions"):
        assert original[name][1]
    if inject_failure:
        read = Path.read_text

        def broken_migration(file, *args, **kwargs):
            sql = read(file, *args, **kwargs)
            if file.name == "_migration_38.sql":
                sql += "\nSELECT missing_migration_column;\n"
            return sql

        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", broken_migration)
            with pytest.raises(StateMachineError, match="Cannot initialize SQLite"):
                StateMachine(path)
        assert stored_rows(path) == original
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 37
    with StateMachine(path) as restored:
        restored.resume_session(sid, package, context=context)
        assert restored._conn.execute("PRAGMA user_version").fetchone()[0] == 39
        assert restored._conn.execute("SELECT * FROM session_time_waits").fetchall() == []
        assert restored._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    current = stored_rows(path)
    assert {name: current[name] for name in original} == original
    with StateMachine(path):
        pass
    assert stored_rows(path) == current
