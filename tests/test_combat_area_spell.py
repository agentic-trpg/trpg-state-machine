"""Exercise point-targeted save damage through the public combat command."""

from random import Random

import pytest
from dnd5e_engine.evaluation_rng import RNGState, RNGTransition
from test_cure_wounds import actor
from test_ordinary_spells import assert_committed_events, observe_evaluation
from test_spell_attack_cast import NPC, OTHER_ENEMY, cast_command
from test_spell_attack_cast import open_spell_combat as open_spell_combat

from trpg_state_machine import StateMachine, StateMachineError

ACID_SPLASH = "acid-splash"


@pytest.mark.parametrize("starting_hp", [40, 1])
def test_point_cast_adopts_two_saves_and_shared_damage(
    open_spell_combat, context, monkeypatch, starting_hp
):
    """Commit distinct save evidence and one shared damage roll for the in-range creatures."""
    seed = 2
    sm, sid, package, loader, db = open_spell_combat(
        seed=seed,
        column=2,
        hp=starting_hp,
        temp_hp=0,
        extra_spells=(ACID_SPLASH,),
        other_enemy_turn=True,
    )
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context)
        old_rng = sm.read_rng_context(sid, package, context=context)
        command = cast_command(
            sm, sid, package, context, ACID_SPLASH, target_id=None, target_zone_id="2,0"
        )
        observed = observe_evaluation(sm, monkeypatch)

        receipt = sm.submit_command(command, package, context=context)

        assert receipt.status == "committed", receipt
        (result,) = observed
        (area,) = [event for event in result.proposed_events if event.type == "area_targeted"]
        assert (area.origin, area.shape, area.size_ft) == ("2,0", "sphere", 5)
        targets = [OTHER_ENEMY, NPC]
        assert area.affected_ids == targets
        assert not area.excluded_ids
        saves = [event for event in result.proposed_events if event.type == "save_rolled"]
        damage = [event for event in result.proposed_events if event.type == "damage_applied"]
        assert [event.target_id for event in saves] == targets
        assert [event.target_id for event in damage] == targets
        rolls = Random(seed)
        base_damage = rolls.randint(1, 6)
        for save, applied in zip(saves, damage, strict=True):
            assert (save.ability, save.dc, save.natural, save.succeeded) == (
                "dex",
                13,
                rolls.randint(1, 20),
                False,
            )
            assert (applied.damage_type, applied.amount) == ("acid", base_damage)
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.resource_state == before.resource_state
        assert after.combat_state.expended_resources == before.combat_state.expended_resources
        assert after.combat_state.turn_serial == before.combat_state.turn_serial
        assert after.combat_state.damage_instance_sequence == (
            before.combat_state.damage_instance_sequence + len(targets)
        )
        for identity in targets:
            assert actor(before, identity).hp_current == starting_hp
            assert actor(after, identity).hp_current == starting_hp - base_damage
        assert after.combat_state.dead_ids == (set(targets) if starting_hp == 1 else set())
        assert (
            actor(after, command.actor_id).hp_current == actor(before, command.actor_id).hp_current
        )
        after_rng = sm.read_rng_context(sid, package, context=context)
        assert after_rng.version == old_rng.version + 1
        assert after_rng.state == RNGState.capture(rolls)
        assert_committed_events(sm, sid, package, context, receipt, result)
        assert sm.submit_command(command, package, context=context) == receipt
        assert len(observed) == 1
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after


@pytest.mark.parametrize(
    "selection",
    [
        {"target_zone_id": "25,0", "target_id": None},
        {"target_zone_id": "2,0", "target_id": NPC},
        {"target_zone_id": "2,0", "target_id": None, "spell_source_id": "class:cleric"},
        {"target_zone_id": "0,0", "target_id": None},
    ],
)
def test_invalid_area_cast_leaves_combat_and_rng_unchanged(open_spell_combat, context, selection):
    """Refuse an out-of-profile placement, selection or caster source before payment."""
    sm, sid, package, _, _ = open_spell_combat(column=2, extra_spells=(ACID_SPLASH,))
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        command = cast_command(sm, sid, package, context, ACID_SPLASH, **selection)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "rejected", receipt
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng


def test_empty_area_spends_action_without_rng(open_spell_combat, context, monkeypatch):
    """Adopt a reachable empty point without inventing a creature target or damage roll."""
    sm, sid, package, _, _ = open_spell_combat(column=4, extra_spells=(ACID_SPLASH,))
    with sm:
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        command = cast_command(
            sm, sid, package, context, ACID_SPLASH, target_id=None, target_zone_id="2,0"
        )
        observed = observe_evaluation(sm, monkeypatch)

        receipt = sm.submit_command(command, package, context=context)

        assert receipt.status == "committed", receipt
        (result,) = observed
        (area,) = [event for event in result.proposed_events if event.type == "area_targeted"]
        assert area.origin == "2,0"
        assert not area.affected_ids
        assert not any(e.type in {"save_rolled", "damage_applied"} for e in result.proposed_events)
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert (
            after.combat_state.damage_instance_sequence
            == before.combat_state.damage_instance_sequence
        )
        assert after.combat_state.turn_serial == before.combat_state.turn_serial
        assert not actor(after, command.actor_id).action_available
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current
        assert sm.read_rng_context(sid, package, context=context) == rng


@pytest.mark.parametrize("mutation", ["empty-rng", "natural", "total", "outcome"])
def test_area_adoption_refuses_inconsistent_rng_or_save_evidence(
    open_spell_combat, context, monkeypatch, mutation
):
    """Refuse a forged RNG advance or incoherent d20 evidence without publishing state."""
    sm, sid, package, _, _ = open_spell_combat(
        seed=2, column=4 if mutation == "empty-rng" else 2, extra_spells=(ACID_SPLASH,)
    )
    with sm:
        evaluate = sm._rules._evaluate_dnd

        async def forged(request):
            """Alter the evidence while preserving the genuine placement and HP proposal."""
            result = await evaluate(request)
            assert result.status == "accepted", result.error
            if mutation == "empty-rng":
                rng = request.rng_context.state.restore()
                rng.randint(1, 6)
                return result.model_copy(
                    update={"rng_transition": RNGTransition.between(request.rng_context, rng)}
                )
            events = list(result.proposed_events)
            index = next(i for i, event in enumerate(events) if event.type == "save_rolled")
            save = events[index]
            updates = {
                "natural": {"natural": None},
                "total": {"roll_total": save.roll_total + 1},
                "outcome": {"succeeded": not save.succeeded},
            }
            events[index] = save.model_copy(update=updates[mutation])
            return result.model_copy(update={"proposed_events": tuple(events)})

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", forged)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        command = cast_command(
            sm, sid, package, context, ACID_SPLASH, target_id=None, target_zone_id="2,0"
        )
        with pytest.raises(StateMachineError, match="Spell area saves and damage"):
            sm.submit_command(command, package, context=context)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
