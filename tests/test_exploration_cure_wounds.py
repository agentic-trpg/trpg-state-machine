"""Public exploration healing through the bound Cure Wounds activity."""

from dataclasses import replace
from random import Random

import pytest
from dnd5e_engine.evaluation_contracts import ExplorationSpellCastPayload
from dnd5e_engine.evaluation_rng import RNGState
from item_support import NPC
from test_cure_wounds import SOURCE, SPELL, actor, cure_world, pool

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand


def cast_command(sm, sid, package, context, target):
    """Select the authorized caster, supplied creature, and the prepared ordinary slot."""
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        "exploration.cure",
        state.pc_actor_id,
        "world.cast_spell",
        ExplorationSpellCastPayload(
            kind="world.cast_spell",
            spell_id=SPELL,
            spell_source_id=SOURCE,
            target_id=target or state.pc_actor_id,
            slot_level=1,
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
        amount = expected_rng.randint(1, 8) + expected_rng.randint(1, 8) + 3
        assert events[2].payload["engine_event"] == {
            "type": "healing_applied",
            "target_id": target_id,
            "amount": amount,
        }
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
        assert sm.read_noncombat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        sm._conn.execute("DROP TRIGGER fail_exploration_heal_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        assert sm.submit_command(command, package, context=context) == receipt


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
