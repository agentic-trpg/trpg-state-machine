"""Exploration self-drinking through the public command and real pinned RE."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import ExplorationItemUsePayload
from dnd5e_engine.evaluation_delta import InventoryConsume
from item_support import A, submit
from test_combat_healing_potion import FIRST, SECOND, potion_world
from test_session_time_coordination import DeadlinePort

from trpg_state_machine import (
    AdvanceSessionTime,
    DropItem,
    StateMachine,
    StateMachineError,
    TypedCommand,
    world_command_from_data,
)


def exploration_potion_world(tmp_path, context, *, full_hp=False, combat=False, timed=False):
    sm, sid, package, loader, db = potion_world(
        tmp_path, context, version="engine-snapshot/20", full_hp=full_hp, combat=combat, timed=timed
    )
    state = sm.read_session(sid, package, context=context)
    command = TypedCommand(
        sid,
        "exploration.drink",
        state.pc_actor_id,
        "world.use_item",
        ExplorationItemUsePayload(
            kind="world.use_item",
            instance_id=SECOND,
            activity_id="ZyWmXem7zzQwFFcF",
            target_id=state.pc_actor_id,
        ),
        state.world_version,
    )
    return sm, sid, package, loader, db, command


def test_self_drinking_commits_selected_bottle_hp_rng_events_and_receipt(tmp_path, context):
    sm, sid, package, loader, db, command = exploration_potion_world(tmp_path, context)
    with sm:
        supplied = world_command_from_data(
            {
                "session_id": sid,
                "command_id": command.command_id,
                "actor_id": command.actor_id,
                "command_type": command.command_type,
                "payload": command.payload.model_dump(mode="json"),
                "expected_world_version": command.expected_world_version,
                "schema_version": command.schema_version,
            }
        )
        assert supplied == command
        original = sm.read_noncombat_snapshot(sid, package, context=context)
        old_value = original.model_dump(mode="json")
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        receipt = sm.submit_command(supplied, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        old_entries = {e.instance_id: e for e in original.inventory_state.entries}
        assert SECOND in old_entries
        assert {e.instance_id: e for e in after.inventory_state.entries} == {
            iid: entry for iid, entry in old_entries.items() if iid != SECOND
        }
        assert FIRST in old_entries
        assert original.model_dump(mode="json") == old_value
        current_events = sm.read_events(sid, package, context=context)
        assert current_events[: len(events)] == events
        added = current_events[len(events) :]
        assert tuple(event.event_seq for event in added) == receipt.event_seqs
        assert all(event.source_command_id == command.command_id for event in added)
        assert all(event.event_type == "world.item_used" for event in added)
        assert all(event.payload["item_instance_id"] == SECOND for event in added)
        intent, healing = [event.payload["engine_event"] for event in added]
        assert intent["type"] == "intent_submitted"
        assert healing["type"] == "healing_applied"
        old_actor = next(a for a in original.character_states if a.entity_id == command.actor_id)
        actor = next(a for a in after.character_states if a.entity_id == command.actor_id)
        assert 2 == old_actor.hp_current < actor.hp_current
        assert actor.hp_current == min(actor.hp_max, old_actor.hp_current + healing["amount"])
        new_rng = sm.read_rng_context(sid, package, context=context)
        assert new_rng != rng
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_noncombat_snapshot(sid, package, context=context) == after
        assert sm.read_rng_context(sid, package, context=context) == new_rng
        assert sm.read_events(sid, package, context=context) == current_events
    with StateMachine(db, dnd_loader=loader) as reopened:
        reopened.resume_session(sid, package, context=context)
        assert reopened.read_noncombat_snapshot(sid, package, context=context) == after
        assert reopened.read_rng_context(sid, package, context=context) == new_rng
        assert reopened.read_events(sid, package, context=context) == current_events
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )


def test_full_hp_keeps_both_bottles_and_rng(tmp_path, context):
    sm, sid, package, _, _, command = exploration_potion_world(tmp_path, context, full_hp=True)
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        actor = next(a for a in before.character_states if a.entity_id == command.actor_id)
        assert actor.hp_current == actor.hp_max
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code, receipt.event_seqs) == (
            "rejected",
            "hp_full",
            (),
        )
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.submit_command(command, package, context=context) == receipt


def test_receipt_write_failure_rolls_back_everything_and_same_id_retries(tmp_path, context):
    sm, sid, package, _, _, command = exploration_potion_world(tmp_path, context)
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_exploration_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='exploration.drink' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=context)
        assert exc.value.code == "storage_error"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert (
            sm._conn.execute(
                "SELECT object_id FROM consumed_items WHERE session_id=?", (sid,)
            ).fetchall()
            == []
        )
        sm._conn.execute("DROP TRIGGER fail_exploration_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        assert sm.submit_command(command, package, context=context) == receipt


def test_exploration_command_cannot_bypass_combat_action_cost(tmp_path, context):
    sm, sid, package, _, _, command = exploration_potion_world(tmp_path, context, combat=True)
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == (
            "rejected",
            "unsupported_combat_world_operation",
        )
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng


def test_dropped_selected_bottle_does_not_fall_back_to_owned_twin(tmp_path, context):
    sm, sid, package, _, _, command = exploration_potion_world(tmp_path, context)
    with sm:
        dropped = submit(sm, sid, package, context, DropItem(SECOND, A, "1,1"), "drop.selected")
        assert dropped.status == "committed"
        state = sm.read_session(sid, package, context=context)
        command = replace(command, expected_world_version=state.world_version)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "item_missing")
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng


def test_unfinished_time_boundary_blocks_potion_until_settled(tmp_path, context):
    context = replace(context, can_advance_time=True)
    sm, sid, package, _, _, command = exploration_potion_world(tmp_path, context, timed=True)
    with sm:
        deadlines = DeadlinePort(sm)
        sm._session_runtime.time.consumers["fixture"] = deadlines
        deadlines.put(sid, "hazard", 18, status="waiting")
        advance = AdvanceSessionTime(60, "activity")
        assert submit(sm, sid, package, context, advance, "advance").status == "committed"
        pending = sm.read_time_sync(sid, package, context=context)
        assert (pending.committed_tick, pending.progress_status) == (18, "waiting")
        command = replace(command, expected_world_version=pending.world_version)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=context)
        assert exc.value.code == "time_advance_pending"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        deadlines.cancel(sid, "hazard")
        assert submit(sm, sid, package, context, advance, "continue").status == "committed"
        settled = sm.read_time_sync(sid, package, context=context)
        assert (settled.committed_tick, settled.progress_status) == (60, "completed")
        command = replace(command, expected_world_version=settled.world_version)
        assert sm.submit_command(command, package, context=context).status == "committed"
        assert sm.read_time_sync(sid, package, context=context).committed_tick == 60


def test_wrong_bottle_in_evaluator_result_is_rejected_before_commit(tmp_path, context, monkeypatch):
    sm, sid, package, _, _, command = exploration_potion_world(tmp_path, context)
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        evaluate = sm._rules._evaluate_dnd

        async def substitute_bottle(request):
            result = await evaluate(request)
            wrong = next(
                e for e in request.state_snapshot.inventory_state.entries if e.instance_id == FIRST
            )
            operations = tuple(
                op.model_copy(
                    update={"expected": wrong, "value": wrong.model_copy(update={"quantity": 0})}
                )
                if type(op) is InventoryConsume
                else op
                for op in result.state_delta.operations
            )
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(update={"operations": operations})
                }
            )

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", substitute_bottle)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=context)
        assert exc.value.code == "invalid_rule_result"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
