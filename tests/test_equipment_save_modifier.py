"""Public standalone saves with an authored, bound held-weapon save clause."""

import json
from dataclasses import asdict, replace

import pytest
import test_bardic_feature_grant as bardic_fixture
from dnd5e_engine.evaluation_contracts import SavePayload
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd5e_srd_data.schema.common import PassiveEffect, PassiveEffectChange
from item_support import item_loader
from spatial_support import command, create
from test_equipment_instance_bindings import combat_equipment_data

from trpg_state_machine import (
    EquipWeapon,
    StateMachine,
    TypedCommand,
    UnequipWeapon,
    world_command_from_data,
)
from trpg_state_machine.choice_models import ChoiceRespondOperationPayload
from trpg_state_machine.errors import StateMachineError
from trpg_state_machine.package import _validate

STAFF = "object.warding_staff"
SLUG = "fixture-warding-quarterstaff"


def _world(tmp_path, context, *, bad_change=False):
    """Bind one fixture-only typed save modifier to an otherwise ordinary weapon."""
    canonical = BundledAssetLoader()
    base = item_loader()
    effect = PassiveEffect(
        _id="fixture-save-bonus",
        name="Held save bonus",
        changes=[
            PassiveEffectChange(
                key="system.bonuses.abilities.save",
                mode=2,
                value="1d4" if bad_change else "+1",
            )
        ],
    )
    staff = canonical.get_weapon("quarterstaff").model_copy(
        update={"slug": SLUG, "name": "Fixture Warding Quarterstaff", "passive_effects": [effect]}
    )
    loader = MemoryAssetLoader(
        items=[*base._items.values(), staff],
        monsters=list(base._monsters.values()),
    )
    data = combat_equipment_data()
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    owner = data["pc_templates"][0]["pc_template_id"]
    data["pc_templates"][0]["character"]["inventory"].append(SLUG)
    data["scenes"][0]["objects"].append(
        {
            "object_id": STAFF,
            "state": None,
            "movable": False,
            "blocks_passage": False,
            "initial_cell": "2,1",
        }
    )
    data["item_transfers"].append(
        {
            "instance_id": STAFF,
            "item_slug": SLUG,
            "initial_owner_id": owner,
            "quantity": 1,
            "charges_remaining_per_unit": 0,
            "policy": "ordinary_public",
        }
    )
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        actor = snapshot["character_states"][0]
        actor.update(
            worn_armor=None,
            worn_armor_instance_id=None,
            shield_equipped=False,
            equipped_shield_instance_id=None,
            other_hand_occupied=False,
        )
        actor["carried_item_slugs"].append(SLUG)
        snapshot["inventory_state"]["entries"].append(
            {
                "instance_id": STAFF,
                "owner_id": owner,
                "item_slug": SLUG,
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        )
    package = _validate(data)
    sm = StateMachine(tmp_path / "equipment-save.sqlite", dnd_loader=loader)
    sid = create(sm, package, context)
    return sm, sid, package, loader


def _json(command):
    """Round-trip the complete public command envelope through JSON."""
    envelope = asdict(command)
    envelope.pop("check_world_effects")
    if hasattr(command.payload, "model_dump"):
        envelope["payload"] = command.payload.model_dump(mode="json")
    return world_command_from_data(json.loads(json.dumps(envelope)))


def _save(sm, sid, package, context, key, *, dc=14):
    """Build a trusted current-PC save from the current world version."""
    state = sm.read_session(sid, package, context=context)
    return _json(
        TypedCommand(
            sid,
            key,
            state.pc_actor_id,
            "rules.save",
            SavePayload(
                kind="rules.save",
                target_id=state.pc_actor_id,
                ability="dex",
                dc=dc,
                advantage=(),
                disadvantage=(),
                is_magical=False,
            ),
            state.world_version,
        )
    )


def _roll(sm, sid, package, context, command):
    """Read the sole committed save's authorized public dice projection."""
    projected = sm.get_command_rolls(sid, command.command_id, package, context=context)
    (roll,) = projected["rolls"]
    return roll


def test_bound_held_save_bonus_follows_public_equip_and_unequip(tmp_path, context):
    """Only the exact held authored instance adds one, including after cold reopen."""
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, loader = _world(tmp_path, context)
    with sm:
        before = _save(sm, sid, package, context, "before")
        no_adjudication = replace(context, can_adjudicate_checks=False)
        first_rng = sm.read_rng_context(sid, package, context=context)
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(before, package, context=no_adjudication)
        assert denied.value.code == "unauthorized"
        assert sm.read_rng_context(sid, package, context=context) == first_rng
        assert sm.submit_command(before, package, context=context).status == "committed"
        plain = _roll(sm, sid, package, context, before)
        assert not any(
            modifier["source"].startswith("equipment:")
            for modifier in plain["roll_detail"]["modifiers"]
        )
        equip = _json(command(sm, sid, package, context, EquipWeapon(STAFF), "equip"))
        assert sm.submit_command(equip, package, context=context).status == "committed"
        fixed = sm.read_noncombat_snapshot(sid, package, context=context)
        actor = next(a for a in fixed.character_states if a.entity_id == before.actor_id)
        assert actor.held_weapon_instance_id == STAFF
        equipped = _save(sm, sid, package, context, "equipped")
        assert sm.submit_command(equipped, package, context=context).status == "committed"
        boosted = _roll(sm, sid, package, context, equipped)
        assert {
            "kind": "fixed",
            "source": f"equipment:{STAFF}:{SLUG}",
            "value": 1,
        } in boosted["roll_detail"]["modifiers"]
        assert boosted["roll_total"] == boosted["natural"] + boosted["modifier"]
        assert sm.submit_command(equipped, package, context=context).status == "committed"
        unequip = _json(command(sm, sid, package, context, UnequipWeapon(STAFF), "unequip"))
        assert sm.submit_command(unequip, package, context=context).status == "committed"
        after = _save(sm, sid, package, context, "after")
        assert sm.submit_command(after, package, context=context).status == "committed"
        unboosted = _roll(sm, sid, package, context, after)
        assert not any(
            modifier["source"].startswith("equipment:")
            for modifier in unboosted["roll_detail"]["modifiers"]
        )
        assert boosted["modifier"] == plain["modifier"] + 1 == unboosted["modifier"] + 1
        final_rng = sm.read_rng_context(sid, package, context=context)
    with StateMachine(tmp_path / "equipment-save.sqlite", dnd_loader=loader) as reopened:
        assert reopened.submit_command(before, package, context=context).status == "committed"
        assert reopened.submit_command(equipped, package, context=context).status == "committed"
        assert reopened.submit_command(after, package, context=context).status == "committed"
        assert _roll(reopened, sid, package, context, equipped) == boosted
        assert reopened.read_rng_context(sid, package, context=context) == final_rng


def test_unreviewed_bound_save_clause_rejects_before_rng(tmp_path, context):
    """A dice expression in the bound weapon cannot substitute for a fixed addend."""
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _ = _world(tmp_path, context, bad_change=True)
    with sm:
        before = sm.read_rng_context(sid, package, context=context)
        equip = _json(command(sm, sid, package, context, EquipWeapon(STAFF), "bad-equip"))
        assert sm.submit_command(equip, package, context=context).status == "rejected"
        assert sm.read_rng_context(sid, package, context=context) == before
        save = _save(sm, sid, package, context, "bad-save")
        assert sm.submit_command(save, package, context=context).status == "rejected"
        assert sm.read_rng_context(sid, package, context=context) == before


def test_late_equip_receipt_failure_keeps_old_hand_and_retry(tmp_path, context):
    """A failed receipt write rolls back the equipment source and its save effect."""
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _ = _world(tmp_path, context)
    with sm:
        old = sm.read_noncombat_snapshot(sid, package, context=context)
        old_rng = sm.read_rng_context(sid, package, context=context)
        equip = _json(command(sm, sid, package, context, EquipWeapon(STAFF), "late-equip"))
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_ward_equip BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='late-equip' BEGIN SELECT RAISE(ABORT,'late equip'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(equip, package, context=context)
        assert error.value.code == "storage_error"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == old
        assert sm.read_rng_context(sid, package, context=context) == old_rng
        assert sm.get_command_receipt(sid, equip.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_ward_equip")
        assert sm.submit_command(equip, package, context=context).status == "committed"
        assert sm.submit_command(equip, package, context=context).status == "committed"


def test_pending_bardic_save_stales_when_exact_held_bonus_is_removed(
    tmp_path, context, monkeypatch
):
    """Continuation rechecks the equipped source while retaining original dice."""
    canonical = BundledAssetLoader()
    authored = canonical.get_weapon("quarterstaff").model_copy(
        update={
            "slug": SLUG,
            "name": "Fixture Warding Quarterstaff",
            "passive_effects": [
                PassiveEffect(
                    _id="fixture-save-bonus",
                    name="Held save bonus",
                    changes=[
                        PassiveEffectChange(key="system.bonuses.abilities.save", mode=2, value="+1")
                    ],
                )
            ],
        }
    )
    original_loader = bardic_fixture.item_loader
    original_validate = bardic_fixture._validate

    def loader_with_staff():
        """Add only the fixture item to the Bard world's bound test loader."""
        loader = original_loader()
        loader._items[SLUG] = authored
        return loader

    def bard_package_with_staff(data):
        """Place one directly carried, initially unheld instance in the fixed world."""
        owner = data["pc_templates"][0]["pc_template_id"]
        data["pc_templates"][0]["character"]["inventory"].append(SLUG)
        data["scenes"][0]["objects"].append(
            {
                "object_id": STAFF,
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": "2,1",
            }
        )
        data["item_transfers"].append(
            {
                "instance_id": STAFF,
                "item_slug": SLUG,
                "initial_owner_id": owner,
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "policy": "ordinary_public",
            }
        )
        for foundation in data["rule_bindings"].values():
            snapshot = foundation["snapshot"]
            snapshot["character_states"][0]["carried_item_slugs"].append(SLUG)
            snapshot["inventory_state"]["entries"].append(
                {
                    "instance_id": STAFF,
                    "owner_id": owner,
                    "item_slug": SLUG,
                    "quantity": 1,
                    "charges_remaining_per_unit": 0,
                    "accessible": True,
                }
            )
        return original_validate(data)

    with monkeypatch.context() as patch:
        patch.setattr(bardic_fixture, "item_loader", loader_with_staff)
        patch.setattr(bardic_fixture, "_validate", bard_package_with_staff)
        db, sid, package, loader, gm = bardic_fixture.make_bard_world(tmp_path, context)
    with StateMachine(db, dnd_loader=loader) as sm:
        grant = bardic_fixture.grant_command(sm, sid, package, gm)
        assert sm.submit_command(grant, package, context=gm).status == "committed"
        equip = _json(command(sm, sid, package, gm, EquipWeapon(STAFF), "bard-equip"))
        assert sm.submit_command(equip, package, context=gm).status == "committed"
        source = _save(sm, sid, package, gm, "bard-save", dc=99)
        opened = sm.submit_command(source, package, context=gm)
        assert opened.status == "pending_choice", opened
        original = _roll(sm, sid, package, gm, source)
        assert any(
            part["source"] == f"equipment:{STAFF}:{SLUG}"
            for part in original["roll_detail"]["modifiers"]
        )
        unequip = _json(command(sm, sid, package, gm, UnequipWeapon(STAFF), "bard-unequip"))
        assert sm.submit_command(unequip, package, context=gm).status == "committed"
        rng = sm.read_rng_context(sid, package, context=gm)
        state = sm.read_session(sid, package, context=gm)
        answer = _json(
            TypedCommand(
                sid,
                "bard-answer",
                state.pc_actor_id,
                "choice.respond",
                ChoiceRespondOperationPayload(source.command_id, "accept"),
                state.world_version,
            )
        )
        refused = sm.submit_command(answer, package, context=gm)
        assert (refused.status, refused.error_code) == ("rejected", "choice_stale")
        assert sm.read_rng_context(sid, package, context=gm) == rng
        assert _roll(sm, sid, package, gm, source) == original
