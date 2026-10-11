"""Durable task authorization, existing Inbox identity and actual SQLite recovery."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Barrier

import pytest
import yaml
from perception_support import synthetic_notice
from test_events import event_data
from test_migrations import assert_preserved, stored_rows
from test_npc_state import SOURCE, enter, grant, request

from trpg_state_machine import SetSceneFlag, StateMachine, StateMachineError, load_adventure_package
from trpg_state_machine.perception import NPCContext

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def evaluation_package(tmp_path):
    data = yaml.safe_load(SOURCE.read_text("utf-8"))
    data["events"] = [event_data()]
    data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    path = tmp_path / "evaluation.yaml"
    path.write_text(yaml.safe_dump(data), "utf-8")
    loaded = load_adventure_package(path)
    assert loaded.status == "valid", loaded.errors
    return loaded.package


def prepare(sm, package, context, key="create.eval"):
    sid = sm.create_session(request(package, context, key), package, context=context).session_id
    enter(sm, sid, package, context, "scene.workshop")
    enter(sm, sid, package, context, "scene.platform")
    npc = grant(sm, sid, package, context)
    grant(sm, sid, package, context, "npc.attendant")
    current = sm.read_session(sid, package, context=context)
    sm._apply(
        sid,
        package,
        SetSceneFlag("scene.platform", "flag.lamp_checked", True),
        expected_world_version=current.world_version,
        context=context,
    )
    for event in sm.read_events(sid, package, context=context):
        assert (
            sm.project_event(
                sid, event.event_seq, package, context=context, judgment=synthetic_notice
            )
            == "resolved"
        )
    observations = sm.read_observations(sid, package, context=npc)
    assert observations
    return sid, npc, tuple(o.observation_id for o in observations)


def create_task(sm, sid, package, npc, ids=(), key="task.first", **changes):
    args = dict(
        context=npc,
        request_id=key,
        mode="interactive",
        reason="player_input",
        expected_npc_state_version=1,
        source_observation_ids=ids,
        player_input="Hello.",
    )
    args.update(changes)
    return sm.create_npc_evaluation_task(sid, package, **args)


def test_task_full_input_repeat_restart_and_internal_provenance(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "state.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        before = stored_rows(db)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        assert create_task(sm, sid, evaluation_package, npc, ids) == task
        data = sm.get_npc_evaluation_task(task.evaluation_id, evaluation_package, context=npc)
        assert data.task == task
        assert data.npc_meta.profile.role == "lamp caretaker"
        assert data.npc_meta.known_facts[0].fact_id == "fact.lamp"
        assert data.observations == sm.read_observations(sid, evaluation_package, context=npc)
        raw = json.dumps(asdict(data))
        assert "buried blue key" not in raw and "source_event_id" not in raw
        assert "attendant private" not in raw
        assert sm.list_npc_evaluation_tasks(sid, evaluation_package, context=npc) == (task,)
        for table in ("npc_states", "npc_private_records", "rng_states", "projection_cursors"):
            assert stored_rows(db)[table] == before[table]
        assert (
            sm._conn.execute("SELECT DISTINCT status FROM npc_observation_work").fetchall()[0][0]
            == "pending"
        )
        assert sm._conn.execute("SELECT COUNT(*) FROM observation_signal_sources").fetchone()[0] > 0
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/_npc_evaluation_worker.py"),
            str(db),
            str(tmp_path / "evaluation.yaml"),
            task.evaluation_id,
            sid,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == json.loads(json.dumps(asdict(data)))


@pytest.mark.parametrize(
    "change,code",
    [
        ({"mode": "other"}, "invalid_evaluation"),
        ({"reason": ""}, "invalid_evaluation"),
        ({"expected_npc_state_version": True}, "invalid_evaluation"),
        ({"expected_npc_state_version": 2}, "npc_version_conflict"),
        ({"source_observation_ids": ["observation.fake"]}, "invalid_evaluation"),
        ({"source_observation_ids": ("observation.fake",)}, "invalid_observation"),
        ({"source_observation_ids": ("observation.fake",) * 2}, "invalid_evaluation"),
        ({"player_input": "x" * 4097}, "invalid_evaluation"),
    ],
)
def test_invalid_task_no_partial_writes(tmp_path, evaluation_package, context, change, code):
    db = tmp_path / "state.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as error:
            create_task(sm, sid, evaluation_package, npc, ids, **change)
        assert error.value.code == code
        assert stored_rows(db) == before


def test_task_request_conflict_active_and_revoked_access(tmp_path, evaluation_package, context):
    with StateMachine(tmp_path / "state.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        with pytest.raises(StateMachineError, match="changed"):
            create_task(sm, sid, evaluation_package, npc, ids, reason="different")
        with pytest.raises(StateMachineError) as error:
            create_task(sm, sid, evaluation_package, npc, ids, key="other")
        assert error.value.code == "evaluation_active"
        sm.grant_npc_access(
            sid, npc.observer_id, "replacement", evaluation_package, context=context
        )
        for call in (
            lambda: create_task(sm, sid, evaluation_package, npc, ids),
            lambda: sm.get_npc_evaluation_task(task.evaluation_id, evaluation_package, context=npc),
            lambda: sm.list_npc_evaluation_tasks(sid, evaluation_package, context=npc),
        ):
            with pytest.raises(StateMachineError) as error:
                call()
            assert error.value.code == "unauthorized"
        new = replace(npc, principal_id="replacement")
        with pytest.raises(StateMachineError):
            sm.get_npc_evaluation_task(task.evaluation_id, evaluation_package, context=new)


def test_dual_npc_session_isolation_and_missing_projection(tmp_path, evaluation_package, context):
    with StateMachine(tmp_path / "state.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        second, other, _ = prepare(sm, evaluation_package, context, "second")
        attendant = NPCContext("agent:npc.attendant", sid, "npc.attendant")
        for bound_sid, bound_context in ((sid, attendant), (second, other)):
            with pytest.raises(StateMachineError) as error:
                create_task(sm, bound_sid, evaluation_package, bound_context, ids)
            assert error.value.code == "invalid_observation"
        sm._conn.execute(
            "UPDATE projection_cursors SET event_seq=0 WHERE session_id=? AND "
            "consumer='perception'",
            (sid,),
        )
        with pytest.raises(StateMachineError) as error:
            create_task(sm, sid, evaluation_package, npc, ids)
        assert error.value.code == "invalid_observation"
        assert not sm.list_npc_evaluation_tasks(sid, evaluation_package, context=npc)


@pytest.mark.parametrize("table", ["npc_evaluation_tasks", "npc_evaluation_observations"])
def test_task_sql_failure_rollback_and_retry(tmp_path, evaluation_package, context, table):
    db = tmp_path / "state.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE INSERT ON {table} BEGIN SELECT "
            "RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            create_task(sm, sid, evaluation_package, npc, ids)
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER inject")
        assert create_task(sm, sid, evaluation_package, npc, ids).status == "running"


def test_concurrent_task_creation_sqlite_unique_authorization(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "state.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
    barrier = Barrier(4)

    def create(_):
        with StateMachine(db) as sm:
            barrier.wait()
            return create_task(sm, sid, evaluation_package, npc, ids)

    with ThreadPoolExecutor(4) as pool:
        tasks = list(pool.map(create, range(4)))
    assert tasks == [tasks[0]] * 4
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM npc_evaluation_tasks").fetchone()[0] == 1


def test_actual_sm14_upgrade_preserves_all_rows_and_backfills_only_existing_inbox(
    tmp_path, evaluation_package, context
):
    old = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "344c5f6", "src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(old)
    db = tmp_path / "old.db"
    env = dict(os.environ, PYTHONPATH=str(old / "src"))
    code = """
import sys
sys.path.insert(0, sys.argv[1])
from trpg_state_machine import (
 StateMachine, load_adventure_package, TrustedContext, CreateSessionRequest, PlayerSelection,
)
from trpg_state_machine import SetSceneFlag
p=load_adventure_package(sys.argv[3]).package
c=TrustedContext('authenticated:alice','player.alice')
with StateMachine(sys.argv[2]) as sm:
 r=CreateSessionRequest('old',p.adventure_id,p.package_version,p.package_digest,(PlayerSelection(c.player_id,'pc_template.observer'),))
 sid=sm.create_session(r,p,context=c).session_id
 sm._apply(sid,p,SetSceneFlag('scene.platform','flag.lamp_checked',True),expected_world_version=0,context=c)
 for e in sm.read_events(sid,p,context=c):
  sm.project_event(sid,e.event_seq,p,context=c)
 assert sm._conn.execute('PRAGMA user_version').fetchone()[0]==10
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(old / "src"), str(db), str(tmp_path / "evaluation.yaml")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    before = stored_rows(db)
    with StateMachine(db) as sm:
        assert sm._conn.execute("SELECT COUNT(*) FROM npc_evaluation_tasks").fetchone()[0] == 0
        assert sm._conn.execute("SELECT COUNT(*) FROM npc_observation_work").fetchone()[0] > 0
        assert sm._conn.execute("SELECT COUNT(*) FROM observation_signal_sources").fetchone()[0] > 0
    assert_preserved(db, before)
    with StateMachine(db):
        pass
    assert_preserved(db, before)


def test_unconfigured_npc_cannot_authorize_cognition(tmp_path, package, context, request_for):
    with StateMachine(tmp_path / "state.db") as sm:
        sid = sm.create_session(request_for(), package, context=context).session_id
        npc = grant(sm, sid, package, context)
        with pytest.raises(StateMachineError) as error:
            create_task(sm, sid, package, npc)
        assert error.value.code == "npc_unconfigured"


def test_process_exit_during_task_transaction_leaves_no_half_task(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "crash.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
    before = stored_rows(db)
    code = """
import os,sys,json
from trpg_state_machine import StateMachine,load_adventure_package
from trpg_state_machine.perception import NPCContext
p=load_adventure_package(sys.argv[2]).package
with StateMachine(sys.argv[1]) as sm:
 sm._conn.create_function('crash',0,lambda:os._exit(23))
 sm._conn.execute('CREATE TEMP TRIGGER crash_task BEFORE INSERT ON '
                  'npc_evaluation_observations BEGIN SELECT crash(); END')
 sm.create_npc_evaluation_task(sys.argv[3],p,
  context=NPCContext('agent:npc.caretaker',sys.argv[3],'npc.caretaker'),
  request_id='crash',mode='interactive',reason='test',expected_npc_state_version=1,
  source_observation_ids=tuple(json.loads(sys.argv[4])))
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(db),
            str(tmp_path / "evaluation.yaml"),
            sid,
            json.dumps(ids),
        ],
        capture_output=True,
    )
    assert process.returncode == 23, process.stderr
    with StateMachine(db) as sm:
        assert stored_rows(db) == before
        assert create_task(sm, sid, evaluation_package, npc, ids).status == "running"
