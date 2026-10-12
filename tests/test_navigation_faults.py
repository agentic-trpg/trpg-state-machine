"""Actual transactions, concurrent connections, controller changes and restart."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Barrier

import pytest
from navigation_support import (
    NPC,
    A,
    B,
    advance,
    advance_command,
    create,
    navigation_data,
    navigation_package,
    pump,
    read,
    start,
    submit,
)
from spatial_support import command
from test_migrations import assert_original_rows_equal, stored_rows
from test_npc_evaluation_tasks import create_task
from test_npc_state import grant

from trpg_state_machine import (
    AdvanceGameTime,
    CancelNavigation,
    ObjectState,
    SetObjectState,
    StartNavigation,
    StateMachine,
    StateMachineError,
)
from trpg_state_machine.npc_results import NPCEvaluationResult, NPCNavigationIntent
from trpg_state_machine.package import _validate

ROOT = Path(__file__).resolve().parents[1]


def worker(db, document, sid, actor, request_id, budget):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/_navigation_worker.py"),
            str(db),
            str(document),
            sid,
            actor,
            "activity.route",
            request_id,
            str(budget),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


@pytest.mark.parametrize("storage", [0, 1])
def test_one_shared_door_opens_from_both_sides_with_either_storage_side(tmp_path, context, storage):
    data = navigation_data()
    if storage:
        door = data["scenes"][0]["objects"].pop(0)
        door["initial_cell"] = "4,3"
        data["scenes"][1]["objects"].append(door)
    package = _validate(data)
    with StateMachine(tmp_path / "door.db") as sm:
        sid = create(sm, package, context)
        assert start(sm, sid, package, context, target=(B, "1,1")).status == "committed"
        assert pump(sm, sid, package, context).activity.status == "completed"
        assert (
            submit(
                sm,
                sid,
                package,
                context,
                SetObjectState(B, "object.platform_gate", ObjectState.CLOSED),
                "close.B",
            ).status
            == "committed"
        )
        assert (
            start(
                sm, sid, package, context, aid="activity.return", target=(A, "2,1"), key="return"
            ).status
            == "committed"
        )
        assert (
            pump(
                sm, sid, package, context, aid="activity.return", request_id="return"
            ).activity.status
            == "completed"
        )
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM objects WHERE object_id='object.platform_gate'"
            ).fetchone()[0]
            == 1
        )
        assert (
            sm._conn.execute(
                "SELECT state FROM objects WHERE object_id='object.workshop_door'"
            ).fetchone()[0]
            == "open"
        )
        assert (
            sm._conn.execute("SELECT cell FROM objects WHERE object_id='object.crate'").fetchone()[
                0
            ]
            == "3,2"
        )


def test_directed_exit_is_respected_without_removing_reverse_door_control(tmp_path, context):
    data = navigation_data()
    data["scenes"][1]["exits"] = []
    package = _validate(data)
    with StateMachine(tmp_path / "directed.db") as sm:
        sid = create(sm, package, context)
        start(sm, sid, package, context, target=(B, "1,1"))
        assert pump(sm, sid, package, context).activity.status == "completed"
        assert (
            submit(
                sm,
                sid,
                package,
                context,
                SetObjectState(B, "object.platform_gate", ObjectState.CLOSED),
                "close",
            ).status
            == "committed"
        )
        refusal = start(
            sm, sid, package, context, aid="activity.reverse", target=(A, "2,1"), key="reverse"
        )
        assert refusal.error_code == "navigation_route_unavailable"


@pytest.mark.parametrize("phase", ["start", "step", "door", "cancel"])
@pytest.mark.parametrize("failure", ["outbox", "progress"])
@pytest.mark.parametrize("npc_actor", [False, True])
def test_sql_failure_rolls_back_world_progress_evidence_rng_and_receipts(
    tmp_path, context, phase, failure, npc_actor
):
    package = navigation_package()
    db = tmp_path / "fault.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        actor = NPC if npc_actor else None
        if npc_actor:
            grant(sm, sid, package, context)
        if phase != "start":
            start(sm, sid, package, context, actor=actor)
        if phase == "door":
            advance(sm, sid, package, context, "approach.1", actor=actor)
            advance(sm, sid, package, context, "approach.2", actor=actor)
        if phase == "start":
            request = command(
                sm,
                sid,
                package,
                context,
                StartNavigation("activity.route", B, "4,2"),
                "fault",
                actor,
            )
        elif phase == "cancel":
            activity = read(sm, sid, package, context, actor=actor)
            request = command(
                sm,
                sid,
                package,
                context,
                CancelNavigation(activity.activity_id, activity.activity_version),
                "fault",
                actor,
            )
        else:
            request = advance_command(sm, sid, package, context, "fault", actor=actor)
        table = "event_outbox" if failure == "outbox" else "navigation_activities"
        verb = "INSERT" if failure == "outbox" or phase == "start" else "UPDATE"
        with sm._store.transaction(write=True):
            sm._conn.execute(
                f"CREATE TRIGGER fail_nav BEFORE {verb} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(request, package, context=context)
        assert exc.value.code == "storage_error"
        assert stored_rows(db) == before
        assert sm.get_command_receipt(sid, "fault", package, context=context) is None
        with sm._store.transaction(write=True):
            sm._conn.execute("DROP TRIGGER fail_nav")
        assert sm.submit_command(request, package, context=context).status == "committed"
        committed = stored_rows(db)
        sm.submit_command(request, package, context=context)
        assert stored_rows(db) == committed


def test_later_package_domain_failure_rolls_back_move_and_progress_but_keeps_refusal(
    tmp_path, context, monkeypatch
):
    package = navigation_package()
    db = tmp_path / "domain.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        start(sm, sid, package, context)
        before = stored_rows(db)

        def fail(*args):
            raise StateMachineError("unsupported_mechanism", "Package tail refused")

        monkeypatch.setattr(sm._package_effects, "_expand_events", fail)
        result = advance(sm, sid, package, context, "tail")
        assert result.status == "rejected" and result.event_seqs == ()
        after = stored_rows(db)
        assert_original_rows_equal(before, after, excluded=("command_receipts",))
        assert len(after["command_receipts"][1]) == len(before["command_receipts"][1]) + 1


@pytest.mark.parametrize("case", ["same", "different", "cancel"])
def test_concurrent_advances_and_cancel_share_occ_and_one_transaction(tmp_path, context, case):
    package = navigation_package()
    db = tmp_path / "race.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        start(sm, sid, package, context)
        first = advance_command(sm, sid, package, context, "race.1")
        if case == "same":
            second = first
        elif case == "different":
            second = replace(first, command_id="race.2")
        else:
            second = replace(
                first,
                command_id="race.cancel",
                command_type="activity.cancel_navigation",
                payload=CancelNavigation("activity.route", 1),
            )
        before = sm.read_session(sid, package, context=context)
    barrier = Barrier(2)

    def execute(request):
        with StateMachine(db) as sm:
            barrier.wait(timeout=20)
            return sm.submit_command(request, package, context=context)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(execute, (first, second)))
    with StateMachine(db) as sm:
        activity = read(sm, sid, package, context)
        after = sm.read_session(sid, package, context=context)
        assert after.world_version == before.world_version + 1
        assert after.rng == before.rng and activity.activity_version == 2
        if case == "same":
            assert receipts[0] == receipts[1] and activity.cursor == 1
        else:
            assert sorted(r.status for r in receipts) == ["committed", "conflict"]
        assert activity.cursor == (0 if activity.status == "cancelled" else 1)


def test_driver_reconciles_committed_lost_reply_before_next_slot(tmp_path, context, monkeypatch):
    package = navigation_package()
    with StateMachine(tmp_path / "lost.db") as sm:
        sid = create(sm, package, context)
        start(sm, sid, package, context)
        original = sm.submit_command

        def lose_reply(*args, **kwargs):
            original(*args, **kwargs)
            raise ConnectionError("reply lost after commit")

        monkeypatch.setattr(sm, "submit_command", lose_reply)
        first = pump(sm, sid, package, context, budget=1)
        assert first.activity.cursor == 1 and len(first.receipts) == 1
        assert pump(sm, sid, package, context, budget=1).activity == first.activity
        monkeypatch.setattr(sm, "submit_command", original)
        assert pump(sm, sid, package, context, request_id="next", budget=1).activity.cursor == 2


def test_halfway_fresh_process_continues_without_replaying_committed_slots(tmp_path, context):
    data = navigation_data()
    package = _validate(data)
    document = tmp_path / "package.json"
    document.write_text(json.dumps(data), "utf-8")
    db = tmp_path / "resume.db"
    with StateMachine(db) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        start(sm, sid, package, context)
        halfway = pump(sm, sid, package, context, request_id="first", budget=2)
        rng = sm.read_session(sid, package, context=context).rng
    same = json.loads(worker(db, document, sid, actor, "first", 2).stdout)
    assert same["activity"] == json.loads(json.dumps(asdict(halfway.activity)))
    done = json.loads(worker(db, document, sid, actor, "continue", 32).stdout)
    assert done["activity"]["status"] == "completed" and done["position"] == {
        "scene_id": B,
        "cell": "4,2",
    }
    assert done["rng"] == json.loads(json.dumps(asdict(rng)))
    assert json.loads(worker(db, document, sid, actor, "continue", 32).stdout) == done


def test_real_task_result_handoff_starts_once_then_program_advances(tmp_path, context):
    package = navigation_package()
    with StateMachine(tmp_path / "handoff.db") as sm:
        sid = create(sm, package, context)
        npc = grant(sm, sid, package, context)
        task = create_task(sm, sid, package, npc)
        version = sm.read_session(sid, package, context=context).world_version
        proposal = NPCEvaluationResult(
            action_intents=(NPCNavigationIntent("activity.route", B, "4,2", version),)
        )
        selected = sm.submit_npc_evaluation_result(
            task.evaluation_id, proposal, package, context=npc
        )
        assert selected.outputs[0].status == "pending"
        assert sm._conn.execute("SELECT count(*) FROM navigation_activities").fetchone()[0] == 0
        delivered = sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context)
        assert delivered.command_receipt.status == "committed"
        assert read(sm, sid, package, context, actor=NPC).cursor == 0
        assert (
            sm.submit_npc_evaluation_result(task.evaluation_id, proposal, package, context=npc)
            == selected
        )
        assert sm.dispatch_npc_output(task.evaluation_id, 0, package, context=context) == delivered
        assert pump(sm, sid, package, context, actor=NPC).activity.status == "completed"
        assert sm._conn.execute("SELECT count(*) FROM npc_evaluation_receipts").fetchone()[0] == 1
        assert sm._conn.execute("SELECT count(*) FROM navigation_activities").fetchone()[0] == 1


def test_revocation_stops_npc_and_schedule_never_overwrites_navigation(tmp_path, context):
    package = navigation_package()
    with StateMachine(tmp_path / "grants.db") as sm:
        sid = create(sm, package, context)
        npc = grant(sm, sid, package, context)
        start(sm, sid, package, context, actor=NPC)
        busy = sm.get_npc_view(sid, NPC, package, context=npc)
        assert (
            submit(
                sm,
                sid,
                package,
                replace(context, can_advance_time=True),
                AdvanceGameTime(0, 12),
                "tick",
            ).status
            == "committed"
        )
        assert (
            sm.get_npc_view(sid, NPC, package, context=npc).runtime.current_activity
            == busy.runtime.current_activity
        )
        with pytest.raises(StateMachineError) as exc:
            advance(
                sm, sid, package, replace(context, controlled_npc_ids=()), "no.grant", actor=NPC
            )
        assert exc.value.code == "unauthorized"
        sm.grant_npc_access(sid, NPC, "replacement.controller", package, context=context)
        before = read(sm, sid, package, context, actor=NPC)
        assert advance(sm, sid, package, context, "revoked", actor=NPC).error_code == "unauthorized"
        assert read(sm, sid, package, context, actor=NPC).cursor == before.cursor
        cancel = CancelNavigation(before.activity_id, before.activity_version)
        assert submit(sm, sid, package, context, cancel, "cancel", NPC).status == "committed"
        assert (
            start(
                sm, sid, package, context, actor=NPC, aid="activity.schedule", key="scheduled"
            ).error_code
            == "unsupported_actor_activity"
        )


@pytest.mark.parametrize("change", ["hp", "position", "combat"])
def test_changed_actor_preconditions_stop_activity_without_execution(tmp_path, context, change):
    data = navigation_data()
    assets = None
    if change == "combat":
        from dnd_support import initialize
        from modern_support import modern_loader
        from test_spatial_world import combat_data

        assets = modern_loader()
        data = combat_data(data, assets)
        # Reapply only the new immutable knowledge extension to this real-RE fixture.
        data["manifest"]["schema_version"] = "state-machine-foundation/3"
        data["navigation"]["pc_template_grants"] = [
            {"pc_template_id": p["pc_template_id"], "region_ids": ["region.public"]}
            for p in data["pc_templates"]
        ]
    package = _validate(data)
    with StateMachine(tmp_path / "conditions.db", dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        start(sm, sid, package, context)
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        if change == "combat":
            assert initialize(sm, sid, package, context).status == "committed"
        else:
            with sm._store.transaction(write=True):
                if change == "hp":
                    sm._characters.set_hp(sid, pc, 0)
                else:
                    from trpg_state_machine.spatial import SpatialPosition

                    sm._characters.move(sid, pc, SpatialPosition(A, "0,1"))
        position = sm.read_actor_position(sid, pc, package, context=context)
        result = advance(sm, sid, package, context, "stop")
        assert result.status == "committed"
        assert read(sm, sid, package, context).status == "blocked"
        assert sm.read_actor_position(sid, pc, package, context=context) == position
        assert all(
            not e.signals
            for e in sm.read_events(sid, package, context=context)
            if e.event_seq in result.event_seqs
        )
