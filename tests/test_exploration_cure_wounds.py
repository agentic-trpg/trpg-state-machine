"""Public exploration healing through the bound Cure Wounds activity."""

from dataclasses import replace
from random import Random

import pytest
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_contracts import ExplorationSpellCastPayload
from dnd5e_engine.evaluation_effects import EffectChangeState, effect_state
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.types.effects import ActiveEffectDuration
from dnd5e_srd_data import BundledAssetLoader
from item_support import NPC
from test_cure_wounds import SOURCE, SPELL, actor, cure_world, pool

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand


def cast_command(
    sm, sid, package, context, target, *, spell=SPELL, slot_level=1, key="exploration.cure"
):
    """Select the authorized caster, supplied creature, and the prepared ordinary slot."""
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        key,
        state.pc_actor_id,
        "world.cast_spell",
        ExplorationSpellCastPayload(
            kind="world.cast_spell",
            spell_id=spell,
            spell_source_id=SOURCE,
            target_id=target or state.pc_actor_id,
            slot_level=slot_level,
        ),
        state.world_version,
    )


@pytest.mark.parametrize("target,target_hp,healed_hp", [(NPC, 2, 19), (NPC, 48, 50), (None, 2, 8)])
def test_public_heal_commits_payment_and_hp(tmp_path, context, target, target_hp, healed_hp):
    sm, sid, package, loader, db = cure_world(
        tmp_path, context, exploration=True, target_hp=target_hp, target_temp_hp=3
    )
    with sm:
        command = cast_command(sm, sid, package, context, target)
        target_id = command.payload.target_id
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        previous_world = sm.read_session(sid, package, context=context).world
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert actor(before, target_id).hp_current == target_hp
        assert actor(after, target_id).hp_current == healed_hp
        assert pool(before).current == 2 and pool(after).current == 1
        assert after.effect_states == before.effect_states == ()
        for creature in before.character_states:
            changes = {}
            if creature.entity_id == command.actor_id:
                changes["spell_slots"] = {1: 1}
            if creature.entity_id == target_id:
                changes["hp_current"] = healed_hp
            assert actor(after, creature.entity_id) == creature.model_copy(update=changes)
        assert sm.read_session(sid, package, context=context).world.game_tick == (
            previous_world.game_tick
        )
        events = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == command.command_id
        ]
        assert tuple(e.event_seq for e in events) == receipt.event_seqs
        assert [e.payload["engine_event"]["type"] for e in events] == [
            "intent_submitted",
            "spell_cast",
            "healing_applied",
        ]
        expected_rng = Random(0)
        faces = [expected_rng.randint(1, 8), expected_rng.randint(1, 8)]
        amount = sum(faces) + 3
        assert events[2].payload["engine_event"] == {
            "type": "healing_applied",
            "target_id": target_id,
            "amount": amount,
            "roll_detail": {
                "dice": [{"die_size": 8, "face": face} for face in faces],
                "fixed_addends": [
                    {"kind": "fixed", "source": "spell_bonus", "value": 3},
                    {"kind": "fixed", "source": "recipient_bonus", "value": 0},
                ],
            },
        }
        projected = sm.get_command_rolls(sid, command.command_id, package, context=context)
        assert projected["rolls"] == [
            {
                "event_seq": events[2].event_seq,
                "type": "healing_applied",
                "target_id": target_id,
                "amount": amount,
                "hp_gained": healed_hp - target_hp,
                "roll_detail": events[2].payload["engine_event"]["roll_detail"],
            }
        ]
        successor = sm.read_rng_context(sid, package, context=context)
        assert successor.version == rng.version + 1
        assert successor.state == RNGState.capture(expected_rng)
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_noncombat_snapshot(sid, package, context=context) == after
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_noncombat_snapshot(sid, package, context=context) == after
        assert reopened.read_rng_context(sid, package, context=context) == successor
        assert reopened.get_command_receipt(sid, command.command_id, package, context=context) == (
            receipt
        )
        assert reopened.get_command_rolls(sid, command.command_id, package, context=context) == (
            projected
        )


def test_full_hp_keeps_life_unchanged_and_pays_cast(tmp_path, context):
    sm, sid, package, _, _ = cure_world(tmp_path, context, exploration=True, target_hp=50)
    with sm:
        command = cast_command(sm, sid, package, context, NPC)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed" and len(receipt.event_seqs) == 3
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert actor(after, NPC) == actor(before, NPC)
        assert pool(after).current == pool(before).current - 1
        assert sm.read_rng_context(sid, package, context=context).version == rng.version + 1
        rolls = sm.get_command_rolls(sid, command.command_id, package, context=context)["rolls"]
        assert len(rolls) == 1 and len(rolls[0]["roll_detail"]["dice"]) == 2
        assert rolls[0]["hp_gained"] == 0


def _authored_zero_hp_target(data, _loader):
    """Supply an explicitly living downed NPC in an exploration-only foundation."""
    data["rule_bindings"].pop("combat_foundation")
    for foundation in data["rule_bindings"].values():
        target = foundation["snapshot"]["character_states"][1]
        target["death_saves"] = {"successes": 1, "failures": 2, "is_stable": False}
        target["conditions"] = [
            {
                "condition": name,
                "source_entity_id": source,
                "scope": "combat",
                "duration_rounds": None,
                "save_dc": None,
                "applied_round": 0,
                "exhaustion_level": 1,
                "source_effect_id": None,
            }
            for name, source in (
                ("unconscious", "implied:event"),
                ("unconscious", "implied:other"),
                ("prone", "implied:event"),
            )
        ]


def test_public_heal_recovers_authored_living_zero_hp_target(tmp_path, context):
    """An explicitly living downed creature uses the same paid public cast."""

    sm, sid, package, loader, db = cure_world(
        tmp_path, context, exploration=True, target_hp=0, configure=_authored_zero_hp_target
    )
    with sm:
        command = cast_command(sm, sid, package, context, NPC, key="exploration.zero.heal")
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        target = actor(after, NPC)
        assert target.is_alive and target.hp_current > 0
        assert actor(before, NPC).death_saves.failures == 2
        assert target.death_saves.model_dump() == {
            "successes": 0,
            "failures": 0,
            "is_stable": False,
        }
        assert [(c.condition, c.source_entity_id) for c in target.conditions] == [
            ("unconscious", "implied:other"),
            ("prone", "implied:event"),
        ]
        assert pool(after).current == pool(before).current - 1
        rolls = sm.get_command_rolls(sid, command.command_id, package, context=context)["rolls"]
        assert len(rolls) == 1
        assert rolls[0]["hp_gained"] == target.hp_current
        cast_events = [
            event.payload["engine_event"]["type"]
            for event in sm.read_events(sid, package, context=context)
            if event.source_command_id == command.command_id
        ]
        assert cast_events == [
            "intent_submitted",
            "spell_cast",
            "healing_applied",
            "condition_removed",
        ]
        successor = sm.read_rng_context(sid, package, context=context)
        assert successor.version == rng.version + 1
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_noncombat_snapshot(sid, package, context=context) == after
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_noncombat_snapshot(sid, package, context=context) == after
        assert reopened.read_rng_context(sid, package, context=context) == successor


def test_zero_hp_heal_receipt_failure_reverts_life_payment_and_rng(tmp_path, context):
    sm, sid, package, _, _ = cure_world(
        tmp_path,
        context,
        exploration=True,
        target_hp=0,
        configure=_authored_zero_hp_target,
    )
    with sm:
        command = cast_command(sm, sid, package, context, NPC, key="exploration.zero.rollback")
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_zero_heal_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='exploration.zero.rollback' "
            "BEGIN SELECT RAISE(ABORT,'injected zero-HP healing receipt failure'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=context)
        assert exc.value.code == "storage_error"
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_zero_heal_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        assert sm.submit_command(command, package, context=context) == receipt
        assert actor(sm.read_noncombat_snapshot(sid, package, context=context), NPC).hp_current > 0


@pytest.mark.parametrize("unknown_modifier", [False, True])
def test_exploration_heal_checks_only_reviewed_elapsed_effects(tmp_path, context, unknown_modifier):
    """Use the public command with an existing walking bonus or an unknown heal effect."""
    spell = BundledAssetLoader().get_spell("longstrider")
    bonus = passive_effect_to_active_effect(
        spell.passive_effects[0], target_id=NPC, caster_id="npc.prior_caster"
    )
    effect = effect_state(bonus.model_copy(update={"duration": ActiveEffectDuration(seconds=3600)}))
    if unknown_modifier:
        unreviewed = EffectChangeState(
            key="healing.received.multiplier", mode="multiply", value=2, priority=20
        )
        effect = effect.model_copy(update={"changes": [*effect.changes, unreviewed]})
    sm, sid, package, loader, db = cure_world(tmp_path, context, exploration=True, target_hp=2)
    with sm:
        # Model a prior cast from another actor using the existing Effects owner.
        with sm._store.transaction(write=True):
            sm._characters.effects.add_effect(
                sid, effect.model_dump(mode="json"), expires_tick=3600
            )
        command = cast_command(sm, sid, package, context, NPC)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        after_rng = sm.read_rng_context(sid, package, context=context)
        assert before.effect_states == after.effect_states == (effect,)
        if unknown_modifier:
            assert (receipt.status, receipt.error_code) == ("rejected", "healing.dependencies")
            assert after == before
            assert after_rng == rng
            assert sm.read_events(sid, package, context=context) == events
        else:
            assert receipt.status == "committed", receipt
            assert pool(after).current == pool(before).current - 1
            assert actor(after, NPC) == actor(before, NPC).model_copy(update={"hp_current": 19})
            assert actor(after, command.actor_id) == actor(before, command.actor_id).model_copy(
                update={"spell_slots": {1: 1}}
            )
            assert after_rng.version == rng.version + 1
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_noncombat_snapshot(sid, package, context=context) == after
        assert sm.read_rng_context(sid, package, context=context) == after_rng
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_noncombat_snapshot(sid, package, context=context) == after
        assert reopened.read_rng_context(sid, package, context=context) == after_rng


@pytest.mark.parametrize(
    "options,invalid_source,error_code",
    [
        ({"target_cell": "2,0"}, False, "target_invalid"),
        ({"solo_dead": True}, False, "target_invalid"),
        ({"prepared": False}, False, "spell.source"),
        ({"verbal": False}, False, "spell.source"),
        ({}, True, "spell.source"),
        ({"slot_count": 0}, False, "spell.slot"),
    ],
)
def test_refused_cast_changes_no_mechanical_facts(
    tmp_path, context, options, invalid_source, error_code
):
    sm, sid, package, _, _ = cure_world(tmp_path, context, exploration=True, **options)
    with sm:
        command = cast_command(sm, sid, package, context, NPC)
        if invalid_source:
            command = replace(
                command,
                payload=command.payload.model_copy(update={"spell_source_id": "class:other"}),
            )
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "rejected" and receipt.error_code == error_code
        assert not receipt.event_seqs
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.submit_command(command, package, context=context) == receipt


def test_late_receipt_failure_reverts_hp_slot_events_and_rng(tmp_path, context):
    sm, sid, package, _, _ = cure_world(tmp_path, context, exploration=True)
    with sm:
        command = cast_command(sm, sid, package, context, NPC)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_exploration_heal_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='exploration.cure' AND EXISTS (SELECT 1 FROM characters "
            "WHERE session_id=NEW.session_id AND actor_id='npc.caretaker' AND current_hp=19) "
            "AND EXISTS (SELECT 1 FROM character_resources WHERE session_id=NEW.session_id "
            "AND resource_id='spell:1' AND current=1) "
            "BEGIN SELECT RAISE(ABORT,'injected exploration healing receipt failure'); END"
        )
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=context)
        assert exc.value.code == "storage_error"
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert sm.get_command_rolls(sid, command.command_id, package, context=context) is None
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        sm._conn.execute("DROP TRIGGER fail_exploration_heal_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        assert sm.submit_command(command, package, context=context) == receipt
        assert (
            len(sm.get_command_rolls(sid, command.command_id, package, context=context)["rolls"])
            == 1
        )


def test_wrong_healing_target_is_rejected_before_adoption(tmp_path, context, monkeypatch):
    sm, sid, package, _, _ = cure_world(tmp_path, context, exploration=True)
    with sm:
        command = cast_command(sm, sid, package, context, NPC)
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        evaluate = sm._rules._evaluate_dnd

        async def wrong_target(request):
            """Return an internally valid alternate actor ID on the healing HP edit."""
            result = await evaluate(request)
            spend, heal = result.state_delta.operations
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={
                            "operations": (
                                spend,
                                heal.model_copy(update={"target_id": command.actor_id}),
                            )
                        }
                    )
                }
            )

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", wrong_target)
        with pytest.raises(StateMachineError) as exc:
            sm.submit_command(command, package, context=context)
        assert exc.value.code == "invalid_rule_result"
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
