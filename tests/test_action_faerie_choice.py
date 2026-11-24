"""Live action choice during ordered public Faerie Fire saves."""

import asyncio

import pytest
from dnd5e_engine.activities.effects import passive_effect_to_active_effect
from dnd5e_engine.evaluation_effects import effect_state
from test_timed_spell_cast import timed_spell_world as timed_spell_world

from trpg_state_machine import TypedCommand, world_command_from_data
from trpg_state_machine.choice_models import ChoiceRespondOperationPayload


def bard_faerie_world(timed_spell_world, *, seed=1):
    """Build a Bard whose real environmental cast includes the current PC."""

    def configure(data):
        data["rule_bindings"].pop("combat_foundation")
        data["actors"][0]["initial_cell"] = "2,0"

    return timed_spell_world(
        database="action-faerie.sqlite",
        caster_class="bard",
        caster_subclass="lore",
        additional_spells=("faerie-fire",),
        seed=seed,
        configure=configure,
    )


def grant(sm, sid, actor):
    """Seed a canonical issued grant through the existing Effects owner."""
    feature = sm._rules.loader.get_feature("bardic-inspiration")
    effect = passive_effect_to_active_effect(
        feature.passive_effects[0],
        target_id=actor,
        caster_id="npc.fixture",
        extra_flags={"bardic_inspiration": {"grant_id": "faerie.grant", "die_sides": 8}},
    )
    with sm._transaction(write=True):
        sm._world.characters.effects.add_effect(
            sid, effect_state(effect).model_dump(mode="json"), expires_tick=3600
        )


def cast(sm, sid, package, context, key="faerie-action"):
    """Decode the public environmental spell command."""
    state = sm.read_session(sid, package, context=context)
    return world_command_from_data(
        {
            "schema_version": "sm-command/1",
            "session_id": sid,
            "command_id": key,
            "actor_id": state.pc_actor_id,
            "command_type": "world.cast_spell",
            "expected_world_version": state.world_version,
            "payload": {
                "kind": "world.cast_spell",
                "spell_id": "faerie-fire",
                "spell_source_id": "class:bard",
                "slot_level": 1,
                "target": {"kind": "point", "cell_id": "0,0"},
            },
        }
    )


@pytest.mark.parametrize("decision", ["accept", "decline"])
def test_faerie_bardic_choice_uses_frozen_prior_rolls(timed_spell_world, decision):
    sm, sid, package, context = bard_faerie_world(timed_spell_world, seed=2)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        grant(sm, sid, actor)
        source = cast(sm, sid, package, context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        pending = sm.submit_command(source, package, context=context)
        assert pending.status == "pending_choice", pending
        assert sm.read_session(sid, package, context=context).world_version == 0
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm._session_runtime.receipt(sid, source.command_id) is None
        original = sm.get_command_rolls(sid, source.command_id, package, context=context)
        assert len(original["rolls"]) == 2
        assert original["rolls"][1]["target_id"] == actor
        assert original["rolls"][1]["succeeded"] is False
        answer = TypedCommand(
            sid,
            "faerie-answer",
            actor,
            "choice.respond",
            ChoiceRespondOperationPayload(source.command_id, decision),
            pending.world_version,
        )
        final = sm.submit_command(answer, package, context=context)
        assert final.status == "committed", final
        rolls = sm.get_command_rolls(sid, source.command_id, package, context=context)["rolls"]
        assert [roll["natural"] for roll in rolls] == [
            roll["natural"] for roll in original["rolls"]
        ]
        assert sm.submit_command(answer, package, context=context) == final
        assert asyncio.run(sm.submit_command_async(answer, package, context=context)) == final
        assert sm.submit_command(source, package, context=context).command_id == source.command_id
        assert not sm._has_pending_action_choice(sid)
        assert bool(
            sm._world.characters.effects.for_target_effect(sid, actor, "effect:inspired")
        ) == (decision == "decline")


def test_faerie_answer_receipt_failure_rolls_back_and_keeps_candidate(
    timed_spell_world, monkeypatch
):
    sm, sid, package, context = bard_faerie_world(timed_spell_world, seed=2)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        grant(sm, sid, actor)
        source = cast(sm, sid, package, context)
        pending = sm.submit_command(source, package, context=context)
        assert pending.status == "pending_choice"
        answer = TypedCommand(
            sid,
            "faerie-answer-fault",
            actor,
            "choice.respond",
            ChoiceRespondOperationPayload(source.command_id, "accept"),
            pending.world_version,
        )
        before_rng = sm.read_rng_context(sid, package, context=context)
        original = sm._session_runtime.record_receipt

        def fail_answer(command, *args, **kwargs):
            if command.command_id == answer.command_id:
                raise RuntimeError("late Faerie answer receipt failure")
            return original(command, *args, **kwargs)

        monkeypatch.setattr(sm._session_runtime, "record_receipt", fail_answer)
        with pytest.raises(RuntimeError, match="late Faerie answer receipt failure"):
            sm.submit_command(answer, package, context=context)
        assert sm.read_session(sid, package, context=context).world_version == 0
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm._has_pending_action_choice(sid)
        assert sm._session_runtime.receipt(sid, source.command_id) is None
        assert sm._session_runtime.receipt(sid, answer.command_id) is None
        monkeypatch.setattr(sm._session_runtime, "record_receipt", original)
        assert sm.submit_command(answer, package, context=context).status == "committed"
