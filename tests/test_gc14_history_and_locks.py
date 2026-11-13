"""Real archived receipts, current locks, NPC handoff and pinned-RE death history."""

import json

import pytest
from dnd_support import initialize
from item_support import item_data, item_loader, navigate
from lock_support import CRED, DOOR, NPC, lock_data
from lock_support import setup as lock_setup
from perception_support import synthetic_notice
from spatial_support import A, B, create, submit
from test_combat_closure import close_command, pc_attack
from test_gc14_review_fixes import (
    MOVER,
    events_for,
    evidence,
    setup,
)
from test_migrations import stored_rows
from test_npc_evaluation_tasks import create_task

from trpg_state_machine import (
    AdvanceNavigation,
    MoveActor,
    ResumeNavigation,
    StartNavigation,
    StateMachine,
    StepActor,
    UnlockDoor,
    load_adventure_package,
    pump_navigation,
)
from trpg_state_machine.npc_results import NPCActionIntent, NPCEvaluationResult
from trpg_state_machine.perception import NPCContext


def test_locked_navigation_departs_only_after_explicit_unlock_and_resume(tmp_path, context):
    data = lock_data()
    data["actors"][0]["initial_cell"] = "0,0"
    data["pc_templates"][0]["initial_cell"] = "2,0"
    data["item_transfers"][0]["initial_owner_id"] = NPC
    for name in ("combat_foundation", "noncombat_foundation"):
        snapshot = data["rule_bindings"][name]["snapshot"]
        next(a for a in snapshot["character_states"] if a["entity_id"] == NPC)[
            "carried_item_slugs"
        ] = ["amulet"]
        snapshot["inventory_state"]["entries"].append(
            {
                "instance_id": CRED,
                "owner_id": NPC,
                "item_slug": "amulet",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        )
    sm, sid, p, db = lock_setup(tmp_path, context, data)
    with sm:
        assert (
            submit(
                sm, sid, p, context, StartNavigation("activity.locked", B, "0,0"), "start", NPC
            ).status
            == "committed"
        )
        blocked_receipt = submit(
            sm, sid, p, context, AdvanceNavigation("activity.locked", 1), "blocked", NPC
        )
        blocked = sm.read_navigation_activity(sid, NPC, "activity.locked", p, context=context)
        assert blocked.status == "blocked" and blocked.cursor == 0
        assert [e.event_type for e in events_for(sm, sid, p, context, blocked_receipt)] == [
            "activity.navigation_blocked"
        ]
        assert sm.read_actor_position(sid, NPC, p, context=context).scene_id == A
        assert (
            submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock", NPC).status
            == "committed"
        )
        assert (
            submit(
                sm,
                sid,
                p,
                context,
                ResumeNavigation(blocked.activity_id, blocked.activity_version),
                "resume",
                NPC,
            ).status
            == "committed"
        )
        pumped = pump_navigation(
            sm.submit_command,
            sm.read_navigation_activity,
            sm.get_command_receipt,
            sid,
            NPC,
            blocked.activity_id,
            p,
            context=context,
            request_id="continue",
            budget=8,
        )
        assert pumped.activity.status == "completed"
        physical = [
            e for r in pumped.receipts for e in events_for(sm, sid, p, context, r) if e.signals
        ]
        assert [e.event_type for e in physical] == [
            "object.state_changed",
            "actor.departed",
            "actor.moved",
        ]
        frozen = evidence(sm, sid, physical[1].event_seq)
        actor = next(
            a for a in [*frozen["actors"], *frozen["source_actors"]] if a["actor_id"] == NPC
        )
        assert actor["mechanical"]["cell"] == actor["cell"] == "0,0"
        assert sm.read_session(sid, p, context=context).current_scene_id == A


def test_real_npc_task_dispatch_keeps_pending_and_replays_one_pair(tmp_path, context):
    sm, sid, p, db, context = setup(tmp_path, context)
    with sm:
        npc = NPCContext(MOVER, sid, MOVER)
        task = create_task(sm, sid, p, npc)
        version = sm.read_session(sid, p, context=context).world_version
        result = NPCEvaluationResult(action_intents=(NPCActionIntent("move_actor", B, version),))
        before = stored_rows(db)
        selection = sm.submit_npc_evaluation_result(task.evaluation_id, result, p, context=npc)
        assert selection.outputs[0].status == "pending"
        assert stored_rows(db)["actors"] == before["actors"]
        assert stored_rows(db)["events"] == before["events"]
        delivery = sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context)
        assert delivery.command_receipt.status == "committed"
        assert [
            e.event_type for e in events_for(sm, sid, p, context, delivery.command_receipt)
        ] == ["actor.departed", "actor.moved"]
        committed = stored_rows(db)
        assert sm.dispatch_npc_output(task.evaluation_id, 0, p, context=context) == delivery
        assert stored_rows(db) == committed


def test_mechanical_departure_survives_real_death_closure_and_history_projection(
    tmp_path, context, monkeypatch
):
    data = item_data()
    data["actors"][0]["initial_cell"] = "0,0"
    data["pc_templates"][0]["initial_cell"] = "2,0"
    data["passages"][0]["door_object_id"] = DOOR
    data["scenes"][0]["objects"][0]["state"] = "open"
    for name in ("combat_foundation", "noncombat_foundation"):
        npc = next(
            a
            for a in data["rule_bindings"][name]["snapshot"]["character_states"]
            if a["entity_id"] == NPC
        )
        npc.update(hp_current=1, hp_max=1)
    document, db = tmp_path / "dnd.json", tmp_path / "death.db"
    document.write_text(json.dumps(data), "utf-8")
    p = load_adventure_package(document).package
    with StateMachine(db, dnd_loader=item_loader()) as sm:
        sid = create(sm, p, context)
        sm.grant_npc_access(sid, NPC, NPC, p, context=context)
        receipt = submit(sm, sid, p, context, MoveActor(NPC, B), "depart", NPC)
        assert receipt.status == "committed"
        departure = events_for(sm, sid, p, context, receipt)[0]
        assert departure.event_type == "actor.departed"
        frozen = evidence(sm, sid, departure.event_seq)
        actor = next(a for a in frozen["actors"] if a["actor_id"] == NPC)
        assert actor["mechanical"]["is_alive"] and actor["mechanical"]["cell"] == "0,0"
        assert (
            submit(sm, sid, p, context, StepActor(B, "0,0", "1,0"), "npc.step", NPC).status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, StepActor(B, "1,0", "0,0"), "npc.return.port", NPC).status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, MoveActor(NPC, A), "npc.return", NPC).status == "committed"
        )
        navigate(sm, sid, p, context, (A, "1,0"), "combat.approach")
        assert initialize(sm, sid, p, context).status == "committed"
        assert (
            sm.submit_command(pc_attack(sm, sid, p, context), p, context=context).status
            == "committed"
        )
        assert (
            sm.submit_command(close_command(sm, sid, p, context), p, context=context).status
            == "committed"
        )
        assert not next(
            a
            for a in sm.read_combat_snapshot(sid, p, context=context).character_states
            if a.entity_id == NPC
        ).is_alive
        assert evidence(sm, sid, departure.event_seq) == frozen
    with StateMachine(db, dnd_loader=item_loader()) as sm:
        sm.resume_session(sid, p, context=context)
        monkeypatch.setattr(
            sm._characters, "position", lambda *a: pytest.fail("History read live coordinates")
        )
        requests = []

        def notice(request):
            requests.append(request)
            assert request.evidence["observer"]["mechanical"]["is_alive"] is True
            assert request.evidence["observer"]["mechanical"]["cell"] == "0,0"
            return synthetic_notice(request)

        assert (
            sm.project_npc_event(
                sid, departure.event_seq, p, context=NPCContext(NPC, sid, NPC), judgment=notice
            )
            == "resolved"
        )
        assert len(requests) == 1
        assert evidence(sm, sid, departure.event_seq) == frozen
