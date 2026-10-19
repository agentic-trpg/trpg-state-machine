"""Existing lawful synthetic observations, independent processing and logical expiry."""

import io
import json
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
from test_game_time import tick, tick_command
from test_migrations import assert_preserved, stored_rows
from test_npc_evaluation_tasks import create_task, prepare
from test_npc_evaluation_tasks import evaluation_package as evaluation_package
from test_npc_modes import new_observation
from test_npc_results import full_result
from test_npc_state import enter

from trpg_state_machine import MoveActor, StateMachine, StateMachineError, load_adventure_package
from trpg_state_machine.npc_results import NPCEvaluationResult
from trpg_state_machine.npc_state import SourceReference
from trpg_state_machine.perception import NPCContext

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ob_package(tmp_path, evaluation_package):
    # No schedule changes: expiry alone must never cancel cognition or change Meta.
    path = tmp_path / "evaluation.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    for actor in data["actors"]:
        actor["npc_state"]["runtime"]["schedule"] = []
    path.write_text(yaml.safe_dump(data), "utf-8")
    result = load_adventure_package(path)
    assert result.status == "valid", result.errors
    return result.package


def advance(sm, sid, package, context, value):
    assert (
        tick(sm, sid, package, replace(context, can_advance_time=True), value).status == "committed"
    )


def test_pending_completed_recent_boundary_and_original_receipt(tmp_path, ob_package, context):
    db = tmp_path / "retention.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        recent = sm.get_recent_npc_observations(sid, ob_package, context=npc)
        assert tuple(x.observation.observation_id for x in recent) == ids
        assert {x.retention.processing_status for x in recent} == {"pending"}
        assert {
            (x.retention.accepted_game_tick, x.retention.expires_game_tick) for x in recent
        } == {(0, 100)}
        task = create_task(sm, sid, ob_package, npc, ids)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, NPCEvaluationResult(), ob_package, context=npc
        )
        assert sm.get_pending_npc_observation_work(sid, ob_package, context=npc) == ()
        assert sm.read_observations(sid, ob_package, context=npc) == tuple(
            x.observation for x in recent
        )
        assert {
            x.retention.processing_status
            for x in sm.get_recent_npc_observations(sid, ob_package, context=npc)
        } == {"completed"}
        advance(sm, sid, ob_package, context, 99)
        assert len(sm.get_recent_npc_observations(sid, ob_package, context=npc)) == len(ids)
        advance(sm, sid, ob_package, context, 100)
        assert sm.read_observations(sid, ob_package, context=npc) == ()
        assert {
            x.retention_status
            for x in sm.get_npc_observation_retention(sid, ob_package, context=npc)
        } == {"expired"}
        assert sm.get_pending_npc_observation_work(sid, ob_package, context=npc) == ()
        before = stored_rows(db)
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, NPCEvaluationResult(), ob_package, context=npc
            )
            == receipt
        )
        assert stored_rows(db) == before


def test_expired_pending_running_task_and_projection_gap_are_preserved(
    tmp_path, ob_package, context
):
    db = tmp_path / "retention.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        task = create_task(sm, sid, ob_package, npc, ids)
        original = stored_rows(db)
        advance(sm, sid, ob_package, context, 10000)
        assert sm.get_recent_npc_observations(sid, ob_package, context=npc) == ()
        works = sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
        assert {x.observation_id for x in works} == set(ids)
        assert {x.retention_status for x in works} == {"expired"}
        assert sm.get_npc_evaluation_task(task.evaluation_id, ob_package, context=npc).task == task
        for table in (
            "event_observations",
            "npc_observation_work",
            "npc_inbox",
            "npc_states",
            "npc_evaluation_tasks",
            "npc_private_records",
            "rng_states",
        ):
            assert stored_rows(db)[table] == original[table]
        sm._conn.execute(
            # Inject unknown historical membership instead of mutating a cached prefix.
            "INSERT INTO perception_event_barriers VALUES (?,1,'test_unknown_scope')",
            (sid,),
        )
        assert sm.get_npc_observation_retention(sid, ob_package, context=npc) == ()


def test_multi_source_history_archives_without_text_reinjection(tmp_path, ob_package, context):
    db = tmp_path / "retention.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        assert len(ids) >= 2
        task = create_task(sm, sid, ob_package, npc, ids)
        result = full_result(sm, sid, ob_package, npc, ids)
        sources = tuple(SourceReference("observation", oid, None) for oid in ids[:2])
        result = replace(
            result, memory_updates=(replace(result.memory_updates[0], sources=sources),)
        )
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, result, ob_package, context=npc
        )
        original = stored_rows(db)
        advance(sm, sid, ob_package, context, 100)
        retention = sm.get_npc_observation_retention(sid, ob_package, context=npc)
        assert {x.observation_id for x in retention if x.archived} == set(ids[:2])
        assert all(x.retention_status == "expired" for x in retention)
        assert sm.read_observations(sid, ob_package, context=npc) == ()
        for table in (
            "npc_record_sources",
            "npc_private_records",
            "observation_signal_sources",
            "npc_evaluation_receipts",
            "npc_inbox",
            "event_observations",
        ):
            assert stored_rows(db)[table] == original[table]
        assert sm.get_npc_evaluation_receipt(task.evaluation_id, ob_package, context=npc) == receipt
        assert (
            len(sm.get_npc_view(sid, npc.observer_id, ob_package, context=npc).belief_history) == 3
        )
        with pytest.raises(sqlite3.IntegrityError):
            sm._conn.execute("DELETE FROM event_observations WHERE observation_id=?", (ids[0],))


def test_reprojection_no_ttl_reset_and_cross_npc_session_authorization(
    tmp_path, ob_package, context
):
    db = tmp_path / "retention.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        other_sid, other, _ = prepare(sm, ob_package, context, "other")
        advance(sm, sid, ob_package, context, 50)
        before = stored_rows(db)
        for event in sm.read_events(sid, ob_package, context=context):
            sm.project_event(
                sid, event.event_seq, ob_package, context=context, judgment=synthetic_notice
            )
        assert stored_rows(db)["npc_observation_work"] == before["npc_observation_work"]
        assert set(ids).isdisjoint(
            x.observation.observation_id
            for x in sm.get_recent_npc_observations(other_sid, ob_package, context=other)
        )
        attendant = NPCContext("agent:npc.attendant", sid, "npc.attendant")
        assert set(ids).isdisjoint(
            x.observation.observation_id
            for x in sm.get_recent_npc_observations(sid, ob_package, context=attendant)
        )
        for method in (
            sm.read_observations,
            sm.get_recent_npc_observations,
            sm.get_pending_npc_observation_work,
            sm.get_npc_observation_retention,
        ):
            for bound in (replace(npc, principal_id="spoof"), other):
                with pytest.raises(StateMachineError, match="context|grant"):
                    method(sid, ob_package, context=bound)


@pytest.mark.parametrize("table", ["world_states", "command_receipts"])
def test_time_transaction_failure_leaves_recent_and_pending_unchanged(
    tmp_path, ob_package, context, table
):
    db = tmp_path / "retention.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE {'UPDATE' if table == 'world_states' else 'INSERT'} "
            f"ON {table} BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            tick(sm, sid, ob_package, replace(context, can_advance_time=True), 100)
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before
        assert sm.get_recent_npc_observations(sid, ob_package, context=npc)


def test_expiry_and_receipt_recovery_in_fresh_process(tmp_path, ob_package, context):
    db = tmp_path / "retention.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        command = tick_command(sm, sid, ob_package, replace(context, can_advance_time=True), 100)
        receipt = sm.submit_command(
            command, ob_package, context=replace(context, can_advance_time=True)
        )
        expected = [
            asdict(x) for x in sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
        ]
    before = stored_rows(db)
    code = """
import json,sys
from dataclasses import asdict
from trpg_state_machine import StateMachine,load_adventure_package
from trpg_state_machine.perception import NPCContext
p=load_adventure_package(sys.argv[2]).package
with StateMachine(sys.argv[1]) as sm:
 c=NPCContext('agent:npc.caretaker',sys.argv[3],'npc.caretaker')
 assert not sm.read_observations(sys.argv[3],p,context=c)
 works=sm.get_pending_npc_observation_work(sys.argv[3],p,context=c)
 print(json.dumps([asdict(x) for x in works]))
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(db), str(tmp_path / "evaluation.yaml"), sid],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == expected
    with StateMachine(db) as sm:
        assert (
            sm.submit_command(command, ob_package, context=replace(context, can_advance_time=True))
            == receipt
        )
    assert stored_rows(db) == before


def test_projection_adoption_failure_atomic_retry(tmp_path, ob_package, context):
    db = tmp_path / "retention.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        new_observation(sm, sid, ob_package, context)
        # A real new Event remains unprojected until the injected lifecycle write succeeds.
        current = sm.read_session(sid, ob_package, context=context)
        sm._apply(
            sid,
            ob_package,
            MoveActor("npc.caretaker", "scene.platform"),
            expected_world_version=current.world_version,
            context=context,
        )
        event = sm.read_events(sid, ob_package, context=context)[-1]
        before = stored_rows(db)
        sm._conn.execute(
            "CREATE TRIGGER inject BEFORE UPDATE ON npc_observation_work "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.project_event(
                sid, event.event_seq, ob_package, context=context, judgment=synthetic_notice
            )
        assert error.value.code == "storage_error"
        after = stored_rows(db)
        assert {k: v for k, v in after.items() if k != "event_outbox"} == {
            k: v for k, v in before.items() if k != "event_outbox"
        }
        # The pre-existing projection attempt counter commits before projection;
        # the failed second transaction must leave its done flag/cursor unchanged.
        expected = {
            (r[0], r[1], r[2], r[3], r[4] + 1, r[5])
            if r[:3] == (sid, event.event_seq, "perception")
            else r
            for r in before["event_outbox"][1]
        }
        assert after["event_outbox"][1] == expected
        sm._conn.execute("DROP TRIGGER inject")
        assert (
            sm.project_event(
                sid, event.event_seq, ob_package, context=context, judgment=synthetic_notice
            )
            == "resolved"
        )
        assert sm.get_pending_npc_observation_work(sid, ob_package, context=npc)


def test_actual_schema15_upgrade_unknown_times_concurrent_and_repeat(tmp_path, ob_package, context):
    source = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "491e98c", "src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(source)
    db = tmp_path / "old.db"
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/_ob_legacy_worker.py"),
            str(source / "src"),
            str(db),
            str(tmp_path / "evaluation.yaml"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    sid = process.stdout.strip()
    before = stored_rows(db)
    barrier = Barrier(3)

    def upgrade(_):
        barrier.wait()
        with StateMachine(db) as sm:
            return sm._conn.execute("PRAGMA user_version").fetchone()[0]

    with ThreadPoolExecutor(3) as pool:
        assert list(pool.map(upgrade, range(3))) == [36] * 3
    assert_preserved(db, before)
    npc = NPCContext("agent:npc.caretaker", sid, "npc.caretaker")
    with StateMachine(db) as sm:
        assert sm.read_observations(sid, ob_package, context=npc) == ()
        unknown = sm.get_npc_observation_retention(sid, ob_package, context=npc)
        assert unknown and {x.retention_status for x in unknown} == {"legacy_unknown"}
        assert all(x.accepted_game_tick is x.expires_game_tick is None for x in unknown)
        enter(sm, sid, ob_package, context, "scene.workshop")
        enter(sm, sid, ob_package, context, "scene.platform")
        new_observation(sm, sid, ob_package, context)
        assert sm.read_observations(sid, ob_package, context=npc)
    after = stored_rows(db)
    with StateMachine(db):
        pass
    assert stored_rows(db) == after
