"""Exact-instance exploration weapon bindings reach ordinary /19 combat."""

import json

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import initialize
from item_support import DAGGER, NPC, SECOND, A, item_loader
from spatial_support import command, create, submit
from test_equipment_instance_bindings import combat_equipment_data

from trpg_state_machine import (
    DropItem,
    EquipWeapon,
    StateMachine,
    StateMachineError,
    TypedCommand,
    UnequipWeapon,
)
from trpg_state_machine.package import _validate


def two_rapiers():
    """Supply two distinguishable directly carried instances of one weapon type."""
    data = combat_equipment_data()
    owner = data["pc_templates"][0]["pc_template_id"]
    next(t for t in data["item_transfers"] if t["instance_id"] == SECOND).update(
        item_slug="rapier", initial_owner_id=owner
    )
    for foundation in data["rule_bindings"].values():
        foundation["snapshot"]["inventory_state"]["entries"].append(
            {
                "instance_id": SECOND,
                "owner_id": owner,
                "item_slug": "rapier",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        )
    return data


def held(snapshot, actor):
    """Read the fixed snapshot's exact bound weapon ID."""
    return next(
        a for a in snapshot.character_states if a.entity_id == actor
    ).held_weapon_instance_id


def raw_actor(sm, sid, actor):
    """Inspect the persisted hand-binding shape."""
    return json.loads(
        sm._conn.execute(
            "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
            (sid, actor),
        ).fetchone()[0]
    )


def test_replace_reopen_and_attack_only_selected_instance(tmp_path, context):
    """A carried twin can replace the held weapon before a later encounter."""
    package = _validate(two_rapiers())
    db = tmp_path / "weapons.db"
    with StateMachine(db, dnd_loader=item_loader()) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        old = sm.read_noncombat_snapshot(sid, package, context=context)
        assert held(old, actor) == DAGGER
        cmd = command(sm, sid, package, context, EquipWeapon(SECOND, "one_handed"), "equip")
        receipt = sm.submit_command(cmd, package, context=context)
        assert receipt.status == "committed", receipt
        assert held(sm.read_noncombat_snapshot(sid, package, context=context), actor) == SECOND
        assert held(old, actor) == DAGGER
        assert "weapon_in_hands" not in raw_actor(sm, sid, actor)
        assert sm.submit_command(cmd, package, context=context) == receipt
        assert (
            submit(sm, sid, package, context, DropItem(DAGGER, A, "0,0"), "drop.old").status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, DropItem(SECOND, A, "0,0"), "drop.held").status
            == "rejected"
        )
        saved = sm.save_session(sid, package, context=context)
    with StateMachine(db, dnd_loader=item_loader()) as sm:
        assert sm.resume_session(sid, package, context=context) == saved
        assert held(sm.read_noncombat_snapshot(sid, package, context=context), actor) == SECOND
        assert initialize(sm, sid, package, context).status == "committed"
        current = sm.read_combat_snapshot(sid, package, context=context)
        assert held(current, actor) == SECOND
        for iid, expected in ((DAGGER, "unavailable"), (SECOND, "available")):
            attack = CombatIntentPayload(
                intent_type="attack", source_id=actor, target_id=NPC, weapon_instance_id=iid
            )
            assert (
                sm.query_action_availability(sid, actor, attack, package, context=context).status
                == expected
            )
            if iid == SECOND:
                version = sm.read_session(sid, package, context=context).world_version
                cmd = TypedCommand(sid, "attack.new", actor, "combat.intent", attack, version)
                assert sm.submit_command(cmd, package, context=context).status == "committed"


def test_unequip_and_stale_expected_binding(tmp_path, context):
    """Putting away clears the hand binding and leaves the item carried."""
    package = _validate(two_rapiers())
    with StateMachine(tmp_path / "unequip.db", dnd_loader=item_loader()) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        assert (
            submit(sm, sid, package, context, EquipWeapon(SECOND, "one_handed"), "swap").status
            == "committed"
        )
        assert (
            submit(sm, sid, package, context, UnequipWeapon(DAGGER), "stale").status == "rejected"
        )
        assert (
            submit(sm, sid, package, context, UnequipWeapon(SECOND), "clear").status == "committed"
        )
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        assert held(snapshot, actor) is None
        assert SECOND in {e.instance_id for e in snapshot.inventory_state.entries}
        assert "held_weapon_instance_id" not in raw_actor(sm, sid, actor)
        assert raw_actor(sm, sid, actor)["weapon_in_hands"] is None
        assert (
            submit(sm, sid, package, context, DropItem(SECOND, A, "0,0"), "drop").status
            == "committed"
        )
        assert initialize(sm, sid, package, context).status == "committed"
        attack = CombatIntentPayload(
            intent_type="attack", source_id=actor, target_id=NPC, weapon_instance_id=SECOND
        )
        assert (
            sm.query_action_availability(sid, actor, attack, package, context=context).status
            == "unavailable"
        )


def test_receipt_failure_rolls_back_binding_event_and_retry(tmp_path, context):
    """A late SQLite receipt fault cannot expose the candidate hand binding."""
    package = _validate(two_rapiers())
    with StateMachine(tmp_path / "fault.db", dnd_loader=item_loader()) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        typed = command(sm, sid, package, context, EquipWeapon(SECOND, "one_handed"), "fault")
        sm._conn.execute(
            "CREATE TEMP TRIGGER weapon_receipt_fault BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='fault' BEGIN SELECT RAISE(ABORT,'weapon receipt fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(typed, package, context=context)
        assert error.value.code == "storage_error"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert held(sm.read_noncombat_snapshot(sid, package, context=context), actor) == DAGGER
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, typed.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER weapon_receipt_fault")
        assert sm.submit_command(typed, package, context=context).status == "committed"
        assert held(sm.read_noncombat_snapshot(sid, package, context=context), actor) == SECOND


def test_shield_blocks_two_handed_grip_and_ground_weapon_is_not_carried(tmp_path, context):
    """A shield occupies the other hand and a ground instance cannot be equipped."""
    shielded = two_rapiers()
    next(t for t in shielded["item_transfers"] if t["instance_id"] == SECOND)["item_slug"] = (
        "longsword"
    )
    shielded["pc_templates"][0]["character"]["inventory"].append("longsword")
    for foundation in shielded["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        next(e for e in snapshot["inventory_state"]["entries"] if e["instance_id"] == SECOND)[
            "item_slug"
        ] = "longsword"
        snapshot["character_states"][0]["carried_item_slugs"].append("longsword")
    package = _validate(shielded)
    with StateMachine(tmp_path / "shield.db", dnd_loader=item_loader()) as sm:
        sid = create(sm, package, context)
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        result = submit(sm, sid, package, context, EquipWeapon(SECOND, "two_handed"), "shield")
        assert (result.status, result.error_code) == ("rejected", "weapon_grip")
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert held(before, actor) == DAGGER

    ground = two_rapiers()
    next(t for t in ground["item_transfers"] if t["instance_id"] == SECOND)["initial_owner_id"] = (
        None
    )
    for foundation in ground["rule_bindings"].values():
        foundation["snapshot"]["inventory_state"]["entries"] = [
            entry
            for entry in foundation["snapshot"]["inventory_state"]["entries"]
            if entry["instance_id"] != SECOND
        ]
    package = _validate(ground)
    with StateMachine(tmp_path / "ground.db", dnd_loader=item_loader()) as sm:
        sid = create(sm, package, context)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        result = submit(sm, sid, package, context, EquipWeapon(SECOND, "one_handed"), "ground")
        assert (result.status, result.error_code) == ("rejected", "invalid_target")
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
