"""Haste and Lethargy through the public combat command and character reads."""

from dataclasses import replace

import pytest
from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_engine.events import ConditionApplied
from dnd5e_engine.rules.character import proficiency_grants
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from dnd_support import create, initialize
from item_support import NPC, item_data, item_loader
from test_hold_effect_atomicity import _effect_counts
from test_hold_effect_candidate import pass_command

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.package import _validate
from trpg_state_machine.perception import GameViewContext


@pytest.fixture
def haste_combat(tmp_path, context):
    """Initialize combat with a Wizard 5, prepared Haste and its material binding."""
    canonical = BundledAssetLoader()
    base = item_loader()
    wizard = canonical.get_class("wizard")
    evoker = canonical.get_subclass("evoker")
    features = sorted(
        {
            grant.slug
            for definition in (wizard, evoker)
            for grant in definition.granted_features
            if grant.ref_type == "feature" and grant.level <= 5
        }
    )
    component = "component-pouch"
    pouch_id = "object.pouch"
    assets = MemoryAssetLoader(
        items=[*base._items.values(), canonical.get_item(component)],
        monsters=list(base._monsters.values()),
        classes=[wizard],
        subclasses=[evoker],
        features=[canonical.get_feature(slug) for slug in features],
        spells=[canonical.get_spell("haste")],
        conditions=[canonical.get_condition("incapacitated")],
    )
    data = item_data()
    data["manifest"]["ruleset_binding"] = ruleset_binding(assets).model_dump(mode="json")
    template = data["pc_templates"][0]
    template["character"]["inventory"] = [component]
    data["scenes"][0]["objects"].append(
        {
            "object_id": pouch_id,
            "state": None,
            "movable": False,
            "blocks_passage": False,
            "initial_cell": "2,1",
        }
    )
    data["item_transfers"].append(
        {
            "instance_id": pouch_id,
            "item_slug": component,
            "initial_owner_id": template["pc_template_id"],
            "quantity": 1,
            "charges_remaining_per_unit": 0,
            "policy": "ordinary_public",
        }
    )
    proficiency = proficiency_grants([(wizard, 5, "primary")])
    slots = {1: 4, 2: 3, 3: 2}
    for foundation in data["rule_bindings"].values():
        snapshot = foundation["snapshot"]
        snapshot["snapshot_schema_version"] = "engine-snapshot/20"
        pc, npc = snapshot["character_states"]
        pc.update(
            class_slug="wizard",
            classes={"wizard": 5},
            character_level=5,
            subclass_slug="evoker",
            granted_features=features,
            intelligence=18,
            spellcasting_ability="int",
            spell_slots=slots,
            custom_counters={"feature_use:arcane-recovery": {"spent": 0}},
            save_proficiencies=sorted(proficiency.saves),
            weapon_proficiencies=sorted(proficiency.weapons),
            carried_item_slugs=[component],
            weapon_in_hands=None,
            weapon_grip="none",
            other_hand_occupied=False,
            ac=10 + (pc["dexterity"] - 10) // 2,
            proficiency_bonus_override=None,
            attack_bonus=None,
            stealth_disadvantage=False,
        )
        pc.pop("spells_known", None)
        npc.update(creature_type="humanoid", granted_features=[])
        npc.pop("spells_known", None)
        caster_id = pc["entity_id"]
        snapshot["inventory_state"]["entries"] = [
            {
                "instance_id": pouch_id,
                "owner_id": caster_id,
                "item_slug": component,
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "accessible": True,
            }
        ]
        snapshot["resource_state"] = {
            "pools": [
                {
                    "kind": "spell_slot",
                    "owner_id": caster_id,
                    "pool_id": f"spell:{level}",
                    "current": capacity,
                    "maximum": capacity,
                    "level": level,
                }
                for level, capacity in slots.items()
            ]
            + [
                {
                    "kind": "feature_uses",
                    "owner_id": caster_id,
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
                    "actor_id": caster_id,
                    "verbal_available": True,
                    "sources": [
                        {
                            "source_id": "class:wizard",
                            "class_slug": "wizard",
                            "ability": "int",
                            "spells": [
                                {
                                    "spell_id": "haste",
                                    "prepared_order": 0,
                                    "material": {
                                        "inventory_instance_id": pouch_id,
                                        "available": True,
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    package = _validate(data)
    db = tmp_path / "haste.sqlite"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create(sm, package, context)
        assert initialize(sm, sid, package, context).status == "committed"
        yield sm, sid, package, assets, db


def _actor(snapshot, actor_id):
    """Find the participant's fixed character state."""
    return next(actor for actor in snapshot.character_states if actor.entity_id == actor_id)


def _active_actor(snapshot):
    """Read the actor whose natural initiative turn is currently open."""
    combat = snapshot.combat_state
    return combat.initiative_ids[combat.current_turn_index]


def _cast(sm, sid, package, context, *, target_id=None):
    """Cast Haste on the selected willing target and return the committed snapshot."""
    before = sm.read_combat_snapshot(sid, package, context=context)
    caster = sm.read_session(sid, package, context=context).pc_actor_id
    target_id = target_id or caster
    assert _active_actor(before) == caster
    assert _actor(before, caster).spell_slots[3] == 2
    command = TypedCommand(
        sid,
        "haste.cast",
        caster,
        "combat.intent",
        CombatIntentPayload(
            intent_type="cast_spell",
            spell_id="haste",
            target_id=target_id,
            willing_target_ids=(target_id,),
            slot_level=3,
            spell_source_id="class:wizard",
        ),
        before.world_version,
    )
    receipt = sm.submit_command(command, package, context=context)
    assert receipt.status == "committed", receipt
    after = sm.read_combat_snapshot(sid, package, context=context)
    (hasted,) = after.effect_states
    assert (hasted.id, hasted.name, hasted.target_id) == ("effect:hasted", "Hasted", target_id)
    assert after.combat_state.concentration_chain == {
        caster: [(target_id, hasted.id, hasted.origin)]
    }
    assert _actor(after, caster).spell_slots == {1: 4, 2: 3, 3: 1}
    assert sm.read_character_ac(sid, target_id, package, context=context).effects == 2
    assert sm.submit_command(command, package, context=context) == receipt
    assert sm.read_combat_snapshot(sid, package, context=context) == after
    return command, after


def _pass(sm, sid, package, context, before, command_id, actor_id):
    """Advance the current actor through one public Pass and return the committed snapshot."""
    command = pass_command(sid, before, command_id)
    assert command.actor_id == actor_id
    receipt = sm.submit_command(command, package, context=context)
    assert receipt.status == "committed", receipt
    return sm.read_combat_snapshot(sid, package, context=context)


def _lethargy(snapshot, target_id):
    """Check the standalone follow-up and its sourced Incapacitated condition."""
    (effect,) = snapshot.effect_states
    assert (effect.id, effect.name, effect.target_id) == ("effect:lethargy", "Lethargy", target_id)
    assert not snapshot.combat_state.concentration_chain
    assert not snapshot.combat_state.concentration_rounds_remaining
    (condition,) = _actor(snapshot, target_id).conditions
    assert (condition.condition, condition.source_effect_id) == (
        "incapacitated",
        effect.id,
    )
    return effect


def _assert_next_turn_expiry(sm, sid, package, context, before, target_id, intervening_id):
    """Pass the other actor's turn, then end the affected target's next natural turn."""
    lethargy = _lethargy(before, target_id)
    target_turn = _pass(sm, sid, package, context, before, "haste.pass.intervening", intervening_id)
    assert _active_actor(target_turn) == target_id
    assert _lethargy(target_turn, target_id) == lethargy
    assert _actor(target_turn, target_id).movement_remaining == 0
    ended = _pass(sm, sid, package, context, target_turn, "haste.pass.next_target", target_id)
    assert _active_actor(ended) == intervening_id
    assert ended.effect_states == ()
    assert _actor(ended, target_id).conditions == []
    assert ended.resource_state == before.resource_state
    assert _effect_counts(sm, sid) == (0, 0)


def test_haste_duration_ends_on_target_turn_and_lethargy_uses_the_following_turn(
    haste_combat, context
):
    """Let Haste run its duration and keep Lethargy through the next target TurnEnd."""
    sm, sid, package, _, _ = haste_combat
    cast, snapshot = _cast(sm, sid, package, context)
    caster = cast.actor_id
    cast_completed_turn = _active_actor(snapshot) == NPC
    if cast_completed_turn:
        snapshot = _pass(sm, sid, package, context, snapshot, "haste.pass.opening", NPC)

    remaining = snapshot.combat_state.concentration_rounds_remaining[caster]
    assert remaining == (9 if cast_completed_turn else 10)
    for turn in range(remaining):
        assert snapshot.combat_state.concentration_rounds_remaining[caster] == remaining - turn
        (effect,) = snapshot.effect_states
        assert effect.id == "effect:hasted"
        snapshot = _pass(sm, sid, package, context, snapshot, f"haste.pass.caster.{turn}", caster)
        if turn + 1 < remaining:
            snapshot = _pass(sm, sid, package, context, snapshot, f"haste.pass.npc.{turn}", NPC)

    _assert_next_turn_expiry(sm, sid, package, context, snapshot, caster, NPC)


def _prepare_drop(sm, sid, package, context, *, target_id=None):
    """Start the affected target's turn and pin its caster's release command."""
    cast, before = _cast(sm, sid, package, context, target_id=target_id)
    target_id = target_id or cast.actor_id
    active = _active_actor(before)
    if active != target_id:
        before = _pass(sm, sid, package, context, before, "haste.pass.opening", active)
    assert _active_actor(before) == target_id
    command = replace(
        cast,
        command_id="haste.drop",
        payload=CombatIntentPayload(intent_type="drop_concentration"),
        expected_world_version=before.world_version,
    )
    return command, before


def test_haste_drop_follow_up_rolls_back_and_replays_as_one_command(haste_combat, context):
    """Fail the receipt after the effect replacement, then retry and finish Lethargy."""
    sm, sid, package, assets, db = haste_combat
    command, before = _prepare_drop(sm, sid, package, context)
    caster = command.actor_id
    rng = sm.read_rng_context(sid, package, context=context)
    events = sm.read_events(sid, package, context=context)
    assert _effect_counts(sm, sid) == (1, 1)
    sm._conn.execute(
        "CREATE TEMP TRIGGER fail_haste_drop BEFORE INSERT ON command_receipts "
        "WHEN NEW.command_id='haste.drop' AND "
        "NOT EXISTS(SELECT 1 FROM effect_concentrations WHERE session_id=NEW.session_id) AND "
        "NOT EXISTS(SELECT 1 FROM ongoing_effects "
        "WHERE session_id=NEW.session_id AND effect_id='effect:hasted') AND "
        "EXISTS(SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id "
        "AND effect_id='effect:lethargy' AND concentration_id IS NULL "
        "AND conditions_json IS NOT NULL AND lifecycle_json IS NOT NULL) "
        "BEGIN SELECT RAISE(ABORT,'injected Haste receipt failure'); END"
    )
    with pytest.raises(StateMachineError) as failure:
        sm.submit_command(command, package, context=context)
    assert failure.value.code == "storage_error"
    assert str(failure.value.__cause__) == "injected Haste receipt failure"
    assert _effect_counts(sm, sid) == (1, 1)
    assert sm.read_combat_snapshot(sid, package, context=context) == before
    assert sm.read_rng_context(sid, package, context=context) == rng
    assert sm.read_events(sid, package, context=context) == events
    assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
    with StateMachine(db, dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == before

    sm._conn.execute("DROP TRIGGER fail_haste_drop")
    receipt = sm.submit_command(command, package, context=context)
    assert receipt.status == "committed", receipt
    after = sm.read_combat_snapshot(sid, package, context=context)
    assert after.world_version == before.world_version + 1 == receipt.world_version
    assert _active_actor(after) == caster
    lethargy = _lethargy(after, caster)
    assert after.resource_state == before.resource_state
    assert sm.read_rng_context(sid, package, context=context) == rng
    assert sm.read_character_ac(sid, caster, package, context=context).effects == 0
    assert _effect_counts(sm, sid) == (0, 1)
    assert before.effect_states[0].id == "effect:hasted"
    assert sm.read_session(sid, package, context=context).character.effects == (
        lethargy.model_dump(mode="json"),
    )
    viewer = GameViewContext(context.principal_id, sid, context.player_id, "player")
    assert sm.get_scene_view(sid, package, context=viewer).own_character.effects == (
        {"id": lethargy.id, "name": lethargy.name},
    )
    with StateMachine(db, dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert reopened.submit_command(command, package, context=context) == receipt
        assert _effect_counts(reopened, sid) == (0, 1)

    later = _pass(sm, sid, package, context, after, "haste.pass.current_target", caster)
    assert _lethargy(later, caster) == lethargy
    _assert_next_turn_expiry(sm, sid, package, context, later, caster, NPC)
    assert sm.get_scene_view(sid, package, context=viewer).own_character.effects == ()


def test_haste_drop_uses_affected_target_turn_when_caster_and_target_differ(haste_combat, context):
    """Keep Lethargy after the target's current turn and across the caster's turn."""
    sm, sid, package, _, _ = haste_combat
    command, before = _prepare_drop(sm, sid, package, context, target_id=NPC)
    caster = command.actor_id
    assert _actor(before, caster).concentration_effect_id == "effect:hasted"
    receipt = sm.submit_command(command, package, context=context)
    assert receipt.status == "committed", receipt
    after = sm.read_combat_snapshot(sid, package, context=context)
    lethargy = _lethargy(after, NPC)
    assert _active_actor(after) == NPC
    assert _actor(after, caster).conditions == []
    assert _actor(after, caster).concentration_effect_id is None

    later = _pass(sm, sid, package, context, after, "haste.pass.current_target", NPC)
    assert _lethargy(later, NPC) == lethargy
    _assert_next_turn_expiry(sm, sid, package, context, later, NPC, caster)


def test_haste_drop_rejects_follow_up_with_forged_empty_condition_lineage(
    haste_combat, context, monkeypatch
):
    """Reject a shape-valid Lethargy proposal that suppresses Incapacitated."""
    sm, sid, package, assets, db = haste_combat
    command, before = _prepare_drop(sm, sid, package, context)
    assert "incapacitated" not in _actor(before, command.actor_id).condition_immunities
    rng = sm.read_rng_context(sid, package, context=context)
    events = sm.read_events(sid, package, context=context)
    evaluate = sm._rules._evaluate_dnd

    async def forge(request):
        """Keep the real child and clock while omitting all evidence of its condition."""
        result = await evaluate(request)
        assert result.status == "accepted"
        operations = result.state_delta.operations
        (follow_up,) = (op for op in operations if type(op) is delta.EffectFollowUpCreate)
        assert follow_up.lineage.statuses == ["incapacitated"]
        assert sum(type(op) is delta.ConditionsUpdate for op in operations) == 1
        assert sum(isinstance(event, ConditionApplied) for event in result.proposed_events) == 1
        empty = follow_up.lineage.model_copy(update={"statuses": []})
        operations = tuple(
            follow_up.model_copy(update={"lineage": empty}) if op is follow_up else op
            for op in operations
            if type(op) is not delta.ConditionsUpdate
        )
        forged = result.model_copy(
            update={
                "state_delta": result.state_delta.model_copy(update={"operations": operations}),
                "proposed_events": tuple(
                    event
                    for event in result.proposed_events
                    if not isinstance(event, ConditionApplied)
                ),
            }
        )
        forged = type(result).model_validate_json(forged.model_dump_json())
        forged.verify_request(request)
        return forged

    with monkeypatch.context() as patch:
        patch.setattr(sm._rules, "_evaluate_dnd", forge)
        with pytest.raises(StateMachineError, match="Follow-up condition lineage") as failure:
            sm.submit_command(command, package, context=context)
    assert failure.value.code == "invalid_rule_result"
    assert sm.read_combat_snapshot(sid, package, context=context) == before
    assert sm.read_rng_context(sid, package, context=context) == rng
    assert sm.read_events(sid, package, context=context) == events
    assert _effect_counts(sm, sid) == (1, 1)
    assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
    with StateMachine(db, dnd_loader=assets) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == before

    assert sm.submit_command(command, package, context=context).status == "committed"
    _lethargy(sm.read_combat_snapshot(sid, package, context=context), command.actor_id)
