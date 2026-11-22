"""Public exploration casting duration, continuation and concentration interruption."""

import pytest
from dnd5e_srd_data.schema.spell import CastingTime, CastingTimeUnit
from test_session_time_coordination import submit as submit_time
from test_session_time_coordination import sync
from test_timed_spell_cast import PERCEPTION_SPELL, first_level_balance
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_magic_light import cast, sources

from trpg_state_machine import (
    AdvanceSessionTime,
    DropWorldConcentration,
    FinishSpellCast,
    StartNavigation,
    StartSpellCast,
    StateMachine,
    StateMachineError,
    TypedCommand,
)


def _world(timed_spell_world, *, slots=4):
    """Bind a supported Perception effect to a 1-minute spell with a Ritual option."""
    return timed_spell_world(
        spell_id=PERCEPTION_SPELL,
        additional_spells=("darkness",),
        first_level_slots=slots,
        configure_spell=lambda spell: spell.model_copy(
            update={
                "casting_time": CastingTime(unit=CastingTimeUnit.MINUTE, value=1),
                "ritual": True,
            }
        ),
    )


def _command(session, key, kind, operation, *, wait=None):
    """Use the selected PC, authorized context and current committed world version."""
    sm, sid, package, context = session
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid, key, state.pc_actor_id, kind, operation, state.world_version, time_wait=wait
    )


def _start(session, *, ritual=False, activity_id="my-casting"):
    """Submit the ordinary slotted or slot-free Ritual route for the reviewed spell."""
    sm, _, package, context = session
    command = _command(
        session,
        activity_id + "-start",
        "activity.start_spell_cast",
        StartSpellCast(
            activity_id=activity_id,
            spell_id=PERCEPTION_SPELL,
            spell_source_id="class:wizard",
            slot_level=None if ritual else 1,
            as_ritual=ritual,
        ),
    )
    receipt = sm.submit_command(command, package, context=context)
    assert receipt.status == "committed", receipt
    return command


def _progress(session, command, activity_id="my-casting"):
    """Read authorized Activity progress through StateMachine's detached view."""
    sm, sid, package, context = session
    return sm.read_spell_cast_activity(sid, command.actor_id, activity_id, package, context=context)


def _effects(session):
    """Read the published effects from the actual NonCombatSnapshot consumer."""
    sm, sid, package, context = session
    return sm.read_noncombat_snapshot(sid, package, context=context).effect_states


@pytest.mark.parametrize(
    ("ritual", "deadline", "initial_slots", "finished_slots"),
    [(False, 60, 4, 3), (True, 660, 0, 0)],
)
def test_exploration_casting_reopens_and_finishes_once(
    timed_spell_world, tmp_path, ritual, deadline, initial_slots, finished_slots
):
    """Stop at the exact deadline and adopt once after saving and reopening."""
    session = _world(timed_spell_world, slots=initial_slots)
    sm, sid, package, context = session
    with sm:
        started = _start(session, ritual=ritual)
        progress = _progress(session, started)
        assert (progress.start_tick, progress.deadline_tick, progress.status) == (
            0,
            deadline,
            "casting",
        )
        assert first_level_balance(sm, started) == initial_slots
        assert not _effects(session)
        if not ritual:
            scene = sm.read_session(sid, package, context=context).current_scene_id
            walk = _command(
                session,
                "navigate-during-cast",
                "activity.start_navigation",
                StartNavigation("activity.walk", scene, "1,0"),
            )
            denied = sm.submit_command(walk, package, context=context)
            assert denied.status == "rejected"
            assert denied.error_code == "activity_conflict"
        assert (
            submit_time(session, AdvanceSessionTime(deadline - 1), "almost").status == "committed"
        )
        assert sync(session).committed_tick == deadline - 1
        assert _progress(session, started) == progress
        loader = sm._rules.loader

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as sm:
        session = (sm, sid, package, context)
        assert _progress(session, started) == progress
        assert (
            submit_time(session, AdvanceSessionTime(deadline + 17), "past-deadline").status
            == "committed"
        )
        pending = sync(session)
        assert pending.committed_tick == deadline
        assert pending.pending_target_tick == deadline + 17
        assert pending.progress_status == "waiting"
        assert pending.time_wait is not None
        assert _progress(session, started).status == "waiting"
        assert first_level_balance(sm, started) == initial_slots
        assert not _effects(session)

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as sm:
        session = (sm, sid, package, context)
        assert sync(session).time_wait == pending.time_wait
        finish = _command(
            session,
            "finish",
            "activity.finish_spell_cast",
            FinishSpellCast("my-casting"),
            wait=pending.time_wait,
        )
        receipt = sm.submit_command(finish, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(finish, package, context=context) == receipt
        assert first_level_balance(sm, started) == finished_slots
        assert len(_effects(session)) == 1
        effect = _effects(session)[0]
        application = sm._characters.effects.spell_application(
            sid, (effect.target_id, effect.id, effect.origin)
        )
        assert application.cast_slot_level == 1
        assert _progress(session, started).status == "completed"

        assert (
            submit_time(session, AdvanceSessionTime(deadline + 17), "continue-clock").status
            == "committed"
        )
        assert sync(session).committed_tick == deadline + 17
        assert (
            submit_time(session, AdvanceSessionTime(deadline + 59), "cast-effect-live").status
            == "committed"
        )
        assert len(_effects(session)) == 1
        assert (
            submit_time(session, AdvanceSessionTime(deadline + 60), "cast-effect-expires").status
            == "committed"
        )
        assert not _effects(session)


def test_voluntary_interruption_ends_focus_and_preserves_earlier_time(timed_spell_world, tmp_path):
    """A real voluntary focus drop after reopen ends this cast without its effect or cost."""
    session = _world(timed_spell_world)
    sm, sid, package, context = session
    with sm:
        _, darkness = cast(
            session, "old-darkness", "darkness", 2, {"kind": "point", "cell_id": "2,0"}
        )
        assert darkness.status == "committed", darkness
        assert len(sources(session)) == 1
        started = _start(session)
        assert not sources(session)
        assert submit_time(session, AdvanceSessionTime(23), "partial").status == "committed"
        loader = sm._rules.loader

    with StateMachine(tmp_path / "timed-spell.sqlite", dnd_loader=loader) as sm:
        session = (sm, sid, package, context)
        assert sync(session).committed_tick == 23
        drop = _command(
            session, "voluntary-break", "world.drop_concentration", DropWorldConcentration()
        )
        receipt = sm.submit_command(drop, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(drop, package, context=context) == receipt
        assert _progress(session, started).status == "cancelled"
        assert sync(session).committed_tick == 23
        assert first_level_balance(sm, started) == 4
        assert not _effects(session)
        assert submit_time(session, AdvanceSessionTime(61), "after-break").status == "committed"
        assert sync(session).time_wait is None
        assert not _effects(session)

        restarted = _start(session, activity_id="another-cast")
        reattempt = _progress(session, restarted, activity_id="another-cast")
        assert (reattempt.start_tick, reattempt.deadline_tick) == (61, 121)


def test_cast_activity_rejects_forged_deadline_without_replacing_focus(
    timed_spell_world, monkeypatch
):
    """A changed RE deadline cannot mutate the clock, spend a slot or remove old focus."""
    session = _world(timed_spell_world)
    sm, sid, package, context = session
    with sm:
        _, darkness = cast(
            session, "old-darkness", "darkness", 2, {"kind": "point", "cell_id": "2,0"}
        )
        assert darkness.status == "committed", darkness
        old_lighting = sources(session)
        assert len(old_lighting) == 1
        command = _command(
            session,
            "forgery-test",
            "activity.start_spell_cast",
            StartSpellCast("my-casting", PERCEPTION_SPELL, "class:wizard", 1),
        )
        original_evaluation = sm._rules._evaluate_dnd

        async def wrong_duration(request):
            result = await original_evaluation(request)
            (begin,) = result.state_delta.operations
            operation = begin.model_copy(update={"duration_seconds": begin.duration_seconds + 1})
            state_delta = result.state_delta.model_copy(update={"operations": (operation,)})
            return result.model_copy(update={"state_delta": state_delta})

        with monkeypatch.context() as scope:
            scope.setattr(sm._rules, "_evaluate_dnd", wrong_duration)
            with pytest.raises(StateMachineError, match="duration differs") as failure:
                sm.submit_command(command, package, context=context)
        assert failure.value.code == "invalid_rule_result"
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert sync(session).committed_tick == 0
        assert sources(session) == old_lighting
        assert first_level_balance(sm, command) == 4
        with pytest.raises(StateMachineError, match="Expected this caster's activity"):
            _progress(session, command)
        assert sm.submit_command(command, package, context=context).status == "committed"
        assert _progress(session, command).deadline_tick == 60


def test_cast_activity_rejects_forged_effect_without_spending_or_clearing_wait(
    timed_spell_world, monkeypatch
):
    """Alter matching RE Delta and event durations and verify the whole cast remains pending."""
    session = _world(timed_spell_world)
    sm, sid, package, context = session
    with sm:
        started = _start(session)
        assert submit_time(session, AdvanceSessionTime(60), "finish-boundary").status == "committed"
        progress = _progress(session, started)
        wait = sync(session).time_wait
        assert wait is not None
        unauthorized = _command(
            session,
            "wrong-activity",
            "activity.finish_spell_cast",
            FinishSpellCast("other-cast"),
            wait=wait,
        )
        with pytest.raises(StateMachineError, match="Casting wait does not authorize") as failure:
            sm.submit_command(unauthorized, package, context=context)
        assert failure.value.code == "unauthorized"
        finish = _command(
            session,
            "finish",
            "activity.finish_spell_cast",
            FinishSpellCast("my-casting"),
            wait=wait,
        )
        original_evaluation = sm._rules._evaluate_dnd

        async def wrong_effect(request):
            result = await original_evaluation(request)
            payment, creation = result.state_delta.operations
            intent, cast_event, applied = result.proposed_events
            effect = creation.value.effect
            duration = effect.duration.model_copy(update={"seconds": 65})
            changed_effect = effect.model_copy(update={"duration": duration})
            changed_creation = creation.model_copy(
                update={"value": creation.value.model_copy(update={"effect": changed_effect})}
            )
            changed_event = applied.model_copy(
                update={"effect": applied.effect.model_copy(update={"duration": duration})}
            )
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": (payment, changed_creation)}
                    ),
                    "proposed_events": (intent, cast_event, changed_event),
                }
            )

        with monkeypatch.context() as scope:
            scope.setattr(sm._rules, "_evaluate_dnd", wrong_effect)
            with pytest.raises(StateMachineError, match="Casting effect differs") as failure:
                sm.submit_command(finish, package, context=context)
        assert failure.value.code == "invalid_rule_result"

        def abort_receipt(*_args):
            """Fail the outer write after the ordinary application and wait resolution."""
            assert sm._world.spell_casts.get(sid, "my-casting")["status"] == "completed"
            assert first_level_balance(sm, started) == 3
            raise StateMachineError("storage_error", "injected finish receipt failure")

        with monkeypatch.context() as scope:
            scope.setattr(sm._session_runtime, "record_receipt", abort_receipt)
            with pytest.raises(StateMachineError, match="injected finish receipt failure"):
                sm.submit_command(finish, package, context=context)
        assert sm.get_command_receipt(sid, finish.command_id, package, context=context) is None
        assert _progress(session, started) == progress
        assert sync(session).time_wait == wait
        assert first_level_balance(sm, started) == 4
        assert not _effects(session)
        assert sm.submit_command(finish, package, context=context).status == "committed"
        assert first_level_balance(sm, started) == 3
