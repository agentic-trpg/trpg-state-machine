"""Abstract clock and schedule: actual SQLite atomicity, authorization and recovery."""

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
from test_dnd_ledger_scope import stored_state
from test_migrations import assert_preserved, stored_rows
from test_npc_evaluation_tasks import evaluation_package as evaluation_package
from test_npc_evaluation_tasks import prepare
from test_npc_results import full_result
from test_npc_state import SOURCE, enter, grant, request

from trpg_state_machine import (
    AdvanceGameTime,
    MoveActor,
    StateMachine,
    StateMachineError,
    TypedCommand,
)
from trpg_state_machine.npc_results import NPCActionIntent, NPCEvaluationResult
from trpg_state_machine.package import _validate

ROOT = Path(__file__).resolve().parents[1]


def tick_command(sm, sid, package, context, next_tick, key=None, expected_tick=None):
    current = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key or f"clock.{next_tick}",
        current.pc_actor_id,
        "world.advance_time",
        AdvanceGameTime(
            current.world.game_tick if expected_tick is None else expected_tick, next_tick
        ),
        current.world_version,
    )


def tick(sm, sid, package, context, next_tick, **kwargs):
    return sm.submit_command(
        tick_command(sm, sid, package, context, next_tick, **kwargs), package, context=context
    )


def npc_view(sm, sid, package, npc):
    return sm.get_npc_view(sid, npc.observer_id, package, context=npc)


def test_start_continue_complete_no_duplicate_and_no_cognition_writes(
    tmp_path, evaluation_package, context
):
    context = replace(context, can_advance_time=True)
    db = tmp_path / "clock.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        before = stored_state(db)
        initial = npc_view(sm, sid, evaluation_package, npc)
        command = tick_command(sm, sid, evaluation_package, context, 10)
        receipt = sm.submit_command(command, evaluation_package, context=context)
        assert receipt.status == "committed"
        active = npc_view(sm, sid, evaluation_package, npc)
        assert active.runtime.current_activity == "Inspect lamp."
        assert active.npc_state_version == initial.npc_state_version + 1
        assert [(r.status, r.started_tick, r.finished_tick) for r in active.schedule_runs] == [
            ("active", 10, None)
        ]
        assert sm.get_npc_mode(sid, evaluation_package, context=npc) == "inactive"
        after = stored_state(db)
        for table in (
            "npc_private_records",
            "npc_record_sources",
            "npc_known_facts",
            "world_facts",
            "rng_states",
            "npc_modes",
            "npc_evaluation_tasks",
            "npc_observation_work",
        ):
            assert after[table] == before[table]
        assert sm.submit_command(command, evaluation_package, context=context) == receipt
        assert stored_state(db) == after
        with pytest.raises(StateMachineError) as conflict:
            sm.submit_command(
                replace(command, payload=AdvanceGameTime(0, 11)),
                evaluation_package,
                context=context,
            )
        assert conflict.value.code == "idempotency_conflict"
        assert tick(sm, sid, evaluation_package, context, 15).status == "committed"
        assert npc_view(sm, sid, evaluation_package, npc) == active
        assert tick(sm, sid, evaluation_package, context, 20).status == "committed"
        final = npc_view(sm, sid, evaluation_package, npc)
        assert final.runtime.current_activity is None
        assert (
            final.schedule_runs[0].status == "completed"
            and final.schedule_runs[0].finished_tick == 20
        )
        assert final.npc_state_version == active.npc_state_version + 1
        assert tick(sm, sid, evaluation_package, context, 21).status == "committed"
        assert npc_view(sm, sid, evaluation_package, npc) == final
        state = sm.read_session(sid, evaluation_package, context=context)
        assert (
            state.world.game_tick == 21
            and state.world_version == command.expected_world_version + 4
        )
        assert state.rng.rng_version == 0
        assert (
            sm._conn.execute(
                "SELECT COUNT(*) FROM command_receipts WHERE session_id=? "
                "AND command_id LIKE 'clock.%'",
                (sid,),
            ).fetchone()[0]
            == 4
        )


@pytest.mark.parametrize(
    "case", ["revoke", "bad_tick", "backwards", "too_large", "bool", "wrong_session"]
)
def test_clock_input_and_authority_fail_without_side_effects(
    tmp_path, evaluation_package, context, case
):
    gm = replace(context, can_advance_time=True)
    db = tmp_path / "clock.db"
    with StateMachine(db) as sm:
        sid, _, _ = prepare(sm, evaluation_package, context)
        command = tick_command(sm, sid, evaluation_package, gm, 10)
        ctx = context if case == "revoke" else gm
        if case == "bad_tick":
            command = replace(command, payload=AdvanceGameTime(-1, 10))
        if case == "backwards":
            command = replace(command, payload=AdvanceGameTime(11, 10))
        if case == "too_large":
            command = replace(command, payload=AdvanceGameTime(0, 2147483648))
        if case == "bool":
            command = replace(command, payload=AdvanceGameTime(False, 10))
        if case == "wrong_session":
            ctx = replace(gm, player_id="player.other")
        before = stored_state(db)
        with pytest.raises(StateMachineError):
            sm.submit_command(command, evaluation_package, context=ctx)
        assert stored_state(db) == before


def test_time_precondition_noop_and_replay_rechecks_grant(tmp_path, evaluation_package, context):
    gm = replace(context, can_advance_time=True)
    with StateMachine(tmp_path / "clock.db") as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        before = sm.read_session(sid, evaluation_package, context=gm)
        receipt = tick(sm, sid, evaluation_package, gm, 10, expected_tick=1)
        assert (receipt.status, receipt.error_code) == ("rejected", "game_time_conflict")
        assert sm.read_session(sid, evaluation_package, context=gm) == before
        assert not npc_view(sm, sid, evaluation_package, npc).schedule_runs
        command = tick_command(sm, sid, evaluation_package, gm, 0, key="clock.noop")
        assert sm.submit_command(command, evaluation_package, context=gm).status == "committed"
        assert sm.read_session(sid, evaluation_package, context=gm) == before
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(command, evaluation_package, context=context)
        assert denied.value.code == "unauthorized"


def test_move_interrupts_without_teleport_and_revisit_does_not_restart(
    tmp_path, evaluation_package, context
):
    context = replace(context, can_advance_time=True)
    with StateMachine(tmp_path / "clock.db") as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        tick(sm, sid, evaluation_package, context, 10)
        current = sm.read_session(sid, evaluation_package, context=context)
        moved = sm.submit_command(
            TypedCommand(
                sid,
                "move.npc",
                npc.observer_id,
                "move_actor",
                MoveActor(npc.observer_id, "scene.workshop"),
                current.world_version,
            ),
            evaluation_package,
            context=context,
        )
        assert moved.status == "committed"
        view = npc_view(sm, sid, evaluation_package, npc)
        assert view.current_scene_id == "scene.workshop" and view.runtime.current_activity is None
        assert view.schedule_runs[0].status == "interrupted"
        tick(sm, sid, evaluation_package, context, 11)
        assert npc_view(sm, sid, evaluation_package, npc) == view
        current = sm.read_session(sid, evaluation_package, context=context)
        sm._apply(
            sid,
            evaluation_package,
            MoveActor(npc.observer_id, "scene.platform"),
            expected_world_version=current.world_version,
            context=context,
        )
        tick(sm, sid, evaluation_package, context, 12)
        restored = npc_view(sm, sid, evaluation_package, npc)
        assert (
            restored.current_scene_id == "scene.platform"
            and restored.runtime.current_activity is None
        )
        assert restored.schedule_runs == view.schedule_runs
        assert restored.npc_state_version == view.npc_state_version


def test_missed_window_and_session_isolation(tmp_path, evaluation_package, context):
    context = replace(context, can_advance_time=True)
    with StateMachine(tmp_path / "clock.db") as sm:
        first, npc, _ = prepare(sm, evaluation_package, context)
        second = sm.create_session(
            request(evaluation_package, context, "second"), evaluation_package, context=context
        ).session_id
        tick(sm, first, evaluation_package, context, 25)
        run = npc_view(sm, first, evaluation_package, npc).schedule_runs[0]
        assert (run.status, run.started_tick, run.finished_tick) == ("missed", None, 25)
        assert sm.read_session(second, evaluation_package, context=context).world.game_tick == 0
        second_npc = grant(sm, second, evaluation_package, context)
        assert not npc_view(sm, second, evaluation_package, second_npc).schedule_runs
        assert (
            sm.read_session(first, evaluation_package, context=context)
            .actors[npc.observer_id]
            .current_scene_id
            == "scene.platform"
        )


@pytest.mark.parametrize(
    "table",
    [
        "world_states",
        "npc_schedule_runs",
        "npc_states",
        "npc_evaluation_tasks",
        "events",
        "event_outbox",
        "command_receipts",
    ],
)
def test_clock_sql_fault_rolls_back_runtime_task_clock_and_receipt(
    tmp_path, evaluation_package, context, table
):
    context = replace(context, can_advance_time=True)
    db = tmp_path / "clock.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        task = sm.request_npc_interaction(
            sid, "Continue working?", evaluation_package, context=npc, request_id="interactive"
        ).task
        command = tick_command(sm, sid, evaluation_package, context, 10)
        before = stored_state(db)
        action = (
            "INSERT"
            if table in {"npc_schedule_runs", "events", "event_outbox", "command_receipts"}
            else "UPDATE"
        )
        sm._conn.execute(
            f"CREATE TEMP TRIGGER fail BEFORE {action} ON {table} "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, evaluation_package, context=context)
        assert error.value.code == "storage_error" and stored_state(db) == before
        sm._conn.execute("DROP TRIGGER fail")
        assert sm.submit_command(command, evaluation_package, context=context).status == "committed"
        assert (
            sm.get_npc_evaluation_task(
                task.evaluation_id, evaluation_package, context=npc
            ).task.status
            == "cancelled"
        )
        assert sm.get_npc_mode(sid, evaluation_package, context=npc) == "interactive"
        assert len(sm.list_npc_evaluation_tasks(sid, evaluation_package, context=npc)) == 1


def test_multiconnection_ticks_one_commit_one_conflict(tmp_path, evaluation_package, context):
    context = replace(context, can_advance_time=True)
    db, barrier = tmp_path / "clock.db", Barrier(2)
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        command = tick_command(sm, sid, evaluation_package, context, 10)

    def execute(index):
        with StateMachine(db) as sm:
            barrier.wait(timeout=10)
            return sm.submit_command(
                replace(command, command_id=f"race.{index}"), evaluation_package, context=context
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(execute, range(2)))
    assert sorted(r.status for r in receipts) == ["committed", "conflict"]
    with StateMachine(db) as sm:
        assert sm.read_session(sid, evaluation_package, context=context).world.game_tick == 10
        assert len(npc_view(sm, sid, evaluation_package, npc).schedule_runs) == 1
        assert npc_view(sm, sid, evaluation_package, npc).npc_state_version == 2


@pytest.mark.parametrize("mode", ["interactive", "background"])
def test_clock_and_task_mode_race_never_starts_extra_background(
    tmp_path, evaluation_package, context, mode
):
    context = replace(context, can_advance_time=True)
    db, barrier = tmp_path / "clock.db", Barrier(2)
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        command = tick_command(sm, sid, evaluation_package, context, 10)

    def execute(operation):
        with StateMachine(db) as sm:
            barrier.wait(timeout=10)
            if operation == "clock":
                return sm.submit_command(command, evaluation_package, context=context)
            if mode == "interactive":
                return sm.request_npc_interaction(
                    sid, "Hello.", evaluation_package, context=npc, request_id="racing.mode"
                )
            return sm.route_npc_observations(
                sid,
                evaluation_package,
                context=npc,
                request_id="racing.gate",
                classification="background",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(execute, ("clock", "mode")))
    with StateMachine(db) as sm:
        tasks = sm.list_npc_evaluation_tasks(sid, evaluation_package, context=npc)
        assert len(tasks) == 1
        assert tasks[0].mode == mode
        assert tasks[0].status in {"running", "cancelled"}
        if tasks[0].status == "running":
            assert tasks[0].expected_npc_state_version == 2
        else:
            assert tasks[0].expected_npc_state_version == 1
        assert sm.get_npc_mode(sid, evaluation_package, context=npc) == (
            "interactive" if mode == "interactive" else "inactive"
        )
        assert sm.read_session(sid, evaluation_package, context=context).world.game_tick == 10


def test_original_event_observation_gate_meta_action_command_clock_flow(
    tmp_path, evaluation_package, context
):
    context = replace(context, can_advance_time=True)
    with StateMachine(tmp_path / "clock.db") as sm:
        sid, npc, ids = prepare(sm, evaluation_package, context)
        routed = sm.route_npc_observations(
            sid,
            evaluation_package,
            context=npc,
            request_id="gate.inspect",
            classification="background",
        )
        assert routed.task and routed.task.source_observation_ids == ids
        world = sm.read_session(sid, evaluation_package, context=context)
        result = replace(
            full_result(sm, sid, evaluation_package, npc, ids),
            action_intents=(NPCActionIntent("move_actor", "scene.workshop", world.world_version),),
        )
        selected = sm.submit_npc_evaluation_result(
            routed.task.evaluation_id, result, evaluation_package, context=npc
        )
        assert selected.status == "completed" and selected.npc_state_version == 2
        delivered = sm.dispatch_npc_output(
            routed.task.evaluation_id, 0, evaluation_package, context=context
        )
        assert delivered.command_receipt.status == "committed"
        tick(sm, sid, evaluation_package, context, 10)
        view = npc_view(sm, sid, evaluation_package, npc)
        assert view.current_scene_id == "scene.workshop" and not view.schedule_runs
        assert view.memories[-1].text == "I heard the chime."
        tick(sm, sid, evaluation_package, context, 20)
        assert npc_view(sm, sid, evaluation_package, npc).schedule_runs[0].status == "missed"
        assert sm.npc_completed_cursor(
            sid, evaluation_package, context=npc
        ) <= sm.perception_cursor(sid, evaluation_package, context=context)
        assert sm._conn.execute("SELECT COUNT(*) FROM npc_evaluation_receipts").fetchone()[0] == 1
        assert sm.read_session(sid, evaluation_package, context=context).rng.rng_version == 0


def test_fresh_process_clock_activity_receipt_recovery(tmp_path, evaluation_package, context):
    context = replace(context, can_advance_time=True)
    db = tmp_path / "clock.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        command = tick_command(sm, sid, evaluation_package, context, 10)
        receipt = sm.submit_command(command, evaluation_package, context=context)
        saved = sm.save_session(sid, evaluation_package, context=context)
        view = npc_view(sm, sid, evaluation_package, npc)
    before = stored_state(db)
    process = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("_clock_resume_worker.py")),
            str(db),
            str(tmp_path / "evaluation.yaml"),
            sid,
            json.dumps(asdict(context)),
            str(command.expected_world_version),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    data = json.loads(process.stdout)
    assert data["world_version"] == saved.world_version and data["game_tick"] == 10
    assert data["receipt"] == asdict(receipt) | {"event_seqs": list(receipt.event_seqs)}
    assert data["npc"] == json.loads(json.dumps(asdict(view)))
    assert stored_state(db) == before


def old_schema14(tmp_path):
    old = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "ca79751", "src"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(old)
    db = tmp_path / "old.db"
    code = """
import sys,json
sys.path.insert(0,sys.argv[1])
from dataclasses import replace
from trpg_state_machine import (
 StateMachine,TrustedContext,CreateSessionRequest,PlayerSelection,SetSceneFlag)
from trpg_state_machine import load_adventure_package
from trpg_state_machine.perception import NPCContext
from trpg_state_machine.npc_results import NPCEvaluationResult
p=load_adventure_package(sys.argv[3]).package
c=TrustedContext('authenticated:alice','player.alice',('npc.caretaker',))
with StateMachine(sys.argv[2]) as sm:
 r=CreateSessionRequest('old',p.adventure_id,p.package_version,p.package_digest,(PlayerSelection(c.player_id,'pc_template.observer'),))
 sid=sm.create_session(r,p,context=c).session_id
 sm._apply(sid,p,SetSceneFlag('scene.platform','flag.lamp_checked',True),expected_world_version=0,context=c)
 sm.grant_npc_access(sid,'npc.caretaker','agent:npc.caretaker',p,context=c)
 n=NPCContext('agent:npc.caretaker',sid,'npc.caretaker')
 t=sm.create_npc_evaluation_task(sid,p,context=n,request_id='old.task',mode='interactive',reason='test',expected_npc_state_version=1)
 memory=sm.get_npc_view(sid,n.observer_id,p,context=n).memories[0]
 result=NPCEvaluationResult(memory_updates=(replace(memory,version=2,
  text='Old committed memory.'),))
 sm.submit_npc_evaluation_result(t.evaluation_id,result,p,context=n)
 assert sm._conn.execute('PRAGMA user_version').fetchone()[0]==14
 print(json.dumps(sid))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(old / "src"), str(db), str(SOURCE)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return db, json.loads(result.stdout)


def test_actual_schema14_concurrent_upgrade_preserves_all_prior_authority(tmp_path, context):
    db, sid = old_schema14(tmp_path)
    original, barrier = stored_rows(db), Barrier(3)

    def upgrade(_):
        barrier.wait(timeout=10)
        with StateMachine(db) as sm:
            return sm._conn.execute("PRAGMA user_version").fetchone()[0]

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(upgrade, range(3))) == [40] * 3
    assert_preserved(db, original)
    from trpg_state_machine import load_adventure_package

    package = load_adventure_package(SOURCE).package
    context = replace(context, can_advance_time=True)
    with StateMachine(db) as sm:
        sm.upgrade_instance_configs(sid, package, context=context)
        assert sm.resume_session(sid, package, context=context).world.game_tick == 0
        npc = grant(sm, sid, package, context)
        assert npc_view(sm, sid, package, npc).memories[-1].text == "Old committed memory."
        assert tick(sm, sid, package, context, 10).status == "committed"
    after = stored_state(db)
    for _ in range(2):
        with StateMachine(db):
            pass
        assert stored_state(db) == after


def test_interrupted_schema15_migration_is_atomic_and_retryable(tmp_path, monkeypatch):
    db, _ = old_schema14(tmp_path)
    before, read = stored_state(db), Path.read_text

    def broken(path, *args, **kwargs):
        text = read(path, *args, **kwargs)
        return (
            text.replace("PRAGMA user_version = 15;", "INVALID MIGRATION;")
            if path.name == "_migration_15.sql"
            else text
        )

    monkeypatch.setattr(Path, "read_text", broken)
    with pytest.raises(StateMachineError):
        StateMachine(db)
    assert stored_state(db) == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
    monkeypatch.undo()
    with StateMachine(db) as sm:
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 40


def test_overlapping_schedule_fails_closed_without_partial_clock(
    tmp_path, evaluation_package, context
):
    import yaml

    data = yaml.safe_load((tmp_path / "evaluation.yaml").read_text("utf-8"))
    schedule = data["actors"][0]["npc_state"]["runtime"]["schedule"]
    schedule.append(dict(schedule[0], schedule_id="schedule.overlap"))
    package = _validate(data)
    context = replace(context, can_advance_time=True)
    db = tmp_path / "clock.db"
    with StateMachine(db) as sm:
        sid = sm.create_session(request(package, context), package, context=context).session_id
        before = stored_state(db)
        receipt = tick(sm, sid, package, context, 10)
        assert (receipt.status, receipt.error_code) == ("rejected", "unsupported_schedule")
        after = stored_state(db)
        assert {k: v for k, v in after.items() if k != "command_receipts"} == {
            k: v for k, v in before.items() if k != "command_receipts"
        }


def test_real_dead_actor_is_not_started_and_combat_clock_is_unsupported(
    tmp_path, evaluation_package, context
):
    import yaml
    from dnd_support import initialize
    from test_combat_closure import close_command, pc_attack
    from test_real_checks import check_setup

    context = replace(context, can_advance_time=True)
    base = yaml.safe_load((tmp_path / "evaluation.yaml").read_text("utf-8"))
    sm, sid, package, _, _ = check_setup(tmp_path, base, context)
    with sm:
        npc = grant(sm, sid, package, context)
        assert tick(sm, sid, package, context, 10).status == "committed"
        assert initialize(sm, sid, package, context).status == "committed"
        assert (
            sm.submit_command(pc_attack(sm, sid, package, context), package, context=context).status
            == "committed"
        )
        rng = sm.read_rng_context(sid, package, context=context)
        rejected = tick(sm, sid, package, context, 17, key="clock.combat")
        assert rejected.error_code == "unsupported_combat_world_operation"
        assert sm.read_session(sid, package, context=context).world.game_tick == 16
        assert (
            sm.submit_command(
                close_command(sm, sid, package, context), package, context=context
            ).status
            == "committed"
        )
        assert not sm._characters.rule_actors(sid)[npc.observer_id]["is_alive"]
        # Read the real committed death flag, without deriving death from HP in SM.
        assert tick(sm, sid, package, context, 17).status == "committed"
        view = npc_view(sm, sid, package, npc)
        assert view.schedule_runs[0].status == "interrupted"
        assert view.runtime.current_activity is None
        assert sm.read_rng_context(sid, package, context=context) == rng


def test_stale_task_result_cannot_overwrite_activity_and_new_task_is_authorized(
    tmp_path, evaluation_package, context
):
    context = replace(context, can_advance_time=True)
    db = tmp_path / "clock.db"
    with StateMachine(db) as sm:
        sid, npc, _ = prepare(sm, evaluation_package, context)
        old = sm.request_npc_interaction(
            sid, "Hello.", evaluation_package, context=npc, request_id="old"
        ).task
        tick(sm, sid, evaluation_package, context, 10)
        before = stored_state(db)
        with pytest.raises(StateMachineError) as error:
            sm.submit_npc_evaluation_result(
                old.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
            )
        assert error.value.code == "evaluation_inactive" and stored_state(db) == before
        new = sm.request_npc_interaction(
            sid, "Continue.", evaluation_package, context=npc, request_id="fresh"
        ).task
        assert new.expected_npc_state_version == 2 and new.mode == "interactive"
        assert (
            sm.submit_npc_evaluation_result(
                new.evaluation_id, NPCEvaluationResult(), evaluation_package, context=npc
            ).status
            == "completed"
        )
        assert (
            npc_view(sm, sid, evaluation_package, npc).runtime.current_activity == "Inspect lamp."
        )


def test_lazy_scene_initialization_reconciles_actual_tick_once(
    tmp_path, evaluation_package, context
):
    context = replace(context, can_advance_time=True)
    with StateMachine(tmp_path / "clock.db") as sm:
        sid = sm.create_session(
            request(evaluation_package, context), evaluation_package, context=context
        ).session_id
        tick(sm, sid, evaluation_package, context, 10)
        assert (
            "npc.attendant" not in sm.read_session(sid, evaluation_package, context=context).actors
        )
        enter(sm, sid, evaluation_package, context, "scene.workshop")
        npc = grant(sm, sid, evaluation_package, context, "npc.attendant")
        view = npc_view(sm, sid, evaluation_package, npc)
        assert view.schedule_runs[0].started_tick == 10 and view.npc_state_version == 2
        enter(sm, sid, evaluation_package, context, "scene.platform")
        enter(sm, sid, evaluation_package, context, "scene.workshop")
        assert npc_view(sm, sid, evaluation_package, npc) == view


def test_bounded_schedule_event_limit_rejects_atomically(tmp_path, evaluation_package, context):
    import yaml

    data = yaml.safe_load((tmp_path / "evaluation.yaml").read_text("utf-8"))
    for actor in data["actors"]:
        runtime = actor["npc_state"]["runtime"]
        original = runtime["schedule"][0]
        runtime["schedule"] = [
            dict(original, schedule_id=f"schedule.slot{i}", start_tick=0, end_tick=i + 1)
            for i in range(16)
        ]
    package = _validate(data)
    context = replace(context, can_advance_time=True)
    db = tmp_path / "clock.db"
    with StateMachine(db) as sm:
        sid, _, _ = prepare(sm, package, context)
        before = stored_state(db)
        command = tick_command(sm, sid, package, context, 30)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.error_code == "event_cascade_limit" and not receipt.event_seqs
        after = stored_state(db)
        assert {k: v for k, v in after.items() if k != "command_receipts"} == {
            k: v for k, v in before.items() if k != "command_receipts"
        }
        assert sm.submit_command(command, package, context=context) == receipt
        assert stored_state(db) == after
