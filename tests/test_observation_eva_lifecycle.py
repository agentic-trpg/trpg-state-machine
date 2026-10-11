"""Mode-correct authorization, frozen Recent scope, explicit recovery and real commit."""

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
from test_memory_gate import classify
from test_migrations import assert_preserved, stored_rows
from test_npc_evaluation_tasks import create_task, prepare
from test_npc_evaluation_tasks import evaluation_package as evaluation_package
from test_npc_modes import new_observation
from test_npc_outputs import action_result
from test_npc_results import full_result
from test_observation_retention import advance
from test_observation_retention import ob_package as ob_package

from trpg_state_machine import ObjectState, SetObjectState, StateMachine, StateMachineError
from trpg_state_machine.npc_results import NPCEvaluationResult
from trpg_state_machine.perception import NPCContext

ROOT = Path(__file__).resolve().parents[1]


def eligible(sm, sid, package, npc, key="eligible"):
    return sm.route_npc_observations(
        sid, package, context=npc, request_id=key, classification="eligible"
    )


@pytest.mark.parametrize("mode", ["inactive", "interactive"])
def test_urgent_mode_routing_completed_recent_background_and_own_meta_sources(
    tmp_path, ob_package, context, mode
):
    db = tmp_path / "eva.db"
    with StateMachine(db) as sm:
        sid, npc, old_ids = prepare(sm, ob_package, context)
        classify(sm, sid, ob_package, npc, "irrelevant", "initial-noop")
        if mode == "interactive":
            first = sm.request_npc_interaction(
                sid, "Hello.", ob_package, context=npc, request_id="interaction"
            ).task
            sm.submit_npc_evaluation_result(
                first.evaluation_id, NPCEvaluationResult(), ob_package, context=npc
            )
        new_observation(sm, sid, ob_package, context)
        classify(sm, sid, ob_package, npc, "urgent", "new")
        routed = eligible(sm, sid, ob_package, npc)
        task = routed.task
        assert task and task.mode == ("background" if mode == "inactive" else "interactive")
        assert set(task.source_observation_ids).isdisjoint(old_ids)
        data = sm.get_npc_evaluation_task(task.evaluation_id, ob_package, context=npc)
        assert {x.observation.observation_id for x in data.recent_observations} == (
            set(old_ids) | set(task.source_observation_ids)
        )
        assert {o.observation_id for o in data.observations} == set(task.source_observation_ids)
        result = full_result(sm, sid, ob_package, npc, old_ids)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, result, ob_package, context=npc
        )
        assert receipt.status == "completed"
        assert (
            len(sm.get_npc_view(sid, npc.observer_id, ob_package, context=npc).belief_history) == 3
        )
        assert not sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
        assert eligible(sm, sid, ob_package, npc, "after").task is None
        before = stored_rows(db)
        assert eligible(sm, sid, ob_package, npc) == routed
        assert (
            sm.submit_npc_evaluation_result(task.evaluation_id, result, ob_package, context=npc)
            == receipt
        )
        assert stored_rows(db) == before


def test_deferred_periodic_batch_and_normal_interaction_without_mandatory_gate(
    tmp_path, ob_package, context
):
    with StateMachine(tmp_path / "eva.db") as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        classify(sm, sid, ob_package, npc, "deferred")
        assert eligible(sm, sid, ob_package, npc, "early").status == "no_eligible_work"
        advance(sm, sid, ob_package, context, 20)
        routed = eligible(sm, sid, ob_package, npc)
        assert routed.task.mode == "background" and routed.task.source_observation_ids == ids
        sm.submit_npc_evaluation_result(
            routed.task.evaluation_id, NPCEvaluationResult(), ob_package, context=npc
        )
        new_observation(sm, sid, ob_package, context)
        # Normal interaction can batch new Pending immediately, without a JEV.
        task = sm.request_npc_interaction(
            sid, "What happened?", ob_package, context=npc, request_id="talk"
        ).task
        assert task.mode == "interactive" and task.source_observation_ids
        assert not set(task.source_observation_ids) & set(ids)


def test_new_urgent_during_task_never_expands_input_or_completion_scope(
    tmp_path, ob_package, context
):
    db = tmp_path / "eva.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        classify(sm, sid, ob_package, npc, "urgent", "first")
        first = eligible(sm, sid, ob_package, npc).task
        original = sm.get_npc_evaluation_task(first.evaluation_id, ob_package, context=npc)
        world = sm.read_session(sid, ob_package, context=context)
        sm._apply(
            sid,
            ob_package,
            SetObjectState("scene.platform", "object.platform_gate", ObjectState.CLOSED),
            expected_world_version=world.world_version,
            context=context,
        )
        for event in sm.read_events(sid, ob_package, context=context):
            sm.project_event(sid, event.event_seq, ob_package, context=context)
        data, _, _ = classify(sm, sid, ob_package, npc, "urgent", "during")
        later_ids = {x.observation.observation_id for x in data.observations}
        assert later_ids.isdisjoint(ids)
        assert eligible(sm, sid, ob_package, npc, "busy").status == "queued"
        assert sm.get_npc_evaluation_task(first.evaluation_id, ob_package, context=npc) == original
        sm.submit_npc_evaluation_result(
            first.evaluation_id, NPCEvaluationResult(), ob_package, context=npc
        )
        pending = {
            x.observation_id
            for x in sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
        }
        assert pending == later_ids
        next_task = eligible(sm, sid, ob_package, npc, "next").task
        assert set(next_task.source_observation_ids) == later_ids
        with sqlite3.connect(db) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM npc_evaluation_tasks WHERE status='running'"
                ).fetchone()[0]
                == 1
            )


@pytest.mark.parametrize("source_fault", ["new_arrival", "foreign_npc", "expired_completed"])
def test_meta_source_scope_requires_task_grant_and_current_recent_validity(
    tmp_path, ob_package, context, source_fault
):
    db = tmp_path / "eva.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        classify(sm, sid, ob_package, npc, "irrelevant", "noop")
        task = create_task(sm, sid, ob_package, npc, (), key="background-only")
        if source_fault == "new_arrival":
            new_observation(sm, sid, ob_package, context)
            bad_id = sm.get_pending_npc_observation_work(sid, ob_package, context=npc)[
                0
            ].observation_id
        elif source_fault == "foreign_npc":
            new_observation(sm, sid, ob_package, context)
            other = NPCContext("agent:npc.attendant", sid, "npc.attendant")
            bad_id = sm.read_observations(sid, ob_package, context=other)[-1].observation_id
        else:
            advance(sm, sid, ob_package, context, 100)
            bad_id = ids[0]
        result = full_result(sm, sid, ob_package, npc, (bad_id,))
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(task.evaluation_id, result, ob_package, context=npc)
        assert error.value.code == "invalid_npc_result"
        after = stored_rows(db)
        assert {k: v for k, v in after.items() if k != "npc_evaluation_tasks"} == {
            k: v for k, v in before.items() if k != "npc_evaluation_tasks"
        }
        assert (
            sm.get_npc_evaluation_task(task.evaluation_id, ob_package, context=npc).task.attempts
            == 1
        )


def test_expiry_notification_recovery_late_result_receipt_and_no_context_effects(
    tmp_path, ob_package, context
):
    db = tmp_path / "eva.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        task = create_task(sm, sid, ob_package, npc, ids)
        assert not sm.get_npc_observation_expiry(sid, ob_package, context=npc, observation_ids=ids)
        advance(sm, sid, ob_package, context, 1000)
        before = stored_rows(db)
        expired = sm.get_npc_observation_expiry(sid, ob_package, context=npc, observation_ids=ids)
        assert {x.observation_id for x in expired} == set(ids)
        assert {x.retention_status for x in expired} == {"expired"}
        assert (
            sm.get_npc_observation_expiry(sid, ob_package, context=npc, observation_ids=ids)
            == expired
        )
        ordinary = sm.get_npc_evaluation_task(task.evaluation_id, ob_package, context=npc)
        assert (
            ordinary.task == task and not ordinary.observations and not ordinary.recent_observations
        )
        evidence = sm.get_npc_evaluation_recovery_input(task.evaluation_id, ob_package, context=npc)
        assert {x.observation.observation_id for x in evidence} == set(ids)
        assert all(
            x.retention.retention_status == "expired" and x.retention.accepted_game_tick == 0
            for x in evidence
        )
        assert stored_rows(db) == before
        result = full_result(sm, sid, ob_package, npc, ids)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, result, ob_package, context=npc
        )
        assert not sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
        assert any(
            x.archived for x in sm.get_npc_observation_retention(sid, ob_package, context=npc)
        )
        with pytest.raises(StateMachineError) as error:
            sm.get_npc_evaluation_recovery_input(task.evaluation_id, ob_package, context=npc)
        assert error.value.code == "evaluation_inactive"
    after = stored_rows(db)
    with StateMachine(db) as sm:
        assert sm.get_npc_evaluation_receipt(task.evaluation_id, ob_package, context=npc) == receipt
        assert (
            sm.submit_npc_evaluation_result(task.evaluation_id, result, ob_package, context=npc)
            == receipt
        )
        assert not sm.read_observations(sid, ob_package, context=npc)
    assert stored_rows(db) == after


def test_expired_pending_new_authorization_after_budget_failure_and_mode_switch(
    tmp_path, ob_package, context
):
    with StateMachine(tmp_path / "eva.db") as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        old = sm.request_npc_interaction(
            sid, "Start.", ob_package, context=npc, request_id="start"
        ).task
        for attempt in range(3):
            failed = sm.report_npc_evaluation_failure(
                old.evaluation_id,
                "context_budget",
                ob_package,
                context=npc,
                expected_attempts=attempt,
            )
        assert failed.status == "failed"
        advance(sm, sid, ob_package, context, 100)
        sm.set_npc_inactive(sid, ob_package, context=npc, request_id="inactive")
        task = eligible(sm, sid, ob_package, npc).task
        assert task.mode == "background" and task.source_observation_ids == ids
        assert sm.get_npc_evaluation_recovery_input(task.evaluation_id, ob_package, context=npc)
        changed = sm.request_npc_interaction(
            sid, "Recover safely.", ob_package, context=npc, request_id="switch"
        ).task
        with pytest.raises(StateMachineError):
            sm.get_npc_evaluation_recovery_input(task.evaluation_id, ob_package, context=npc)
        with pytest.raises(StateMachineError):
            sm.submit_npc_evaluation_result(
                task.evaluation_id, NPCEvaluationResult(), ob_package, context=npc
            )
        assert changed.mode == "interactive" and changed.source_observation_ids == ids
        sm.submit_npc_evaluation_result(
            changed.evaluation_id, NPCEvaluationResult(), ob_package, context=npc
        )
        assert not sm.get_pending_npc_observation_work(sid, ob_package, context=npc)


def test_expiry_and_recovery_authorization_foreign_scope_revocation(tmp_path, ob_package, context):
    db = tmp_path / "eva.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        other_sid, other, other_ids = prepare(sm, ob_package, context, "other")
        task = create_task(sm, sid, ob_package, npc, ids)
        advance(sm, sid, ob_package, context, 100)
        before = stored_rows(db)
        for call in (
            lambda: sm.get_npc_observation_expiry(
                sid, ob_package, context=npc, observation_ids=other_ids
            ),
            lambda: sm.get_npc_observation_expiry(other_sid, ob_package, context=npc),
            lambda: sm.get_npc_evaluation_recovery_input(
                task.evaluation_id, ob_package, context=other
            ),
        ):
            with pytest.raises(StateMachineError):
                call()
        assert stored_rows(db) == before
        sm.grant_npc_access(sid, npc.observer_id, "replacement", ob_package, context=context)
        for call in (
            lambda: sm.get_npc_evaluation_recovery_input(
                task.evaluation_id, ob_package, context=npc
            ),
            lambda: sm.get_npc_observation_expiry(sid, ob_package, context=npc),
            lambda: eligible(sm, sid, ob_package, npc),
        ):
            with pytest.raises(StateMachineError) as error:
                call()
            assert error.value.code == "unauthorized"


@pytest.mark.parametrize(
    "table", ["npc_evaluation_recent_scope", "npc_evaluation_observations", "npc_routing_requests"]
)
def test_authorization_sql_failure_rollback_retry(tmp_path, ob_package, context, table):
    db = tmp_path / "eva.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        classify(sm, sid, ob_package, npc, "urgent")
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            eligible(sm, sid, ob_package, npc)
        assert error.value.code == "storage_error" and stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER inject")
        assert eligible(sm, sid, ob_package, npc).task


@pytest.mark.parametrize(
    "table",
    [
        "npc_private_records",
        "npc_record_sources",
        "npc_output_handoffs",
        "npc_evaluation_receipts",
        "npc_observation_work",
    ],
)
def test_expired_recovery_result_faults_leave_all_authority_unchanged(
    tmp_path, ob_package, context, table
):
    db = tmp_path / "eva.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        task = create_task(sm, sid, ob_package, npc, ids)
        result = replace(
            full_result(sm, sid, ob_package, npc, ids),
            action_intents=action_result(sm, sid, ob_package, context).action_intents,
        )
        advance(sm, sid, ob_package, context, 100)
        before = stored_rows(db)
        operation = "UPDATE" if table == "npc_observation_work" else "INSERT"
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(task.evaluation_id, result, ob_package, context=npc)
        assert error.value.code == "storage_error" and stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER inject")
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, result, ob_package, context=npc
            ).status
            == "completed"
        )


@pytest.mark.parametrize("race", ["mode", "expiry"])
def test_domain_authorization_races_serialized_and_one_valid_task(
    tmp_path, ob_package, context, race
):
    db = tmp_path / "eva.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        classify(sm, sid, ob_package, npc, "urgent")
    barrier = Barrier(2)

    def route():
        with StateMachine(db) as sm:
            barrier.wait()
            return eligible(sm, sid, ob_package, npc)

    def competing():
        with StateMachine(db) as sm:
            barrier.wait()
            if race == "mode":
                return sm.request_npc_interaction(
                    sid, "Switch.", ob_package, context=npc, request_id="switch"
                )
            return advance(sm, sid, ob_package, context, 100)

    with ThreadPoolExecutor(2) as pool:
        one, two = pool.submit(route), pool.submit(competing)
        routed, _ = one.result(), two.result()
    with StateMachine(db) as sm:
        tasks = sm.list_npc_evaluation_tasks(sid, ob_package, context=npc)
        running = [t for t in tasks if t.status == "running"]
        assert len(running) == 1
        if race == "mode":
            assert running[0].mode == "interactive"
        else:
            assert routed.task.status == "running" and not sm.read_observations(
                sid, ob_package, context=npc
            )
            assert sm.get_npc_evaluation_recovery_input(
                running[0].evaluation_id, ob_package, context=npc
            )


def test_fresh_process_recovery_with_receipt_and_frozen_scope(tmp_path, ob_package, context):
    db = tmp_path / "eva.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        classify(sm, sid, ob_package, npc, "urgent")
        task = eligible(sm, sid, ob_package, npc).task
        advance(sm, sid, ob_package, context, 100)
        expected = [
            asdict(x)
            for x in sm.get_npc_evaluation_recovery_input(
                task.evaluation_id, ob_package, context=npc
            )
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
 task=sm.get_npc_evaluation_task(sys.argv[4],p,context=c)
 assert not task.recent_observations and not task.observations
 evidence=sm.get_npc_evaluation_recovery_input(sys.argv[4],p,context=c)
 print(json.dumps([asdict(x) for x in evidence]))
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(db),
            str(tmp_path / "evaluation.yaml"),
            sid,
            task.evaluation_id,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == json.loads(json.dumps(expected))
    assert stored_rows(db) == before
    with StateMachine(db) as sm:
        result = full_result(sm, sid, ob_package, npc, ids)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, result, ob_package, context=npc
        )
    after = stored_rows(db)
    with StateMachine(db) as sm:
        assert sm.get_npc_evaluation_receipt(task.evaluation_id, ob_package, context=npc) == receipt
        assert (
            sm.submit_npc_evaluation_result(task.evaluation_id, result, ob_package, context=npc)
            == receipt
        )
    assert stored_rows(db) == after


def test_actual_ob2_upgrade_preserves_gate_task_scope_and_recovers_old_work(
    tmp_path, ob_package, context
):
    source = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "e694975", "src"],
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
            str(ROOT / "tests/_ob_gate_legacy_worker.py"),
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
    sid, eid = json.loads(process.stdout)
    before = stored_rows(db)
    with StateMachine(db) as sm:
        assert_preserved(db, before)
        npc = NPCContext("agent:npc.caretaker", sid, "npc.caretaker")
        data = sm.get_npc_evaluation_task(eid, ob_package, context=npc)
        assert data.task.status == "running" and data.observations
        assert data.recent_observations == ()  # No invented grant to old Task.
        assert (
            sm._conn.execute("SELECT count(*) FROM npc_evaluation_recent_scope").fetchone()[0] == 0
        )
        advance(sm, sid, ob_package, context, 100)
        evidence = sm.get_npc_evaluation_recovery_input(eid, ob_package, context=npc)
        assert {x.observation.observation_id for x in evidence} == set(
            data.task.source_observation_ids
        )
        sm.submit_npc_evaluation_result(eid, NPCEvaluationResult(), ob_package, context=npc)
        assert not sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
