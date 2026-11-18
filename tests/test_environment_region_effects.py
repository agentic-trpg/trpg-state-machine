"""Fixed toxic fog through public exploration, mechanical state and player views."""

from dataclasses import replace

import pytest
from lock_support import PASSAGE, A, B
from test_migrations import stored_rows
from test_object_protection import large_data
from world_support import command, setup, submit

from trpg_state_machine import (
    EnvironmentArea,
    EnvironmentConditions,
    EnvironmentSceneFacts,
    EnvironmentSource,
    InitializeEnvironment,
    Obscurant,
    StateMachineError,
    StepActor,
    TraversePassage,
)
from trpg_state_machine.perception import GameViewContext

FOG = EnvironmentConditions(obscurant=Obscurant("fog", "dense"))


def _toxic_fog(source_id, *cells):
    """Build a fixed source whose region applies Poisoned during exploration."""
    return EnvironmentSource(source_id, FOG, EnvironmentArea(cells), region_condition="poisoned")


def _seed(sm, sid, package, context, *scenes):
    """Install fixed sources with the public environment management grant."""
    receipt = sm.environment.initialize(
        sid,
        tuple(
            InitializeEnvironment(scene, EnvironmentSceneFacts(sources=sources))
            for scene, sources in scenes
        ),
        package,
        command_id="seed.fog",
        expected_world_version=sm.read_session(sid, package, context=context).world_version,
        context=replace(context, can_manage_environment=True),
    )
    assert receipt.status == "committed", receipt


def _poisoned_effects(sm, sid, package, context, pc):
    """Check condition provenance and the player's authorized effect summaries."""
    snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
    actor = next(actor for actor in snapshot.character_states if actor.entity_id == pc)
    effects = tuple(effect for effect in snapshot.effect_states if effect.target_id == pc)
    assert len(actor.conditions) == len(effects)
    assert all(condition.condition == "poisoned" for condition in actor.conditions)
    assert {condition.source_effect_id for condition in actor.conditions} == {
        effect.id for effect in effects
    }
    viewer = GameViewContext(context.principal_id, sid, context.player_id, "player")
    own_character = sm.get_scene_view(sid, package, context=viewer).own_character
    assert own_character is not None
    assert sorted(own_character.effects, key=lambda effect: effect["id"]) == sorted(
        ({"id": effect.id, "name": effect.name} for effect in effects),
        key=lambda effect: effect["id"],
    )
    return effects


def test_public_steps_keep_effects_for_each_overlapping_toxic_fog_source(tmp_path, context):
    """Enter, stay within and leave each region on the same mechanical PC."""
    sm, sid, package, _ = setup(tmp_path, context, large_data())
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        alpha = _toxic_fog("fog.alpha", "1,0", "2,0", "3,0")
        beta = _toxic_fog("fog.beta", "3,0", "4,0")
        ordinary = EnvironmentSource("fog.ordinary", FOG, EnvironmentArea(("5,0",)))
        _seed(sm, sid, package, context, (A, (alpha, beta, ordinary)))
        assert _poisoned_effects(sm, sid, package, context, pc) == ()

        enter = command(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "enter.alpha")
        receipt = sm.submit_command(enter, package, context=context)
        assert receipt.status == "committed", receipt
        (effect_alpha,) = _poisoned_effects(sm, sid, package, context, pc)
        assert sm.submit_command(enter, package, context=context) == receipt
        assert _poisoned_effects(sm, sid, package, context, pc) == (effect_alpha,)

        inside = StepActor(A, "1,0", "2,0")
        preview = sm.query_world_action(sid, pc, inside, package, context=context)
        assert preview.status == "available", preview
        assert submit(sm, sid, package, context, inside, "stay.alpha").status == "committed"
        assert _poisoned_effects(sm, sid, package, context, pc) == (effect_alpha,)

        overlap = submit(sm, sid, package, context, StepActor(A, "2,0", "3,0"), "enter.beta")
        assert overlap.status == "committed", overlap
        effects = _poisoned_effects(sm, sid, package, context, pc)
        assert len(effects) == 2 and effect_alpha in effects
        (effect_beta,) = (effect for effect in effects if effect.id != effect_alpha.id)

        leave_alpha = submit(sm, sid, package, context, StepActor(A, "3,0", "4,0"), "leave.alpha")
        assert leave_alpha.status == "committed", leave_alpha
        assert _poisoned_effects(sm, sid, package, context, pc) == (effect_beta,)

        leave_beta = submit(sm, sid, package, context, StepActor(A, "4,0", "5,0"), "leave.beta")
        assert leave_beta.status == "committed", leave_beta
        assert _poisoned_effects(sm, sid, package, context, pc) == ()
        assert sm.read_session(sid, package, context=context).phase == "exploration"


def test_toxic_fog_scene_transfer_and_late_write_failure_are_atomic(tmp_path, context):
    """Transfer between two scene-scoped sources with the same local ID."""
    data = large_data()
    data["lockable_doors"] = []
    data["scenes"][0]["objects"][0]["state"] = "open"
    sm, sid, package, db = setup(tmp_path, context, data)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        assert (
            submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "step.away").status
            == "committed"
        )
        fog = _toxic_fog("fog.shared", "0,0")
        _seed(sm, sid, package, context, (A, (fog,)), (B, (fog,)))
        assert (
            submit(sm, sid, package, context, StepActor(A, "1,0", "0,0"), "enter.source").status
            == "committed"
        )
        (source_effect,) = _poisoned_effects(sm, sid, package, context, pc)
        crossing = command(
            sm, sid, package, context, TraversePassage(PASSAGE, A, "0,0"), "cross.fog"
        )
        before = stored_rows(db)
        old_position = sm.read_actor_position(sid, pc, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail BEFORE INSERT ON event_outbox "
            "BEGIN SELECT RAISE(ABORT,'fog transfer fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(crossing, package, context=context)
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before
        assert sm.read_actor_position(sid, pc, package, context=context) == old_position
        assert _poisoned_effects(sm, sid, package, context, pc) == (source_effect,)
        assert sm.get_command_receipt(sid, crossing.command_id, package, context=context) is None

        sm._conn.execute("DROP TRIGGER fail")
        receipt = sm.submit_command(crossing, package, context=context)
        assert receipt.status == "committed", receipt
        position = sm.read_actor_position(sid, pc, package, context=context)
        assert (position.scene_id, position.cell) == (B, "0,0")
        (destination_effect,) = _poisoned_effects(sm, sid, package, context, pc)
        assert destination_effect.id != source_effect.id
