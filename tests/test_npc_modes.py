"""Database-serialized scheduling, Gate, cancellation and bounded invocation failures."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier

import pytest
from perception_support import synthetic_notice
from test_migrations import stored_rows
from test_npc_evaluation_tasks import create_task, prepare
from test_npc_evaluation_tasks import evaluation_package as evaluation_package
from test_npc_results import full_result

from trpg_state_machine import MoveActor, StateMachine, StateMachineError
from trpg_state_machine.npc_results import NPCEvaluationResult


def new_observation(sm, sid, package, context, destination="scene.workshop"):
    current = sm.read_session(sid, package, context=context)
    sm._apply(
        sid,
        package,
        MoveActor("npc.caretaker", destination),
        expected_world_version=current.world_version,
        context=context,
    )
    for event in sm.read_events(sid, package, context=context):
        assert sm.project_event(
            sid, event.event_seq, package, context=context, judgment=synthetic_notice
        ) in (
            "resolved",
            "already_complete",
        )


@pytest.mark.parametrize("classification", ["safe_retention", "no_op"])
def test_inactive_gate_auditable_no_meta_no_duplicate_or_new_evidence_loss(
    tmp_path, evaluation_package, context, classification
):
    db = tmp_path / "mode.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        before = stored_rows(db)
        assert sm.get_npc_mode(sid, evaluation_package, context=npc) == "inactive"
        routed = sm.route_npc_observations(
            sid, evaluation_package, context=npc, request_id="gate", classification=classification
        )
        assert routed.status == classification and routed.task is None
        assert len(routed.observation_work_ids) == len(ids)
        assert sm._conn.execute("SELECT COUNT(*) FROM npc_gate_decisions").fetchone()[0] == len(ids)
        for table in (
            "npc_private_records",
            "npc_states",
            "world_facts",
            "rng_states",
            "npc_evaluation_tasks",
        ):
            assert stored_rows(db)[table] == before[table]
        new_observation(sm, sid, evaluation_package, context)
        after = stored_rows(db)
        assert (
            sm.route_npc_observations(
                sid,
                evaluation_package,
                context=npc,
                request_id="gate",
                classification=classification,
            )
            == routed
        )
        assert stored_rows(db) == after
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM npc_observation_work WHERE status='pending'"
            ).fetchone()[0]
            > 0
        )


def test_gate_uncertain_invalid_unauthorized_and_projection_gap_preserve_work(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "mode.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        route = sm.route_npc_observations(
            sid, evaluation_package, context=npc, request_id="uncertain"
        )
        assert route.status == "deferred"
        assert not sm._conn.execute("SELECT 1 FROM npc_gate_decisions").fetchone()
        for classification, bound in (
            ("false", npc),
            ("background", replace(npc, principal_id="spoof")),
        ):
            before = stored_rows(db)
            with pytest.raises(StateMachineError):
                sm.route_npc_observations(
                    sid,
                    evaluation_package,
                    context=bound,
                    request_id="bad",
                    classification=classification,
                )
            assert stored_rows(db) == before
        sm._conn.execute(
            "UPDATE npc_perception_cursors SET event_seq=0 WHERE session_id=? AND npc_id=?",
            (sid, npc.observer_id),
        )
        routed = sm.route_npc_observations(
            sid, evaluation_package, context=npc, request_id="gap", classification="background"
        )
        assert routed.status == "no_visible_work" and routed.task is None
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM npc_observation_work WHERE status='pending'"
            ).fetchone()[0]
            > 0
        )


def test_background_requires_gate_and_interactive_idle_excludes_background(
    tmp_path, evaluation_package, context
):
    with StateMachine(tmp_path / "mode.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        with pytest.raises(StateMachineError) as error:
            create_task(sm, sid, evaluation_package, npc, ids, mode="background")
        assert error.value.code == "npc_mode_conflict"
        gate = sm.route_npc_observations(
            sid, evaluation_package, context=npc, request_id="bg", classification="background"
        )
        assert gate.task.mode == "background"
        changed = sm.request_npc_interaction(
            sid, "Talk to me.", evaluation_package, context=npc, request_id="interact"
        )
        assert changed.task.source_observation_ids == ids
        assert (
            sm.get_npc_evaluation_task(
                gate.task.evaluation_id, evaluation_package, context=npc
            ).task.status
            == "cancelled"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                gate.task.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
            )
        assert error.value.code == "evaluation_inactive"
        sm.submit_npc_evaluation_result(
            changed.task.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
        )
        assert sm.get_npc_mode(sid, evaluation_package, context=npc) == "interactive"
        new_observation(sm, sid, evaluation_package, context)
        active = sm.route_npc_observations(
            sid, evaluation_package, context=npc, request_id="idle", classification="safe_retention"
        )
        assert active.task.mode == "interactive"
        assert not sm._conn.execute("SELECT 1 FROM npc_gate_decisions").fetchone()
        before_ids = active.task.source_observation_ids
        new_observation(sm, sid, evaluation_package, context, "scene.platform")
        queued = sm.route_npc_observations(
            sid, evaluation_package, context=npc, request_id="queued", classification="background"
        )
        assert queued.status == "queued" and queued.task is None
        assert (
            sm.get_npc_evaluation_task(
                active.task.evaluation_id, evaluation_package, context=npc
            ).task.source_observation_ids
            == before_ids
        )


def test_both_switches_idempotent_transfer_work_and_preserve_meta(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "mode.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        first = sm.request_npc_interaction(
            sid, "First input.", evaluation_package, context=npc, request_id="first"
        )
        result = full_result(sm, sid, evaluation_package, npc, ids)
        sm.submit_npc_evaluation_result(
            first.task.evaluation_id, result, evaluation_package, context=npc
        )
        committed = stored_rows(db)["npc_private_records"]
        new_observation(sm, sid, evaluation_package, context)
        changed = sm.set_npc_inactive(
            sid, evaluation_package, context=npc, request_id="inactive", evaluate_pending=True
        )
        assert changed.task.mode == "background" and changed.task.expected_npc_state_version == 2
        assert not set(changed.task.source_observation_ids) & set(ids)
        assert (
            sm.set_npc_inactive(
                sid, evaluation_package, context=npc, request_id="inactive", evaluate_pending=True
            )
            == changed
        )
        interactive = sm.request_npc_interaction(
            sid, "New input.", evaluation_package, context=npc, request_id="new"
        )
        assert interactive.task.source_observation_ids == changed.task.source_observation_ids
        assert (
            sm.request_npc_interaction(
                sid, "New input.", evaluation_package, context=npc, request_id="new"
            )
            == interactive
        )
        with pytest.raises(StateMachineError) as error:
            sm.request_npc_interaction(
                sid, "Changed input.", evaluation_package, context=npc, request_id="new"
            )
        assert error.value.code == "idempotency_conflict"
        assert stored_rows(db)["npc_private_records"] == committed
        inactive = sm.set_npc_inactive(sid, evaluation_package, context=npc, request_id="stop")
        assert inactive.task is None
        assert (
            sm.get_npc_evaluation_task(
                interactive.task.evaluation_id, evaluation_package, context=npc
            ).task.status
            == "cancelled"
        )


def test_invocation_failure_budget_replay_correction_and_cancelled_never_revived(
    tmp_path, evaluation_package, context
):
    with StateMachine(tmp_path / "mode.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        once = sm.report_npc_evaluation_failure(
            task.evaluation_id, "timeout", evaluation_package, context=npc, expected_attempts=0
        )
        assert once.status == "running" and once.attempts == 1
        assert (
            sm.report_npc_evaluation_failure(
                task.evaluation_id, "timeout", evaluation_package, context=npc, expected_attempts=0
            )
            == once
        )
        sm.submit_npc_evaluation_result(
            task.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
        )
        new = sm.request_npc_interaction(
            sid, "Another question.", evaluation_package, context=npc, request_id="new"
        ).task
        for attempt in range(3):
            failed = sm.report_npc_evaluation_failure(
                new.evaluation_id,
                "api_error",
                evaluation_package,
                context=npc,
                expected_attempts=attempt,
            )
        assert failed.status == "failed" and failed.attempts == 3
        with pytest.raises(StateMachineError):
            sm.submit_npc_evaluation_result(
                new.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
            )
        latest = sm.request_npc_interaction(
            sid, "Try later.", evaluation_package, context=npc, request_id="latest"
        ).task
        sm.set_npc_inactive(sid, evaluation_package, context=npc, request_id="leave")
        with pytest.raises(StateMachineError):
            sm.report_npc_evaluation_failure(
                latest.evaluation_id,
                "timeout",
                evaluation_package,
                context=npc,
                expected_attempts=0,
            )


@pytest.mark.parametrize(
    "table,operation",
    [
        ("npc_modes", "UPDATE"),
        ("npc_mode_requests", "INSERT"),
        ("npc_evaluation_tasks", "UPDATE"),
        ("npc_evaluation_observations", "INSERT"),
    ],
)
def test_switch_sql_failures_restore_old_task_mode_and_work(
    tmp_path, evaluation_package, context, table, operation
):
    db = tmp_path / "mode.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        sm.route_npc_observations(
            sid, evaluation_package, context=npc, request_id="bg", classification="background"
        )
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.request_npc_interaction(
                sid, "Hello.", evaluation_package, context=npc, request_id="switch"
            )
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before


@pytest.mark.parametrize(
    "table,operation",
    [
        ("npc_gate_decisions", "INSERT"),
        ("npc_observation_work", "UPDATE"),
        ("npc_routing_requests", "INSERT"),
    ],
)
def test_gate_sql_failure_preserves_evidence(
    tmp_path, evaluation_package, context, table, operation
):
    db = tmp_path / "mode.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.route_npc_observations(
                sid,
                evaluation_package,
                context=npc,
                request_id="gate",
                classification="safe_retention",
            )
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before


@pytest.mark.parametrize("iteration", range(4))
def test_result_commit_switch_race_has_one_serialized_outcome(
    tmp_path, evaluation_package, context, iteration
):
    db = tmp_path / "mode.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        old = sm.route_npc_observations(
            sid, evaluation_package, context=npc, request_id="bg", classification="background"
        ).task
        result = full_result(sm, sid, evaluation_package, npc, ids)
    barrier = Barrier(2)

    def submit():
        with StateMachine(db) as sm:
            barrier.wait()
            try:
                return sm.submit_npc_evaluation_result(
                    old.evaluation_id, result, evaluation_package, context=npc
                )
            except StateMachineError as error:
                return error.code

    def switch():
        with StateMachine(db) as sm:
            barrier.wait()
            return sm.request_npc_interaction(
                sid, "Now.", evaluation_package, context=npc, request_id="switch"
            )

    with ThreadPoolExecutor(2) as pool:
        submitted = pool.submit(submit)
        switched = pool.submit(switch)
        receipt, change = submitted.result(), switched.result()
    with StateMachine(db) as sm:
        saved = sm.get_npc_evaluation_task(old.evaluation_id, evaluation_package, context=npc)
        assert saved.task.status in ("completed", "cancelled")
        assert saved.npc_meta.npc_state_version == (2 if saved.task.status == "completed" else 1)
        assert change.task.expected_npc_state_version == saved.npc_meta.npc_state_version
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM npc_evaluation_tasks WHERE status='running'"
            ).fetchone()[0]
            == 1
        )
        assert (receipt == "evaluation_inactive") == (saved.task.status == "cancelled")


def test_mode_and_task_recovery_fresh_process(tmp_path, evaluation_package, context):
    db = tmp_path / "mode.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        change = sm.request_npc_interaction(
            sid, "Recover this input.", evaluation_package, context=npc, request_id="switch"
        )
    code = """
import json,sys
from dataclasses import asdict
from trpg_state_machine import StateMachine,load_adventure_package
from trpg_state_machine.perception import NPCContext
p=load_adventure_package(sys.argv[2]).package
with StateMachine(sys.argv[1]) as sm:
 c=NPCContext('agent:npc.caretaker',sys.argv[3],'npc.caretaker')
 print(json.dumps([sm.get_npc_mode(sys.argv[3],p,context=c),
  asdict(sm.get_npc_evaluation_task(sys.argv[4],p,context=c).task)]))
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(db),
            str(tmp_path / "evaluation.yaml"),
            sid,
            change.task.evaluation_id,
        ],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == [
        "interactive",
        json.loads(json.dumps(asdict(change.task))),
    ]
