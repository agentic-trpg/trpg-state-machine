"""Five-kind Meta atomicity, provenance, selected Result and durable handoff."""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier

import pytest
from test_migrations import stored_rows
from test_npc_evaluation_tasks import create_task, prepare
from test_npc_evaluation_tasks import evaluation_package as evaluation_package

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.npc_results import NPCActionIntent, NPCDialogue, NPCEvaluationResult
from trpg_state_machine.npc_state import SourceReference


def full_result(sm, sid, package, npc, ids):
    view = sm.get_npc_view(sid, npc.observer_id, package, context=npc)
    sources = (SourceReference("observation", ids[0], None),)
    memory = replace(view.memories[0], version=2, text="I heard the chime.", sources=sources)
    belief = replace(
        view.belief_history[-1],
        version=3,
        text="I suspect the lamp is wooden.",
        confidence_basis_points=4000,
        status="active",
        sources=sources,
    )
    relationship = replace(
        view.relationships[-1], version=2, text="The visitor seems friendly.", sources=sources
    )
    goal = replace(view.goals[0], version=2, text="Inspect the sound.", sources=sources)
    plan = replace(view.plans[0], version=2, goal_version=2, sources=sources)
    return NPCEvaluationResult(None, (memory,), (belief,), (relationship,), (goal,), (plan,), ())


def authority_rows(db):
    return {k: v for k, v in stored_rows(db).items() if k != "npc_evaluation_tasks"}


def test_five_meta_types_history_sources_world_isolation_and_replay(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "meta.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = full_result(sm, sid, evaluation_package, npc, ids)
        before = stored_rows(db)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, result, evaluation_package, context=npc
        )
        assert receipt.status == "completed" and receipt.npc_state_version == 2
        after = stored_rows(db)
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, result, evaluation_package, context=npc
            )
            == receipt
        )
        assert stored_rows(db) == after
        view = sm.get_npc_view(sid, npc.observer_id, evaluation_package, context=npc)
        assert [(b.version, b.is_current) for b in view.belief_history] == [
            (1, False),
            (2, False),
            (3, True),
        ]
        assert view.belief_history[-1].sources == result.belief_updates[0].sources
        assert len(view.memories) == 2 and len(view.goals) == 2 and len(view.plans) == 2
        assert sm._conn.execute("SELECT COUNT(*) FROM npc_record_evaluations").fetchone()[0] == 5
        for table in (
            "sessions",
            "world_facts",
            "rng_states",
            "events",
            "event_outbox",
            "command_receipts",
        ):
            assert after[table] == before[table]
        assert (
            sm._conn.execute("SELECT DISTINCT status FROM npc_observation_work").fetchone()[0]
            == "completed"
        )
        assert (
            sm.get_npc_evaluation_receipt(task.evaluation_id, evaluation_package, context=npc)
            == receipt
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
            )
        assert error.value.code == "idempotency_conflict"
    with StateMachine(db) as sm:
        assert (
            sm.get_npc_evaluation_receipt(task.evaluation_id, evaluation_package, context=npc)
            == receipt
        )
        assert sm.get_npc_view(sid, npc.observer_id, evaluation_package, context=npc) == view


@pytest.mark.parametrize(
    "field,change",
    [
        ("memory_updates", {"version": 3}),
        ("memory_updates", {"epistemic_status": "imagined"}),
        (
            "memory_updates",
            {"sources": (SourceReference("observation", "observation.fake", None),)},
        ),
        ("memory_updates", {"sources": (SourceReference("memory", "memory.attendant_only", 1),)}),
        ("memory_updates", {"sources": (SourceReference("package", "npc.attendant", None),)}),
        ("memory_updates", {"sources": (SourceReference("memory", "memory.backstory", True),)}),
        ("belief_updates", {"confidence_basis_points": True}),
        ("belief_updates", {"confidence_basis_points": 10001}),
        ("belief_updates", {"is_current": False}),
        ("relationship_updates", {"target_id": "npc.foreign"}),
        ("relationship_updates", {"stance": "unknown"}),
        ("goal_updates", {"status": "unknown"}),
        ("plan_updates", {"goal_id": "goal.attendant_only"}),
        ("plan_updates", {"steps": ["bad container"]}),
        ("plan_updates", {"text": "x" * 4097}),
    ],
)
def test_any_invalid_meta_writes_none_then_correctable(
    tmp_path, evaluation_package, context, field, change
):
    db = tmp_path / "meta.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = full_result(sm, sid, evaluation_package, npc, ids)
        invalid = replace(result, **{field: (replace(getattr(result, field)[0], **change),)})
        before = authority_rows(db)
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id, invalid, evaluation_package, context=npc
            )
        assert error.value.code == "invalid_npc_result"
        assert authority_rows(db) == before
        current = sm.get_npc_evaluation_task(
            task.evaluation_id, evaluation_package, context=npc
        ).task
        assert current.status == "running" and current.attempts == 1
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, result, evaluation_package, context=npc
            ).npc_state_version
            == 2
        )


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"dialogue": None, "principal_id": "spoof"},
        NPCEvaluationResult(memory_updates=[]),
        NPCEvaluationResult(memory_updates=("fake",)),
    ],
)
def test_closed_seven_field_type(tmp_path, evaluation_package, context, result):
    with StateMachine(tmp_path / "meta.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id, result, evaluation_package, context=npc
            )
        assert error.value.code == "invalid_npc_result"
        assert (
            sm.get_npc_evaluation_receipt(task.evaluation_id, evaluation_package, context=npc)
            is None
        )


@pytest.mark.parametrize(
    "table,operation",
    [
        ("npc_private_records", "INSERT"),
        ("npc_private_records", "UPDATE"),
        ("npc_record_sources", "INSERT"),
        ("npc_plan_goals", "INSERT"),
        ("npc_record_evaluations", "INSERT"),
        ("npc_states", "UPDATE"),
        ("npc_output_handoffs", "INSERT"),
        ("npc_evaluation_receipts", "INSERT"),
        ("npc_evaluation_tasks", "UPDATE"),
    ],
)
def test_sql_failure_rolls_back_selected_result_meta_outputs_and_version(
    tmp_path, evaluation_package, context, table, operation
):
    db = tmp_path / "meta.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = replace(
            full_result(sm, sid, evaluation_package, npc, ids),
            action_intents=(NPCActionIntent("move_actor", "scene.workshop", 3),),
        )
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id, result, evaluation_package, context=npc
            )
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER inject")
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, result, evaluation_package, context=npc
            ).status
            == "completed"
        )


def test_empty_result_completes_without_changing_npc_version(tmp_path, evaluation_package, context):
    db = tmp_path / "meta.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        before = stored_rows(db)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
        )
        assert receipt.npc_state_version == 1 and receipt.outputs == ()
        assert stored_rows(db)["npc_private_records"] == before["npc_private_records"]


def test_invalid_actions_independent_meta_and_outputs_are_not_execution(
    tmp_path, evaluation_package, context
):
    with StateMachine(tmp_path / "meta.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = replace(
            full_result(sm, sid, evaluation_package, npc, ids),
            dialogue=NPCDialogue("I heard a chime.", (), "private", ids),
            action_intents=(
                NPCActionIntent("attack_everyone", "scene.workshop", 3),
                NPCActionIntent("move_actor", "scene.workshop", 3),
            ),
        )
        before = sm.read_session(sid, evaluation_package, context=context)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, result, evaluation_package, context=npc
        )
        assert receipt.npc_state_version == 2
        assert [o.status for o in receipt.outputs] == ["pending", "rejected", "pending"]
        assert receipt.outputs[-1].command_id != task.evaluation_id
        assert sm.read_session(sid, evaluation_package, context=context) == before
        assert sm._conn.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 3


def test_stale_cancelled_revoked_results_and_receipt_access(tmp_path, evaluation_package, context):
    with StateMachine(tmp_path / "meta.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        sm._conn.execute(
            "UPDATE npc_states SET npc_state_version=2 WHERE session_id=? AND npc_id=?",
            (sid, npc.observer_id),
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
            )
        assert error.value.code == "npc_version_conflict"
        sm._conn.execute(
            "UPDATE npc_evaluation_tasks SET status='cancelled' WHERE evaluation_id=?",
            (task.evaluation_id,),
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                task.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
            )
        assert error.value.code == "evaluation_inactive"
        sm.grant_npc_access(
            sid, npc.observer_id, "replacement", evaluation_package, context=context
        )
        for call in (
            lambda: sm.get_npc_evaluation_receipt(
                task.evaluation_id, evaluation_package, context=npc
            ),
            lambda: sm.submit_npc_evaluation_result(
                task.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
            ),
        ):
            with pytest.raises(StateMachineError) as error:
                call()
            assert error.value.code == "unauthorized"


def test_concurrent_same_result_one_meta_and_fresh_process_receipt(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "meta.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = full_result(sm, sid, evaluation_package, npc, ids)
    barrier = Barrier(4)

    def submit(_):
        with StateMachine(db) as sm:
            barrier.wait()
            return sm.submit_npc_evaluation_result(
                task.evaluation_id, result, evaluation_package, context=npc
            )

    with ThreadPoolExecutor(4) as pool:
        receipts = list(pool.map(submit, range(4)))
    assert receipts == [receipts[0]] * 4
    code = """
import json,sys
from dataclasses import asdict
from trpg_state_machine import StateMachine,load_adventure_package
from trpg_state_machine.perception import NPCContext
p=load_adventure_package(sys.argv[2]).package
with StateMachine(sys.argv[1]) as sm:
 c=NPCContext('agent:npc.caretaker',sys.argv[3],'npc.caretaker')
 print(json.dumps(asdict(sm.get_npc_evaluation_receipt(sys.argv[4],p,context=c))))
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
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == json.loads(json.dumps(asdict(receipts[0])))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM npc_evaluation_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM npc_record_evaluations").fetchone()[0] == 5
