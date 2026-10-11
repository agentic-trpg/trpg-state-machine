"""Output/Command reconciliation and continuous cognition prefix in real SQLite."""

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
from test_migrations import assert_preserved, stored_rows
from test_npc_evaluation_tasks import create_task, prepare
from test_npc_evaluation_tasks import evaluation_package as evaluation_package
from test_npc_modes import new_observation
from test_npc_results import full_result
from test_npc_state import grant

from trpg_state_machine import MoveActor, StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.npc_results import NPCActionIntent, NPCDialogue, NPCEvaluationResult
from trpg_state_machine.npc_state import SourceReference

ROOT = Path(__file__).resolve().parents[1]


def action_result(sm, sid, package, context, **changes):
    version = sm.read_session(sid, package, context=context).world_version
    result = NPCEvaluationResult(
        action_intents=(NPCActionIntent("move_actor", "scene.workshop", version),)
    )
    return replace(result, **changes)


def worker(mode, db, source, sid, eid, ordinal=0):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/_npc_runtime_worker.py"),
            mode,
            str(db),
            str(source),
            sid,
            eid,
            str(ordinal),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize("mode", ["interactive", "background"])
def test_complete_manual_result_vertical_slice_and_fresh_process_resume(
    tmp_path, evaluation_package, context, mode
):
    db = tmp_path / "runtime.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        if mode == "interactive":
            task = sm.request_npc_interaction(
                sid, "What did you hear?", evaluation_package, context=npc, request_id="interaction"
            ).task
        else:
            task = sm.route_npc_observations(
                sid, evaluation_package, context=npc, request_id="gate", classification="background"
            ).task
        assert task.mode == mode and task.source_observation_ids == ids
        before = sm.npc_completed_cursor(sid, evaluation_package, context=npc)
        projected = sm.perception_cursor(sid, evaluation_package, context=context)
        sm.get_npc_evaluation_task(task.evaluation_id, evaluation_package, context=npc)
        sm.read_observations(sid, evaluation_package, context=npc)
        assert sm.npc_completed_cursor(sid, evaluation_package, context=npc) == before < projected
        selected = full_result(sm, sid, evaluation_package, npc, ids)
        command_candidate = action_result(sm, sid, evaluation_package, context)
        snapshot = sm.read_session(sid, evaluation_package, context=context)
        selected = replace(
            selected,
            dialogue=NPCDialogue(
                "I heard a chime. The lamp might be wooden.", (snapshot.pc_actor_id,), "local", ids
            ),
            action_intents=command_candidate.action_intents,
        )
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, selected, evaluation_package, context=npc
        )
        assert receipt.npc_state_version == 2 and len(receipt.outputs) == 2
        assert sm.npc_completed_cursor(sid, evaluation_package, context=npc) == projected
        assert all(
            o.status == "pending"
            for o in sm.get_npc_output_status(task.evaluation_id, evaluation_package, context=npc)
        )
        published = sm.dispatch_npc_output(
            task.evaluation_id, 0, evaluation_package, context=context
        )
        assert published.status == "published" and published.command_receipt is None
        assert (
            sm.read_session(sid, evaluation_package, context=context).world_version
            == snapshot.world_version
        )
        speech = sm.read_events(sid, evaluation_package, context=context)[-1]
        assert speech.event_type == "npc.dialogue_published"
        assert speech.signals[0].content == selected.dialogue.text
        assert (
            sm.dispatch_npc_output(task.evaluation_id, 0, evaluation_package, context=context)
            == published
        )
    # Execution uses a new interpreter and the original independent command identity.
    dispatched = worker("dispatch", db, tmp_path / "evaluation.yaml", sid, task.evaluation_id, 1)
    assert dispatched.returncode == 0, dispatched.stderr
    executed = json.loads(dispatched.stdout)
    assert executed["outputs"][1]["command_receipt"]["status"] == "committed"
    assert executed["outputs"][1]["command_id"] == receipt.outputs[1].command_id
    assert executed["snapshot"]["actors"]["npc.caretaker"]["current_scene_id"] == "scene.workshop"
    assert executed["snapshot"]["world_version"] == snapshot.world_version + 1
    assert executed["snapshot"]["rng"] == json.loads(json.dumps(asdict(snapshot.rng)))
    assert executed["receipt"] == json.loads(json.dumps(asdict(receipt)))
    resumed = worker("resume", db, tmp_path / "evaluation.yaml", sid, task.evaluation_id)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout) == executed
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM npc_evaluation_receipts").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM command_receipts WHERE command_id=?",
                (receipt.outputs[1].command_id,),
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM npc_record_evaluations").fetchone()[0] == 5
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_outbox WHERE event_seq=?",
                (published.published_event_seq,),
            ).fetchone()[0]
            == 2
        )


def test_process_exit_after_command_commit_reconciles_original_receipt_once(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "runtime.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = action_result(sm, sid, evaluation_package, context)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, result, evaluation_package, context=npc
        )
        before = sm.read_session(sid, evaluation_package, context=context)
    crashed = worker(
        "crash_after_command", db, tmp_path / "evaluation.yaml", sid, task.evaluation_id
    )
    assert crashed.returncode == 23, crashed.stderr
    with StateMachine(db) as sm:
        status = sm.get_npc_output_status(task.evaluation_id, evaluation_package, context=npc)[0]
        assert status.status == "pending" and status.command_receipt.status == "committed"
        after = sm.read_session(sid, evaluation_package, context=context)
        assert after.world_version == before.world_version + 1
        assert (
            sm.get_npc_evaluation_receipt(task.evaluation_id, evaluation_package, context=npc)
            == receipt
        )
        cursor = sm.npc_completed_cursor(sid, evaluation_package, context=npc)
        delivered = sm.dispatch_npc_output(
            task.evaluation_id, 0, evaluation_package, context=context
        )
        assert (
            delivered.status == "dispatched" and delivered.command_receipt == status.command_receipt
        )
        assert sm.read_session(sid, evaluation_package, context=context) == after
        assert sm.npc_completed_cursor(sid, evaluation_package, context=npc) == cursor


@pytest.mark.parametrize("kind", ["action", "dialogue"])
def test_concurrent_dispatch_uses_one_command_or_publication(
    tmp_path, evaluation_package, context, kind
):
    db = tmp_path / "runtime.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        pc = sm.read_session(sid, evaluation_package, context=context).pc_actor_id
        result = (
            action_result(sm, sid, evaluation_package, context)
            if kind == "action"
            else NPCEvaluationResult(dialogue=NPCDialogue("Hello.", (pc,), "local", ids))
        )
        sm.submit_npc_evaluation_result(task.evaluation_id, result, evaluation_package, context=npc)
        events = sm.read_events(sid, evaluation_package, context=context)
    barrier = Barrier(4)

    def dispatch(_):
        with StateMachine(db) as sm:
            barrier.wait()
            return sm.dispatch_npc_output(
                task.evaluation_id, 0, evaluation_package, context=context
            )

    with ThreadPoolExecutor(4) as pool:
        delivered = list(pool.map(dispatch, range(4)))
    assert delivered == [delivered[0]] * 4
    with StateMachine(db) as sm:
        assert len(sm.read_events(sid, evaluation_package, context=context)) == len(events) + 1


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("private", "private"),
        ("private_audience", "rejected"),
        ("remote", "rejected"),
        ("wrong_source", "rejected"),
        ("subset", "rejected"),
    ],
)
def test_dialogue_private_or_unauthorized_cannot_publish_world_fact(
    tmp_path, evaluation_package, context, variant, expected
):
    with StateMachine(tmp_path / "runtime.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        pc = sm.read_session(sid, evaluation_package, context=context).pc_actor_id
        dialogue = NPCDialogue("I opened the door.", (), "private", ids)
        if variant == "private_audience":
            dialogue = replace(dialogue, audience_ids=(pc,))
        if variant == "remote":
            dialogue = replace(dialogue, visibility="local", audience_ids=("npc.attendant",))
        if variant == "wrong_source":
            dialogue = replace(dialogue, source_observation_ids=("observation.fake",))
        if variant == "subset":
            dialogue = replace(dialogue, visibility="local", audience_ids=())
        result = replace(full_result(sm, sid, evaluation_package, npc, ids), dialogue=dialogue)
        before = sm.read_session(sid, evaluation_package, context=context)
        events = sm.read_events(sid, evaluation_package, context=context)
        sm.submit_npc_evaluation_result(task.evaluation_id, result, evaluation_package, context=npc)
        delivery = sm.dispatch_npc_output(
            task.evaluation_id, 0, evaluation_package, context=context
        )
        assert delivery.status == expected
        assert sm.read_events(sid, evaluation_package, context=context) == events
        assert sm.read_session(sid, evaluation_package, context=context) == before
        assert (
            sm.get_npc_view(sid, npc.observer_id, evaluation_package, context=npc).npc_state_version
            == 2
        )


def test_failed_action_independent_receipt_does_not_reopen_eva_or_work(
    tmp_path, evaluation_package, context
):
    with StateMachine(tmp_path / "runtime.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = replace(
            full_result(sm, sid, evaluation_package, npc, ids),
            action_intents=(NPCActionIntent("move_actor", "scene.workshop", 999),),
        )
        before = sm.read_session(sid, evaluation_package, context=context)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, result, evaluation_package, context=npc
        )
        cursor = sm.npc_completed_cursor(sid, evaluation_package, context=npc)
        output = sm.dispatch_npc_output(task.evaluation_id, 0, evaluation_package, context=context)
        assert output.status == "dispatched" and output.command_receipt.status == "conflict"
        assert output.command_receipt.error_code == "version_conflict"
        assert sm.read_session(sid, evaluation_package, context=context) == before
        assert sm.npc_completed_cursor(sid, evaluation_package, context=npc) == cursor
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, result, evaluation_package, context=npc
            )
            == receipt
        )
        assert (
            sm.dispatch_npc_output(task.evaluation_id, 0, evaluation_package, context=context)
            == output
        )


@pytest.mark.parametrize(
    "table", ["events", "event_evidence", "event_records", "event_outbox", "npc_output_handoffs"]
)
def test_dialogue_publication_sql_failure_rolls_back_event_outbox_and_delivery(
    tmp_path, evaluation_package, context, table
):
    db = tmp_path / "runtime.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        pc = sm.read_session(sid, evaluation_package, context=context).pc_actor_id
        result = NPCEvaluationResult(dialogue=NPCDialogue("A chime!", (pc,), "local", ids))
        sm.submit_npc_evaluation_result(task.evaluation_id, result, evaluation_package, context=npc)
        before = stored_rows(db)
        operation = "UPDATE" if table == "npc_output_handoffs" else "INSERT"
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.dispatch_npc_output(task.evaluation_id, 0, evaluation_package, context=context)
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER inject")
        assert (
            sm.dispatch_npc_output(
                task.evaluation_id, 0, evaluation_package, context=context
            ).status
            == "published"
        )


@pytest.mark.parametrize("table", ["npc_observation_work", "npc_completion_cursors"])
def test_completion_sql_failure_rolls_back_meta_result_and_output(
    tmp_path, evaluation_package, context, table
):
    db = tmp_path / "runtime.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = full_result(sm, sid, evaluation_package, npc, ids)
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_npc_evaluation_result(
                task.evaluation_id, result, evaluation_package, context=npc
            )
        assert stored_rows(db) == before


def test_real_projection_then_cognition_gaps_cannot_advance_continuous_cursor(
    tmp_path, evaluation_package, context
):
    with StateMachine(tmp_path / "runtime.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        sm.submit_npc_evaluation_result(
            task.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
        )
        prefix = sm.npc_completed_cursor(sid, evaluation_package, context=npc)
        first = sm.read_session(sid, evaluation_package, context=context)
        sm._apply(
            sid,
            evaluation_package,
            MoveActor(npc.observer_id, "scene.workshop"),
            expected_world_version=first.world_version,
            context=context,
        )
        second = sm.read_session(sid, evaluation_package, context=context)
        sm._apply(
            sid,
            evaluation_package,
            MoveActor(npc.observer_id, "scene.platform"),
            expected_world_version=second.world_version,
            context=context,
        )
        end = sm.read_events(sid, evaluation_package, context=context)[-1].event_seq
        sm.project_event(sid, end, evaluation_package, context=context)
        assert sm.perception_cursor(sid, evaluation_package, context=context) == prefix
        assert sm.npc_completed_cursor(sid, evaluation_package, context=npc) == prefix
        sm.project_event(sid, end - 1, evaluation_package, context=context)
        fresh = [
            o.observation_id
            for o in sm.read_observations(sid, evaluation_package, context=npc)
            if o.observation_id not in ids
        ]
        assert len(fresh) == 2
        later = create_task(sm, sid, evaluation_package, npc, (fresh[-1],), key="later")
        sm.submit_npc_evaluation_result(
            later.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
        )
        assert sm.npc_completed_cursor(sid, evaluation_package, context=npc) == prefix
        earlier = sm.route_npc_observations(
            sid, evaluation_package, context=npc, request_id="earlier"
        ).task
        assert earlier.source_observation_ids == (fresh[0],)
        sm.submit_npc_evaluation_result(
            earlier.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
        )
        assert sm.npc_completed_cursor(sid, evaluation_package, context=npc) == end


def test_no_npc_view_or_task_id_grants_command_control_and_revoke_is_rechecked(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "runtime.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        sm.submit_npc_evaluation_result(
            task.evaluation_id,
            action_result(sm, sid, evaluation_package, context),
            evaluation_package,
            context=npc,
        )
        for bound in (
            npc,
            replace(context, controlled_npc_ids=()),
            replace(context, principal_id="foreign"),
        ):
            before = stored_rows(db)
            with pytest.raises(StateMachineError):
                sm.dispatch_npc_output(task.evaluation_id, 0, evaluation_package, context=bound)
            assert stored_rows(db) == before
        sm.grant_npc_access(
            sid, npc.observer_id, "replacement", evaluation_package, context=context
        )
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as error:
            sm.dispatch_npc_output(task.evaluation_id, 0, evaluation_package, context=context)
        assert error.value.code == "unauthorized" and stored_rows(db) == before


def test_output_command_guard_rechecks_revocation_inside_command_transaction(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "runtime.db"

    class RevokingStateMachine(StateMachine):
        def submit_command(self, command, package, *, context):
            self.grant_npc_access(
                command.session_id, command.actor_id, "replacement", package, context=context
            )
            return super().submit_command(command, package, context=context)

    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        sm.submit_npc_evaluation_result(
            task.evaluation_id,
            action_result(sm, sid, evaluation_package, context),
            evaluation_package,
            context=npc,
        )
        before = sm.read_session(sid, evaluation_package, context=context)
    with RevokingStateMachine(db) as sm:
        with pytest.raises(StateMachineError) as error:
            sm.dispatch_npc_output(task.evaluation_id, 0, evaluation_package, context=context)
        assert error.value.code == "unauthorized"
        assert sm.read_session(sid, evaluation_package, context=context) == before
        assert sm._conn.execute("SELECT status FROM npc_output_handoffs").fetchone()[0] == "pending"


def test_handoff_command_identity_payload_pinned_and_sql_immutable(
    tmp_path, evaluation_package, context
):
    with StateMachine(tmp_path / "runtime.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id,
            action_result(sm, sid, evaluation_package, context),
            evaluation_package,
            context=npc,
        )
        with pytest.raises(sqlite3.IntegrityError):
            sm._conn.execute("UPDATE npc_output_handoffs SET payload_json='{}'")
        cmd = TypedCommand(
            sid,
            receipt.outputs[0].command_id,
            npc.observer_id,
            "move_actor",
            MoveActor(npc.observer_id, "scene.platform"),
            3,
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(cmd, evaluation_package, context=context)
        assert error.value.code == "idempotency_conflict"


def test_two_npcs_and_sessions_cannot_use_foreign_observation_or_memory(
    tmp_path, evaluation_package, context
):
    with StateMachine(tmp_path / "runtime.db") as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        new_observation(sm, sid, evaluation_package, context)
        attendant = grant(sm, sid, evaluation_package, context, "npc.attendant")
        other_ids = tuple(
            o.observation_id
            for o in sm.read_observations(sid, evaluation_package, context=attendant)
        )
        ids = tuple(
            o.observation_id for o in sm.read_observations(sid, evaluation_package, context=npc)
        )
        other_sid, foreign, foreign_ids = prepare(sm, evaluation_package, context, "other.session")
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = full_result(sm, sid, evaluation_package, npc, ids)
        for source in (
            SourceReference("observation", other_ids[0], None),
            SourceReference("observation", foreign_ids[0], None),
        ):
            invalid = replace(
                result, memory_updates=(replace(result.memory_updates[0], sources=(source,)),)
            )
            with pytest.raises(StateMachineError):
                sm.submit_npc_evaluation_result(
                    task.evaluation_id, invalid, evaluation_package, context=npc
                )
        raw = json.dumps(
            asdict(sm.get_npc_evaluation_task(task.evaluation_id, evaluation_package, context=npc))
        )
        assert "Attendant private backstory" not in raw and "buried blue key" not in raw
        assert (
            sm.get_npc_view(
                other_sid, foreign.observer_id, evaluation_package, context=foreign
            ).npc_state_version
            == 1
        )


@pytest.mark.parametrize("version,commit", [(11, "c94955f"), (12, "44f8e90"), (13, "2ddd79e")])
def test_actual_prior_npc_runtime_migration_preserves_tasks_meta_outputs_and_recovers_completion(
    tmp_path, evaluation_package, context, version, commit
):
    old = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", commit, "src"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(old)
    db = tmp_path / "old.db"
    code = """
import sys,json
sys.path.insert(0,sys.argv[1])
from dataclasses import replace
from trpg_state_machine import (
 StateMachine,TrustedContext,CreateSessionRequest,PlayerSelection,SetSceneFlag,load_adventure_package)
from trpg_state_machine.perception import NPCContext
p=load_adventure_package(sys.argv[3]).package
c=TrustedContext('authenticated:alice','player.alice',('npc.caretaker',))
with StateMachine(sys.argv[2]) as sm:
 r=CreateSessionRequest('old',p.adventure_id,p.package_version,p.package_digest,
  (PlayerSelection(c.player_id,'pc_template.observer'),))
 sid=sm.create_session(r,p,context=c).session_id
 sm.grant_npc_access(sid,'npc.caretaker','agent:npc.caretaker',p,context=c)
 sm._apply(sid,p,SetSceneFlag('scene.platform','flag.lamp_checked',True),
  expected_world_version=0,context=c)
 for e in sm.read_events(sid,p,context=c): sm.project_event(sid,e.event_seq,p,context=c)
 n=NPCContext('agent:npc.caretaker',sid,'npc.caretaker')
 ids=tuple(o.observation_id for o in sm.read_observations(sid,p,context=n))
 t=sm.create_npc_evaluation_task(sid,p,context=n,request_id='old.task',mode='interactive',
  reason='test',expected_npc_state_version=1,source_observation_ids=ids)
 if int(sys.argv[4])>=12:
  from trpg_state_machine.npc_results import NPCEvaluationResult,NPCActionIntent
  from trpg_state_machine.npc_state import SourceReference
  m=sm.get_npc_view(sid,'npc.caretaker',p,context=n).memories[0]
  m=replace(m,version=2,sources=(SourceReference('observation',ids[0],None),))
  sm.submit_npc_evaluation_result(t.evaluation_id,NPCEvaluationResult(memory_updates=(m,),
   action_intents=(NPCActionIntent('move_actor','scene.workshop',1),)),p,context=n)
 assert sm._conn.execute('PRAGMA user_version').fetchone()[0]==int(sys.argv[4])
 print(json.dumps([sid,t.evaluation_id]))
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(old / "src"),
            str(db),
            str(tmp_path / "evaluation.yaml"),
            str(version),
        ],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    sid, eid = json.loads(process.stdout)
    before = stored_rows(db)
    original_work = before.pop("npc_observation_work")
    with StateMachine(db) as sm:
        npc = grant(sm, sid, evaluation_package, context)
        task = sm.get_npc_evaluation_task(eid, evaluation_package, context=npc).task
        assert task.status == ("running" if version == 11 else "completed")
        if version == 11:
            columns, rows = original_work
            assert {
                tuple(r)
                for r in sm._conn.execute(
                    f"SELECT {','.join(columns)} FROM npc_observation_work"
                ).fetchall()
            } == rows
            assert (
                sm._conn.execute(
                    "SELECT 1 FROM npc_observation_work WHERE accepted_game_tick IS NOT NULL "
                    "OR expires_game_tick IS NOT NULL"
                ).fetchone()
                is None
            )
            assert sm.npc_completed_cursor(
                sid, evaluation_package, context=npc
            ) < sm.perception_cursor(sid, evaluation_package, context=context)
        else:
            assert (
                sm._conn.execute("SELECT DISTINCT status FROM npc_observation_work").fetchone()[0]
                == "completed"
            )
            assert sm.npc_completed_cursor(
                sid, evaluation_package, context=npc
            ) == sm.perception_cursor(sid, evaluation_package, context=context)
    assert_preserved(db, before)
    with StateMachine(db):
        pass
    assert_preserved(db, before)


@pytest.mark.parametrize(
    "table,operation", [("actors", "UPDATE"), ("events", "INSERT"), ("command_receipts", "INSERT")]
)
def test_action_storage_failure_keeps_committed_meta_and_rolls_back_world_command(
    tmp_path, evaluation_package, context, table, operation
):
    db = tmp_path / "runtime.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = replace(
            full_result(sm, sid, evaluation_package, npc, ids),
            action_intents=action_result(sm, sid, evaluation_package, context).action_intents,
        )
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, result, evaluation_package, context=npc
        )
        before = stored_rows(db)
        sm._conn.execute(
            f"CREATE TRIGGER inject BEFORE {operation} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.dispatch_npc_output(task.evaluation_id, 0, evaluation_package, context=context)
        assert error.value.code == "storage_error" and stored_rows(db) == before
        assert (
            sm.get_npc_evaluation_receipt(task.evaluation_id, evaluation_package, context=npc)
            == receipt
        )
        sm._conn.execute("DROP TRIGGER inject")
        assert (
            sm.dispatch_npc_output(
                task.evaluation_id, 0, evaluation_package, context=context
            ).command_receipt.status
            == "committed"
        )


def test_gate_completed_cursor_isolated_between_npcs_and_sessions(
    tmp_path, evaluation_package, context
):
    with StateMachine(tmp_path / "runtime.db") as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        other_sid, other, _ = prepare(sm, evaluation_package, context, "other")
        attendant = grant(sm, sid, evaluation_package, context, "npc.attendant")
        before_other = sm.npc_completed_cursor(other_sid, evaluation_package, context=other)
        before_attendant = sm.npc_completed_cursor(sid, evaluation_package, context=attendant)
        sm.route_npc_observations(
            sid,
            evaluation_package,
            context=npc,
            request_id="retained",
            classification="safe_retention",
        )
        assert sm.npc_completed_cursor(
            sid, evaluation_package, context=npc
        ) == sm.perception_cursor(sid, evaluation_package, context=context)
        assert sm.npc_completed_cursor(other_sid, evaluation_package, context=other) == before_other
        assert (
            sm.npc_completed_cursor(sid, evaluation_package, context=attendant) == before_attendant
        )


def test_mode_switch_keeps_committed_action_and_cancelled_late_result_cannot_complete_work(
    tmp_path, evaluation_package, context
):
    with StateMachine(tmp_path / "runtime.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
        result = action_result(sm, sid, evaluation_package, context)
        receipt = sm.submit_npc_evaluation_result(
            task.evaluation_id, result, evaluation_package, context=npc
        )
        command = sm.dispatch_npc_output(
            task.evaluation_id, 0, evaluation_package, context=context
        ).command_receipt
        saved = sm.read_session(sid, evaluation_package, context=context)
        for event in sm.read_events(sid, evaluation_package, context=context):
            sm.project_event(sid, event.event_seq, evaluation_package, context=context)
        pending = sm.route_npc_observations(
            sid, evaluation_package, context=npc, request_id="next"
        ).task
        cursor = sm.npc_completed_cursor(sid, evaluation_package, context=npc)
        sm.set_npc_inactive(sid, evaluation_package, context=npc, request_id="leave")
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                pending.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
            )
        assert error.value.code == "evaluation_inactive"
        assert sm.npc_completed_cursor(sid, evaluation_package, context=npc) == cursor
        assert sm.read_session(sid, evaluation_package, context=context) == saved
        assert (
            sm.get_npc_evaluation_receipt(task.evaluation_id, evaluation_package, context=npc)
            == receipt
        )
        assert (
            sm.get_npc_output_status(task.evaluation_id, evaluation_package, context=npc)[
                0
            ].command_receipt
            == command
        )
        sm.route_npc_observations(
            sid,
            evaluation_package,
            context=npc,
            request_id="retained",
            classification="safe_retention",
        )
        assert sm.npc_completed_cursor(sid, evaluation_package, context=npc) > cursor


def test_gate_cursor_failure_rolls_back_disposition_and_preserves_work(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "runtime.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        before = stored_rows(db)
        sm._conn.execute(
            "CREATE TRIGGER inject BEFORE UPDATE ON npc_completion_cursors "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError):
            sm.route_npc_observations(
                sid, evaluation_package, context=npc, request_id="retain", classification="no_op"
            )
        assert stored_rows(db) == before


def test_process_exit_during_meta_commit_restores_records_task_and_work(
    tmp_path, evaluation_package, context
):
    db = tmp_path / "runtime.db"
    with StateMachine(db) as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        task = create_task(sm, sid, evaluation_package, npc, ids)
    before = stored_rows(db)
    code = """
import json,os,sys
from dataclasses import replace
from trpg_state_machine import StateMachine,load_adventure_package
from trpg_state_machine.npc_results import NPCEvaluationResult
from trpg_state_machine.npc_state import SourceReference
from trpg_state_machine.perception import NPCContext
p=load_adventure_package(sys.argv[2]).package
with StateMachine(sys.argv[1]) as sm:
 c=NPCContext('agent:npc.caretaker',sys.argv[3],'npc.caretaker')
 v=sm.get_npc_evaluation_task(sys.argv[4],p,context=c)
 m=replace(v.npc_meta.memories[0],version=2,
  sources=(SourceReference('observation',v.task.source_observation_ids[0],None),))
 sm._conn.create_function('crash',0,lambda:os._exit(23))
 sm._conn.execute('CREATE TEMP TRIGGER crash_meta BEFORE INSERT ON '
  'npc_evaluation_receipts BEGIN SELECT crash(); END')
 sm.submit_npc_evaluation_result(sys.argv[4],NPCEvaluationResult(memory_updates=(m,)),p,context=c)
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
    )
    assert process.returncode == 23, process.stderr
    with StateMachine(db) as sm:
        assert stored_rows(db) == before
        assert (
            sm.submit_npc_evaluation_result(
                task.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
            ).status
            == "completed"
        )
