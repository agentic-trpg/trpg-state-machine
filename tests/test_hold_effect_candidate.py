"""Hold Person lifecycle through the public SM command entry point."""

from copy import deepcopy
from dataclasses import replace
from random import Random

import pytest
from dnd5e_engine.evaluation_class_dependencies import operation_dependencies
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.rules.character import proficiency_grants
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import create, initialize
from item_support import NPC, item_data, item_loader

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.package import _validate, canonical_json

SECOND_TARGET = "npc.attendant"


def hold_world(tmp_path, context, *, seed=0, invalid_ac=False, caster_hp=30, two_targets=False):
    """Author a Wizard 5, one pouch and one or two living Humanoids in the encounter."""
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
    data["pc_templates"][0]["character"]["hp"] = {"current": caster_hp, "max": 30}
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
    first = next(actor for actor in data["actors"] if actor["actor_id"] == NPC)
    first["initial_cell"] = "1,0"
    if two_targets:
        second = deepcopy(first)
        second.update(actor_id=SECOND_TARGET, name="Attendant", initial_cell="2,0")
        second["npc_state"].update(
            initial_knowledge_ids=[], memories=[], beliefs=[], relationships=[], goals=[], plans=[]
        )
        data["actors"].append(second)
        data["scenes"][0]["actor_ids"].append(SECOND_TARGET)
        data["navigation"]["actor_grants"].append(
            {"actor_id": SECOND_TARGET, "region_ids": ["region.public"]}
        )
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
            hp_current=caster_hp,
            hp_max=30,
            ac=99 if invalid_ac else 10 + (pc["dexterity"] - 10) // 2,
            proficiency_bonus_override=None,
            attack_bonus=None,
            stealth_disadvantage=False,
        )
        pc.pop("spells_known", None)
        npc.update(hp_current=40, hp_max=40, creature_type="humanoid", granted_features=[])
        npc.pop("spells_known", None)
        if two_targets:
            snapshot["character_states"].append(
                {**deepcopy(npc), "entity_id": SECOND_TARGET, "name": "Attendant", "initiative": 0}
            )
            if "combat_state" in snapshot:
                combat = snapshot["combat_state"]
                for key in ("initiative_ids", "encounter_ids"):
                    combat[key].append(SECOND_TARGET)
                combat["actor_zone"][SECOND_TARGET] = "2,0"
                for key in ("movement_ledgers", "monster_slug_by_entity", "xp_value_by_entity"):
                    combat[key][SECOND_TARGET] = deepcopy(combat[key][NPC])
            else:
                foundation["xp"][SECOND_TARGET] = 0
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


def hold_cast_command(
    sm, sid, package, context, *, command_id="hold.cast", slot_level=2, target_ids=None
):
    """Create a public cast command pinned to the current world version."""
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        command_id,
        state.pc_actor_id,
        "combat.intent",
        CombatIntentPayload(
            intent_type="cast_spell",
            spell_id="hold-person",
            target_id=target_ids[0] if target_ids else NPC,
            target_ids=target_ids,
            slot_level=slot_level,
            spell_source_id="class:wizard",
        ),
        state.world_version,
    )


def hold_attack_command(sid, caster, world_version, *, command_id="hold.attack"):
    """Create the NPC's ordinary attack against the concentrating caster."""
    return TypedCommand(
        sid,
        command_id,
        NPC,
        "combat.intent",
        CombatIntentPayload(
            intent_type="attack", source_id=NPC, target_id=caster, stat_block_action_id="scimitar"
        ),
        world_version,
    )


def pass_command(sid, snapshot, command_id):
    """Use the actual current turn's actor and the fixed snapshot's version."""
    turn = snapshot.combat_state
    return TypedCommand(
        sid,
        command_id,
        turn.initiative_ids[turn.current_turn_index],
        "combat.intent",
        CombatIntentPayload(intent_type="pass"),
        snapshot.world_version,
    )


@pytest.mark.parametrize("seed", [0, 5])
@pytest.mark.parametrize("slot_level", [2, 3])
def test_hold_public_cast_and_reopen(tmp_path, context, seed, slot_level):
    """Adopt one real typed graph and its payment in the shared commit."""
    sm, sid, package, loader, db = hold_world(tmp_path, context, seed=seed)
    with sm:
        command = hold_cast_command(sm, sid, package, context, slot_level=slot_level)
        before = sm.read_combat_snapshot(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        (effect,) = after.effect_states
        assert effect.target_id == (NPC if seed == 0 else command.actor_id)
        assert tuple(after.combat_state.concentration_chain[command.actor_id]) == (
            (effect.target_id, effect.id, effect.origin),
        )
        target = next(actor for actor in after.character_states if actor.entity_id == NPC)
        assert [(cond.condition, cond.source_effect_id) for cond in target.conditions] == (
            [("paralyzed", effect.id)] if seed == 0 else []
        )
        caster = next(
            actor for actor in after.character_states if actor.entity_id == command.actor_id
        )
        expected_slots = {1: 4, 2: 3, 3: 2}
        expected_slots[slot_level] -= 1
        assert caster.spell_slots == expected_slots
        assert {
            pool.level: pool.current
            for pool in after.resource_state.pools
            if pool.kind == "spell_slot" and pool.owner_id == command.actor_id
        } == expected_slots
        assert after.combat_state.expended_resources[command.actor_id] == {
            f"spell_slot:{slot_level}": 1
        }
        assert before.effect_states == ()
        assert sm.submit_command(command, package, context=context) == receipt
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert reopened.get_command_receipt(sid, command.command_id, package, context=context) == (
            receipt
        )


def test_hold_level_below_spell_is_rejected_by_rules_engine(tmp_path, context, monkeypatch):
    """Keep every mechanical fact when real evaluation rejects an available level-one slot."""
    sm, sid, package, _, _ = hold_world(tmp_path, context)
    with sm:
        command = hold_cast_command(sm, sid, package, context, slot_level=1)
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm.read_events(sid, package, context=context)
        caster = next(
            actor for actor in before.character_states if actor.entity_id == command.actor_id
        )
        assert caster.spell_slots[1] == 4
        evaluate = sm._rules._evaluate_dnd
        evaluations = []

        async def record_evaluation(request):
            """Observe the actual rules-engine refusal without substituting a result."""
            result = await evaluate(request)
            evaluations.append((request.payload.slot_level, result.status, result.error.code))
            return result

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", record_evaluation)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "no_slot")
        assert receipt.world_version == before.world_version
        assert receipt.event_seqs == ()
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) == receipt
        assert sm.submit_command(command, package, context=context) == receipt
        assert evaluations == [(1, "rejected", "no_slot")]
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_events(sid, package, context=context) == before_events


@pytest.mark.parametrize("seed", [0, 1])
def test_hold_public_target_turn(tmp_path, context, seed):
    """Use the same RE graph for a target repeat save or empty anchor turn."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=seed)
    with sm:
        cast = hold_cast_command(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        turn = pass_command(sid, before, "hold.target.turn")
        assert turn.actor_id == NPC
        receipt = sm.submit_command(turn, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert len(after.effect_states) == (1 if seed == 0 else 0)
        target = next(actor for actor in after.character_states if actor.entity_id == NPC)
        assert bool(target.conditions) == (seed == 0)
        assert bool(after.combat_state.concentration_chain) == (seed == 0)
        assert after.world_version > before.world_version
        assert after.combat_state.current_turn_index != before.combat_state.current_turn_index


def test_hold_public_caster_turn_decrements(tmp_path, context):
    """RE's caster TurnEnd decrements its own concentration counter once."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=0)
    with sm:
        cast = hold_cast_command(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        first = sm.read_combat_snapshot(sid, package, context=context)
        target_turn = pass_command(sid, first, "hold.target.turn")
        assert target_turn.actor_id == NPC
        rng = sm.read_rng_context(sid, package, context=context)
        wrong_context = replace(context, controlled_npc_ids=())
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(target_turn, package, context=wrong_context)
        assert error.value.code == "unauthorized"
        assert sm.get_command_receipt(sid, target_turn.command_id, package, context=context) is None
        assert sm.read_combat_snapshot(sid, package, context=context) == first
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.submit_command(target_turn, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert before.combat_state.concentration_rounds_remaining[cast.actor_id] == 9
        caster_turn = pass_command(sid, before, "hold.caster.turn")
        assert caster_turn.actor_id == cast.actor_id
        assert sm.submit_command(caster_turn, package, context=context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.combat_state.concentration_rounds_remaining[cast.actor_id] == 8


def test_hold_public_caster_duration_expires(tmp_path, context):
    """A saved-against anchor expires on the ninth caster TurnEnd, then plain Pass works."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=5)
    with sm:
        cast = hold_cast_command(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        for turn in range(18):
            before = sm.read_combat_snapshot(sid, package, context=context)
            assert before.effect_states
            command = pass_command(sid, before, f"hold.duration.{turn}")
            assert sm.submit_command(command, package, context=context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert not after.effect_states
        assert not after.combat_state.concentration_chain
        assert not after.combat_state.concentration_rounds_remaining
        command = pass_command(sid, after, "hold.duration.plain")
        assert sm.submit_command(command, package, context=context).status == "committed"
        plain = sm.read_combat_snapshot(sid, package, context=context)
        assert plain.combat_state.current_turn_index != after.combat_state.current_turn_index
        assert not plain.effect_states


@pytest.mark.parametrize("seed", [9, 11])
def test_hold_public_damage_concentration(tmp_path, context, seed):
    """A real positive-HP attack against the caster adopts RE concentration."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=seed)
    with sm:
        cast = hold_cast_command(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert before.effect_states[0].target_id == cast.actor_id
        attack = hold_attack_command(sid, cast.actor_id, before.world_version)
        receipt = sm.submit_command(attack, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        hp_before = next(
            a for a in before.character_states if a.entity_id == cast.actor_id
        ).hp_current
        hp_after = next(
            a for a in after.character_states if a.entity_id == cast.actor_id
        ).hp_current
        assert 0 < hp_after < hp_before
        assert len(after.effect_states) == (0 if seed == 9 else 1)
        assert bool(after.combat_state.concentration_chain) == (seed == 11)
