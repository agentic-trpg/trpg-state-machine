"""Exercise a fixed duration Overlay through public casts, turns, rests and recovery."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_delta import WorldRestEffectEnd
from dnd5e_engine.evaluation_ruleset import (
    RULESET_ID,
    FixedSpellDuration,
    RulesetOverlay,
    SpellDurationOverlay,
)
from item_support import NPC
from spatial_support import command as world_command
from test_combat_closure import close_command
from test_combat_start import start_command
from test_cure_wounds import actor
from test_hold_effect_candidate import pass_command
from test_long_rest import rest_command
from test_session_time_coordination import submit
from test_timed_spell_cast import cast_command
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import (
    AdvanceSessionTime,
    DismissWorldSpell,
    ShortRest,
    StateMachine,
    StateMachineError,
)

AID = "aid"
SPEED = "longstrider"
BEACON = "beacon-of-hope"
DATABASE = "overlay.sqlite"
REST_END = "test.after_first_round"


def project_overlay(*, exceptions=None):
    """Select the reviewed project duration policy with explicit exceptions."""
    return RulesetOverlay(
        schema_version="ruleset-overlay/1",
        overlay_id="project-durations",
        revision="1",
        base_ruleset_id=RULESET_ID,
        spell_durations=SpellDurationOverlay(
            applies_to=(AID, SPEED),
            default="until_long_rest",
            overrides=exceptions or {},
        ),
    )


def overlay_world(timed_spell_world, *, overlay=None, concentration=False, combat=False):
    """Give a Life cleric actual spell-slot and Hit Dice balances to recover."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["pc_templates"][0]["character"]["hp"]["current"] = 2
        snapshot = data["rule_bindings"]["noncombat_foundation"]["snapshot"]
        pc = snapshot["character_states"][0]
        pc["hp_current"] = 2
        snapshot["resource_state"]["pools"].append(
            {
                "kind": "hit_dice",
                "owner_id": pc["entity_id"],
                "pool_id": "dice:cleric",
                "current": 2,
                "maximum": 5,
                "class_slug": "cleric",
                "die_size": 8,
            }
        )

    sm, sid, package, context = timed_spell_world(
        caster_class="cleric",
        caster_subclass="life",
        spell_id=AID,
        additional_spells=(SPEED, BEACON) if concentration else (SPEED,),
        database=DATABASE,
        configure=configure,
        ruleset_overlay=overlay,
        combat_end_conditions={REST_END: lambda snapshot: snapshot.combat_state.round_number > 1}
        if combat
        else None,
    )
    return (
        sm,
        sid,
        package,
        replace(
            context,
            can_start_combat=combat,
            can_authorize_long_rest=True,
            can_authorize_short_rest=True,
        ),
    )


def cast(session, spell_id, slot_level, command_id, target_id=None):
    """Submit a paid public spell and return its actual Receipt."""
    sm, _, package, host = session
    command = cast_command(
        session,
        source_id="class:cleric",
        spell_id=spell_id,
        slot_level=slot_level,
        command_id=command_id,
        target_id=target_id,
    )
    receipt = sm.submit_command(command, package, context=host)
    assert receipt.status == "committed", receipt
    return receipt


def balances(snapshot):
    """Read the exact pool balances from the fixed public view."""
    return {pool.pool_id: pool.current for pool in snapshot.resource_state.pools}


def test_base_duration_keeps_aid_eight_hours_and_longstrider_one_hour(timed_spell_world):
    session = overlay_world(timed_spell_world)
    sm, sid, package, host = session
    with sm:
        cast(session, AID, 2, "base.aid")
        cast(session, SPEED, 1, "base.speed")
        effects = sm.read_noncombat_snapshot(sid, package, context=host).effect_states
        assert [effect.duration.seconds for effect in effects] == [28800, 3600]
        assert submit(session, AdvanceSessionTime(3599), "before.speed").status == "committed"
        assert sm.read_noncombat_snapshot(sid, package, context=host).effect_states == effects
        assert submit(session, AdvanceSessionTime(3600), "end.speed").status == "committed"
        assert sm.read_noncombat_snapshot(sid, package, context=host).effect_states == effects[:1]
        assert submit(session, AdvanceSessionTime(28799), "before.aid").status == "committed"
        assert sm.read_noncombat_snapshot(sid, package, context=host).effect_states == effects[:1]
        assert submit(session, AdvanceSessionTime(28800), "end.aid").status == "committed"
        assert not sm.read_noncombat_snapshot(sid, package, context=host).effect_states


def test_combat_and_cold_reopen_then_long_rest_expiry_and_replay(
    timed_spell_world, tmp_path, monkeypatch
):
    session = overlay_world(timed_spell_world, overlay=project_overlay(), combat=True)
    sm, sid, package, host = session
    db = tmp_path / DATABASE
    loader = sm._world.loader
    with sm:
        pc = sm.read_session(sid, package, context=host).pc_actor_id
        base = sm.read_noncombat_snapshot(sid, package, context=host)
        cast(session, AID, 3, "project.aid.strong")
        cast(session, AID, 2, "project.aid.weak")
        cast(session, SPEED, 1, "project.speed")
        cast(session, AID, 2, "project.aid.other", target_id=NPC)
        applied = sm.read_noncombat_snapshot(sid, package, context=host)
        assert len(applied.effect_states) == 4
        assert all(
            effect.duration.until_long_rest and effect.duration.seconds is None
            for effect in applied.effect_states
        )
        assert actor(applied, pc).hp_current == actor(base, pc).hp_current + 10
        assert actor(applied, pc).hp_max == actor(base, pc).hp_max + 10
        assert submit(session, AdvanceSessionTime(28801), "past.elapsed").status == "committed"
        assert sm.read_noncombat_snapshot(sid, package, context=host).effect_states == (
            applied.effect_states
        )

        opening = start_command(sm, sid, package, host, end_condition_id=REST_END)
        opened = sm.submit_command(opening, package, context=host)
        assert opened.status == "committed", opened
        combat = sm.read_combat_snapshot(sid, package, context=host)
        assert combat.effect_states == applied.effect_states
        denied = rest_command(sm, sid, package, host, "rest.during.combat")
        assert sm.submit_command(denied, package, context=host).error_code == (
            "unsupported_combat_world_operation"
        )
        first_round = combat.combat_state.round_number
        for key in ("pass.first", "pass.second"):
            receipt = sm.submit_command(pass_command(sid, combat, key), package, context=host)
            assert receipt.status == "committed", receipt
            combat = sm.read_combat_snapshot(sid, package, context=host)
            assert combat.effect_states == applied.effect_states
        assert combat.combat_state.round_number == first_round + 1
        assert sm.read_time_sync(sid, package, context=host).committed_tick == 28807
        closing = close_command(sm, sid, package, host)
        closed = sm.submit_command(closing, package, context=host)
        assert closed.status == "committed", closed
        assert sm.read_session(sid, package, context=host).phase == "exploration"
        short = replace(
            rest_command(sm, sid, package, host, "short.rest"),
            command_type="activity.short_rest",
            payload=ShortRest(),
        )
        short_receipt = sm.submit_command(short, package, context=host)
        assert short_receipt.status == "committed", short_receipt
        before = sm.read_noncombat_snapshot(sid, package, context=host)
        assert before.effect_states == applied.effect_states
        sm.save_session(sid, package, context=host)

    def assert_before_rest(current, command, *, events, rng, tick):
        assert current.read_noncombat_snapshot(sid, package, context=host) == before
        assert current.get_command_receipt(sid, command.command_id, package, context=host) is None
        assert current.read_events(sid, package, context=host) == events
        assert current.read_rng_context(sid, package, context=host) == rng
        assert current.read_time_sync(sid, package, context=host).committed_tick == tick
        assert current._activity_records.last_long_rest(sid, pc) is None

    (other_identity,) = [
        (effect.target_id, effect.id, effect.origin)
        for effect in before.effect_states
        if effect.target_id == NPC
    ]

    with StateMachine(db, dnd_loader=loader) as sm:
        sm.resume_session(sid, package, context=host)
        command = rest_command(sm, sid, package, host, "long.rest")
        tick = sm.read_time_sync(sid, package, context=host).committed_tick
        rng = sm.read_rng_context(sid, package, context=host)
        events = sm.read_events(sid, package, context=host)
        assert_before_rest(sm, command, events=events, rng=rng, tick=tick)
        other_application = sm._characters.effects.spell_application(sid, other_identity)
        assert (
            other_application.caster_id,
            other_application.cast_command_id,
            other_application.cast_slot_level,
            other_application.expires_tick,
        ) == (pc, "project.aid.other", 2, None)
        evaluate = sm._rules._evaluate_dnd

        async def forged_pair(request):
            result = await evaluate(request)
            ending = next(
                op for op in result.state_delta.operations if type(op) is WorldRestEffectEnd
            )
            inflated = ending.hp.maximum + 1
            forged = ending.model_copy(
                update={
                    "hp": ending.hp.model_copy(update={"current": inflated, "maximum": inflated})
                }
            )
            operations = tuple(
                forged if op is ending else op for op in result.state_delta.operations
            )
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(update={"operations": operations})
                }
            )

        with monkeypatch.context() as fault:
            fault.setattr(sm._rules, "_evaluate_dnd", forged_pair)
            with pytest.raises(StateMachineError) as rejected:
                sm.submit_command(command, package, context=host)
            assert rejected.value.code == "invalid_rule_result"
        assert_before_rest(sm, command, events=events, rng=rng, tick=tick)
        finish = sm._session_runtime.finish

        def fail_after_receipt(*args, **kwargs):
            finish(*args, **kwargs)
            raise RuntimeError("overlay rest failed after receipt write")

        with monkeypatch.context() as fault:
            fault.setattr(sm._session_runtime, "finish", fail_after_receipt)
            with pytest.raises(RuntimeError, match="overlay rest failed after receipt write"):
                sm.submit_command(command, package, context=host)
        assert_before_rest(sm, command, events=events, rng=rng, tick=tick)

    with StateMachine(db, dnd_loader=loader) as sm:
        assert_before_rest(sm, command, events=events, rng=rng, tick=tick)
        receipt = sm.submit_command(command, package, context=host)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(command, package, context=host) == receipt
        after = sm.read_noncombat_snapshot(sid, package, context=host)
        assert after.effect_states == tuple(
            effect for effect in before.effect_states if effect.target_id == NPC
        )
        assert actor(after, NPC) == actor(before, NPC)
        assert sm._characters.effects.spell_application(sid, other_identity) == other_application
        assert actor(after, pc).hp_current == actor(after, pc).hp_max == actor(base, pc).hp_max
        assert actor(before, pc).hp_max == actor(base, pc).hp_max + 10
        assert balances(after) == {**balances(base), "dice:cleric": 5}
        assert sm.read_rng_context(sid, package, context=host) == rng
        completed = tick + 28800
        assert sm.read_time_sync(sid, package, context=host).committed_tick == completed
        confirmed = [
            event
            for event in sm.read_events(sid, package, context=host)
            if event.event_seq in receipt.event_seqs
        ]
        expiries = [event.payload for event in confirmed if event.event_type == "effect.expired"]
        assert [tuple(event["identity"]) for event in expiries] == [
            (effect.target_id, effect.id, effect.origin)
            for effect in before.effect_states
            if effect.target_id == pc
        ]
        assert all(
            event["boundary_tick"] == completed and event["reason"] == "long_rest"
            for event in expiries
        )
        [resolved] = [
            event.payload["engine_event"]
            for event in confirmed
            if event.payload.get("engine_event", {}).get("type") == "rest_resolved"
        ]
        assert resolved["hp_regained"] == actor(base, pc).hp_max - actor(base, pc).hp_current
        sm.save_session(sid, package, context=host)

    with StateMachine(db, dnd_loader=loader) as sm:
        sm.resume_session(sid, package, context=host)
        assert sm.read_noncombat_snapshot(sid, package, context=host) == after
        assert sm.submit_command(command, package, context=host) == receipt


def test_fixed_exception_and_concentration_keep_their_own_lifetimes(timed_spell_world):
    overlay = project_overlay(exceptions={SPEED: FixedSpellDuration(rounds=10)})
    session = overlay_world(timed_spell_world, overlay=overlay, concentration=True)
    sm, sid, package, host = session
    with sm:
        cast(session, AID, 2, "cast.aid")
        cast(session, SPEED, 1, "cast.fixed.speed")
        cast(session, BEACON, 3, "cast.concentration", target_id=NPC)
        before = sm.read_noncombat_snapshot(sid, package, context=host)
        aid, speed, concentration = before.effect_states
        assert aid.duration.until_long_rest
        assert speed.duration.seconds == 60
        assert concentration.duration.seconds == 60 and concentration.flags["concentration"]
        assert submit(session, AdvanceSessionTime(59), "before.deadline").status == "committed"
        assert sm.read_noncombat_snapshot(sid, package, context=host).effect_states == (
            before.effect_states
        )
        expired = submit(session, AdvanceSessionTime(60), "at.deadline")
        assert expired.status == "committed", expired
        assert sm.read_noncombat_snapshot(sid, package, context=host).effect_states == (aid,)
        assert sm._characters.effects.concentration_group(sid, aid.target_id) is None


def test_fixed_speed_expiry_can_settle_inside_a_long_rest_with_aid(timed_spell_world):
    overlay = project_overlay(exceptions={SPEED: FixedSpellDuration(rounds=10)})
    session = overlay_world(timed_spell_world, overlay=overlay)
    sm, sid, package, host = session
    with sm:
        base = sm.read_noncombat_snapshot(sid, package, context=host)
        cast(session, AID, 2, "cast.aid")
        cast(session, SPEED, 1, "cast.fixed.speed")
        aid, speed = sm.read_noncombat_snapshot(sid, package, context=host).effect_states
        rest = rest_command(sm, sid, package, host, "rest.crosses.deadline")
        receipt = sm.submit_command(rest, package, context=host)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=host)
        assert after.effect_states == ()
        pc = actor(after, rest.actor_id)
        assert pc.hp_current == pc.hp_max == actor(base, pc.entity_id).hp_max
        expired = [
            event.payload
            for event in sm.read_events(sid, package, context=host)
            if event.event_seq in receipt.event_seqs and event.event_type == "effect.expired"
        ]
        assert [event["boundary_tick"] for event in expired] == [60, 28800]
        assert [tuple(event["identity"]) for event in expired] == [
            (effect.target_id, effect.id, effect.origin) for effect in (speed, aid)
        ]


def test_original_caster_can_end_a_rest_bound_aid_source_early(timed_spell_world):
    session = overlay_world(timed_spell_world, overlay=project_overlay())
    sm, sid, package, host = session
    with sm:
        cast(session, AID, 3, "cast.strong")
        cast(session, AID, 2, "cast.weak")
        before = sm.read_noncombat_snapshot(sid, package, context=host)
        stronger, weaker = before.effect_states
        target = actor(before, stronger.target_id)
        dismissal = world_command(
            sm,
            sid,
            package,
            host,
            DismissWorldSpell(target.entity_id, stronger.id, stronger.origin, "cast.strong"),
            "dismiss.strong",
        )
        receipt = sm.submit_command(dismissal, package, context=host)
        assert receipt.status == "committed", receipt
        after = sm.read_noncombat_snapshot(sid, package, context=host)
        assert after.effect_states == (weaker,)
        remaining = actor(after, target.entity_id)
        assert (remaining.hp_current, remaining.hp_max) == (
            target.hp_current - 5,
            target.hp_max - 5,
        )
        assert balances(after) == balances(before)


def test_fixed_speed_exception_ends_at_tenth_completed_combat_round(timed_spell_world):
    overlay = project_overlay(exceptions={SPEED: FixedSpellDuration(rounds=10)})
    session = overlay_world(timed_spell_world, overlay=overlay, combat=True)
    sm, sid, package, host = session
    with sm:
        cast(session, AID, 2, "cast.aid")
        cast(session, SPEED, 1, "cast.fixed.speed")
        opening = start_command(sm, sid, package, host)
        receipt = sm.submit_command(opening, package, context=host)
        assert receipt.status == "committed", receipt
        combat = sm.read_combat_snapshot(sid, package, context=host)
        aid, speed = combat.effect_states
        initial_round = combat.combat_state.round_number
        for turn in range(1, 21):
            command = pass_command(sid, combat, f"turn.{turn}")
            receipt = sm.submit_command(command, package, context=host)
            assert receipt.status == "committed", receipt
            combat = sm.read_combat_snapshot(sid, package, context=host)
            if turn < 20:
                assert combat.effect_states == (aid, speed)
            assert combat.combat_state.round_number == initial_round + turn // 2
            assert sm.read_time_sync(sid, package, context=host).committed_tick == 6 * (turn // 2)
        assert combat.effect_states == (aid,)
        [expiry] = [
            event
            for event in sm.read_events(sid, package, context=host)
            if event.event_type == "effect.expired"
        ]
        assert expiry.event_seq in receipt.event_seqs
        assert expiry.payload["boundary_tick"] == 60
        assert tuple(expiry.payload["identity"]) == (speed.target_id, speed.id, speed.origin)
