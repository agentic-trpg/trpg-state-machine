"""Exploration shield binding changes a later real attack's AC decision."""

import random

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import initialize
from item_support import DAGGER, NPC
from spatial_support import create
from test_equipment_instance_bindings import combat_equipment_data

from trpg_state_machine import (
    EquipShield,
    StateMachine,
    StateMachineError,
    TrustedContext,
    TypedCommand,
)
from trpg_state_machine.package import _validate


def _loader():
    """Supply the reviewed ordinary Cleric Protector assets and NPC action."""
    bundled = BundledAssetLoader()
    return MemoryAssetLoader(
        classes=[bundled.get_class("cleric")],
        features=[
            bundled.get_feature(slug)
            for slug in ("spellcasting", "divine-order", "divine-order-protector")
        ],
        items=[
            bundled.get_item(slug) for slug in ("mace", "chain-shirt", "shield", "dagger", "rope")
        ],
        monsters=[bundled.get_monster("bandit")],
    )


def _package():
    """Create one shieldless /19 Cleric with a reachable spare shield."""
    data = combat_equipment_data()
    data["manifest"]["ruleset_binding"] = ruleset_binding(_loader()).model_dump(mode="json")
    next(t for t in data["item_transfers"] if t["instance_id"] == DAGGER)["item_slug"] = "mace"
    for transfer in data["item_transfers"]:
        if transfer["item_slug"] == "leather-armor":
            transfer["item_slug"] = "chain-shirt"
    data["pc_templates"][0]["character"]["inventory"] = [
        "mace" if slug == "rapier" else "chain-shirt" if slug == "leather-armor" else slug
        for slug in data["pc_templates"][0]["character"]["inventory"]
    ]
    for foundation in data["rule_bindings"].values():
        foundation["rng_state"] = RNGState.capture(random.Random(12)).model_dump(mode="json")
        if "combat_state" in foundation["snapshot"]:
            foundation["snapshot"]["combat_state"]["current_turn_index"] = 1
        foundation["snapshot"]["resource_state"] = {
            "pools": [
                {
                    "kind": "spell_slot",
                    "owner_id": "pc_template.observer",
                    "pool_id": "spell:1",
                    "current": 2,
                    "maximum": 2,
                    "level": 1,
                }
            ]
        }
        actor = foundation["snapshot"]["character_states"][0]
        actor.update(
            ac=15,
            class_slug="cleric",
            classes={"cleric": 1},
            granted_features=["spellcasting", "divine-order", "divine-order-protector"],
            save_proficiencies=["wis", "cha"],
            weapon_proficiencies=[
                "simple_melee",
                "simple_ranged",
                "martial_melee",
                "martial_ranged",
            ],
            spell_slots={1: 2},
            worn_armor="medium",
            weapon_in_hands="mace",
            shield_equipped=False,
            other_hand_occupied=False,
            equipped_shield_instance_id=None,
        )
        actor["carried_item_slugs"] = [
            "mace" if slug == "rapier" else "chain-shirt" if slug == "leather-armor" else slug
            for slug in actor["carried_item_slugs"]
        ]
        for entry in foundation["snapshot"]["inventory_state"]["entries"]:
            if entry["item_slug"] == "rapier":
                entry["item_slug"] = "mace"
            elif entry["item_slug"] == "leather-armor":
                entry["item_slug"] = "chain-shirt"
    return _validate(data)


@pytest.mark.parametrize("equip_shield", [False, True])
def test_shield_changes_real_attack_result(tmp_path, equip_shield):
    """The same NPC roll hits AC 15 and misses AC 17 after an exploration swap."""
    context = TrustedContext("authenticated:alice", "player.alice", (NPC,))
    package = _package()
    with StateMachine(tmp_path / "attack.db", dnd_loader=_loader()) as sm:
        sid = create(sm, package, context)
        actor_id = sm.read_session(sid, package, context=context).pc_actor_id
        old = sm.read_noncombat_snapshot(sid, package, context=context)
        if equip_shield:
            command = TypedCommand(
                sid,
                "equip.shield",
                actor_id,
                "world.equip_shield",
                EquipShield("object.shield_spare"),
                sm.read_session(sid, package, context=context).world_version,
            )
            assert sm.submit_command(command, package, context=context).status == "committed"
            assert next(a for a in old.character_states if a.entity_id == actor_id).ac == 15
        assert initialize(sm, sid, package, context).status == "committed"
        intent = CombatIntentPayload(
            intent_type="attack",
            source_id=NPC,
            stat_block_action_id="scimitar",
            target_id=actor_id,
        )
        attack = TypedCommand(
            sid,
            "npc.attack",
            NPC,
            "combat.intent",
            intent,
            sm.read_session(sid, package, context=context).world_version,
        )
        assert sm.submit_command(attack, package, context=context).status == "committed"
        rolls = [
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.event_type == "combat.engine_event"
            and event.payload["engine_event"].get("type") == "attack_rolled"
        ]
        assert len(rolls) == 1
        assert (rolls[0]["natural"], rolls[0]["roll_total"], rolls[0]["is_hit"]) == (
            16,
            16,
            not equip_shield,
        )
        actor = next(
            actor
            for actor in sm.read_combat_snapshot(sid, package, context=context).character_states
            if actor.entity_id == actor_id
        )
        assert (actor.ac, actor.hp_current) == ((17, 8) if equip_shield else (15, 5))


def test_unreviewed_class_foundation_remains_rejected():
    """The narrow /19 exception cannot admit unrelated class levels."""
    data = combat_equipment_data()
    for foundation in data["rule_bindings"].values():
        actor = foundation["snapshot"]["character_states"][0]
        actor.update(classes={"fighter": 3}, class_slug="fighter", character_level=3)
    with pytest.raises(StateMachineError) as error:
        _validate(data)
    assert error.value.code == "unsupported_combat_foundation"
