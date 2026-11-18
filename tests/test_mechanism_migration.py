"""Reject an incompatible development Schema34 database without changing its stored data."""

import sqlite3

import pytest
from test_migrations import stored_rows

from trpg_state_machine import StateMachine, StateMachineError


def test_conflicting_development_schema34_is_refused_without_writing(tmp_path):
    db = tmp_path / "other-lineage.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE object_mechanisms (session_id TEXT,object_id TEXT,status TEXT)")
        conn.execute("INSERT INTO object_mechanisms VALUES ('session.old','object.trap','armed')")
        conn.execute("PRAGMA user_version=34")
    before = stored_rows(db)
    with pytest.raises(StateMachineError) as error:
        StateMachine(db)
    assert error.value.code == "unsupported_storage_schema"
    assert stored_rows(db) == before
