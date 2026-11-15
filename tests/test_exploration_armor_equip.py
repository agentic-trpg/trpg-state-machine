"""Public /19 armor and shield bindings preserve exact item identity and AC."""

import pytest
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader
from item_support import item_loader
from spatial_support import command, create, submit
from test_equipment_instance_bindings import combat_equipment_data
from test_ongoing_effect_storage import _effect

from trpg_state_machine import (
    CharacterAC,
    EquipArmor,
    EquipShield,
    StateMachine,
    StateMachineError,
    UnequipShield,
)
from trpg_state_machine.package import _validate
from trpg_state_machine.session_memory import ActiveItem


def armor_package(*, unrelated=0):
    """Give a Fighter 1 direct leather and chain-shirt instances with known AC."""
    data = combat_equipment_data()
    del data["rule_bindings"]["combat_foundation"]
    data["manifest"]["ruleset_binding"] = ruleset_binding(BundledAssetLoader()).model_dump(
        mode="json"
    )
    owner = data["pc_templates"][0]["pc_template_id"]
    data["pc_templates"][0]["character"]["inventory"].append("chain-shirt")
    next(t for t in data["item_transfers"] if t["instance_id"] == "object.armor_spare")[
        "item_slug"
    ] = "chain-shirt"
    snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
    actor = snapshot["character_states"][0]
    actor.update(
        classes={"fighter": 1},
        class_slug="fighter",
        character_level=1,
        dexterity=14,
        fighting_styles=["defense"],
        ac=16,
    )
    actor["carried_item_slugs"].append("chain-shirt")
    next(
        e
        for e in snapshot["inventory_state"]["entries"]
        if e["instance_id"] == "object.armor_spare"
    )["item_slug"] = "chain-shirt"
    for index in range(unrelated):
        identity = f"object.unrelated_equipment_{index}"
        data["scenes"][0]["objects"].append(
            {
                "object_id": identity,
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": "2,1",
            }
        )
        data["item_transfers"].append(
            {
                "instance_id": identity,
                "item_slug": "shield",
                "initial_owner_id": None,
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "policy": "ordinary_public",
            }
        )
    assert owner == actor["entity_id"]
    return _validate(data)


def pc(sm, sid, package, context):
    """Return the controlled PC and its current mechanical snapshot actor."""
    actor_id = sm.read_session(sid, package, context=context).pc_actor_id
    actor = next(
        actor
        for actor in sm.read_noncombat_snapshot(sid, package, context=context).character_states
        if actor.entity_id == actor_id
    )
    return actor_id, actor


@pytest.mark.parametrize("unrelated", [0, 64])
def test_hot_equipment_resolves_shared_items_without_reads(
    tmp_path, context, monkeypatch, unrelated
):
    """Preview and RE snapshots reuse one loaded equipment field with zero lookup work."""
    package = armor_package(unrelated=unrelated)
    with StateMachine(tmp_path / "equipment.db", dnd_loader=BundledAssetLoader()) as sm:
        sid = create(sm, package, context)
        actor_id = sm.read_session(sid, package, context=context).pc_actor_id
        memory = sm._store.memory
        with sm._transaction():
            equipped = sm._characters.equipment(sid, actor_id)
            assert equipped.shield.object_id == "object.shield_equipped"
            assert equipped.shield is memory.active_item(
                sm._world_facts.object(sid, "object.shield_equipped")
            )
            assert equipped.shield.item_slug == "shield"
            spare = memory.active_item(sm._world_facts.object(sid, "object.shield_spare"))
            assert spare is not equipped.shield
        # A different actor fact forces a fresh RE projection without invalidating equipment.
        with sm._transaction(write=True):
            sm._characters.set_hp(sid, actor_id, 7)

        statements = []
        reads = []
        item_allocations = []
        object_lookups = []
        equipment = sm._characters.equipment
        get_object = sm._world_facts.object
        init = ActiveItem.__init__

        def observe_item(self, memory, row):
            item_allocations.append(row["object_id"])
            init(self, memory, row)

        def observe_object(session_id, instance_id):
            object_lookups.append(instance_id)
            return get_object(session_id, instance_id)

        def observe_equipment(session_id, owner_id):
            start = len(statements)
            before_json = memory.stats["json_parses"]
            before_items = len(item_allocations)
            value = equipment(session_id, owner_id)
            if owner_id == actor_id:
                reads.append(value)
                assert value is equipped
                assert value.shield is equipped.shield
                assert not statements[start:]
                assert memory.stats["json_parses"] == before_json
                assert len(item_allocations) == before_items
            return value

        with monkeypatch.context() as patch:
            patch.setattr(ActiveItem, "__init__", observe_item)
            patch.setattr(sm._characters, "equipment", observe_equipment)
            patch.setattr(sm._world_facts, "object", observe_object)
            sm._conn.set_trace_callback(statements.append)
            try:
                preview = sm.query_world_action(
                    sid, actor_id, EquipShield("object.shield_spare"), package, context=context
                )
                assert preview.status == "available"
                assert reads
                assert "object.shield_spare" in object_lookups
                assert "object.shield_equipped" not in object_lookups
                assert "object.armor_equipped" not in object_lookups
                preview_reads = len(reads)
                assert pc(sm, sid, package, context)[1].hp_current == 7
                assert len(reads) > preview_reads
                assert not item_allocations
            finally:
                sm._conn.set_trace_callback(None)

        assert (
            submit(sm, sid, package, context, EquipShield("object.shield_spare"), "replace").status
            == "committed"
        )
        with sm._transaction():
            updated = equipment(sid, actor_id)
            assert updated.shield is spare
            assert updated.weapon is equipped.weapon
            assert updated.armor is equipped.armor


def test_armor_shield_replace_reopen_and_old_snapshot(tmp_path, context):
    """One exact armor instance changes stored AC; shield swaps stow the old ID."""
    package = armor_package()
    path = tmp_path / "armor.db"
    with StateMachine(path, dnd_loader=BundledAssetLoader()) as sm:
        sid = create(sm, package, context)
        actor_id, old = pc(sm, sid, package, context)
        assert (old.ac, old.worn_armor_instance_id) == (16, "object.armor_equipped")
        assert sm.read_character_ac(sid, actor_id, package, context=context).effective == 16
        receipt = submit(sm, sid, package, context, EquipArmor("object.armor_spare"), "armor")
        assert receipt.status == "committed", receipt
        _, now = pc(sm, sid, package, context)
        assert (now.ac, now.worn_armor_instance_id) == (18, "object.armor_spare")
        assert (old.ac, old.worn_armor_instance_id) == (16, "object.armor_equipped")
        assert sm.read_character_ac(sid, actor_id, package, context=context).base == 18
        assert (
            submit(
                sm, sid, package, context, UnequipShield("object.shield_equipped"), "unshield"
            ).status
            == "committed"
        )
        _, unshielded = pc(sm, sid, package, context)
        assert (unshielded.ac, unshielded.equipped_shield_instance_id) == (16, None)
        assert (
            submit(sm, sid, package, context, EquipShield("object.shield_spare"), "shield").status
            == "committed"
        )
        _, equipped = pc(sm, sid, package, context)
        assert (equipped.ac, equipped.equipped_shield_instance_id) == (18, "object.shield_spare")
        saved = sm.save_session(sid, package, context=context)
    with StateMachine(path, dnd_loader=BundledAssetLoader()) as sm:
        assert sm.resume_session(sid, package, context=context) == saved
        _, reopened = pc(sm, sid, package, context)
        assert (reopened.ac, reopened.worn_armor_instance_id) == (18, "object.armor_spare")
        assert reopened.equipped_shield_instance_id == "object.shield_spare"
        with sm._transaction():
            assert sm._characters.equipment(sid, actor_id).shield is sm._store.memory.active_item(
                sm._world_facts.object(sid, "object.shield_spare")
            )


def test_armor_receipt_fault_rolls_back_ac_binding_event(tmp_path, context):
    """A late receipt failure rolls back the selected armor, AC and event."""
    package = armor_package()
    with StateMachine(tmp_path / "fault.db", dnd_loader=BundledAssetLoader()) as sm:
        sid = create(sm, package, context)
        actor_id, before = pc(sm, sid, package, context)
        events = sm.read_events(sid, package, context=context)
        typed = command(sm, sid, package, context, EquipArmor("object.armor_spare"), "fault")
        sm._conn.execute(
            "CREATE TEMP TRIGGER armor_receipt_fault BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='fault' BEGIN SELECT RAISE(ABORT,'armor receipt fault'); END"
        )
        with sm._transaction():
            previous_armor = sm._characters.equipment(sid, actor_id).armor
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(typed, package, context=context)
        assert error.value.code == "storage_error"
        assert pc(sm, sid, package, context)[1] == before
        assert sm.read_character_ac(sid, actor_id, package, context=context).base == 16
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, "fault", package, context=context) is None
        assert (
            sm._conn.execute(
                "SELECT worn_armor_instance_id FROM character_equipment "
                "WHERE session_id=? AND actor_id=?",
                (sid, actor_id),
            ).fetchone()[0]
            == "object.armor_equipped"
        )
        with sm._transaction():
            assert sm._characters.equipment(sid, actor_id).armor is previous_armor
        sm._conn.execute("DROP TRIGGER armor_receipt_fault")
        assert sm.submit_command(typed, package, context=context).status == "committed"


def test_wrong_kind_and_stale_binding_are_rejected(tmp_path, context):
    """A shield cannot enter the body slot and stale shield identity cannot be removed."""
    package = armor_package()
    with StateMachine(tmp_path / "reject.db", dnd_loader=BundledAssetLoader()) as sm:
        sid = create(sm, package, context)
        before = pc(sm, sid, package, context)[1]
        wrong = submit(sm, sid, package, context, EquipArmor("object.shield_spare"), "wrong")
        stale = submit(sm, sid, package, context, UnequipShield("object.shield_spare"), "stale")
        assert (wrong.status, wrong.error_code) == ("rejected", "invalid_target")
        assert (stale.status, stale.error_code) == ("rejected", "equipment_binding_conflict")
        assert pc(sm, sid, package, context)[1] == before


def test_character_ac_reads_active_effect_once_without_persisting_it(tmp_path, context):
    """The trusted AC view includes only active scalar effects; base AC stays fixed."""
    package = armor_package()
    with StateMachine(tmp_path / "effect.db", dnd_loader=BundledAssetLoader()) as sm:
        sid = create(sm, package, context)
        actor_id = sm.read_session(sid, package, context=context).pc_actor_id
        active = _effect(actor_id, "active")
        active["changes"] = [{"key": "ac.bonus", "mode": "add", "value": "2", "priority": 0}]
        disabled = _effect(actor_id, "disabled")
        disabled["disabled"] = True
        disabled["changes"] = [{"key": "ac.bonus", "mode": "add", "value": "50", "priority": 0}]
        with sm._store.transaction(write=True):
            sm._characters.effects.add_effect(sid, active, ordinal=0)
            sm._characters.effects.add_effect(sid, disabled, ordinal=1)
        assert sm.read_character_ac(sid, actor_id, package, context=context) == CharacterAC(
            16, 2, 18
        )
        assert pc(sm, sid, package, context)[1].ac == 16
        with sm._store.transaction(write=True):
            sm._characters.effects.remove_effect(sid, actor_id, "active", "test")
        assert sm.read_character_ac(sid, actor_id, package, context=context) == CharacterAC(
            16, 0, 16
        )


def test_equipment_rejects_a_different_frozen_rules_loader(tmp_path, context):
    """An opened Session cannot reprice armor using unrelated local asset data."""
    package = armor_package()
    path = tmp_path / "wrong-loader.db"
    with StateMachine(path, dnd_loader=BundledAssetLoader()) as sm:
        sid = create(sm, package, context)
    with StateMachine(path, dnd_loader=item_loader()) as sm:
        typed = command(sm, sid, package, context, EquipArmor("object.armor_spare"), "mismatch")
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(typed, package, context=context)
        assert error.value.code == "ruleset_binding_mismatch"
        assert sm.get_command_receipt(sid, typed.command_id, package, context=context) is None
