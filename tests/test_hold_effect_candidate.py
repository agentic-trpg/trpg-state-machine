"""Controlled Hold Person candidate through the shared SM rule submission."""

import asyncio
from random import Random

import pytest
from dnd5e_engine.evaluation_class_dependencies import operation_dependencies
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.evaluation_spell_dependencies import invocation_dependencies
from dnd5e_engine.rules.character import proficiency_grants
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import create, initialize
from item_support import NPC, item_data, item_loader

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.package import _validate, canonical_json


def hold_world(tmp_path, context, *, seed=0, invalid_ac=False):
    """Author a Wizard 5, one component pouch and one living Humanoid."""
    canonical = BundledAssetLoader()
    base = item_loader()
    wizard = canonical.get_class("wizard")
    evoker = canonical.get_subclass("evoker")
    grants = {
        grant.slug
        for definition in (wizard, evoker)
        for grant in definition.granted_features
        if grant.ref_type == "feature" and grant.level <= 5
    }
    spell = canonical.get_spell("hold-person")
    loader = MemoryAssetLoader(
        items=[*base._items.values(), canonical.get_item("component-pouch")],
        monsters=list(base._monsters.values()),
        classes=[wizard],
        subclasses=[evoker],
        features=[canonical.get_feature(name) for name in sorted(grants)],
        spells=[spell],
        conditions=[canonical.get_condition("paralyzed")],
    )
    data = item_data()
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    data["manifest"]["package_version"] = "effect-01/1"
    owner = data["pc_templates"][0]["pc_template_id"]
    data["pc_templates"][0]["character"]["inventory"] = ["component-pouch"]
    data["pc_templates"][0]["character"]["hp"] = {"current": 30, "max": 30}
    data["scenes"][0]["objects"].append(
        {
            "object_id": "object.pouch",
            "state": None,
            "movable": False,
            "blocks_passage": False,
            "initial_cell": "2,1",
        }
    )
    data["item_transfers"].append(
        {
            "instance_id": "object.pouch",
            "item_slug": "component-pouch",
            "initial_owner_id": owner,
            "quantity": 1,
            "charges_remaining_per_unit": 0,
            "policy": "ordinary_public",
        }
    )
    next(actor for actor in data["actors"] if actor["actor_id"] == NPC)["initial_cell"] = "1,0"
    weapon_grants = proficiency_grants([(wizard, 5, "primary")])
    for foundation in data["rule_bindings"].values():
        foundation["rng_state"] = RNGState.capture(Random(seed)).model_dump(mode="json")
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/20"
        pc, npc = snapshot["character_states"]
        pc.update(
            class_slug="wizard",
            classes={"wizard": 5},
            character_level=5,
            subclass_slug="evoker",
            granted_features=sorted(grants),
            intelligence=18,
            spellcasting_ability="int",
            spell_slots={1: 4, 2: 3, 3: 2},
            custom_counters={"feature_use:arcane-recovery": {"spent": 0}},
            save_proficiencies=sorted(weapon_grants.saves),
            weapon_proficiencies=sorted(weapon_grants.weapons),
            carried_item_slugs=["component-pouch"],
            weapon_in_hands=None,
            weapon_grip="none",
            other_hand_occupied=False,
            hp_current=30,
            hp_max=30,
            ac=99 if invalid_ac else 10 + (pc["dexterity"] - 10) // 2,
            proficiency_bonus_override=None,
            attack_bonus=None,
            stealth_disadvantage=False,
        )
        pc.pop("spells_known", None)
        npc.update(hp_current=40, hp_max=40, creature_type="humanoid", granted_features=[])
        npc.pop("spells_known", None)
        snapshot["inventory_state"]["entries"] = [
            {
                "instance_id": "object.pouch",
                "owner_id": pc["entity_id"],
                "item_slug": "component-pouch",
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        ]
        snapshot["resource_state"] = {
            "pools": [
                {
                    "kind": "spell_slot",
                    "owner_id": pc["entity_id"],
                    "pool_id": f"spell:{level}",
                    "current": capacity,
                    "maximum": capacity,
                    "level": level,
                }
                for level, capacity in ((1, 4), (2, 3), (3, 2))
            ]
            + [
                {
                    "kind": "feature_uses",
                    "owner_id": pc["entity_id"],
                    "pool_id": "arcane-recovery",
                    "feature_slug": "arcane-recovery",
                    "current": 1,
                    "maximum": 1,
                }
            ]
        }
        snapshot["casting_state"] = {
            "actors": [
                {
                    "actor_id": pc["entity_id"],
                    "verbal_available": True,
                    "sources": [
                        {
                            "source_id": "class:wizard",
                            "class_slug": "wizard",
                            "ability": "int",
                            "spells": [
                                {
                                    "spell_id": "hold-person",
                                    "prepared_order": 0,
                                    "material": {
                                        "inventory_instance_id": "object.pouch",
                                        "available": True,
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    # The authored class/equipment profile must pass the same RE dependency
    # gate used by foundation admission.
    for foundation in data["rule_bindings"].values():
        if "combat_state" in foundation["snapshot"]:
            from dnd5e_engine.evaluation_state import CombatSnapshot

            snapshot = CombatSnapshot.model_validate_json(canonical_json(foundation["snapshot"]))
            failure = operation_dependencies(snapshot, loader, equipped=True)
            assert (failure is not None) == invalid_ac
    package = _validate(data)
    db = tmp_path / "hold.sqlite"
    sm = StateMachine(db, dnd_loader=loader)
    sid = create(sm, package, context)
    assert initialize(sm, sid, package, context).status == "committed"
    return sm, sid, package, loader, db


def test_foundation_rejects_unreviewed_wizard_ac(tmp_path, context):
    """Class allowlist removal still refuses an inconsistent authored AC."""
    with pytest.raises(StateMachineError) as error:
        hold_world(tmp_path, context, invalid_ac=True)
    assert error.value.code == "unsupported_combat_foundation"


def candidate_submit(sm, command, package, context):
    """Use the internal opt-in after the same context and fingerprint preparation."""
    with sm._store.transaction():
        row = sm._sessions._session(command.session_id, package, context)
        fingerprint = sm._session_runtime.fingerprint(command, row, context)
    return asyncio.run(
        sm._combat._submit_dnd(command, package, context, fingerprint, hold_candidate=True)
    )


@pytest.mark.parametrize("seed", [0, 5])
def test_hold_candidate_cast_and_reopen(tmp_path, context, seed):
    """Adopt one real typed graph and its payment in the shared commit."""
    sm, sid, package, loader, db = hold_world(tmp_path, context, seed=seed)
    with sm:
        state = sm.read_session(sid, package, context=context)
        command = TypedCommand(
            sid,
            "hold.cast",
            state.pc_actor_id,
            "combat.intent",
            CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="hold-person",
                target_id=NPC,
                slot_level=2,
                spell_source_id="class:wizard",
            ),
            state.world_version,
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert invocation_dependencies(before, loader, NPC, damaging=True) is None
        receipt = candidate_submit(sm, command, package, context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert len(after.effect_states) == 1
        assert after.effect_states[0].target_id == (NPC if seed == 0 else state.pc_actor_id)
        assert after.combat_state.concentration_chain[state.pc_actor_id]
        assert before.effect_states == ()
        assert candidate_submit(sm, command, package, context) == receipt
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after


@pytest.mark.parametrize("seed", [0, 1])
def test_hold_candidate_target_turn(tmp_path, context, seed):
    """Use the same RE graph for a target repeat save or empty anchor turn."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=seed)
    with sm:
        state = sm.read_session(sid, package, context=context)
        cast = TypedCommand(
            sid,
            "hold.cast",
            state.pc_actor_id,
            "combat.intent",
            CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="hold-person",
                target_id=NPC,
                slot_level=2,
                spell_source_id="class:wizard",
            ),
            state.world_version,
        )
        assert candidate_submit(sm, cast, package, context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        actor_id = before.combat_state.initiative_ids[before.combat_state.current_turn_index]
        assert actor_id == NPC
        turn = TypedCommand(
            sid,
            "hold.target.turn",
            actor_id,
            "combat.intent",
            CombatIntentPayload(intent_type="pass"),
            before.world_version,
        )
        receipt = candidate_submit(sm, turn, package, context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert len(after.effect_states) == (1 if seed == 0 else 0)
        assert after.world_version > before.world_version
        assert after.combat_state.current_turn_index != before.combat_state.current_turn_index


def test_hold_candidate_caster_turn_decrements(tmp_path, context):
    """RE's caster TurnEnd decrements its own concentration counter once."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=0)
    with sm:
        state = sm.read_session(sid, package, context=context)
        cast = TypedCommand(
            sid,
            "hold.cast",
            state.pc_actor_id,
            "combat.intent",
            CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="hold-person",
                target_id=NPC,
                slot_level=2,
                spell_source_id="class:wizard",
            ),
            state.world_version,
        )
        assert candidate_submit(sm, cast, package, context).status == "committed"
        first = sm.read_combat_snapshot(sid, package, context=context)
        target_turn = TypedCommand(
            sid,
            "hold.target.turn",
            NPC,
            "combat.intent",
            CombatIntentPayload(intent_type="pass"),
            first.world_version,
        )
        assert candidate_submit(sm, target_turn, package, context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert before.combat_state.concentration_rounds_remaining[state.pc_actor_id] == 9
        caster_turn = TypedCommand(
            sid,
            "hold.caster.turn",
            state.pc_actor_id,
            "combat.intent",
            CombatIntentPayload(intent_type="pass"),
            before.world_version,
        )
        assert candidate_submit(sm, caster_turn, package, context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.combat_state.concentration_rounds_remaining[state.pc_actor_id] == 8


def test_hold_candidate_caster_duration_expires(tmp_path, context):
    """An anchor from a successful initial save expires on the ninth caster TurnEnd."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=5)
    with sm:
        state = sm.read_session(sid, package, context=context)
        cast = TypedCommand(
            sid,
            "hold.cast",
            state.pc_actor_id,
            "combat.intent",
            CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="hold-person",
                target_id=NPC,
                slot_level=2,
                spell_source_id="class:wizard",
            ),
            state.world_version,
        )
        assert candidate_submit(sm, cast, package, context).status == "committed"
        for turn in range(18):
            before = sm.read_combat_snapshot(sid, package, context=context)
            assert before.effect_states
            actor_id = before.combat_state.initiative_ids[before.combat_state.current_turn_index]
            command = TypedCommand(
                sid,
                f"hold.duration.{turn}",
                actor_id,
                "combat.intent",
                CombatIntentPayload(intent_type="pass"),
                before.world_version,
            )
            assert candidate_submit(sm, command, package, context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert not after.effect_states
        assert not after.combat_state.concentration_chain
        assert not after.combat_state.concentration_rounds_remaining


@pytest.mark.parametrize("seed", [9, 11])
def test_hold_candidate_damage_concentration(tmp_path, context, seed):
    """A real positive-HP attack against the caster adopts RE concentration."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=seed)
    with sm:
        state = sm.read_session(sid, package, context=context)
        cast = TypedCommand(
            sid,
            "hold.cast",
            state.pc_actor_id,
            "combat.intent",
            CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="hold-person",
                target_id=NPC,
                slot_level=2,
                spell_source_id="class:wizard",
            ),
            state.world_version,
        )
        assert candidate_submit(sm, cast, package, context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert before.effect_states[0].target_id == state.pc_actor_id
        attack = TypedCommand(
            sid,
            "hold.attack",
            NPC,
            "combat.intent",
            CombatIntentPayload(
                intent_type="attack",
                source_id=NPC,
                target_id=state.pc_actor_id,
                stat_block_action_id="scimitar",
            ),
            before.world_version,
        )
        receipt = candidate_submit(sm, attack, package, context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        hp_before = next(
            a for a in before.character_states if a.entity_id == state.pc_actor_id
        ).hp_current
        hp_after = next(
            a for a in after.character_states if a.entity_id == state.pc_actor_id
        ).hp_current
        assert 0 < hp_after < hp_before
        assert len(after.effect_states) == (0 if seed == 9 else 1)
