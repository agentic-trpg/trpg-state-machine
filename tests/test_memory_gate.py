"""Host-only Gate candidate boundary, exact scope, durable deadlines and concurrency."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from test_migrations import stored_rows
from test_npc_evaluation_tasks import create_task, prepare
from test_npc_evaluation_tasks import evaluation_package as evaluation_package
from test_npc_modes import new_observation
from test_observation_retention import advance
from test_observation_retention import ob_package as ob_package

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.npc_modes import MemoryGateJudgment
from trpg_state_machine.perception import JudgmentResult, NPCContext


def classify(sm, sid, package, npc, classification, key="gate", scope=None):
    data = sm.get_npc_memory_gate_input(
        sid, package, context=npc, request_id=key, observation_work_ids=scope
    )
    judgments = tuple(MemoryGateJudgment(x.work_id, classification) for x in data.observations)
    result = sm.route_npc_observations(
        sid, package, context=npc, request_id=key, judgments=judgments
    )
    return data, judgments, result


@pytest.mark.parametrize("classification", ["urgent", "deferred", "irrelevant", "uncertain"])
def test_four_classifications_processing_recent_deadlines_and_idempotency(
    tmp_path, ob_package, context, classification
):
    db = tmp_path / "gate.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        before = stored_rows(db)
        data, judgments, result = classify(sm, sid, ob_package, npc, classification)
        assert data.schema_version == "npc-memory-gate/2" and data.npc_id == npc.observer_id
        assert data.mode == "inactive" and result.status == "classified" and result.task is None
        assert {x.observation.observation_id for x in data.observations} == set(ids)
        assert all(x.latest_start_game_tick + 10 < x.expires_game_tick for x in data.observations)
        pending = sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
        assert len(pending) == (0 if classification == "irrelevant" else len(ids))
        assert len(sm.read_observations(sid, ob_package, context=npc)) == len(ids)
        due = sm.get_npc_evaluation_eligibility(sid, ob_package, context=npc)
        assert len(due) == (len(ids) if classification == "urgent" else 0)
        after = stored_rows(db)
        assert sm.get_npc_memory_gate_input(sid, ob_package, context=npc, request_id="gate") == data
        assert (
            sm.route_npc_observations(
                sid,
                ob_package,
                context=npc,
                request_id="gate",
                judgments=tuple(reversed(judgments)),
            )
            == result
        )
        assert stored_rows(db) == after
        for table in (
            "npc_private_records",
            "npc_states",
            "rng_states",
            "event_observations",
            "npc_evaluation_tasks",
            "world_states",
        ):
            assert after[table] == before[table]
        changed = (
            replace(
                judgments[0],
                classification="uncertain" if classification != "uncertain" else "urgent",
            ),
            *judgments[1:],
        )
        with pytest.raises(StateMachineError) as error:
            sm.route_npc_observations(
                sid, ob_package, context=npc, request_id="gate", judgments=changed
            )
        assert error.value.code == "idempotency_conflict"


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "unknown", "bad_class", "mapping", "perception"]
)
def test_invalid_gate_batch_never_defaults_to_irrelevant(tmp_path, ob_package, context, fault):
    db = tmp_path / "gate.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        data = sm.get_npc_memory_gate_input(sid, ob_package, context=npc, request_id="gate")
        judgments = tuple(MemoryGateJudgment(x.work_id, "irrelevant") for x in data.observations)
        broken = {
            "missing": judgments[:-1],
            "duplicate": (judgments[0],) * len(judgments),
            "unknown": (replace(judgments[0], work_id="work.unknown"), *judgments[1:]),
            "bad_class": (replace(judgments[0], classification=False), *judgments[1:]),
            "mapping": tuple(
                {"work_id": x.work_id, "classification": "irrelevant"} for x in judgments
            ),
            "perception": JudgmentResult(()),
        }[fault]
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as error:
            sm.route_npc_observations(
                sid, ob_package, context=npc, request_id="gate", judgments=broken
            )
        assert error.value.code == "invalid_gate" and stored_rows(db) == before
        assert sm.get_pending_npc_observation_work(sid, ob_package, context=npc)


def test_mixed_scope_and_legacy_completed_semantics_remain_distinct(tmp_path, ob_package, context):
    with StateMachine(tmp_path / "gate.db") as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        works = sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
        scope = tuple(x.work_id for x in works[:2])
        data = sm.get_npc_memory_gate_input(
            sid, ob_package, context=npc, request_id="mixed", observation_work_ids=scope
        )
        answers = (
            MemoryGateJudgment(data.observations[0].work_id, "urgent"),
            MemoryGateJudgment(data.observations[1].work_id, "irrelevant"),
        )
        sm.route_npc_observations(
            sid, ob_package, context=npc, request_id="mixed", judgments=answers
        )
        pending = {
            x.work_id for x in sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
        }
        assert answers[0].work_id in pending and answers[1].work_id not in pending
        # Legacy input stays explicit: old safe_retention/no_op semantics are not renamed.
        old = sm.route_npc_observations(
            sid, ob_package, context=npc, request_id="legacy", classification="safe_retention"
        )
        assert old.status == "safe_retention"
        assert not sm.get_pending_npc_observation_work(sid, ob_package, context=npc)
        assert sm.read_observations(sid, ob_package, context=npc)
        with pytest.raises(StateMachineError):
            sm.get_npc_memory_gate_input(sid, ob_package, context=npc, request_id="legacy")


@pytest.mark.parametrize("late", ["expired", "completed", "occupied"])
def test_late_candidate_revalidates_current_work(tmp_path, ob_package, context, late):
    db = tmp_path / "gate.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, ob_package, context)
        data = sm.get_npc_memory_gate_input(sid, ob_package, context=npc, request_id="late")
        if late == "expired":
            advance(sm, sid, ob_package, context, 100)
            with pytest.raises(StateMachineError) as error:
                sm.get_npc_memory_gate_input(sid, ob_package, context=npc, request_id="late")
            assert error.value.code == "stale_gate"
        elif late == "completed":
            sm.route_npc_observations(
                sid, ob_package, context=npc, request_id="noop", classification="no_op"
            )
        else:
            create_task(sm, sid, ob_package, npc, ids)
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as error:
            sm.route_npc_observations(
                sid,
                ob_package,
                context=npc,
                request_id="late",
                judgments=tuple(
                    MemoryGateJudgment(x.work_id, "irrelevant") for x in data.observations
                ),
            )
        assert error.value.code == "stale_gate" and stored_rows(db) == before


@pytest.mark.parametrize("scope", [[], (), ("work.x", "work.x"), (False,), ("\ud800",)])
def test_invalid_gate_input_scope_has_no_write(tmp_path, ob_package, context, scope):
    db = tmp_path / "gate.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as error:
            sm.get_npc_memory_gate_input(
                sid, ob_package, context=npc, request_id="scope", observation_work_ids=scope
            )
        assert error.value.code == "invalid_gate" and stored_rows(db) == before


def test_foreign_scope_changed_scope_and_revoked_authorization(tmp_path, ob_package, context):
    db = tmp_path / "gate.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        _, other, _ = prepare(sm, ob_package, context, "other")
        attendant = NPCContext("agent:npc.attendant", sid, "npc.attendant")
        data, judgments, _ = classify(sm, sid, ob_package, npc, "uncertain")
        wid = data.observations[0].work_id
        for bound_sid, bound in ((sid, attendant), (other.session_id, other)):
            before = stored_rows(db)
            with pytest.raises(StateMachineError):
                sm.get_npc_memory_gate_input(
                    bound_sid,
                    ob_package,
                    context=bound,
                    request_id="foreign",
                    observation_work_ids=(wid,),
                )
            assert stored_rows(db) == before
        with pytest.raises(StateMachineError) as error:
            sm.get_npc_memory_gate_input(
                sid, ob_package, context=npc, request_id="gate", observation_work_ids=(wid,)
            )
        assert error.value.code == "idempotency_conflict"
        sm.grant_npc_access(sid, npc.observer_id, "replacement", ob_package, context=context)
        before = stored_rows(db)
        for call in (
            lambda: sm.get_npc_memory_gate_input(sid, ob_package, context=npc, request_id="gate"),
            lambda: sm.get_npc_evaluation_eligibility(sid, ob_package, context=npc),
            lambda: sm.route_npc_observations(
                sid, ob_package, context=npc, request_id="gate", judgments=judgments
            ),
        ):
            with pytest.raises(StateMachineError) as error:
                call()
            assert error.value.code == "unauthorized"
        assert stored_rows(db) == before


def test_periodic_maximum_wait_no_batch_reset_new_ob_and_restart(tmp_path, ob_package, context):
    db = tmp_path / "gate.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        first, _, _ = classify(sm, sid, ob_package, npc, "deferred")
        advance(sm, sid, ob_package, context, 19)
        assert sm.get_npc_evaluation_eligibility(sid, ob_package, context=npc) == ()
        new_observation(sm, sid, ob_package, context)
        classify(sm, sid, ob_package, npc, "uncertain", "combined")
        advance(sm, sid, ob_package, context, 20)
        due = sm.get_npc_evaluation_eligibility(sid, ob_package, context=npc)
        assert {x.work_id for x in due} == {x.work_id for x in first.observations}
        assert {x.reason for x in due} == {"periodic"}
        assert {x.latest_start_game_tick for x in due} == {60}
        advance(sm, sid, ob_package, context, 60)
        due = sm.get_npc_evaluation_eligibility(sid, ob_package, context=npc)
        assert {x.reason for x in due if x.latest_start_game_tick == 60} == {"maximum_wait"}
        assert [x.latest_start_game_tick for x in due] == sorted(
            x.latest_start_game_tick for x in due
        )
    before = stored_rows(db)
    with StateMachine(db) as sm:
        assert sm.get_npc_evaluation_eligibility(sid, ob_package, context=npc) == due
        with pytest.raises(sqlite3.IntegrityError):
            sm._conn.execute("UPDATE npc_observation_work SET latest_start_game_tick=90")
    assert stored_rows(db) == before


@pytest.mark.parametrize(
    "table", ["npc_observation_work", "npc_gate_decisions", "npc_routing_requests"]
)
def test_classification_sql_failure_atomic_retriable(tmp_path, ob_package, context, table):
    db = tmp_path / "gate.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        data = sm.get_npc_memory_gate_input(sid, ob_package, context=npc, request_id="gate")
        answers = tuple(MemoryGateJudgment(x.work_id, "irrelevant") for x in data.observations)
        before = stored_rows(db)
        operation = "INSERT" if table == "npc_gate_decisions" else "UPDATE"
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.route_npc_observations(
                sid, ob_package, context=npc, request_id="gate", judgments=answers
            )
        assert error.value.code == "storage_error" and stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER inject")
        assert (
            sm.route_npc_observations(
                sid, ob_package, context=npc, request_id="gate", judgments=answers
            ).status
            == "classified"
        )


def test_concurrent_same_candidate_one_durable_noop(tmp_path, ob_package, context):
    db = tmp_path / "gate.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, ob_package, context)
        data = sm.get_npc_memory_gate_input(sid, ob_package, context=npc, request_id="gate")
        answers = tuple(MemoryGateJudgment(x.work_id, "irrelevant") for x in data.observations)
    barrier = Barrier(3)

    def submit(_):
        with StateMachine(db) as sm:
            barrier.wait()
            return sm.route_npc_observations(
                sid, ob_package, context=npc, request_id="gate", judgments=answers
            )

    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(submit, range(3)))
    assert results == [results[0]] * 3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM npc_gate_decisions").fetchone()[0] == len(answers)
        assert conn.execute("SELECT count(*) FROM npc_evaluation_tasks").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
