"""Fixed toxic fog through public exploration, mechanical state and player views."""

from collections import Counter
from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_region_effect import region_condition_identity
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from lock_support import PASSAGE, A, B, lock_loader
from spatial_support import create
from test_migrations import stored_rows
from test_object_protection import large_data
from test_scoped_runtime_validation import grow, measure, observe
from world_support import command, setup, submit

from trpg_state_machine import (
    AddEnvironmentSource,
    BatchEnvironment,
    EnvironmentArea,
    EnvironmentConditions,
    EnvironmentSceneFacts,
    EnvironmentSource,
    InitializeEnvironment,
    Obscurant,
    RemoveEnvironmentSource,
    ReplaceEnvironmentSource,
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


@pytest.mark.parametrize("immune", [False, True])
def test_dynamic_area_changes_update_stationary_actor(tmp_path, context, immune):
    """Creating, moving or removing a source reconciles only its current coverage."""
    loader = get_default_ruleset()
    data = large_data()
    data["manifest"]["ruleset_binding"] = loader.binding.model_dump(mode="json")
    if immune:
        for foundation in data["rule_bindings"].values():
            foundation["snapshot"]["character_states"][0]["condition_immunities"] = ["deafened"]
    package = _validate(data)
    with StateMachine(tmp_path / "world.db", dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        gm = replace(context, can_manage_environment=True)
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        origin = sm.read_actor_position(sid, pc, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        first = EnvironmentSource(
            "silent.first",
            EnvironmentConditions(),
            EnvironmentArea(("0,0",)),
            region_effect_rule="condition:deafened",
        )
        other = replace(first, source_id="silent.other")

        def current_effects():
            snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
            actor = next(actor for actor in snapshot.character_states if actor.entity_id == pc)
            assert {record.condition for record in actor.conditions} == (
                {"deafened"} if snapshot.effect_states else set()
            )
            assert {record.source_effect_id for record in actor.conditions} == {
                effect.id for effect in snapshot.effect_states
            }
            return {effect.id for effect in snapshot.effect_states}

        def effect_ids(*sources):
            if immune:
                return set()
            return {
                region_condition_identity(A, s.source_id, pc, rule_ref=s.region_rule_ref)[1]
                for s in sources
            }

        before = sm.read_noncombat_snapshot(sid, package, context=context)
        add = command(sm, sid, package, gm, AddEnvironmentSource(A, first), "cover.stationary")
        receipt = sm.submit_command(add, package, context=gm)
        assert receipt.status == "committed", receipt
        assert current_effects() == effect_ids(first)
        assert not before.effect_states
        assert sm.submit_command(add, package, context=gm) == receipt
        assert current_effects() == effect_ids(first)

        distant = replace(first, area=EnvironmentArea(("2,0",)))
        batch = BatchEnvironment(
            (ReplaceEnvironmentSource(A, distant), AddEnvironmentSource(A, other))
        )
        assert submit(sm, sid, package, gm, batch, "change.same.tick").status == "committed"
        assert current_effects() == effect_ids(other)
        assert (
            submit(sm, sid, package, gm, ReplaceEnvironmentSource(A, first), "move.back").status
            == "committed"
        )
        assert current_effects() == effect_ids(first, other)
        assert (
            submit(
                sm, sid, package, gm, RemoveEnvironmentSource(A, first.source_id), "end.first"
            ).status
            == "committed"
        )
        assert current_effects() == effect_ids(other)
        assert (
            submit(
                sm, sid, package, gm, RemoveEnvironmentSource(A, other.source_id), "end.other"
            ).status
            == "committed"
        )
        assert current_effects() == set()
        assert sm.read_actor_position(sid, pc, package, context=context).cell == origin.cell
        assert sm.read_rng_context(sid, package, context=context) == rng


@pytest.mark.parametrize(
    ("initial_cell", "expected_effects"),
    [("0,0", 0), ("1,0", 1)],
    ids=["same_footprint", "new_coverage"],
)
def test_initialization_and_runtime_area_edit_use_distinct_boundaries(
    tmp_path, context, monkeypatch, initial_cell, expected_effects
):
    """A same-batch runtime edit compares with the immediately preceding seed."""
    from trpg_state_machine import ongoing_effects

    sm, sid, package, _ = setup(tmp_path, context, large_data())
    gm = replace(context, can_manage_environment=True)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        source = _toxic_fog("fog.seeded", initial_cell)
        updated = replace(source, area=EnvironmentArea(("0,0",)), priority=1)
        batch = BatchEnvironment(
            (
                InitializeEnvironment(A, EnvironmentSceneFacts(sources=(source,))),
                ReplaceEnvironmentSource(A, updated),
            )
        )
        calls = Counter()
        observe(monkeypatch, ongoing_effects, "plan_region_effect_transition", calls, "region")
        receipt = submit(sm, sid, package, gm, batch, "seed.and.edit")
        assert receipt.status == "committed", receipt
        assert sm.environment.read_facts(sid, A, package, context=gm).sources == (updated,)
        assert sm.read_actor_position(sid, pc, package, context=context).cell == "0,0"
        assert len(_poisoned_effects(sm, sid, package, context, pc)) == expected_effects
        assert calls["region"] == expected_effects


def test_dynamic_region_withdrawal_rolls_back_and_replays(tmp_path, context):
    """A late receipt fault preserves the source, its effects and overlapping sources."""
    sm, sid, package, db = setup(tmp_path, context, large_data())
    gm = replace(context, can_manage_environment=True)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        first, second = (_toxic_fog(name, "0,0") for name in ("fog.first", "fog.second"))
        batch = BatchEnvironment((AddEnvironmentSource(A, first), AddEnvironmentSource(A, second)))
        assert submit(sm, sid, package, gm, batch, "cover.actor").status == "committed"
        effects = _poisoned_effects(sm, sid, package, context, pc)
        assert len(effects) == 2
        before = stored_rows(db)
        facts = sm.environment.read_facts(sid, A, package, context=gm)
        assert facts.sources == (first, second)
        remove = command(
            sm, sid, package, gm, RemoveEnvironmentSource(A, first.source_id), "withdraw.first"
        )
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_region_withdrawal BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='withdraw.first' AND "
            "(SELECT count(*) FROM ongoing_effects WHERE session_id=NEW.session_id)=1 "
            "BEGIN SELECT RAISE(ABORT,'withdrawal fault after effect adoption'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(remove, package, context=gm)
        assert error.value.code == "storage_error"
        assert stored_rows(db) == before
        assert sm.environment.read_facts(sid, A, package, context=gm) == facts
        assert _poisoned_effects(sm, sid, package, context, pc) == effects
        assert sm.get_command_receipt(sid, remove.command_id, package, context=gm) is None
        sm._conn.execute("DROP TRIGGER fail_region_withdrawal")

        receipt = sm.submit_command(remove, package, context=gm)
        assert receipt.status == "committed", receipt
        remaining = _poisoned_effects(sm, sid, package, context, pc)
        assert len(remaining) == 1 and remaining[0] in effects
        assert sm.environment.read_facts(sid, A, package, context=gm).sources == (second,)

    with StateMachine(db, dnd_loader=lock_loader()) as reopened:
        assert reopened.submit_command(remove, package, context=gm) == receipt
        assert _poisoned_effects(reopened, sid, package, context, pc) == remaining
        assert reopened.environment.read_facts(sid, A, package, context=gm).sources == (second,)


def test_dynamic_region_write_cost_stays_local_with_unrelated_growth(
    tmp_path, context, monkeypatch
):
    """Count SQL reads, rows and rule calls for the same public coverage edit."""
    from trpg_state_machine import ongoing_effects

    measured = []
    gm = replace(context, can_manage_environment=True)
    for n in (0, 24):
        directory = tmp_path / str(n)
        directory.mkdir()
        sm, sid, package, _ = setup(directory, context, large_data())
        with sm, monkeypatch.context() as patch:
            pc = sm.read_session(sid, package, context=context).pc_actor_id
            grow(sm, sid, n)
            calls = Counter()
            observe(patch, ongoing_effects, "plan_region_effect_transition", calls, "region")
            source = _toxic_fog("local", "0,0")
            add = command(sm, sid, package, gm, AddEnvironmentSource(A, source), "add")
            with measure(sm) as (counts, statements):
                added = sm.submit_command(add, package, context=gm)
            assert added.status == "committed", added
            assert len(_poisoned_effects(sm, sid, package, context, pc)) == 1
            occupancy_sql = [
                sql
                for sql in statements
                if sql.startswith("SELECT actor_id,cell FROM actors")
                and "current_scene_id=? AND cell IN (SELECT value FROM json_each(?))" in sql
            ]
            assert len(occupancy_sql) == 1
            assert calls == {"region": 1}
            plan = sm._conn.execute("EXPLAIN QUERY PLAN " + occupancy_sql[0], (sid, A, '["0,0"]'))
            assert any("actors_cell_lookup" in row[3] for row in plan)
            measured.append(counts)
    assert measured[0] == measured[1]


@pytest.mark.parametrize(
    "configuration",
    [
        {"region_effect_rule": "condition:unknown"},
        {"region_effect_rule": "condition:deafened"},
        {
            "region_effect": {
                "name": "Malformed Fog",
                "transfer": False,
                "changes": [{"key": "check.bonus", "mode": 2, "value": "bad formula"}],
            }
        },
    ],
)
def test_unavailable_fixed_area_rule_is_rejected_before_movement(tmp_path, context, configuration):
    """Both authoring routes refuse unknown or absent bound condition content."""
    sm, sid, package, _ = setup(tmp_path, context, large_data())
    with sm:
        gm = replace(context, can_manage_environment=True)
        source = EnvironmentSource("bad.area", FOG, EnvironmentArea(("1,0",)), **configuration)
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


@pytest.mark.parametrize("status", ["blinded", "restrained"])
def test_bound_condition_region_requires_a_legal_exit(tmp_path, context, status):
    """Refuse a canonical condition whose movement or visibility would prevent exit."""
    data = large_data()
    loader = get_default_ruleset()
    data["manifest"]["ruleset_binding"] = loader.binding.model_dump(mode="json")
    package = _validate(data)
    with StateMachine(tmp_path / "world.db", dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        source = EnvironmentSource(
            "canonical.area",
            EnvironmentConditions(),
            EnvironmentArea(("1,0",)),
            region_effect_rule=f"condition:{status}",
        )
        rejected = submit(
            sm,
            sid,
            package,
            replace(context, can_manage_environment=True),
            AddEnvironmentSource(A, source),
            "bad.region",
        )
        assert rejected.error_code == "invalid_environment", rejected
        assert submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "step").status == (
            "committed"
        )
        snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        assert not snapshot.effect_states


def test_passive_region_retains_noncheck_modifier_and_filters_immune_status(tmp_path, context):
    """Use native mixed mechanics through public source adoption and a coverage exit."""
    loader = get_default_ruleset()
    data = large_data()
    data["manifest"]["ruleset_binding"] = loader.binding.model_dump(mode="json")
    for foundation in data["rule_bindings"].values():
        foundation["snapshot"]["character_states"][0]["condition_immunities"] = ["poisoned"]
    package = _validate(data)
    with StateMachine(tmp_path / "world.db", dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        source = EnvironmentSource(
            "mixed.area",
            FOG,
            EnvironmentArea(("1,0",)),
            region_effect={
                "name": "Mixed Fog",
                "transfer": False,
                "statuses": ["deafened", "poisoned"],
                "changes": [{"key": "attack.roll.bonus", "mode": 2, "value": "-2"}],
            },
        )
        _seed(sm, sid, package, context, (A, (source,)))
        assert submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "enter").status == (
            "committed"
        )
        entered = sm.read_noncombat_snapshot(sid, package, context=context)
        (effect,) = entered.effect_states
        assert effect.statuses == {"deafened"}
        assert [change.key for change in effect.changes] == ["attack.roll.bonus"]
        actor = next(actor for actor in entered.character_states if actor.entity_id == pc)
        (condition,) = actor.conditions
        assert (condition.scope, condition.source_effect_id) == ("session", effect.id)
        assert submit(sm, sid, package, context, StepActor(A, "1,0", "2,0"), "exit").status == (
            "committed"
        )
        assert not sm.read_noncombat_snapshot(sid, package, context=context).effect_states


@pytest.mark.parametrize("native", [False, True])
def test_public_fixed_area_motion_uses_same_hot_lookup_with_unrelated_growth(
    tmp_path, context, monkeypatch, native
):
    """Compare actual moves after admitting 32 extra sources on an unrelated cell."""
    from trpg_state_machine import ongoing_effects, scene_lifecycle

    measured = []
    for unrelated_count in (0, 32):
        directory = tmp_path / str(unrelated_count)
        directory.mkdir()
        sm, sid, package, _ = setup(directory, context, large_data())
        with sm, monkeypatch.context() as patch:
            target = _toxic_fog("local", "2,0")
            if native:
                target = replace(
                    target,
                    region_condition=None,
                    region_effect={
                        "name": "Local modifier",
                        "transfer": False,
                        "changes": [{"key": "check.bonus", "mode": 2, "value": "2"}],
                    },
                )
            sources = [target]
            sources.extend(_toxic_fog(f"remote.{i}", "8,8") for i in range(unrelated_count))
            _seed(sm, sid, package, context, (A, tuple(sources)))
            counts = Counter()
            observe(patch, sm.environment, "_region_condition_index", counts, "index_builds")
            observe(patch, ongoing_effects, "plan_region_effect_transition", counts, "rule_calls")
            observe(patch, scene_lifecycle, "region_rule_input", counts, "effect_inputs")
            warm = submit(sm, sid, package, context, StepActor(A, "0,0", "1,0"), "warm.cell.index")
            assert warm.status == "committed", warm
            assert counts == {"index_builds": 1}
            counts.clear()
            with measure(sm) as (sql_counts, statements):
                enter = submit(sm, sid, package, context, StepActor(A, "1,0", "2,0"), "enter")
                leave = submit(sm, sid, package, context, StepActor(A, "2,0", "3,0"), "leave")
            assert enter.status == leave.status == "committed"
            assert counts == {"effect_inputs": 2, "rule_calls": 2}
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
