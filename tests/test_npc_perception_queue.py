"""Real SQLite queues, independent NPC ordering, routing and historical runtimes."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest
import yaml
from dnd_support import create
from perception_support import synthetic_notice
from test_dnd_runtime import attack, setup
from test_event_local_evidence import GATE, N1, N2, A, B, command, enter, evidence
from test_events import event_data
from test_migrations import stored_rows
from test_npc_state import SOURCE
from test_observation_retention import advance

from trpg_state_machine import (
    ObjectState,
    SetObjectState,
    SetSceneFlag,
    StateMachine,
    StateMachineError,
)
from trpg_state_machine.npc_modes import MemoryGateJudgment
from trpg_state_machine.npc_results import NPCEvaluationResult
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import JudgmentResult, NPCContext, SignalJudgment

ROOT = Path(__file__).resolve().parents[1]


def negative(request):
    return JudgmentResult(tuple(SignalJudgment(s.signal_id, False) for s in request.signals))


@pytest.fixture
def queue_data():
    data = yaml.safe_load(SOURCE.read_text("utf-8"))
    data["scenes"][0]["actor_ids"].append(N2)
    data["scenes"][1]["actor_ids"] = []
    data["actors"][1]["initial_scene_id"] = A
    return data


def prepared(tmp_path, data, context):
    package = _validate(data)
    db = tmp_path / "queue.db"
    sm = StateMachine(db)
    sid = create(sm, package, context)
    contexts = tuple(NPCContext("worker:" + npc, sid, npc) for npc in (N1, N2))
    for npc in contexts:
        if sm._conn.execute("SELECT 1 FROM actors WHERE actor_id=?", (npc.observer_id,)).fetchone():
            sm.grant_npc_access(sid, npc.observer_id, npc.principal_id, package, context=context)
    for event in sm.read_events(sid, package, context=context):
        assert (
            sm.project_event(
                sid, event.event_seq, package, context=context, judgment=synthetic_notice
            )
            == "resolved"
        )
    for npc in contexts:
        if sm._conn.execute(
            "SELECT 1 FROM npc_access WHERE observer_id=?", (npc.observer_id,)
        ).fetchone():
            sm.route_npc_observations(
                sid, package, context=npc, request_id="initial", classification="no_op"
            )
    return sm, db, sid, package, contexts


def change(sm, sid, package, context, state, key):
    cmd = command(
        sm, sid, package, context, SetObjectState(A, GATE, state), "set_object_state", key
    )
    receipt = sm.submit_command(cmd, package, context=context)
    assert receipt.status == "committed" and len(receipt.event_seqs) == 1
    return receipt.event_seqs[0], cmd, receipt


def test_independent_positive_unresolved_gate_eva_and_completion(tmp_path, queue_data, context):
    sm, db, sid, package, (one, two) = prepared(tmp_path, queue_data, context)
    with sm:
        seq, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "open")
        assert sm.project_npc_event(sid, seq, package, context=two) == "unresolved"
        assert (
            sm.project_npc_event(sid, seq, package, context=one, judgment=synthetic_notice)
            == "resolved"
        )
        assert sm.perception_cursor(sid, package, context=context) == seq - 1
        assert sm.npc_perception_cursor(sid, package, context=one) == seq
        assert sm.npc_perception_cursor(sid, package, context=two) == seq - 1
        assert sm.get_pending_npc_observation_work(sid, package, context=two) == ()
        work = sm.get_pending_npc_observation_work(sid, package, context=one)
        assert len(work) == 1 and work[0].processing_status == "pending"
        gate = sm.get_npc_memory_gate_input(sid, package, context=one, request_id="gate")
        assert tuple(o.work_id for o in gate.observations) == (work[0].work_id,)
        with pytest.raises(StateMachineError) as error:
            sm.get_npc_memory_gate_input(sid, package, context=two, request_id="empty")
        assert error.value.code == "invalid_gate"
        sm.route_npc_observations(
            sid,
            package,
            context=one,
            request_id="gate",
            judgments=(MemoryGateJudgment(work[0].work_id, "urgent"),),
        )
        task = sm.route_npc_observations(
            sid, package, context=one, request_id="eva", classification="eligible"
        ).task
        assert task and task.source_observation_ids == (work[0].observation_id,)
        data = sm.get_npc_evaluation_task(task.evaluation_id, package, context=one)
        assert all(o.observer_id == N1 for o in data.observations)
        assert all(o.observation.observer_id == N1 for o in data.recent_observations)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, NPCEvaluationResult(), package, context=one
        )
        assert sm.npc_completed_cursor(sid, package, context=one) == seq
        assert sm.npc_completed_cursor(sid, package, context=two) == seq - 1
        assert (
            sm.get_recent_npc_observations(sid, package, context=one)[
                -1
            ].retention.processing_status
            == "completed"
        )
        # Global audit is neither NPC projection proof nor its query fence.
        sm._conn.execute("UPDATE projection_cursors SET event_seq=0 WHERE consumer='perception'")
        assert (
            sm.read_observations(sid, package, context=one)[-1].observation_id
            == work[0].observation_id
        )
        snapshot = stored_rows(db)
        assert (
            sm.project_npc_event(
                sid, seq, package, context=one, judgment=lambda _: pytest.fail("Selected judgment")
            )
            == "already_complete"
        )
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, NPCEvaluationResult(), package, context=one
            )
            == receipt
        )
        assert stored_rows(db) == snapshot
        assert sm.project_npc_event(sid, seq, package, context=two, judgment=negative) == "resolved"
        assert sm.perception_cursor(sid, package, context=context) == seq
        assert sm.npc_perception_cursor(sid, package, context=two) == seq
        assert sm.get_pending_npc_observation_work(sid, package, context=two) == ()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT npc_id,complete FROM npc_perception_queue WHERE event_seq=? ORDER BY npc_id",
            (seq,),
        ).fetchall() == [(N2, 1), (N1, 1)]
        assert conn.execute(
            "SELECT observer_id FROM event_observations WHERE event_seq=?", (seq,)
        ).fetchall() == [(N1,)]


def test_same_npc_out_of_order_hidden_work_restart_then_negative_releases(
    tmp_path, queue_data, context
):
    sm, db, sid, package, (one, two) = prepared(tmp_path, queue_data, context)
    with sm:
        first, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "first")
        later, cmd, receipt = change(sm, sid, package, context, ObjectState.CLOSED, "later")
        assert (
            sm.project_npc_event(sid, later, package, context=one, judgment=synthetic_notice)
            == "resolved"
        )
        assert sm.project_npc_event(sid, first, package, context=one) == "unresolved"
        assert sm.npc_perception_cursor(sid, package, context=one) == first - 1
        assert sm.get_pending_npc_observation_work(sid, package, context=one) == ()
        oid = sm._conn.execute(
            "SELECT observation_id FROM event_observations WHERE event_seq=? AND observer_id=?",
            (later, N1),
        ).fetchone()[0]
        with pytest.raises(StateMachineError) as error:
            sm.create_npc_evaluation_task(
                sid,
                package,
                context=one,
                request_id="hidden",
                mode="interactive",
                reason="observation",
                expected_npc_state_version=1,
                source_observation_ids=(oid,),
            )
        assert error.value.code == "invalid_observation"
        assert (
            sm.route_npc_observations(
                sid, package, context=one, request_id="hidden-route", classification="background"
            ).status
            == "no_visible_work"
        )
        assert [e.event_seq for e in sm.get_npc_perception_queue(sid, package, context=one)] == [
            2,
            first,
            later,
        ]
        saved = sm.save_session(sid, package, context=context)
    with StateMachine(db) as sm:
        assert sm.resume_session(sid, package, context=context) == saved
        assert sm.project_npc_event(sid, later, package, context=one) == "already_complete"
        assert (
            sm.project_npc_event(sid, first, package, context=one, judgment=negative) == "resolved"
        )
        assert sm.npc_perception_cursor(sid, package, context=one) == later
        assert sm.npc_perception_cursor(sid, package, context=two) == first - 1
        assert sm.read_observations(sid, package, context=one)[-1].observation_id == oid
        assert (
            sm.get_pending_npc_observation_work(sid, package, context=one)[0].observation_id == oid
        )
        before = stored_rows(db)
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert stored_rows(db) == before


def test_multisignal_single_queue_and_observation_per_npc(tmp_path, queue_data, context):
    definition = event_data()
    other = deepcopy(definition["signals"][0])
    other.update(signal_id="signal.visual", channel="visual", content="A flash.")
    definition["signals"].append(other)
    queue_data["events"] = [definition]
    queue_data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    sm, db, sid, package, (one, two) = prepared(tmp_path, queue_data, context)
    with sm:
        cmd = command(
            sm,
            sid,
            package,
            context,
            SetSceneFlag(A, "flag.lamp_checked", True),
            "set_scene_flag",
            "fire",
        )
        result = sm.submit_command(cmd, package, context=context)
        seq = result.event_seqs[-1]
        for npc in (one, two):
            queue = sm.get_npc_perception_queue(sid, package, context=npc)
            assert queue[-1].event_seq == seq and queue[-1].signal_ids == (
                "signal.chime",
                "signal.visual",
            )
            assert not queue[-1].complete
            assert (
                sm.project_npc_event(sid, seq, package, context=npc, judgment=synthetic_notice)
                == "resolved"
            )
        with sqlite3.connect(db) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM npc_perception_queue WHERE event_seq=?", (seq,)
                ).fetchone()[0]
                == 2
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM event_records WHERE event_seq=?", (seq,)
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM event_evidence WHERE event_seq=?", (seq,)
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM event_observations WHERE event_seq=?", (seq,)
                ).fetchone()[0]
                == 2
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM observation_signal_sources "
                    "WHERE signal_id IN ('signal.chime','signal.visual')"
                ).fetchone()[0]
                == 4
            )


def test_non_candidate_scene_can_advance_past_other_npc_gap(tmp_path, queue_data, context):
    queue_data["scenes"][0]["actor_ids"] = [N1]
    queue_data["scenes"][1]["actor_ids"] = [N2]
    queue_data["actors"][1]["initial_scene_id"] = B
    sm, _, sid, package, (one, _) = prepared(tmp_path, queue_data, context)
    with sm:
        enter(sm, sid, package, context, B, "cross")
        two = NPCContext("worker:" + N2, sid, N2)
        sm.grant_npc_access(sid, N2, two.principal_id, package, context=context)
        assert (
            sm.project_npc_event(sid, 4, package, context=two, judgment=synthetic_notice)
            == "resolved"
        )
        assert sm.perception_cursor(sid, package, context=context) == 2
        assert sm.npc_perception_cursor(sid, package, context=two) == 4
        assert sm.npc_perception_cursor(sid, package, context=one) == 2
        assert (
            sm.read_observations(sid, package, context=two)[0].perceived_signals[0].content
            == "Someone enters the scene."
        )
        assert sm.project_npc_event(sid, 3, package, context=two) == "not_candidate"
        assert [e.event_seq for e in sm.get_npc_perception_queue(sid, package, context=two)] == [4]


def test_zero_signal_has_no_queue_and_does_not_hold_npc_cursor(tmp_path, queue_data, context):
    sm, _, sid, package, (one, two) = prepared(tmp_path, queue_data, context)
    with sm:
        cmd = command(
            sm,
            sid,
            package,
            context,
            SetSceneFlag(A, "flag.lamp_checked", True),
            "set_scene_flag",
            "flag",
        )
        seq = sm.submit_command(cmd, package, context=context).event_seqs[0]
        assert (
            sm._conn.execute(
                "SELECT 1 FROM npc_perception_queue WHERE event_seq=?", (seq,)
            ).fetchone()
            is None
        )
        assert sm._conn.execute("SELECT count(*) FROM perception_event_barriers").fetchone()[0] == 0
        for npc in (one, two):
            assert sm.npc_perception_cursor(sid, package, context=npc) == seq
            assert sm.project_npc_event(sid, seq, package, context=npc) == "not_candidate"
        assert sm.perception_cursor(sid, package, context=context) == seq - 1
        assert sm.project_event(sid, seq, package, context=context) == "resolved"


def test_unknown_engine_phase_is_durable_quarantine_not_scope_exclusion(
    tmp_path, package_data, context
):
    sm, sid, package, _ = setup(tmp_path, package_data, context)
    db = tmp_path / "dnd.db"
    with sm:
        sm.grant_npc_access(sid, N1, "worker", package, context=context)
        npc = NPCContext("worker", sid, N1)
        cmd = attack(sm, sid, package, context)
        receipt = sm.submit_command(cmd, package, context=context)
        seq = receipt.event_seqs[0]
        assert (
            sm._conn.execute(
                "SELECT reason FROM perception_event_barriers WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == "engine.intermediate_phase_unavailable"
        )
        assert (
            sm._conn.execute(
                "SELECT 1 FROM npc_perception_queue WHERE event_seq=?", (seq,)
            ).fetchone()
            is None
        )
        sample = evidence(db, sid, seq)
    with StateMachine(db) as sm:

        def deny_current(action, table, *args):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ
                and table in ("actors", "mechanical_actors", "mechanical_scenes")
                else sqlite3.SQLITE_OK
            )

        sm._conn.set_authorizer(deny_current)
        assert (
            sm.project_npc_event(
                sid,
                seq,
                package,
                context=npc,
                judgment=lambda _: pytest.fail("Missing mechanical history"),
            )
            == "unresolved"
        )
        sm._conn.set_authorizer(None)
        assert sm.npc_perception_cursor(sid, package, context=npc) < seq
        assert evidence(db, sid, seq) == sample
        before = stored_rows(db)
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert stored_rows(db) == before


@pytest.mark.parametrize("table", ["npc_perception_queue", "perception_event_barriers"])
def test_event_queue_capture_failure_rolls_back_whole_command(tmp_path, queue_data, context, table):
    if table == "perception_event_barriers":
        definition = event_data()
        definition["signals"][0]["scope"]["scene_id"] = B
        queue_data["events"] = [definition]
        queue_data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    sm, db, sid, package, _ = prepared(tmp_path, queue_data, context)
    with sm:
        cmd = (
            command(
                sm,
                sid,
                package,
                context,
                SetSceneFlag(A, "flag.lamp_checked", True),
                "set_scene_flag",
                "fire",
            )
            if table == "perception_event_barriers"
            else command(
                sm,
                sid,
                package,
                context,
                SetObjectState(A, GATE, ObjectState.OPEN),
                "set_object_state",
                "open",
            )
        )
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TEMP TRIGGER inject BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(cmd, package, context=context)
        assert error.value.code == "storage_error" and stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER inject")
        assert sm.submit_command(cmd, package, context=context).status == "committed"


@pytest.mark.parametrize(
    "table,action",
    [
        ("npc_perception_queue", "UPDATE"),
        ("npc_inbox", "INSERT"),
        ("npc_observation_work", "INSERT"),
        ("event_outbox", "UPDATE"),
    ],
)
def test_projection_failure_cannot_leave_partial_npc_completion(
    tmp_path, queue_data, context, table, action
):
    sm, db, sid, package, (one, _) = prepared(tmp_path, queue_data, context)
    with sm:
        seq, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "open")
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TEMP TRIGGER inject BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.project_npc_event(sid, seq, package, context=one, judgment=synthetic_notice)
        assert error.value.code == "storage_error"
        after = stored_rows(db)
        assert {k: v for k, v in before.items() if k != "event_outbox"} == {
            k: v for k, v in after.items() if k != "event_outbox"
        }
        assert (
            sm._conn.execute(
                "SELECT complete FROM npc_perception_queue WHERE npc_id=? AND event_seq=?",
                (N1, seq),
            ).fetchone()[0]
            == 0
        )
        sm._conn.execute("DROP TRIGGER inject")
        assert (
            sm.project_npc_event(sid, seq, package, context=one, judgment=synthetic_notice)
            == "resolved"
        )


def test_global_done_is_never_npc_completion_proof_and_auth_is_rechecked(
    tmp_path, queue_data, context
):
    sm, db, sid, package, (one, two) = prepared(tmp_path, queue_data, context)
    with sm:
        seq, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "open")
        sm._conn.execute(
            "UPDATE event_outbox SET done=1 WHERE event_seq=? AND consumer='perception'", (seq,)
        )
        assert sm.npc_perception_cursor(sid, package, context=one) == seq - 1
        assert sm.get_pending_npc_observation_work(sid, package, context=one) == ()
        assert not sm.get_npc_perception_queue(sid, package, context=one)[-1].complete
        assert sm.project_npc_event(sid, seq, package, context=one) == "unresolved"
        for caller in (
            context,
            replace(one, principal_id="spoof"),
            replace(one, observer_id=N2),
            replace(one, session_id="absent"),
        ):
            with pytest.raises(StateMachineError) as error:
                sm.project_npc_event(sid, seq, package, context=caller, judgment=synthetic_notice)
            assert error.value.code == "unauthorized"

        def revoke(request):
            sm.grant_npc_access(sid, N1, "replacement", package, context=context)
            return synthetic_notice(request)

        with pytest.raises(StateMachineError) as error:
            sm.project_npc_event(sid, seq, package, context=one, judgment=revoke)
        assert error.value.code == "unauthorized"
        assert (
            sm._conn.execute(
                "SELECT complete FROM npc_perception_queue WHERE npc_id=? AND event_seq=?",
                (N1, seq),
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(StateMachineError):
            sm.get_npc_perception_queue(sid, package, context=one)
        assert not sm.get_npc_perception_queue(sid, package, context=two)[-1].complete


def test_ttl_and_cognition_remain_independent_while_other_npc_unresolved(
    tmp_path, queue_data, context
):
    for actor in queue_data["actors"]:
        actor["npc_state"]["runtime"]["schedule"] = []
    sm, db, sid, package, (one, two) = prepared(tmp_path, queue_data, context)
    with sm:
        seq, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "open")
        sm.project_npc_event(sid, seq, package, context=one, judgment=synthetic_notice)
        oid = sm.get_pending_npc_observation_work(sid, package, context=one)[0].observation_id
        advance(sm, sid, package, context, 99)
        assert oid in {o.observation_id for o in sm.read_observations(sid, package, context=one)}
        advance(sm, sid, package, context, 100)
        assert sm.read_observations(sid, package, context=one) == ()
        work = sm.get_pending_npc_observation_work(sid, package, context=one)
        assert len(work) == 1 and work[0].retention_status == "expired"
        assert work[0].processing_status == "pending"
        assert sm.get_pending_npc_observation_work(sid, package, context=two) == ()
        expiry = sm.get_npc_observation_expiry(sid, package, context=one, observation_ids=(oid,))
        assert expiry[0].observation_id == oid
        assert sm.npc_completed_cursor(sid, package, context=one) == seq - 1
        assert sm.npc_perception_cursor(sid, package, context=one) > seq
        assert sm.npc_perception_cursor(sid, package, context=two) == seq - 1
        with pytest.raises(StateMachineError) as error:
            sm.get_npc_memory_gate_input(sid, package, context=one, request_id="expired")
        assert error.value.code == "invalid_gate"
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM npc_observation_work WHERE observation_id=?", (oid,)
            ).fetchone()[0]
            == 1
        )


def historical_save(tmp_path, data, reference):
    old = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", reference, "src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as files:
        files.extractall(old)
    fixture = tmp_path / "package.json"
    fixture.write_text(json.dumps(data), encoding="utf-8")
    db = tmp_path / "historical.db"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/_per3_legacy_worker.py"), str(db), str(fixture)],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(old / "src")),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return db, json.loads(result.stdout)


@pytest.mark.parametrize("reference", ["e88efa2", "5deea78"])
def test_real_schema18_upgrade_uses_own_proofs_preserves_all_old_rows_concurrent_restart(
    tmp_path, queue_data, context, reference
):
    db, (complete, partial) = historical_save(tmp_path, queue_data, reference)
    original = stored_rows(db)
    package = _validate(queue_data)
    rendezvous = Barrier(3)

    def upgrade(_):
        rendezvous.wait(timeout=10)
        with StateMachine(db) as sm:
            return sm._conn.execute("PRAGMA user_version").fetchone()[0]

    with ThreadPoolExecutor(3) as pool:
        assert list(pool.map(upgrade, range(3))) == [25] * 3
    for _ in range(2):
        with StateMachine(db) as sm:
            for sid in (complete, partial):
                npc = NPCContext("worker:" + N1, sid, N1)
                assert sm.npc_perception_cursor(sid, package, context=npc) == 3
                assert (
                    sm.project_npc_event(
                        sid,
                        3,
                        package,
                        context=npc,
                        judgment=lambda _: pytest.fail("Historical selected result"),
                    )
                    == "already_complete"
                )
                assert len(sm.read_observations(sid, package, context=npc)) == 2
                assert (
                    sm.get_pending_npc_observation_work(sid, package, context=npc)[
                        0
                    ].processing_status
                    == "pending"
                )
            other = NPCContext("worker:" + N2, partial, N2)
            assert sm.perception_cursor(partial, package, context=context) == 2
            assert sm.npc_perception_cursor(partial, package, context=other) == 2
            if reference == "5deea78":
                assert (
                    sm.project_npc_event(
                        partial,
                        3,
                        package,
                        context=other,
                        judgment=lambda _: pytest.fail("Unproven legacy phase"),
                    )
                    == "unresolved"
                )
                assert (
                    sm._conn.execute(
                        "SELECT reason FROM perception_event_barriers "
                        "WHERE session_id=? AND event_seq=3",
                        (partial,),
                    ).fetchone()[0]
                    == "legacy_candidates_or_stage_unknown"
                )
                assert (
                    sm.get_npc_perception_queue(partial, package, context=other)[-1].event_seq == 2
                )
            else:
                assert not sm.get_npc_perception_queue(partial, package, context=other)[-1].complete
            assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    after = stored_rows(db)
    from test_migrations import assert_original_rows_equal

    assert_original_rows_equal(original, after, excluded=("event_outbox",))
    if reference == "e88efa2":
        with StateMachine(db) as sm:
            assert (
                sm.project_npc_event(partial, 3, package, context=other, judgment=negative)
                == "resolved"
            )
            assert sm.npc_perception_cursor(partial, package, context=other) == 3
            assert sm.perception_cursor(partial, package, context=context) == 3


@pytest.mark.parametrize("phase", ["ddl", "backfill"])
def test_interrupted_schema19_migration_rolls_back_and_recovers(
    tmp_path, queue_data, monkeypatch, phase
):
    db, _ = historical_save(tmp_path, queue_data, "e88efa2")
    original = stored_rows(db)
    connect = sqlite3.connect

    class Interrupted(sqlite3.Connection):
        def execute(self, sql, *args):
            if (phase == "ddl" and "CREATE TABLE perception_event_barriers" in sql) or (
                phase == "backfill" and "INSERT INTO npc_perception_queue" in sql
            ):
                raise sqlite3.OperationalError("interrupted migration")
            return super().execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, factory=Interrupted, **k))
    with pytest.raises(StateMachineError) as error:
        StateMachine(db)
    assert error.value.code == "storage_error"
    monkeypatch.undo()
    assert stored_rows(db) == original
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
    with StateMachine(db) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 25
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_queue_identity_and_completion_constraints_are_real_sqlite(tmp_path, queue_data, context):
    sm, _, sid, package, (one, _) = prepared(tmp_path, queue_data, context)
    with sm:
        seq, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "open")
        for sql, args in (
            (
                "UPDATE npc_perception_queue SET complete=1 "
                "WHERE session_id=? AND npc_id=? AND event_seq=?",
                (sid, N1, seq),
            ),
            (
                "UPDATE npc_perception_queue SET signal_ids_json='[]' "
                "WHERE session_id=? AND npc_id=? AND event_seq=?",
                (sid, N1, seq),
            ),
            (
                "DELETE FROM npc_perception_queue WHERE session_id=? AND npc_id=? AND event_seq=?",
                (sid, N1, seq),
            ),
            ("INSERT INTO npc_perception_queue VALUES (?,?,?,'[]','discovery',0)", (sid, N1, seq)),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                sm._conn.execute(sql, args)
        assert not sm.get_npc_perception_queue(sid, package, context=one)[-1].complete


@pytest.mark.parametrize("same_npc", [False, True])
def test_concurrent_npc_workers_commit_once_and_aggregate_from_queue(
    tmp_path, queue_data, context, same_npc
):
    sm, db, sid, package, (one, two) = prepared(tmp_path, queue_data, context)
    with sm:
        seq, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "open")
    rendezvous = Barrier(2)

    def run(npc):
        with StateMachine(db) as worker:

            def judgment(request):
                rendezvous.wait(timeout=10)
                return synthetic_notice(request)

            return worker.project_npc_event(sid, seq, package, context=npc, judgment=judgment)

    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(run, (one, one if same_npc else two))) == ["resolved"] * 2
    with StateMachine(db) as sm:
        assert sm._conn.execute(
            "SELECT count(*) FROM event_observations WHERE event_seq=?", (seq,)
        ).fetchone()[0] == (1 if same_npc else 2)
        assert sm._conn.execute(
            "SELECT count(*) FROM npc_observation_work w "
            "JOIN event_observations o USING(observation_id) WHERE o.event_seq=?",
            (seq,),
        ).fetchone()[0] == (1 if same_npc else 2)
        assert sm.perception_cursor(sid, package, context=context) == (seq - 1 if same_npc else seq)
        assert sm.npc_perception_cursor(sid, package, context=one) == seq
        assert sm.npc_perception_cursor(sid, package, context=two) == (seq - 1 if same_npc else seq)


def test_cross_session_queue_cursor_and_worker_authorization(tmp_path, queue_data, context):
    sm, db, sid, package, (one, _) = prepared(tmp_path, queue_data, context)
    with sm:
        other = create(sm, package, context, "another-session")
        bound = NPCContext(one.principal_id, other, N1)
        sm.grant_npc_access(other, N1, one.principal_id, package, context=context)
        sm.project_event(other, 1, package, context=context)
        sm.project_npc_event(other, 2, package, context=bound, judgment=synthetic_notice)
        seq, _, _ = change(sm, sid, package, context, ObjectState.OPEN, "open")
        assert sm.project_npc_event(sid, seq, package, context=one) == "unresolved"
        with pytest.raises(StateMachineError) as error:
            sm.project_npc_event(sid, seq, package, context=bound, judgment=synthetic_notice)
        assert error.value.code == "unauthorized"
        assert sm.npc_perception_cursor(other, package, context=bound) == 2
        assert sm.get_npc_perception_queue(other, package, context=bound)[-1].event_seq == 2
        assert sm.get_npc_perception_queue(sid, package, context=one)[-1].event_seq == seq
        assert sm.read_observations(other, package, context=bound)[0].observer_id == N1
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM npc_perception_queue WHERE session_id=? AND event_seq=?",
                (other, seq),
            ).fetchone()[0]
            == 0
        )


def test_missing_scope_quarantine_covers_later_instantiated_npc(tmp_path, queue_data, context):
    queue_data["scenes"][0]["actor_ids"] = [N1]
    queue_data["scenes"][1]["actor_ids"] = [N2]
    queue_data["actors"][1]["initial_scene_id"] = B
    definition = event_data()
    definition["signals"][0]["scope"]["scene_id"] = B
    queue_data["events"] = [definition]
    queue_data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    sm, db, sid, package, (one, _) = prepared(tmp_path, queue_data, context)
    with sm:
        cmd = command(
            sm,
            sid,
            package,
            context,
            SetSceneFlag(A, "flag.lamp_checked", True),
            "set_scene_flag",
            "fire",
        )
        receipt = sm.submit_command(cmd, package, context=context)
        seq = receipt.event_seqs[-1]
        for prefix in receipt.event_seqs[:-1]:
            sm.project_event(sid, prefix, package, context=context, judgment=synthetic_notice)
        frozen = evidence(db, sid, seq)
        enter(sm, sid, package, context, B, "instantiate")
        two = NPCContext("worker:" + N2, sid, N2)
        sm.grant_npc_access(sid, N2, two.principal_id, package, context=context)
        last = sm.read_events(sid, package, context=context)[-1].event_seq
        sm.project_npc_event(sid, last, package, context=two, judgment=synthetic_notice)
        for npc in (one, two):
            assert sm.npc_perception_cursor(sid, package, context=npc) == seq - 1
            assert (
                sm.project_npc_event(
                    sid,
                    seq,
                    package,
                    context=npc,
                    judgment=lambda _: pytest.fail("Unknown historical scope"),
                )
                == "unresolved"
            )
        assert sm.read_observations(sid, package, context=two) == ()
    with StateMachine(db) as sm:
        assert evidence(db, sid, seq) == frozen
        assert (
            sm._conn.execute(
                "SELECT reason FROM perception_event_barriers WHERE event_seq=?", (seq,)
            ).fetchone()[0]
            == "missing_scope_scene"
        )
        assert sm.npc_perception_cursor(sid, package, context=two) == seq - 1


def test_zero_candidate_evidence_creates_no_queue_or_quarantine(tmp_path, package_data, context):
    package_data["actors"] = []
    for scene in package_data["scenes"]:
        scene["actor_ids"] = []
    package = _validate(package_data)
    db = tmp_path / "empty.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        assert evidence(db, sid, 2)["discovery"]["by_signal"] == {"signal.visible": []}
        assert sm._conn.execute("SELECT count(*) FROM npc_perception_queue").fetchone()[0] == 0
        assert sm._conn.execute("SELECT count(*) FROM perception_event_barriers").fetchone()[0] == 0
        for seq in (1, 2):
            assert (
                sm.project_event(
                    sid,
                    seq,
                    package,
                    context=context,
                    judgment=lambda _: pytest.fail("No candidates"),
                )
                == "resolved"
            )
        assert sm.perception_cursor(sid, package, context=context) == 2


def test_initial_session_queue_failure_has_no_partial_session_or_creation_receipt(
    tmp_path, queue_data, context
):
    db, package = tmp_path / "create.db", _validate(queue_data)
    with StateMachine(db) as sm:
        before = stored_rows(db)
        sm._conn.execute(
            "CREATE TEMP TRIGGER inject BEFORE INSERT ON npc_perception_queue "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            create(sm, package, context)
        assert error.value.code == "storage_error" and stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER inject")
        sid = create(sm, package, context)
        assert create(sm, package, context) == sid
        assert sm._conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert sm._conn.execute("SELECT count(*) FROM npc_perception_queue").fetchone()[0] == 2
