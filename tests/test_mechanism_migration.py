"""Schema31 table contract in a Schema33 save: exact preservation and atomic upgrade."""

import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from lock_support import A, lock_loader
from test_migrations import assert_original_rows_equal, stored_rows
from test_world_mechanisms import LEVER, mechanism_data
from world_support import setup, submit

from trpg_state_machine import ActivateMechanism, StateMachine, StateMachineError


def test_schema33_armed_spent_preserved_migration_failure_atomic_and_retry(
    tmp_path, context, monkeypatch
):
    data = mechanism_data()
    data["lockable_doors"] = []
    obj, definition = (
        deepcopy(data["scenes"][0]["objects"][-1]),
        deepcopy(data["world_objects"][-1]),
    )
    obj["object_id"] = "object.other"
    definition.update(object_id="object.other", definition_ref="definition.other")
    data["scenes"][0]["objects"].append(obj)
    data["world_objects"].append(definition)
    sm, sid, package, db = setup(tmp_path, context, data)
    with sm:
        receipt = submit(sm, sid, package, context, ActivateMechanism(A, LEVER, "0,0"), "legacy")
        assert receipt.status == "committed"
    # Restore precisely the historical table contract using its bundled migration.
    with sqlite3.connect(db) as conn:
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND (name GLOB 'spatial_index_*' OR name GLOB 'integrity_*')"
        ).fetchall():
            conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        for table in (
            "object_spatial_cells",
            "scene_integrity_dependencies",
            "scene_integrity",
            "instance_configs",
        ):
            conn.execute("DROP TABLE " + table)
        conn.execute("DROP INDEX actors_cell_lookup")
        # Current setup includes additive TIME37/38 and ENV35/36 storage. Remove
        # those additions before claiming this fixture is a Schema33 database.
        for table in (
            "session_time_waits",
            "time_command_admissions",
            "session_time_progress",
            "session_time_runs",
        ):
            conn.execute("DROP TABLE " + table)
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'env_*'"
        ).fetchall():
            conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        conn.execute("DROP TABLE environment_provider_bindings")
        conn.execute("DROP TABLE scene_environment_versions")
        conn.execute("DROP TABLE scene_environments")
        rows = conn.execute("SELECT session_id,object_id,status FROM object_mechanisms").fetchall()
        conn.execute("DROP TABLE mechanism_activations")
        conn.execute("DROP TABLE object_mechanisms")
        sql = (Path(__file__).parents[1] / "src/trpg_state_machine/_migration_31.sql").read_text(
            "utf-8"
        )
        start, end = sql.index("CREATE TABLE"), sql.index("PRAGMA")
        conn.execute(sql[start:end])
        conn.executemany("INSERT INTO object_mechanisms VALUES (?,?,?)", rows)
        conn.execute("PRAGMA user_version=33")
    before = stored_rows(db)
    read = Path.read_text

    def fail(path, *args, **kwargs):
        sql = read(path, *args, **kwargs)
        return (
            sql.replace("PRAGMA user_version = 34;", "INVALID MIGRATION;")
            if path.name == "_migration_34.sql"
            else sql
        )

    monkeypatch.setattr(Path, "read_text", fail)
    with pytest.raises(StateMachineError) as error:
        StateMachine(db, dnd_loader=lock_loader())
    assert error.value.code == "storage_error"
    assert stored_rows(db) == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 33
    monkeypatch.setattr(Path, "read_text", read)
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert_original_rows_equal(before, stored_rows(db))
        assert restored._world_facts.mechanism_state(sid, LEVER).remaining_uses == 0
        assert restored._world_facts.mechanism_state(sid, "object.other").remaining_uses == 1
        assert restored._conn.execute("SELECT * FROM mechanism_activations").fetchall() == []
        assert restored._conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert restored._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.get_command_receipt(sid, "legacy", package, context=context) == receipt


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
