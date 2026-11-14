"""Exact-instance potion use through the real pinned item evaluator and shared commit."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import ItemUsePayload
from dnd5e_engine.evaluation_delta import ActionBudgetUpdate, InventoryConsume
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import initialize
from item_support import A, create, item_data, item_loader, navigate, submit

from trpg_state_machine import PickupItem, StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.package import _validate

FIRST, SECOND = "object.potion_first", "object.potion_second"


def potion_world(tmp_path, context, *, last_turn=False):
    """Create two authored normal healing potions, then pick one before combat."""
    base = item_loader()
    bundled = BundledAssetLoader()
    loader = MemoryAssetLoader(
        items=[*base._items.values(), bundled.get_item("potion-of-healing")],
        monsters=list(base._monsters.values()),
    )
    data = item_data()
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    data["manifest"]["package_version"] = "item-01/1"
    data["pc_templates"][0]["character"]["hp"]["current"] = 2
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/19"
        actor = snapshot["character_states"][0]
        actor.update(hp_current=2, weapon_in_hands=None, weapon_grip="none")
        if last_turn:
            actor.update(action_available=False, movement_remaining=0)
    for iid in (FIRST, SECOND):
        data["scenes"][0]["objects"].append(
            {
                "object_id": iid,
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": "2,1",
            }
        )
        data["item_transfers"].append(
            {
                "instance_id": iid,
                "item_slug": "potion-of-healing",
                "initial_owner_id": None,
                "quantity": 1,
                "charges_remaining_per_unit": 1,
                "policy": "ordinary_public",
            }
        )
    package = _validate(data)
    db = tmp_path / "potion.db"
    sm = StateMachine(db, dnd_loader=loader)
    sid = create(sm, package, context)
    navigate(sm, sid, package, context, (A, "1,1"), "near.potion")
    for iid in (FIRST, SECOND):
        assert (
            submit(sm, sid, package, context, PickupItem(iid, A, "2,1"), "pick." + iid).status
            == "committed"
        )
    assert initialize(sm, sid, package, context).status == "committed"
    return sm, sid, package, loader, db


def potion_command(sm, sid, package, context, key="drink"):
    """Select the first authored bottle using the live Session version."""
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key,
        state.pc_actor_id,
        "combat.item",
        ItemUsePayload(
            kind="combat.item",
            instance_id=FIRST,
            activity_id="ZyWmXem7zzQwFFcF",
            target_id=state.pc_actor_id,
        ),
        state.world_version,
    )


def test_real_potion_consumes_only_selected_identity_and_reopens(tmp_path, context):
    sm, sid, package, loader, db = potion_world(tmp_path, context)
    with sm:
        command = potion_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        assert any(e.instance_id == FIRST for e in before.inventory_state.entries)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert (
            next(a for a in after.character_states if a.entity_id == command.actor_id).hp_current
            > 2
        )
        assert FIRST not in {e.instance_id for e in after.inventory_state.entries}
        assert next(e for e in after.inventory_state.entries if e.instance_id == SECOND) == next(
            e for e in before.inventory_state.entries if e.instance_id == SECOND
        )
        assert not next(
            a for a in after.character_states if a.entity_id == command.actor_id
        ).bonus_action_available
        assert sm.read_rng_context(sid, package, context=context) != rng
        assert sm.submit_command(command, package, context=context) == receipt
        assert (
            sm._conn.execute(
                "SELECT command_id FROM consumed_items WHERE session_id=? AND object_id=?",
                (sid, FIRST),
            ).fetchone()[0]
            == command.command_id
        )
        assert (
            sm._conn.execute(
                "SELECT complete FROM scene_integrity WHERE session_id=? AND scene_id=?",
                (sid, A),
            ).fetchone()[0]
            == 1
        )
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchone() is None
        assert any(e.instance_id == FIRST for e in before.inventory_state.entries)
    with StateMachine(db, dnd_loader=loader) as reopened:
        # Active combat remains process-bound, while the scene and consumed
        # identity must survive a fresh connection.
        with reopened._store.transaction():
            row = reopened._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (sid,)
            ).fetchone()
            reopened.scene_lifecycle.validate_initialized(row, package)
            assert (
                reopened._conn.execute(
                    "SELECT 1 FROM consumed_items WHERE session_id=? AND object_id=?", (sid, FIRST)
                ).fetchone()
                is not None
            )
            assert reopened._conn.execute("PRAGMA foreign_key_check").fetchone() is None
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )


def test_potion_fault_rolls_back_exact_identity_and_hp(tmp_path, context):
    sm, sid, package, _, _ = potion_world(tmp_path, context)
    with sm:
        command = potion_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_potion_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='drink' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(command, package, context=context)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert (
            sm._conn.execute(
                "SELECT 1 FROM consumed_items WHERE session_id=? AND object_id=?", (sid, FIRST)
            ).fetchone()
            is None
        )
        sm._conn.execute("DROP TRIGGER fail_potion_receipt")
        assert sm.submit_command(command, package, context=context).status == "committed"


def test_potion_rejects_other_target_without_mutation(tmp_path, context):
    sm, sid, package, _, _ = potion_world(tmp_path, context)
    with sm:
        command = potion_command(sm, sid, package, context)
        invalid = replace(
            command,
            command_id="other",
            payload=command.payload.model_copy(update={"target_id": "npc.caretaker"}),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(invalid, package, context=context)
        assert error.value.code == "invalid_target"
        assert sm.read_combat_snapshot(sid, package, context=context) == before


def test_potion_can_advance_a_fully_spent_turn(tmp_path, context):
    """RE's supported turn advance and ledger output survive narrow adoption."""
    sm, sid, package, _, _ = potion_world(tmp_path, context, last_turn=True)
    with sm:
        command = potion_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.combat_state.current_turn_index != before.combat_state.current_turn_index
        assert after.combat_state.last_ended_turn == (
            before.combat_state.round_number,
            command.actor_id,
        )


@pytest.mark.parametrize(
    "fault", ["wrong_bottle", "free_budget", "missing_heal", "extra_action", "wrong_event_target"]
)
def test_malformed_item_result_never_consumes_or_commits(tmp_path, context, fault):
    """An accepted envelope cannot substitute another bottle or skip payment."""
    sm, sid, package, _, _ = potion_world(tmp_path, context)
    with sm:
        command = potion_command(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        original = sm._rules._evaluate_dnd

        async def malformed(request):
            result = await original(request)
            if fault == "wrong_event_target":
                events = list(result.proposed_events)
                index = next(
                    i for i, event in enumerate(events) if event.type == "intent_submitted"
                )
                events[index] = events[index].model_copy(update={"target_id": "npc.caretaker"})
                return result.model_copy(update={"proposed_events": tuple(events)})
            operations = list(result.state_delta.operations)
            if fault == "wrong_bottle":
                other = next(
                    entry
                    for entry in request.state_snapshot.inventory_state.entries
                    if entry.instance_id == SECOND
                )
                index = next(i for i, op in enumerate(operations) if type(op) is InventoryConsume)
                operations[index] = operations[index].model_copy(
                    update={
                        "expected": other,
                        "value": other.model_copy(update={"quantity": 0}),
                    }
                )
            elif fault == "free_budget":
                operations = [op for op in operations if type(op) is not ActionBudgetUpdate]
            elif fault == "extra_action":
                index = next(i for i, op in enumerate(operations) if type(op) is ActionBudgetUpdate)
                budget = operations[index]
                operations[index] = budget.model_copy(
                    update={"value": budget.value.model_copy(update={"action_available": False})}
                )
            else:
                operations = [op for op in operations if op.kind != "actor.hp_delta"]
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    )
                }
            )

        sm._rules._evaluate_dnd = malformed
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
