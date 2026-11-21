"""Consume independent elapsed movement and armor effects through public combat commands."""

from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_srd_data import BundledAssetLoader
from dnd_support import initialize
from item_support import NPC
from test_hold_effect_candidate import pass_command
from test_session_time_coordination import submit
from test_timed_spell_cast import cast_command, first_level_balance, move_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import AdvanceSessionTime, TypedCommand


def test_public_longstrider_recast_keeps_two_deadlines_and_one_walk_bonus(timed_spell_world):
    """Apply two ordinary casts and settle their exact instances across two rounds."""
    session = timed_spell_world(walking=True)
    sm, sid, package, context = session

    def read():
        return sm.read_combat_snapshot(sid, package, context=context)

    def execute(command):
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        return receipt

    def assert_expiry(receipt, effect, tick):
        (expiry,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in receipt.event_seqs and event.event_type == "effect.expired"
        ]
        assert tuple(expiry.payload["identity"]) == (effect.target_id, effect.id, effect.origin)
        assert expiry.payload["boundary_tick"] == tick

    with sm:
        initial = cast_command(session, command_id="first-longstrider")
        execute(initial)
        assert submit(session, AdvanceSessionTime(6), "between-casts").status == "committed"
        recast = cast_command(session, command_id="second-longstrider")
        recast_receipt = execute(recast)
        fixed = sm.read_noncombat_snapshot(sid, package, context=context)
        first, second = fixed.effect_states
        assert first.id == second.id == "effect:longstrider"
        assert first.target_id == second.target_id == initial.actor_id
        assert first.origin != second.origin
        assert execute(recast) == recast_receipt
        assert first_level_balance(sm, initial) == 2

        assert submit(session, AdvanceSessionTime(3594), "before-expiry").status == "committed"
        assert initialize(sm, sid, package, context).status == "committed"
        execute(pass_command(sid, read(), "npc-before-first-expiry"))
        pc = initial.actor_id
        start = next(actor for actor in read().character_states if actor.entity_id == pc)
        assert start.movement_remaining == 40
        execute(move_command(session, "7,0", "walk-on-overlap"))
        first_expiry = execute(pass_command(sid, read(), "expire-first-longstrider"))
        assert_expiry(first_expiry, first, 3600)
        assert read().effect_states == (second,)

        execute(pass_command(sid, read(), "npc-before-second-expiry"))
        execute(move_command(session, "6,0", "walk-on-surviving-cast"))
        before_final = next(actor for actor in read().character_states if actor.entity_id == pc)
        assert before_final.movement_remaining == 35
        second_expiry = execute(pass_command(sid, read(), "expire-second-longstrider"))
        assert_expiry(second_expiry, second, 3606)
        final = read()
        assert not final.effect_states
        actor = next(actor for actor in final.character_states if actor.entity_id == pc)
        assert actor.movement_remaining == 25
        assert final.combat_state.movement_ledgers[pc].spent_ft == 5
        assert fixed.effect_states == (first, second)


def test_barkskin_and_longstrider_in_one_encounter(timed_spell_world):
    """Walk beyond base speed and apply the armor floor before independent expiry."""
    session = timed_spell_world(walking=True, attack_after_walk=True, seed=3)
    sm, sid, package, context = session

    def read():
        return sm.read_combat_snapshot(sid, package, context=context)

    def execute(command):
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        return receipt

    def advance_turn(key):
        return execute(pass_command(sid, read(), key))

    def effects(snapshot):
        return {effect.id for effect in snapshot.effect_states}

    def remaining(snapshot):
        return next(
            actor.movement_remaining for actor in snapshot.character_states if actor.entity_id == pc
        )

    def assert_expiry(receipt, effect, tick):
        (expiry,) = [
            event
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in receipt.event_seqs and event.event_type == "effect.expired"
        ]
        assert tuple(expiry.payload["identity"]) == (effect.target_id, effect.id, effect.origin)
        assert expiry.payload["boundary_tick"] == tick
        assert sm.read_time_sync(sid, package, context=context).committed_tick == tick

    with sm:
        cast = cast_command(session)
        pc = cast.actor_id
        execute(cast)
        (speed,) = sm.read_noncombat_snapshot(sid, package, context=context).effect_states
        # A prior consenting Barkskin is the encounter precondition. Public
        # exploration casting only admits the selected PC, whose reviewed combat
        # profile here is a Wizard. Seed the effect through its real owner.
        barkskin = BundledAssetLoader().get_spell("barkskin")
        armor = effect_state(
            passive_effect_to_active_effect(
                barkskin.passive_effects[0], target_id=pc, caster_id="npc.druid"
            )
        )
        with sm._transaction(write=True):
            sm._characters.effects.add_effect(sid, armor.model_dump(mode="json"), expires_tick=3606)

        assert submit(session, AdvanceSessionTime(3588), "before-combat").status == "committed"
        initialized = initialize(sm, sid, package, context)
        assert initialized.status == "committed", initialized
        both_effects = {"effect:longstrider", armor.id}
        assert effects(read()) == both_effects
        advance_turn("first-ogre-pass")
        execute(move_command(session, "7,0", "walk-35-feet"))
        assert remaining(read()) == 5
        advance_turn("open-second-round")

        before = read()
        assert effects(before) == both_effects
        strike = CombatIntentPayload(
            intent_type="attack", target_id=pc, stat_block_action_id="greatclub"
        )
        availability = sm.query_action_availability(sid, NPC, strike, package, context=context)
        assert availability.status == "available", availability
        attack = execute(
            TypedCommand(sid, "ogre-attack", NPC, "combat.intent", strike, before.world_version)
        )
        (roll,) = [
            event.payload["engine_event"]
            for event in sm.read_events(sid, package, context=context)
            if event.event_seq in attack.event_seqs
            and event.event_type == "combat.engine_event"
            and event.payload["engine_event"]["type"] == "attack_rolled"
        ]
        assert roll["roll_total"] == 14
        assert not roll["is_hit"]
        assert sm.read_character_ac(sid, pc, package, context=context).effective == 17

        execute(move_command(session, "7,1", "stay-in-reach"))
        assert remaining(read()) == 35
        speed_expiry = advance_turn("expire-longstrider")
        assert_expiry(speed_expiry, speed, 3600)
        assert effects(read()) == {armor.id}
        assert remaining(read()) == 25
        assert sm.read_character_ac(sid, pc, package, context=context).effective == 17

        advance_turn("third-ogre-pass")
        assert remaining(read()) == 30
        armor_expiry = advance_turn("expire-barkskin")
        assert_expiry(armor_expiry, armor, 3606)
        assert not read().effect_states
        ac = sm.read_character_ac(sid, pc, package, context=context)
        assert ac.effective == ac.base == roll["roll_total"]


def test_timed_world_effects_with_unreviewed_senses_still_refuse_combat(timed_spell_world):
    """Keep ordinary attack and move from ignoring a live Darkvision effect."""
    session = timed_spell_world(
        walking=True, attack_after_walk=True, additional_spells=("darkvision",)
    )
    sm, sid, package, context = session
    with sm:
        speed = cast_command(session)
        assert sm.submit_command(speed, package, context=context).status == "committed"
        sight = cast_command(session, spell_id="darkvision", slot_level=2)
        assert sm.submit_command(sight, package, context=context).status == "committed"
        assert initialize(sm, sid, package, context).status == "committed"

        initial = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        attack = TypedCommand(
            sid,
            "attack-with-darkvision",
            NPC,
            "combat.intent",
            CombatIntentPayload(
                intent_type="attack", target_id=speed.actor_id, stat_block_action_id="greatclub"
            ),
            initial.world_version,
        )
        receipt = sm.submit_command(attack, package, context=context)
        assert (receipt.status, receipt.error_code, receipt.event_seqs) == (
            "rejected",
            "snapshot.capability",
            (),
        )
        assert sm.read_combat_snapshot(sid, package, context=context) == initial
        opening = pass_command(sid, initial, "open-wizard-turn")
        assert sm.submit_command(opening, package, context=context).status == "committed"

        ready = sm.read_combat_snapshot(sid, package, context=context)
        move = move_command(session, "1,0", "move-with-darkvision")
        receipt = sm.submit_command(move, package, context=context)
        assert (receipt.status, receipt.error_code, receipt.event_seqs) == (
            "rejected",
            "movement.capability",
            (),
        )
        assert sm.read_combat_snapshot(sid, package, context=context) == ready
        assert sm.read_rng_context(sid, package, context=context) == rng
