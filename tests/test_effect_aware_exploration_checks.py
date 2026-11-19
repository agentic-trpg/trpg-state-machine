"""Exploration checks consume the committed effects on the rolling actor."""

from dataclasses import replace

from dnd5e_engine.evaluation_rng import RNGState
from lock_support import A
from test_object_protection import large_data
from test_real_checks import check_command
from world_support import setup, submit

from trpg_state_machine import (
    EnvironmentArea,
    EnvironmentConditions,
    EnvironmentSceneFacts,
    EnvironmentSource,
    InitializeEnvironment,
    Obscurant,
    StepActor,
)
from trpg_state_machine.environment_commands import source_from_data


def test_public_checks_use_poisoned_while_either_fog_source_is_active(tmp_path, context):
    """Check exact d20 consumption before, inside and after overlapping fog."""
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _ = setup(tmp_path, context, large_data())
    with sm:
        state = sm.read_session(sid, package, context=context)
        fog = EnvironmentConditions(obscurant=Obscurant("fog", "dense"))
        sources = (
            EnvironmentSource(
                "fog.alpha", fog, EnvironmentArea(("1,0", "2,0")), region_condition="poisoned"
            ),
            EnvironmentSource(
                "fog.beta",
                fog,
                EnvironmentArea(("2,0", "3,0")),
                region_effect_rule="condition:poisoned",
            ),
        )
        seeded = sm.environment.initialize(
            sid,
            (InitializeEnvironment(A, EnvironmentSceneFacts(sources=sources)),),
            package,
            command_id="seed.check-fog",
            expected_world_version=state.world_version,
            context=replace(context, can_manage_environment=True),
        )
        assert seeded.status == "committed", seeded

        def check(key, *, condition_sources=0, skill="perception", advantage=(), expected_draws=1):
            """Assert public event provenance and the exact committed RNG state."""
            snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
            actor = next(a for a in snapshot.character_states if a.entity_id == state.pc_actor_id)
            assert len(actor.conditions) == condition_sources
            assert all(record.condition == "poisoned" for record in actor.conditions)
            command = check_command(
                sm, sid, package, context, key, skill=skill, ability="wis", advantage=advantage
            )
            rng_context = sm.read_rng_context(sid, package, context=context)
            rng = rng_context.state.restore()
            rolls = [rng.randint(1, 20) for _ in range(expected_draws)]
            receipt = sm.submit_command(command, package, context=context)
            assert receipt.status == "committed", receipt
            (event,) = [
                event
                for event in sm.read_events(sid, package, context=context)
                if event.event_seq in receipt.event_seqs
                and event.event_type == "rules.check_rolled"
            ]
            engine = event.payload["engine_event"]
            assert engine["skill"] == skill and engine["ability"] == "wis"
            assert engine["advantage"] == ("disadvantage" if expected_draws == 2 else "normal")
            assert engine["disadvantage_sources"] == (
                ["condition:attacker"] if condition_sources else []
            )
            assert engine["advantage_sources"] == list(advantage)
            assert engine["natural"] == engine["rolled_natural"] == min(rolls)
            after_rng = sm.read_rng_context(sid, package, context=context)
            assert after_rng.state == RNGState.capture(rng)
            assert after_rng.version == rng_context.version + 1
            return rolls

        check("check.clear")
        assert submit(
            sm, sid, package, context, StepActor(A, "0,0", "1,0"), "enter.alpha"
        ).status == ("committed")
        # The fixture's next draws are 14 then 2, so picking the first die fails.
        assert check("check.search", condition_sources=1, expected_draws=2) == [14, 2]
        assert submit(
            sm, sid, package, context, StepActor(A, "1,0", "2,0"), "enter.beta"
        ).status == ("committed")
        check("check.overlap", condition_sources=2, skill=None, expected_draws=2)
        check("check.cancelled", condition_sources=2, advantage=("flag",))
        assert submit(
            sm, sid, package, context, StepActor(A, "2,0", "3,0"), "leave.alpha"
        ).status == ("committed")
        check("check.remaining", condition_sources=1, expected_draws=2)
        assert submit(
            sm, sid, package, context, StepActor(A, "3,0", "4,0"), "leave.beta"
        ).status == ("committed")
        check("check.restored", skill=None)


def test_configured_region_effect_applies_to_perception_only_while_inside(tmp_path, context):
    """A custom configured effect follows the public movement and check paths."""
    context = replace(context, can_adjudicate_checks=True)
    sm, sid, package, _ = setup(tmp_path, context, large_data())
    with sm:
        state = sm.read_session(sid, package, context=context)
        source = source_from_data(
            {
                "source_id": "region.clear-echo",
                "conditions": {},
                "area": {"cells": ["1,0", "2,0"]},
                "region_effect": {
                    "name": "Clear Echo",
                    "transfer": False,
                    "changes": [
                        {"key": "check.skill.perception.bonus", "mode": 2, "value": "2"},
                        {"key": "flags.advantage.check.perception", "mode": 5, "value": "true"},
                    ],
                },
            }
        )
        seeded = sm.environment.initialize(
            sid,
            (InitializeEnvironment(A, EnvironmentSceneFacts(sources=(source,))),),
            package,
            command_id="seed.clear-echo",
            expected_world_version=state.world_version,
            context=replace(context, can_manage_environment=True),
        )
        assert seeded.status == "committed", seeded

        def check_modifier(key, skill, *, empowered=False):
            """Check the roll mode and return the modifier from the committed RE event."""
            command = check_command(sm, sid, package, context, key, skill=skill, ability="wis")
            receipt = sm.submit_command(command, package, context=context)
            assert receipt.status == "committed", receipt
            (event,) = [
                event
                for event in sm.read_events(sid, package, context=context)
                if event.event_seq in receipt.event_seqs
                and event.event_type == "rules.check_rolled"
            ]
            result = event.payload["engine_event"]
            assert result["advantage"] == ("advantage" if empowered else "normal")
            assert result["advantage_sources"] == (["effect"] if empowered else [])
            assert result["roll_total"] == result["natural"] + result["modifier"]
            return result["modifier"]

        def effect_states():
            return sm.read_noncombat_snapshot(sid, package, context=context).effect_states

        perception = check_modifier("perception.outside", "perception")
        insight = check_modifier("insight.outside", "insight")
        assert effect_states() == ()

        entered = submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "enter.clear-echo")
        assert entered.status == "committed", entered
        (effect,) = effect_states()
        assert effect.name == "Clear Echo" and effect.target_id == state.pc_actor_id
        assert check_modifier("perception.inside", "perception", empowered=True) == perception + 2
        assert check_modifier("insight.inside", "insight") == insight

        moved = submit(sm, sid, package, context, StepActor(A, "1,0", "2,0"), "step.clear-echo")
        assert moved.status == "committed", moved
        assert effect_states() == (effect,)

        left = submit(sm, sid, package, context, StepActor(A, "2,0", "3,0"), "leave.clear-echo")
        assert left.status == "committed", left
        assert effect_states() == ()
        assert check_modifier("perception.restored", "perception") == perception
