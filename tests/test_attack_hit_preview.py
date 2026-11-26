"""Public authorization and read-only hit preview for current combat attacks."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_ruleset import get_default_ruleset, ruleset_binding
from dnd5e_srd_data import MemoryAssetLoader
from dnd_support import initialize
from item_support import DAGGER, item_loader
from spatial_support import create
from test_container_admission import authority
from test_equipment_instance_bindings import combat_equipment_data

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.package import _validate


@pytest.mark.parametrize(
    "target_prone,target_dodging,expected_mode,expected_fraction,advantage,disadvantage",
    [
        (False, False, "normal", (3, 5), (), ()),
        (True, False, "advantage", (21, 25), ("target:Prone",), ()),
        (False, True, "disadvantage", (9, 25), (), ("dodge",)),
        (True, True, "normal", (3, 5), ("target:Prone",), ("dodge",)),
    ],
)
def test_preview_requires_trusted_mechanics_grant_and_submit_revalidates(
    tmp_path,
    context,
    target_prone,
    target_dodging,
    expected_mode,
    expected_fraction,
    advantage,
    disadvantage,
):
    data = combat_equipment_data(version="engine-snapshot/20")
    old_loader = item_loader()
    canonical = get_default_ruleset()
    loader = MemoryAssetLoader(
        items=[old_loader.get_item(slug) for slug in old_loader.list_slugs("items")],
        monsters=[old_loader.get_monster(slug) for slug in old_loader.list_slugs("monsters")],
        conditions=[canonical.get_condition("prone")],
    )
    data["manifest"]["ruleset_binding"] = ruleset_binding(loader).model_dump(mode="json")
    for foundation in data["rule_bindings"].values():
        target = next(
            actor
            for actor in foundation["snapshot"]["character_states"]
            if actor["entity_id"] == "npc.caretaker"
        )
        target["ac"] = 15
        target["dodging"] = target_dodging
    package = _validate(data)
    with StateMachine(tmp_path / "combat.db", dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        if target_prone:
            with sm._transaction(write=True):
                sm._characters.add_condition(
                    sid,
                    "npc.caretaker",
                    {
                        "condition": "prone",
                        "source_entity_id": "implied:event",
                        "scope": "combat",
                        "duration_rounds": None,
                        "save_dc": None,
                        "applied_round": 0,
                        "exhaustion_level": 1,
                        "source_effect_id": None,
                    },
                )
        session = sm.read_session(sid, package, context=context)
        actor = session.pc_actor_id
        fixed = sm.read_combat_snapshot(sid, package, context=context)
        attacker_state = next(a for a in fixed.character_states if a.entity_id == actor)
        target_state = next(a for a in fixed.character_states if a.entity_id == "npc.caretaker")
        assert (attacker_state.dexterity, attacker_state.proficiency_bonus_override) == (18, 2)
        assert attacker_state.attack_bonus is None  # Dex +4 and proficiency +2.
        assert target_state.ac == 15
        payload = CombatIntentPayload(
            intent_type="attack",
            source_id=actor,
            target_id="npc.caretaker",
            weapon_instance_id=DAGGER,
        )
        before_rows = authority(sm)
        before_rng = sm.read_rng_context(sid, package, context=context)
        hidden = sm.query_action_availability(sid, actor, payload, package, context=context)
        assert hidden.status == "available"
        assert hidden.hit_preview.status == "unknown"
        with pytest.raises(StateMachineError):
            sm.query_action_availability(
                sid,
                actor,
                payload,
                package,
                context=replace(
                    context, principal_id="intruder", can_preview_attack_mechanics=True
                ),
            )
        privileged = replace(context, can_preview_attack_mechanics=True)
        visible = sm.query_action_availability(sid, actor, payload, package, context=privileged)
        assert visible.status == "available"
        assert visible.hit_preview.status == "known", visible
        assert visible.hit_preview.mode == expected_mode
        assert visible.hit_preview.advantage_reasons == advantage
        assert visible.hit_preview.disadvantage_reasons == disadvantage
        assert (
            visible.hit_preview.probability_numerator,
            visible.hit_preview.probability_denominator,
        ) == expected_fraction
        assert visible.snapshot_world_version == session.world_version
        assert authority(sm) == before_rows
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        command = TypedCommand(
            sid, "preview.followup", actor, "combat.intent", payload, session.world_version
        )
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        no_budget = sm.query_action_availability(sid, actor, payload, package, context=privileged)
        assert no_budget.status == "unavailable"
        assert no_budget.hit_preview.status == "unknown"
