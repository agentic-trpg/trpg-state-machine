"""Authoritative input versions: real SQLite writers, read scopes and counter evidence."""

import json
import sqlite3
from dataclasses import replace

import pytest
from lock_support import CRED, A, B, lock_loader
from test_environment_service import (
    WEATHER,
    Conditions,
    Facts,
    Source,
    gm,
    project,
    seed,
    value,
)
from test_environment_snapshot import Effects, install
from test_game_time import tick
from test_migrations import stored_rows
from test_object_protection import large_data
from test_world_spatial import OBSTACLE
from world_support import command, setup, submit

from trpg_state_machine import (
    AddEnvironmentSource,
    BatchEnvironment,
    EnvironmentArea,
    HeightInterval,
    Precipitation,
    ReplaceEnvironmentSource,
    RepositionObject,
    SetEnvironmentBase,
    StateMachine,
    StepActor,
    Temperature,
    Wind,
)
from trpg_state_machine.errors import StateMachineError
from trpg_state_machine.object_protection import PrecipitationProtection


def ready(tmp_path, context):
    sm, sid, package, db = setup(tmp_path, context, large_data())
    wo, effects = install(sm, sid, package, context)
    seed(sm, sid, package, context)
    return sm, sid, package, db, wo, effects


def counts(monkeypatch, sm):
    result = dict(sources=0, objects=0, projection=0, compose=0, map=0)
    for obj, name, key in (
        (sm.environment, "_inputs", "sources"),
        (sm._objects.spatial, "prepare_scene_protection", "objects"),
        (sm._objects.spatial, "scene_protection_contributions", "projection"),
        (sm.environment, "_compose_cell", "compose"),
        (sm._world.grid.reader, "scene_grid", "map"),
    ):
        original = getattr(obj, name)

        def counted(*args, original=original, key=key, **kwargs):
            result[key] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(obj, name, counted)
    return result


def test_version_hit_rain_and_wind_call_counts(tmp_path, context, monkeypatch):
    sm, sid, p, _, wo, effects = ready(tmp_path, context)
    with sm:
        count = counts(monkeypatch, sm)
        a = project(sm, sid, p, "3,3", height=3)
        cold = dict(count)
        assert project(sm, sid, p, "3,3", height=3) == a
        assert count == cold
        b = project(sm, sid, p, "4,3", height=3)
        assert project(sm, sid, p, "4,3", height=3) == b
        assert (count["sources"], count["objects"], count["projection"], count["compose"]) == (
            2,
            1,
            1,
            2,
        )
        count.update(dict.fromkeys(count, 0))
        original = sm.environment.versions.current(sid, A)
        weather = replace(WEATHER, precipitation=Precipitation("rain", "light"))
        assert (
            submit(sm, sid, p, gm(context), SetEnvironmentBase(A, weather), "rain").status
            == "committed"
        )
        assert sm.environment.versions.current(sid, A) == (original[0] + 1, original[1])
        count.update(dict.fromkeys(count, 0))  # Count queries separately from writer admission.
        assert value(project(sm, sid, p, "3,3", height=3).precipitation).intensity == "light"
        assert (count["sources"], count["objects"], count["projection"], count["compose"]) == (
            1,
            0,
            0,
            1,
        )
        submit(
            sm,
            sid,
            p,
            gm(context),
            SetEnvironmentBase(A, replace(weather, wind=Wind(20, "east"))),
            "wind",
        )
        assert sm.environment.versions.current(sid, A) == (original[0] + 2, original[1] + 1)
        count.update(dict.fromkeys(count, 0))
        assert project(sm, sid, p, "3,4", height=3).wind_exposure.status != "none"
        assert (count["sources"], count["objects"], count["projection"], count["compose"]) == (
            1,
            1,
            1,
            1,
        )
        reads = wo.reads, effects.reads
        project(sm, sid, p, "3,4", height=3)
        assert (wo.reads, effects.reads) == reads


def test_hits_never_read_detailed_inputs_in_trusted_public_and_range_interfaces(
    tmp_path, context, monkeypatch
):
    sm, sid, p, _, _, _ = ready(tmp_path, context)
    with sm:
        env = sm.environment
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        a = project(sm, sid, p, "3,3")
        public = env.query_cell(sid, pc, A, "3,3", p, context=context)
        with sm._transaction():
            ranges = env.project_height_range(sid, A, "3,3", HeightInterval(0, 8), p)
        public_ranges = env.query_height_range(
            sid, pc, A, "3,3", HeightInterval(0, 8), p, context=context
        )

        def forbidden(*a, **k):
            raise AssertionError("Version hit scanned detailed environment/protection/map input")

        monkeypatch.setattr(env, "_inputs", forbidden)
        monkeypatch.setattr(sm._objects.spatial, "prepare_scene_protection", forbidden)
        monkeypatch.setattr(sm._world.grid.reader, "scene_grid", forbidden)
        assert project(sm, sid, p, "3,3") == a
        assert env.query_cell(sid, pc, A, "3,3", p, context=context) == public
        with sm._transaction():
            assert env.project_height_range(sid, A, "3,3", HeightInterval(0, 8), p) == ranges
        assert (
            env.query_height_range(sid, pc, A, "3,3", HeightInterval(0, 8), p, context=context)
            == public_ranges
        )
        with pytest.raises(StateMachineError):
            env.query_cell(sid, pc, A, "3,3", p, context=replace(context, player_id="other"))


@pytest.mark.parametrize(
    "change,protect",
    [
        (dict(conditions=Conditions(wind=Wind(20, "east"))), 1),
        (dict(area=EnvironmentArea(("4,3",))), 1),
        (dict(priority=2), 1),
        (dict(observable=False), 1),
        (dict(exposure="outdoor"), 0),
        (dict(conditions=Conditions(wind=Wind(30, "north"))), 1),
    ],
)
def test_wind_source_changes_are_classified_inside_writer(tmp_path, context, change, protect):
    sm, sid, p, _, _, _ = ready(tmp_path, context)
    with sm:
        source = Source(
            "wind", Conditions(wind=Wind(20, "north")), EnvironmentArea(("3,3",)), observable=True
        )
        submit(sm, sid, p, gm(context), AddEnvironmentSource(A, source), "add")
        before = sm.environment.versions.current(sid, A)
        submit(
            sm,
            sid,
            p,
            gm(context),
            ReplaceEnvironmentSource(A, replace(source, **change)),
            "replace",
        )
        assert sm.environment.versions.current(sid, A) == (before[0] + 1, before[1] + protect)


def test_skip_versions_batch_replay_rejection_conflict_and_rollback_alias(
    tmp_path, context, monkeypatch
):
    sm, sid, p, _, _, _ = ready(tmp_path, context)
    with sm:
        baseline = project(sm, sid, p, "3,3")
        start = sm.environment.versions.current(sid, A)
        with pytest.raises(RuntimeError), sm._transaction(write=True):
            sm._world.facts.set_environment(
                sid, A, Facts(replace(WEATHER, temperature=Temperature(99)))
            )
            assert sm.environment.versions.current(sid, A) == (start[0] + 1, start[1])
            assert value(sm.environment.project_cell(sid, A, "3,3", p).temperature) == Temperature(
                99
            )
            raise RuntimeError("version N candidate must not be shared")
        assert sm.environment.versions.current(sid, A) == start
        assert project(sm, sid, p, "3,3") == baseline
        cmd = command(
            sm,
            sid,
            p,
            gm(context),
            SetEnvironmentBase(A, replace(WEATHER, temperature=Temperature(-99))),
            "real.N",
        )
        assert sm.submit_command(cmd, p, context=gm(context)).status == "committed"
        assert sm.environment.versions.current(sid, A) == (start[0] + 1, start[1])
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(-99)
        replay_version = sm.environment.versions.current(sid, A)
        sm.submit_command(cmd, p, context=gm(context))
        assert sm.environment.versions.current(sid, A) == replay_version
        for index in (1, 2, 3):
            submit(
                sm,
                sid,
                p,
                gm(context),
                SetEnvironmentBase(A, replace(WEATHER, temperature=Temperature(index))),
                f"v{index}",
            )
        count = counts(monkeypatch, sm)
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(3)
        assert count["compose"] == 1
        before = sm.environment.versions.current(sid, A)
        batch = BatchEnvironment(
            (
                SetEnvironmentBase(A, Conditions(Temperature(4))),
                SetEnvironmentBase(A, Conditions(Temperature(5))),
            )
        )
        submit(sm, sid, p, gm(context), batch, "batch")
        assert sm.environment.versions.current(sid, A)[0] == before[0] + 1
        current = sm.environment.versions.current(sid, A)
        rejected = BatchEnvironment(
            (
                SetEnvironmentBase(A, Conditions(Temperature(7))),
                ReplaceEnvironmentSource(A, Source("absent", Conditions())),
            )
        )
        assert submit(sm, sid, p, gm(context), rejected, "rejected").status == "rejected"
        assert sm.environment.versions.current(sid, A) == current
        assert (
            sm.submit_command(replace(cmd, command_id="stale"), p, context=gm(context)).status
            == "conflict"
        )
        assert sm.environment.versions.current(sid, A) == current


def test_versions_follow_owner_properties_and_physical_writes_not_unrelated_mutations(
    tmp_path, context
):
    sm, sid, p, _, wo, effects = ready(tmp_path, context)
    with sm:
        env = sm.environment
        initial = env.versions.current(sid, A)
        submit(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "actor")
        assert tick(sm, sid, p, replace(context, can_advance_time=True), 1).status == "committed"
        from trpg_state_machine import PickupItem

        submit(sm, sid, p, context, PickupItem(CRED, A, "2,0"), "unrelated")
        assert env.versions.current(sid, A) == initial
        assert project(sm, sid, p, "3,3").ground_precipitation.status == "none"
        wo.current = replace(wo.current, precipitation=PrecipitationProtection((), ("rain",)))
        assert env.versions.current(sid, A) == (initial[0], initial[1] + 1)
        assert project(sm, sid, p, "3,3").ground_precipitation.status != "none"
        effects.current = (Source("spell", Conditions(Temperature(77)), observable=True),)
        changed = env.versions.current(sid, A)
        assert changed == (initial[0] + 1, initial[1] + 1)
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(77)
        submit(
            sm,
            sid,
            p,
            replace(context, can_place_world_objects=True),
            RepositionObject(A, OBSTACLE, "3,3", "4,3", "north", "east"),
            "move",
        )
        assert env.versions.current(sid, A) == (changed[0], changed[1] + 1)
        with sm._transaction(write=True):
            # Trusted import exercises BOTH old and destination-scene dependencies.
            sm._world._initialize_scene(sid, B, p)
        before_a, before_b = env.versions.current(sid, A), env.versions.current(sid, B)
        with sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE objects SET scene_id=?,cell='0,0' WHERE session_id=? AND object_id=?",
                (B, sid, OBSTACLE),
            )
        assert env.versions.current(sid, A)[1] == before_a[1] + 1
        assert env.versions.current(sid, B)[1] == before_b[1] + 1
        before_b = env.versions.current(sid, B)
        with sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE objects SET cell='1,0' WHERE session_id=? AND object_id=?", (sid, OBSTACLE)
            )
        assert env.versions.current(sid, B)[1] == before_b[1] + 1


def test_old_reader_result_is_not_published_as_current_and_versions_do_not_accumulate(
    tmp_path, context
):
    sm, sid, p, db, _, _ = ready(tmp_path, context)
    with sm, StateMachine(db, dnd_loader=lock_loader()) as writer:
        env = sm.environment
        project(sm, sid, p, "3,3")
        old = env.versions.current(sid, A)
        with sm._transaction():
            assert env.versions.current(sid, A) == old
            submit(
                writer,
                sid,
                p,
                gm(context),
                SetEnvironmentBase(A, Conditions(Temperature(88))),
                "new",
            )
            assert value(env.project_cell(sid, A, "4,3", p).temperature) == Temperature(8)
        assert not any(key[1] == old and key[2][-2:] == ("4,3", None) for key in env._cells._values)
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(88)
        current = env.versions.current(sid, A)
        assert all(k[1] == current for k in env._cells._values)
        for number in range(8):
            submit(
                writer,
                sid,
                p,
                gm(context),
                SetEnvironmentBase(A, Conditions(Temperature(number))),
                f"next{number}",
            )
            project(sm, sid, p, "3,3")
        assert len(env._cells) == 1


def test_unregistered_third_party_provider_cannot_share_cross_query_results(
    tmp_path, context, monkeypatch
):
    sm, sid, p, _, _, _ = ready(tmp_path, context)
    with sm:
        provider = Effects()
        sm.environment.effects_provider = provider
        count = counts(monkeypatch, sm)
        project(sm, sid, p, "3,3")
        provider.current = (Source("external", Conditions(Temperature(-10)), observable=True),)
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(-10)
        assert (count["sources"], count["objects"], count["compose"]) == (2, 2, 2)


def test_time_boundaries_and_continuous_inputs_are_scene_and_role_specific(tmp_path, context):
    from trpg_state_machine.environment_versions import EnvironmentTimeWatch

    sm, sid, p, _, _, owner = ready(tmp_path, context)
    with sm:
        owner.current = (Source("timed", Conditions(wind=Wind(20, "east")), observable=True),)

        class ClockReader:
            def environment_sources(self, sid, scene_id, package):
                if scene_id != A:
                    return ()
                current = sm._world.facts.time_state(sid)[1]
                return owner.current if 2 <= current < 5 else ()

        old = sm.environment.effects_provider
        sm.environment.effects_provider = replace(
            old,
            key="timed/1",
            reader=ClockReader(),
            watches=tuple(replace(w, scene_ids=(A,)) for w in old.watches),
            time=EnvironmentTimeWatch((2, 5), wind=True, scene_ids=(A,)),
        )
        sm.environment.bind_providers(sid, p, context=gm(context))
        env = sm.environment
        base, other = env.versions.current(sid, A), env.versions.current(sid, B)
        ctx = replace(context, can_advance_time=True)
        for next_tick, delta in ((1, 0), (2, 1), (4, 1), (5, 2), (9, 2)):
            assert tick(sm, sid, p, ctx, next_tick).status == "committed"
            assert env.versions.current(sid, A) == (base[0] + delta, base[1] + delta)
            assert env.versions.current(sid, B) == other
            assert value(project(sm, sid, p, "3,3").wind).wind_from == (
                "east" if 2 <= next_tick < 5 else "north"
            )

        class ContinuousReader:
            def environment_sources(self, sid, scene_id, package):
                current = sm._world.facts.time_state(sid)[1]
                return (
                    (Source("temperature", Conditions(Temperature(current)), observable=True),)
                    if scene_id == A
                    else ()
                )

        env.effects_provider = replace(
            old,
            key="continuous/1",
            reader=ContinuousReader(),
            watches=(),
            time=EnvironmentTimeWatch(continuous=True, scene_ids=(A,)),
        )
        env.bind_providers(sid, p, context=gm(context))
        before, other = env.versions.current(sid, A), env.versions.current(sid, B)
        for delta in (1, 2):
            tick(sm, sid, p, ctx, 9 + delta)
            assert env.versions.current(sid, A) == (before[0] + delta, before[1])
            assert env.versions.current(sid, B) == other
            assert value(project(sm, sid, p, "3,3").temperature) == Temperature(9 + delta)


def test_actor_container_dependencies_only_invalidate_when_declared(tmp_path, context):
    from lock_support import DOOR

    from trpg_state_machine.environment_versions import EnvironmentInputWatch

    sm, sid, p, _, owner, _ = ready(tmp_path, context)
    with sm:
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        old = sm._objects.spatial.protection_provider

        class DependentRoof:
            def current_protection(self, sid, oid, package, *, candidate):
                current = owner.current
                actor = sm._world._actor_position(sid, pc)
                door = sm._world.facts.object(sid, DOOR)
                if actor.cell == "1,0" or door["state"] == "open":
                    return replace(current, precipitation=PrecipitationProtection((), ("rain",)))
                return current

        provider = replace(
            old,
            key="actor.container/1",
            reader=DependentRoof(),
            watches=old.watches
            + (
                EnvironmentInputWatch(
                    "actors",
                    "actor_id",
                    pc,
                    ("cell", "current_scene_id"),
                    "protection",
                    scene_ids=(A,),
                ),
                EnvironmentInputWatch(
                    "objects",
                    "object_id",
                    DOOR,
                    ("state", "scene_id"),
                    "protection",
                    scene_ids=(A,),
                ),
            ),
        )
        sm._objects.spatial.protection_provider = provider
        sm.environment.bind_providers(sid, p, context=gm(context))
        before = sm.environment.versions.current(sid, A)
        assert project(sm, sid, p, "3,3").ground_precipitation.status == "none"
        submit(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "dependent.actor")
        assert sm.environment.versions.current(sid, A) == (before[0], before[1] + 1)
        assert project(sm, sid, p, "3,3").ground_precipitation.status != "none"
        # The same mechanism covers owner-declared container/ancestor inputs.
        with sm._transaction(write=True):
            sm._world.facts.set_door_lock(sid, DOOR, False)
            sm._conn.execute(
                "UPDATE objects SET state='open' WHERE session_id=? AND object_id=?", (sid, DOOR)
            )
        assert sm.environment.versions.current(sid, A)[1] == before[1] + 2


def test_effect_wind_input_move_and_delete_bump_both_versions(tmp_path, context):
    sm, sid, p, _, _, owner = ready(tmp_path, context)
    with sm:
        before = sm.environment.versions.current(sid, A)
        owner.current = (Source("wind", Conditions(wind=Wind(20, "east")), observable=True),)
        assert sm.environment.versions.current(sid, A) == (before[0] + 1, before[1] + 1)
        assert value(project(sm, sid, p).wind).wind_from == "east"
        before = sm.environment.versions.current(sid, A)
        with sm._transaction(write=True):
            # Key movement removes wind from this provider although its JSON is unchanged.
            sm._conn.execute(
                "UPDATE fixture_environment_inputs SET name='elsewhere' WHERE session_id=?", (sid,)
            )
        assert sm.environment.versions.current(sid, A) == (before[0] + 1, before[1] + 2)
        # The protection JSON also changed membership, accounting for the second bump.
        assert value(project(sm, sid, p).wind).wind_from == "north"


def test_speed_change_can_change_wind_selection_across_different_owners(tmp_path, context):
    sm, sid, p, _, _, effects = ready(tmp_path, context)
    with sm:
        submit(
            sm,
            sid,
            p,
            gm(context),
            AddEnvironmentSource(
                A, Source("fact.wind", Conditions(wind=Wind(20, "east")), observable=True)
            ),
            "wind.fact",
        )
        effects.current = (
            Source("effect.wind", Conditions(wind=Wind(20, "east")), observable=True),
        )
        assert project(sm, sid, p).wind.status == "ready"
        before = sm.environment.versions.current(sid, A)
        effects.current = (
            replace(effects.current[0], conditions=Conditions(wind=Wind(30, "east"))),
        )
        assert sm.environment.versions.current(sid, A) == (before[0] + 1, before[1] + 1)
        assert project(sm, sid, p).wind.status == "unsupported"


def test_failed_commit_restores_versions_and_uncommitted_results_never_publish(tmp_path, context):
    sm, sid, p, db, _, _ = ready(tmp_path, context)
    with sm:
        baseline = project(sm, sid, p)
        before = sm.environment.versions.current(sid, A)
        rows = stored_rows(db)
        cmd = command(
            sm,
            sid,
            p,
            gm(context),
            SetEnvironmentBase(A, replace(WEATHER, temperature=Temperature(99))),
            "commit.fail",
        )
        original = sm._store._conn

        class FailCommit:
            def __getattr__(self, name):
                return getattr(original, name)

            def commit(self):
                if not sm._store._scope.write:
                    return original.commit()
                assert sm.environment.versions.current(sid, A)[0] == before[0] + 1
                assert value(
                    sm.environment.project_cell(sid, A, "2,1", p).temperature
                ) == Temperature(99)
                raise sqlite3.OperationalError("injected COMMIT failure")

        sm._store._conn = FailCommit()
        try:
            with pytest.raises(StateMachineError):
                sm.submit_command(cmd, p, context=gm(context))
        finally:
            sm._store._conn = original
        assert stored_rows(db) == rows
        assert sm.environment.versions.current(sid, A) == before
        assert project(sm, sid, p) == baseline


def test_provider_registration_failure_is_atomic_and_replay_is_a_noop(tmp_path, context):
    from trpg_state_machine.environment_versions import EnvironmentInputWatch

    sm, sid, p, db, _, _ = ready(tmp_path, context)
    with sm:
        before = stored_rows(db)
        good = sm.environment.effects_provider
        sm.environment.bind_providers(sid, p, context=gm(context))
        assert stored_rows(db) == before
        bad = replace(
            good,
            key="broken/1",
            watches=(
                EnvironmentInputWatch("missing_owner_table", "name", "owner", ("input",), "source"),
            ),
        )
        with sm._transaction(write=True), pytest.raises(ValueError):
            sm.environment.versions.bind(sid, A, p, bad, "effects")
        assert stored_rows(db) == before
        with pytest.raises(StateMachineError):
            sm.environment.bind_providers(sid, p, context=context)
        assert stored_rows(db) == before


def test_schema35_migration_is_atomic_and_preserves_all_original_rows(
    tmp_path, context, monkeypatch
):
    import io
    import os
    import subprocess
    import sys
    import zipfile
    from pathlib import Path

    from test_migrations import assert_original_rows_equal

    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "old-source"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "382a4ae", "src"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        zipped.extractall(source)
    code = """
from pathlib import Path
from world_support import setup
from test_environment_service import seed
from test_object_protection import large_data
from trpg_state_machine import TrustedContext
import sys,json
c=TrustedContext('authenticated:alice','player.alice')
sm,sid,p,db=setup(Path(sys.argv[1]),c,large_data());seed(sm,sid,p,c)
print(json.dumps([sid,sm._conn.execute('PRAGMA user_version').fetchone()[0]]));sm.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(source / "src"), str(root / "tests"))),
        },
    )
    sid, version = json.loads(result.stdout)
    assert version == 35
    db = tmp_path / "world.db"
    before = stored_rows(db)
    read = Path.read_text

    def broken(path, *a, **k):
        text = read(path, *a, **k)
        return (
            text.replace("PRAGMA user_version = 36;", "INVALID MIGRATION;")
            if path.name == "_migration_36.sql"
            else text
        )

    monkeypatch.setattr(Path, "read_text", broken)
    with pytest.raises(StateMachineError):
        StateMachine(db, dnd_loader=lock_loader())
    assert stored_rows(db) == before
    monkeypatch.undo()
    from trpg_state_machine.package import _validate

    p = _validate(large_data())
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        sm.upgrade_instance_configs(sid, p, context=context)
        sm.resume_session(sid, p, context=context)
        assert_original_rows_equal(before, stored_rows(db))
        assert sm.environment.versions.current(sid, A) == (0, 0)
        baseline = project(sm, sid, p)
        submit(
            sm,
            sid,
            p,
            gm(context),
            SetEnvironmentBase(A, replace(WEATHER, temperature=Temperature(19))),
            "new",
        )
        assert sm.environment.versions.current(sid, A) == (1, 0)
        current = project(sm, sid, p)
        assert current != baseline
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        restored.resume_session(sid, p, context=context)
        assert restored.environment.versions.current(sid, A) == (1, 0)
        assert project(restored, sid, p) == current


def test_static_protection_and_grid_writes_advance_only_protection(tmp_path, context):
    from trpg_state_machine import SceneEnvironmentProtection, SetEnvironmentProtection
    from trpg_state_machine.package import canonical_json

    sm, sid, p, _, _, _ = ready(tmp_path, context)
    with sm:
        env = sm.environment
        before = env.versions.current(sid, A)
        op = SetEnvironmentProtection(A, SceneEnvironmentProtection(("2,1",), ()))
        submit(sm, sid, p, gm(context), op, "static")
        assert env.versions.current(sid, A) == (before[0], before[1] + 1)
        assert project(sm, sid, p).ground_precipitation.status == "none"
        with sm._transaction(write=True):
            raw = sm._world.facts.spatial_scene(sid, A)
            raw["grid"]["width"] += 1
            sm._conn.execute(
                "UPDATE mechanical_scenes SET state_json=? WHERE session_id=? AND scene_id=?",
                (canonical_json(raw), sid, A),
            )
        assert env.versions.current(sid, A) == (before[0], before[1] + 2)
        assert project(sm, sid, p, "9,0").cell == "9,0"


def test_real_container_and_carry_changes_follow_existing_ground_only_model(tmp_path, context):
    from environment_version_support import providers
    from test_world_containers import BOX, container_data

    from trpg_state_machine import DropItem, PickupItem, PutItem, TakeItem
    from trpg_state_machine.object_protection import (
        CurrentProtectionProperties,
        RainProtection,
        WindProtection,
    )

    sm, sid, p, _ = setup(tmp_path, context, container_data())
    with sm:
        providers(
            sm,
            sid,
            p,
            context,
            CurrentProtectionProperties(RainProtection(((0, 0),)), WindProtection(())),
            CRED,
        )
        seed(sm, sid, p, context)
        env = sm.environment
        assert project(sm, sid, p, "2,0").ground_precipitation.status == "none"
        submit(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "walk")
        before = env.versions.current(sid, A)
        assert (
            submit(sm, sid, p, context, PickupItem(CRED, A, "2,0"), "pickup").status == "committed"
        )
        assert env.versions.current(sid, A) == (before[0], before[1] + 1)
        assert project(sm, sid, p, "2,0").ground_precipitation.status != "none"
        unchanged = env.versions.current(sid, A)
        for index, operation in enumerate(
            (
                PutItem(A, CRED, BOX, "1,0"),
                PickupItem(BOX, A, "1,0"),
                StepActor(A, "1,0", "0,0"),
                TakeItem(A, CRED, BOX, "0,0"),
            )
        ):
            assert submit(sm, sid, p, context, operation, f"nested{index}").status == "committed"
            assert env.versions.current(sid, A) == unchanged
        assert submit(sm, sid, p, context, DropItem(CRED, A, "0,0"), "drop").status == "committed"
        assert env.versions.current(sid, A) == (unchanged[0], unchanged[1] + 1)
        assert project(sm, sid, p, "0,0").ground_precipitation.status == "none"


def test_standalone_internal_and_whole_area_hits_need_no_environment_map_preparation(
    tmp_path, context, monkeypatch
):
    sm, sid, p, _, _, _ = ready(tmp_path, context)
    with sm:
        env = sm.environment
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        baseline = env.project_cell(sid, A, "2,1", p)
        whole = env.query_area(sid, pc, A, EnvironmentArea(), p, context=context)

        def forbidden(*a, **k):
            raise AssertionError("A version hit unnecessarily rebuilt environment inputs")

        monkeypatch.setattr(env, "_inputs", forbidden)
        monkeypatch.setattr(sm._world.grid.reader, "scene_grid", forbidden)
        assert env.project_cell(sid, A, "2,1", p) == baseline
        assert env.query_area(sid, pc, A, EnvironmentArea(), p, context=context) == whole


def test_provider_manifest_reopen_and_missing_dependency_trigger_fail_closed(tmp_path, context):
    from environment_version_support import EffectsOwner, ProtectionOwner

    sm, sid, p, db, _, _ = ready(tmp_path, context)
    effects, protection = sm.environment.effects_provider, sm._objects.spatial.protection_provider
    baseline = project(sm, sid, p, "3,3")
    sm.close()
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        restored.environment.effects_provider = replace(
            effects, reader=EffectsOwner(restored, sid, "effects_json")
        )
        restored._objects.spatial.protection_provider = replace(
            protection, reader=ProtectionOwner(restored, sid, "protection_json")
        )
        assert restored.environment.versions.cacheable(
            sid,
            A,
            restored.environment.effects_provider,
            restored._objects.spatial.protection_provider,
        )[0]
        assert project(restored, sid, p, "3,3") == baseline
        trigger = restored._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'env_provider_%'"
        ).fetchone()[0]
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TRIGGER "' + trigger + '"')
    with pytest.raises(StateMachineError) as refused:
        StateMachine(db, dnd_loader=lock_loader())
    assert refused.value.code == "unsupported_storage_schema"


def test_registering_absent_default_providers_does_not_change_input_versions(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm:
        before = stored_rows(db)
        sm.environment.bind_providers(sid, p, context=gm(context))
        assert stored_rows(db) == before


def test_formal_provider_versions_follow_open_and_damage_with_rollback(tmp_path, context):
    from environment_version_support import providers
    from test_object_spatial_service import oriented_data

    from trpg_state_machine import ObjectState, SetObjectState
    from trpg_state_machine.object_protection import (
        CurrentProtectionProperties,
        RainProtection,
        WindProtection,
    )

    data = oriented_data(anchor="1,0", footprint=[[0, 0]], blocking=False, openable=True)
    data["world_objects"][-1]["capabilities"].append(
        {
            "kind": "durability",
            "armor_class": 10,
            "max_hp": 10,
            "initial_hp": 10,
            "damage_profile_ref": "scenario.explicit_settled_damage",
            "damage_threshold": None,
            "destruction_policy_ref": "disable_preserve_obstruction",
        }
    )
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        owner, _ = providers(
            sm,
            sid,
            p,
            context,
            CurrentProtectionProperties(RainProtection(((0, 0),)), WindProtection(())),
            OBSTACLE,
        )
        old = sm._objects.spatial.protection_provider

        class Stateful:
            def current_protection(self, sid, oid, package, *, candidate):
                current = owner.current
                return (
                    replace(current, rain=RainProtection(()))
                    if (candidate.open_state == "open" or candidate.destroyed)
                    else current
                )

        sm._objects.spatial.protection_provider = replace(old, key="stateful/1", reader=Stateful())
        sm.environment.bind_providers(sid, p, context=gm(context))
        seed(sm, sid, p, context)
        env = sm.environment
        before = env.versions.current(sid, A)
        assert project(sm, sid, p, "1,0").ground_precipitation.status == "none"
        submit(sm, sid, p, context, SetObjectState(A, OBSTACLE, ObjectState.OPEN), "open")
        assert env.versions.current(sid, A) == (before[0], before[1] + 1)
        assert project(sm, sid, p, "1,0").ground_precipitation.status != "none"
        submit(sm, sid, p, context, SetObjectState(A, OBSTACLE, ObjectState.CLOSED), "closed")
        prior = env.versions.current(sid, A)
        with pytest.raises(RuntimeError), sm._transaction(write=True):
            sm._world.facts.apply_object_hp(sid, OBSTACLE, 10, 0)
            assert env.versions.current(sid, A) == (prior[0], prior[1] + 1)
            assert env.project_cell(sid, A, "1,0", p).ground_precipitation.status != "none"
            raise RuntimeError("rollback admitted damage")
        assert env.versions.current(sid, A) == prior
        assert project(sm, sid, p, "1,0").ground_precipitation.status == "none"
        with sm._transaction(write=True):
            sm._world.facts.apply_object_hp(sid, OBSTACLE, 10, 0)
        assert env.versions.current(sid, A) == (prior[0], prior[1] + 1)
        assert project(sm, sid, p, "1,0").ground_precipitation.status != "none"
