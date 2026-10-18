"""Real Schema33/main34/ENV34 runtimes and the combined atomic Schema35 upgrade."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier

import pytest
from lock_support import B, lock_loader
from test_environment_service import gm
from test_mechanism_service import extended_data
from test_migrations import assert_original_rows_equal, stored_rows
from test_world_mechanisms import LEVER, mechanism_data
from world_support import submit

from trpg_state_machine import (
    EnvironmentConditions,
    SetEnvironmentBase,
    StateMachine,
    StateMachineError,
    Temperature,
)
from trpg_state_machine.package import _validate

ROOT = Path(__file__).resolve().parents[1]
HISTORY = {"33": "9ce60cb", "main34": "afe6871", "env34": "06c7af8"}


def historical_save(tmp_path, lineage):
    """Produce filled saves through actual archived Session/command implementations."""
    source = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", HISTORY[lineage], "src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(source)
    data = extended_data(max_uses=3) if lineage == "main34" else mechanism_data()
    data["lockable_doors"] = []
    if lineage != "main34":
        obj, definition = (
            deepcopy(data["scenes"][0]["objects"][-1]),
            deepcopy(data["world_objects"][-1]),
        )
        obj["object_id"] = "object.other"
        definition.update(object_id="object.other", definition_ref="definition.other")
        data["scenes"][0]["objects"].append(obj)
        data["world_objects"].append(definition)
    package_file = tmp_path / "package.json"
    package_file.write_text(json.dumps(data), encoding="utf-8")
    code = """
import json,sys
from pathlib import Path
from dataclasses import replace
from world_support import setup,submit
from spatial_support import create
from lock_support import A,B
from test_world_mechanisms import LEVER
from trpg_state_machine import TrustedContext,ActivateMechanism,AdvanceGameTime
c=TrustedContext('authenticated:alice','player.alice')
data=json.loads(Path(sys.argv[2]).read_text('utf-8'))
sm,sid,p,db=setup(Path(sys.argv[1]),c,data)
other=create(sm,p,c,'second.session')
op=(ActivateMechanism(A,LEVER,'0,0','up') if sys.argv[3]=='main34'
    else ActivateMechanism(A,LEVER,'0,0'))
assert submit(sm,sid,p,c,op,'activation').status=='committed'
tick=submit(sm,sid,p,replace(c,can_advance_time=True),AdvanceGameTime(0,3),'tick')
assert tick.status=='committed'
if sys.argv[3]=='env34':
    from trpg_state_machine import (
        SetEnvironmentBase,AddEnvironmentSource,EnvironmentConditions,
        Temperature,EnvironmentSource,EnvironmentArea,
    )
    gm=replace(c,can_manage_environment=True)
    for scene,temp in [(A,8),(B,-12)]:
        op=SetEnvironmentBase(scene,EnvironmentConditions(temperature=Temperature(temp)))
        assert submit(sm,sid,p,gm,op,'env.'+scene).status=='committed'
    source=EnvironmentSource('local.saved',EnvironmentConditions(temperature=Temperature(25)),
        EnvironmentArea(('0,0',)),observable=True)
    assert submit(sm,sid,p,gm,AddEnvironmentSource(B,source),'source').status=='committed'
print(json.dumps([sid,other,sm._conn.execute('PRAGMA user_version').fetchone()[0]]))
sm.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(package_file), lineage],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(source / "src"), str(ROOT / "tests"))),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    sid, other, version = json.loads(result.stdout)
    assert version == (33 if lineage == "33" else 34)
    return tmp_path / "world.db", sid, other, _validate(data)


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
    assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 35
    assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert sm._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert sm._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert sm._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("lineage", HISTORY)
def test_real_historical_upgrade_preserves_sessions_environment_and_mechanisms(
    tmp_path, context, lineage
):
    db, sid, other, package = historical_save(tmp_path, lineage)
    before = stored_rows(db)
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        assert_current(sm)
        assert_original_rows_equal(before, stored_rows(db))
        sm.resume_session(sid, package, context=context)
        assert sm.world_time.read(sid, package, context=context).game_tick == 3
        assert sm.world_time.read(other, package, context=context).game_tick == 0
        assert (
            sm.get_command_receipt(sid, "activation", package, context=context).status
            == "committed"
        )
        state = sm._world_facts.mechanism_state(sid, LEVER)
        assert state.activation_count == 1
        assert state.remaining_uses == (2 if lineage == "main34" else 0)
        assert sm._world_facts.mechanism_state(other, LEVER).activation_count == 0
        if lineage == "main34":
            assert len(sm._conn.execute("SELECT * FROM mechanism_activations").fetchall()) == 1
        else:
            assert sm._world_facts.mechanism_state(sid, "object.other").remaining_uses == 1
            assert sm._conn.execute("SELECT * FROM mechanism_activations").fetchall() == []
        facts = sm.environment.read_facts(sid, B, package, context=gm(context))
        if lineage == "env34":
            assert facts.base.temperature == Temperature(-12)
            assert facts.sources[0].source_id == "local.saved"
            assert facts.sources[0].conditions.temperature == Temperature(25)
        else:
            assert facts is None
        assert sm.environment.read_facts(other, B, package, context=gm(context)) is None
        # Upgrading/opening/querying must not manufacture receipts, events, RNG or ENV defaults.
        assert_original_rows_equal(before, stored_rows(db))
    upgraded = snapshot(db)
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        assert_current(sm)
    assert snapshot(db) == upgraded
    # Both services remain operational on the migrated real Session.
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        assert (
            submit(
                sm,
                other,
                package,
                gm(context),
                SetEnvironmentBase(B, EnvironmentConditions(temperature=Temperature(19))),
                "new.env",
            ).status
            == "committed"
        )
        assert sm._world_facts.mechanism_state(other, LEVER).remaining_uses == (
            3 if lineage == "main34" else 1
        )


@pytest.mark.parametrize(
    "lineage,step", [("33", 34), ("33", 35), ("main34", 35), ("env34", 34), ("env34", 35)]
)
def test_historical_upgrade_failure_rolls_back_every_table_and_version(
    tmp_path, monkeypatch, lineage, step
):
    db, _, _, _ = historical_save(tmp_path, lineage)
    before = snapshot(db)
    read = Path.read_text

    def broken(path, *args, **kwargs):
        sql = read(path, *args, **kwargs)
        if path.name == f"_migration_{step}.sql":
            marker = (
                "DROP TABLE old_object_mechanisms;" if step == 34 else "PRAGMA user_version = 35;"
            )
            return sql.replace(marker, "INVALID MIGRATION;")
        return sql

    monkeypatch.setattr(Path, "read_text", broken)
    with pytest.raises(StateMachineError) as error:
        StateMachine(db, dnd_loader=lock_loader())
    assert error.value.code == "storage_error"
    assert snapshot(db) == before
    monkeypatch.setattr(Path, "read_text", read)
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        assert_current(sm)
        assert_original_rows_equal(before[2], stored_rows(db))


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


def test_env34_concurrent_openers_migrate_once_under_existing_lock(tmp_path):
    db, _, _, _ = historical_save(tmp_path, "env34")
    before = stored_rows(db)
    barrier = Barrier(3)

    def opener(_):
        barrier.wait()
        with StateMachine(db, dnd_loader=lock_loader()) as sm:
            assert_current(sm)
            return sm._conn.execute("PRAGMA user_version").fetchone()[0]

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(opener, range(3))) == [35, 35, 35]
    assert_original_rows_equal(before, stored_rows(db))


@pytest.mark.parametrize("damage", ["env_shape", "partial_mechanism", "missing_env35"])
def test_ambiguous_or_partial_schema_is_rejected_without_writes(tmp_path, damage):
    db, _, _, _ = historical_save(tmp_path, "env34")
    with sqlite3.connect(db) as connection:
        if damage == "env_shape":
            connection.execute("ALTER TABLE scene_environments ADD COLUMN unrecognized TEXT")
        elif damage == "partial_mechanism":
            connection.execute("CREATE TABLE mechanism_activations (unrecognized TEXT)")
        else:
            connection.execute("DROP TABLE scene_environments")
            connection.execute("PRAGMA user_version=35")
    before = snapshot(db)
    with pytest.raises(StateMachineError) as error:
        StateMachine(db, dnd_loader=lock_loader())
    assert error.value.code == "unsupported_storage_schema"
    assert snapshot(db) == before
