"""Fixed toxic fog through public exploration, mechanical state and player views."""

from collections import Counter
from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_region_effect import region_condition_identity
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from lock_support import PASSAGE, A, B
from spatial_support import create
from test_migrations import stored_rows
from test_object_protection import large_data
from test_scoped_runtime_validation import measure, observe
from world_support import command, setup, submit

from trpg_state_machine import (
    AddEnvironmentSource,
    EnvironmentArea,
    EnvironmentConditions,
    EnvironmentSceneFacts,
    EnvironmentSource,
    InitializeEnvironment,
    Obscurant,
    StateMachine,
    StateMachineError,
    StepActor,
    TraversePassage,
)
from trpg_state_machine.package import _validate
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


@pytest.mark.parametrize("immune", [False, True])
def test_public_fixed_area_rule_applies_and_clears_deafened(tmp_path, context, immune):
    """Resolve a second explicit rule through the same environment and movement commands."""
    data = large_data()
    loader = get_default_ruleset()
    data["manifest"]["ruleset_binding"] = loader.binding.model_dump(mode="json")
    if immune:
        for foundation in data["rule_bindings"].values():
            foundation["snapshot"]["character_states"][0]["condition_immunities"] = ["deafened"]
    package = _validate(data)
    with StateMachine(tmp_path / "world.db", dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        rule_ref = "condition:deafened"
        source = EnvironmentSource(
            "silent.area",
            EnvironmentConditions(),
            EnvironmentArea(("1,0", "2,0")),
            region_effect_rule=rule_ref,
        )
        receipt = submit(
            sm,
            sid,
            package,
            replace(context, can_manage_environment=True),
            AddEnvironmentSource(A, source),
            "add.silent.area",
        )
        assert receipt.status == "committed", receipt

        enter = submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "enter.silent")
        assert enter.status == "committed", enter
        entered = sm.read_noncombat_snapshot(sid, package, context=context)
        actor = next(actor for actor in entered.character_states if actor.entity_id == pc)
        if immune:
            assert not actor.conditions
            assert not entered.effect_states
        else:
            target, effect_id, origin = region_condition_identity(
                A, source.source_id, pc, rule_ref=rule_ref
            )
            (condition,) = actor.conditions
            assert (condition.condition, condition.scope, condition.source_effect_id) == (
                "deafened",
                "session",
                effect_id,
            )
            (effect,) = entered.effect_states
            assert (effect.target_id, effect.id, effect.origin) == (target, effect_id, origin)
            viewer = GameViewContext(context.principal_id, sid, context.player_id, "player")
            player = sm.get_scene_view(sid, package, context=viewer).own_character
            assert player is not None
            assert tuple(player.effects) == ({"id": effect_id, "name": "Deafened"},)

        preview = sm.query_world_action(
            sid, pc, StepActor(A, "1,0", "2,0"), package, context=context
        )
        assert preview.status == "available", preview
        stay = submit(sm, sid, package, context, StepActor(A, "1,0", "2,0"), "stay.silent")
        assert stay.status == "committed", stay
        stayed = sm.read_noncombat_snapshot(sid, package, context=context)
        assert stayed.effect_states == entered.effect_states
        leave = submit(sm, sid, package, context, StepActor(A, "2,0", "3,0"), "leave.silent")
        assert leave.status == "committed", leave
        left = sm.read_noncombat_snapshot(sid, package, context=context)
        assert not left.effect_states
        assert not next(
            actor for actor in left.character_states if actor.entity_id == pc
        ).conditions
        if not immune:
            assert entered.effect_states[0].statuses == {"deafened"}


@pytest.mark.parametrize("rule_ref", ["condition:unknown", "condition:deafened"])
def test_unavailable_fixed_area_rule_is_rejected_before_movement(tmp_path, context, rule_ref):
    """Both authoring routes refuse unknown or absent bound condition content."""
    sm, sid, package, _ = setup(tmp_path, context, large_data())
    with sm:
        gm = replace(context, can_manage_environment=True)
        source = EnvironmentSource(
            "bad.area", FOG, EnvironmentArea(("1,0",)), region_effect_rule=rule_ref
        )
        initial = submit(
            sm,
            sid,
            package,
            gm,
            InitializeEnvironment(A, EnvironmentSceneFacts(sources=(source,))),
            "bad.seed",
        )
        assert initial.error_code == "invalid_environment"
        added = submit(sm, sid, package, gm, AddEnvironmentSource(A, source), "bad.add")
        assert added.error_code == "invalid_environment"
        assert sm.environment.read_facts(sid, A, package, context=gm) is None
        step = submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "safe.move")
        assert step.status == "committed", step


def test_public_fixed_area_motion_uses_same_hot_lookup_with_unrelated_growth(
    tmp_path, context, monkeypatch
):
    """Compare actual moves after admitting 32 extra sources on an unrelated cell."""
    from trpg_state_machine import ongoing_effects

    measured = []
    for unrelated_count in (0, 32):
        directory = tmp_path / str(unrelated_count)
        directory.mkdir()
        sm, sid, package, _ = setup(directory, context, large_data())
        with sm, monkeypatch.context() as patch:
            sources = [_toxic_fog("local", "2,0")]
            sources.extend(_toxic_fog(f"remote.{i}", "8,8") for i in range(unrelated_count))
            _seed(sm, sid, package, context, (A, tuple(sources)))
            counts = Counter()
            observe(patch, sm.environment, "_region_condition_index", counts, "index_builds")
            observe(
                patch, ongoing_effects, "plan_region_condition_transition", counts, "rule_calls"
            )
            warm = submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "warm.cell.index")
            assert warm.status == "committed", warm
            assert counts == {"index_builds": 1}
            counts.clear()
            with measure(sm) as (sql_counts, statements):
                enter = submit(sm, sid, package, context, StepActor(A, "1,0", "2,0"), "enter")
                leave = submit(sm, sid, package, context, StepActor(A, "2,0", "3,0"), "leave")
            assert enter.status == leave.status == "committed"
            assert counts == {"rule_calls": 2}
            keyed = [
                sql
                for sql in statements
                if "FROM ongoing_effects WHERE session_id=? AND target_id=? AND effect_id=?" in sql
            ]
            assert keyed
            measured.append((sql_counts, keyed))
    assert measured[0] == measured[1]


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
