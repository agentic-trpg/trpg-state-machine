"""Deterministic cache evidence through real Environment / Session / command paths."""

import sqlite3
from dataclasses import FrozenInstanceError, asdict, replace

import pytest
from lock_support import CRED, DOOR, A, B, lock_loader
from spatial_support import create
from test_environment_service import (
    WEATHER,
    Conditions,
    Facts,
    Protection,
    Source,
    gm,
    project,
    seed,
    value,
)
from test_game_time import tick
from test_migrations import stored_rows
from test_object_protection import large_data
from test_object_spatial_service import oriented_data
from test_world_spatial import OBSTACLE
from world_support import command, setup, submit

import trpg_state_machine.world_spatial as spatial_module
from trpg_state_machine import (
    AddEnvironmentSource,
    EnvironmentArea,
    HeightInterval,
    ObjectState,
    Obscurant,
    PickupItem,
    Precipitation,
    RemoveEnvironmentSource,
    ReplaceEnvironmentSource,
    RepositionObject,
    SetEnvironmentBase,
    SetEnvironmentProtection,
    SetObjectState,
    StateMachine,
    StaticPrecipitationProtection,
    StepActor,
    Temperature,
    Wind,
)
from trpg_state_machine.commands import command_fingerprint
from trpg_state_machine.environment_commands import environment_operation_from_data
from trpg_state_machine.errors import StateMachineError
from trpg_state_machine.object_protection import (
    PRECIPITATION_KINDS,
    CurrentProtectionProperties,
    PrecipitationProtection,
    RainProtection,
    WindBoundary,
    WindProtection,
)
from trpg_state_machine.package import _validate


class WO:
    def __init__(self):
        self.reads = 0
        self.current = CurrentProtectionProperties(
            wind=WindProtection((WindBoundary((0, 0), "north", 3, 0, 6),)),
            base_height_ft=0,
            precipitation=PrecipitationProtection(
                tuple((x, y) for x in range(4) for y in range(4)), PRECIPITATION_KINDS
            ),
        )

    def current_protection(self, sid, oid, package, *, candidate):
        self.reads += 1
        return (
            self.current
            if oid == OBSTACLE
            else CurrentProtectionProperties(RainProtection(()), WindProtection(()))
        )


class Effects:
    current = ()

    def environment_sources(self, sid, scene_id, package):
        return self.current


def counters(monkeypatch, sm):
    counts = dict(compose=0, scene=0, roof=0, visibility=0, light=0, target=0)

    def wrap(obj, name, counter, predicate=lambda *a, **k: True):
        original = getattr(obj, name)

        def counted(*args, **kwargs):
            if predicate(*args, **kwargs):
                counts[counter] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(obj, name, counted)

    wrap(sm.environment, "_compose_cell", "compose")
    wrap(sm._objects.spatial, "scene_protection_contributions", "scene")
    wrap(spatial_module, "_project_protection", "roof", lambda oid, *a: oid == OBSTACLE)
    wrap(sm._world, "_prepare_visible_targets", "visibility")
    wrap(sm._objects.lights, "project", "light")
    wrap(sm._world, "_visible_target", "target")
    return counts


def install(sm, sid, package, context):
    from environment_version_support import providers

    return providers(sm, sid, package, context, WO().current, OBSTACLE)


def test_a_projection_serves_b_without_claiming_same_environment(tmp_path, context, monkeypatch):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm:
        wo, _ = install(sm, sid, p, context)
        source = Source(
            "warm",
            Conditions(Temperature(31), obscurant=Obscurant("smoke", "dense")),
            EnvironmentArea(("4,3",)),
            observable=True,
        )
        seed(sm, sid, p, context, Facts(WEATHER, (source,)))
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        count = counters(monkeypatch, sm)
        before = stored_rows(db)

        def query(cell):
            return sm.environment.query_cell(sid, pc, A, cell, p, context=context)

        a = query("3,3")
        assert query("3,3") == a
        b = query("4,3")
        assert query("4,3") == b
        assert (count["compose"], count["scene"], count["roof"]) == (2, 1, 1)
        assert wo.reads == 1  # Revision hits and the B-cell projection reuse avoid owner reads.
        assert a.ground_precipitation.status == b.ground_precipitation.status == "none"
        assert value(a.temperature) == Temperature(8) and value(b.temperature) == Temperature(31)
        assert a.obscurant != b.obscurant
        assert count["visibility"] == count["target"] == 4
        assert count["light"] == 1  # unchanged RAM inputs reuse the derived light grid
        assert len(sm.environment._cells) == 2  # The other 14 covered cells were not composed.
        with pytest.raises(FrozenInstanceError):
            a.temperature.status = "none"
        assert stored_rows(db) == before


def test_weather_effects_and_source_fields_invalidate_only_needed_layer(
    tmp_path, context, monkeypatch
):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        _, effects = install(sm, sid, p, context)
        seed(sm, sid, p, context)
        count = counters(monkeypatch, sm)
        project(sm, sid, p, "3,3")
        weather = replace(WEATHER, precipitation=Precipitation("rain", "light"))
        submit(sm, sid, p, gm(context), SetEnvironmentBase(A, weather), "weather")
        assert value(project(sm, sid, p, "3,3").precipitation).intensity == "light"
        assert (count["compose"], count["scene"], count["roof"]) == (2, 1, 1)
        source = Source(
            "local", Conditions(Temperature(30)), EnvironmentArea(("3,3",)), observable=True
        )
        submit(sm, sid, p, gm(context), AddEnvironmentSource(A, source), "add")
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(30)
        changes = (
            replace(source, area=EnvironmentArea(("4,3",))),
            replace(source, priority=2),
            replace(source, exposure="outdoor"),
            replace(source, observable=False),
            replace(source, conditions=Conditions(Temperature(-7))),
        )
        for index, changed in enumerate(changes):
            submit(sm, sid, p, gm(context), ReplaceEnvironmentSource(A, changed), f"replace{index}")
            previous = count["compose"]
            result = project(sm, sid, p, "3,3")
            assert count["compose"] == previous + 1
            assert value(result.temperature) == (
                Temperature(8) if index == 0 else Temperature(-7) if index == 4 else Temperature(30)
            )
        submit(sm, sid, p, gm(context), RemoveEnvironmentSource(A, "local"), "remove")
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(8)
        effects.current = (replace(source, source_id="effect"),)
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(30)
        effects.current = (replace(effects.current[0], area=EnvironmentArea(("4,3",))),)
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(8)
        assert value(project(sm, sid, p, "4,3").temperature) == Temperature(30)
        effects.current = (replace(effects.current[0], conditions=Conditions(Temperature(40))),)
        assert value(project(sm, sid, p, "4,3").temperature) == Temperature(40)
        effects.current = ()
        assert value(project(sm, sid, p, "4,3").temperature) == Temperature(8)
        effects.current = None
        assert project(sm, sid, p, "4,3").effects_status == "unconfigured"
        assert count["scene"] == count["roof"] == 1


def test_live_properties_pose_and_wind_direction(tmp_path, context, monkeypatch):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        wo, _ = install(sm, sid, p, context)
        seed(sm, sid, p, context)
        count = counters(monkeypatch, sm)
        assert project(sm, sid, p, "3,4", height=3).wind_exposure.status == "none"
        # WindBoundary itself is not in its own downwind shelter.
        assert project(sm, sid, p, "3,3", height=3).wind_exposure.status != "none"
        assert count["scene"] == 1
        ctx = replace(gm(context), can_place_world_objects=True)
        move = RepositionObject(A, OBSTACLE, "3,3", "4,3", "north", "east")
        assert submit(sm, sid, p, ctx, move, "rotate").status == "committed"
        assert project(sm, sid, p, "3,4", height=3).wind_exposure.status != "none"
        submit(
            sm, sid, p, ctx, SetEnvironmentBase(A, replace(WEATHER, wind=Wind(20, "east"))), "east"
        )
        assert project(sm, sid, p, "3,3", height=3).wind_exposure.status == "none"
        assert project(sm, sid, p, "4,3", height=3).wind_exposure.status != "none"
        assert project(sm, sid, p, "3,3", height=6).wind_exposure.status != "none"
        wo.current = replace(
            wo.current,
            wind=WindProtection(()),
            precipitation=PrecipitationProtection((), PRECIPITATION_KINDS),
        )
        assert project(sm, sid, p, "3,3", height=3).wind_exposure.status != "none"
        assert project(sm, sid, p, "4,3").ground_precipitation.status != "none"
        assert count["scene"] == 4


def test_open_damage_and_failed_damage_follow_current_provider_inputs(tmp_path, context):
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

    class StatefulWO(WO):
        def current_protection(self, sid, oid, package, *, candidate):
            current = super().current_protection(sid, oid, package, candidate=candidate)
            if oid == OBSTACLE and (candidate.open_state == "open" or candidate.destroyed):
                return replace(
                    current, precipitation=PrecipitationProtection((), PRECIPITATION_KINDS)
                )
            return current

    with sm:
        sm._objects.spatial.protection_provider = StatefulWO()
        seed(sm, sid, p, context)
        assert project(sm, sid, p, "1,0").ground_precipitation.status == "none"
        assert (
            submit(
                sm, sid, p, context, SetObjectState(A, OBSTACLE, ObjectState.OPEN), "open"
            ).status
            == "committed"
        )
        assert project(sm, sid, p, "1,0").ground_precipitation.status != "none"
        assert (
            submit(
                sm, sid, p, context, SetObjectState(A, OBSTACLE, ObjectState.CLOSED), "close"
            ).status
            == "committed"
        )
        assert project(sm, sid, p, "1,0").ground_precipitation.status == "none"
        # Trusted owner write seam; this does not introduce a player damage command.
        with pytest.raises(RuntimeError), sm._transaction(write=True):
            sm._world.facts.apply_object_hp(sid, OBSTACLE, 10, 0)
            assert (
                sm.environment.project_cell(sid, A, "1,0", p).ground_precipitation.status != "none"
            )
            raise RuntimeError("failed adoption")
        assert project(sm, sid, p, "1,0").ground_precipitation.status == "none"
        with sm._transaction(write=True):
            sm._world.facts.apply_object_hp(sid, OBSTACLE, 10, 0)
        assert project(sm, sid, p, "1,0").ground_precipitation.status != "none"


@pytest.mark.parametrize("kind", PRECIPITATION_KINDS)
def test_explicit_object_static_and_local_precipitation(tmp_path, context, kind):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    weather = replace(WEATHER, precipitation=Precipitation(kind, "heavy"))
    with sm:
        wo, _ = install(sm, sid, p, context)
        seed(sm, sid, p, context, Facts(weather))
        result = project(sm, sid, p, "3,3")
        assert result.precipitation.status == "ready" and value(result.precipitation).kind == kind
        assert result.ground_precipitation.status == "none"
        # Explicit subset cannot silently protect another type.
        wo.current = replace(
            wo.current, rain=None, precipitation=PrecipitationProtection(((0, 0),), (kind,))
        )
        assert project(sm, sid, p, "3,3").ground_precipitation.status == "none"
        local = Source(
            "local",
            Conditions(precipitation=Precipitation(kind, "heavy")),
            EnvironmentArea(("3,3",)),
            observable=True,
        )
        submit(sm, sid, p, gm(context), AddEnvironmentSource(A, local), "local")
        assert project(sm, sid, p, "3,3").ground_precipitation.status == "ready"
        submit(sm, sid, p, gm(context), RemoveEnvironmentSource(A, "local"), "stop")
        static = Protection((), (), (StaticPrecipitationProtection(("2,1",), (kind,)),))
        op = SetEnvironmentProtection(A, static)
        assert environment_operation_from_data("world.set_environment_protection", asdict(op)) == op
        assert submit(sm, sid, p, gm(context), op, "static").status == "committed"
        assert project(sm, sid, p).ground_precipitation.status == "none"
        submit(sm, sid, p, gm(context), SetEnvironmentProtection(A, Protection((), ())), "clear")
        assert project(sm, sid, p).ground_precipitation.status != "none"


@pytest.mark.parametrize("kind", PRECIPITATION_KINDS)
def test_legacy_rain_properties_and_json_remain_rain_only(tmp_path, context, kind):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        wo, _ = install(sm, sid, p, context)
        wo.current = CurrentProtectionProperties(RainProtection(((0, 0),)), WindProtection(()))
        seed(
            sm, sid, p, context, Facts(replace(WEATHER, precipitation=Precipitation(kind, "light")))
        )
        legacy = environment_operation_from_data(
            "world.set_environment_protection",
            {"scene_id": A, "protection": {"rain_cells": ["2,1"], "wind": []}},
        )
        submit(sm, sid, p, gm(context), legacy, "legacy")
        for cell in ("2,1", "3,3"):
            exposed = project(sm, sid, p, cell).ground_precipitation
            assert (exposed.status == "none") == (kind == "rain")


def test_session_scene_package_height_grid_and_unrelated_changes(tmp_path, context, monkeypatch):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        install(sm, sid, p, context)
        source = Source(
            "low",
            Conditions(Temperature(-5)),
            EnvironmentArea(("3,3",), HeightInterval(0, 2)),
            observable=True,
        )
        seed(sm, sid, p, context, Facts(WEATHER, (source,)))
        seed(sm, sid, p, context, Facts(Conditions(Temperature(99))), scene=B, key="sceneB")
        sid2 = create(sm, p, context, key="second")
        seed(sm, sid2, p, context, Facts(Conditions(Temperature(40))), key="seed2")
        count = counters(monkeypatch, sm)
        assert project(sm, sid, p, "3,3").temperature.status == "unknown"
        assert value(project(sm, sid, p, "3,3", height=0).temperature) == Temperature(-5)
        assert value(project(sm, sid, p, "3,3", height=2).temperature) == Temperature(8)
        assert value(project(sm, sid, p, "0,0", scene=B).temperature) == Temperature(99)
        assert value(project(sm, sid2, p, "3,3").temperature) == Temperature(40)
        changed = large_data()
        changed["manifest"]["title"] += " new"
        other = _validate(changed)
        # Trusted internal calls own binding; even there separate Package inputs never alias.
        project(sm, sid, other, "3,3")
        assert count["compose"] == 6
        project(sm, sid, replace(p, package_version="different"), "3,3")
        assert count["compose"] == 7
        baseline = project(sm, sid, p, "3,3")
        assert submit(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "step").status == "committed"
        assert tick(sm, sid, p, replace(context, can_advance_time=True), 1).status == "committed"
        assert (
            submit(sm, sid, p, context, PickupItem(CRED, A, "2,0"), "unrelated.item").status
            == "committed"
        )
        with sm._transaction(write=True):
            # Lock details do not belong in protection identity when the provider
            # continues to return the same properties and the pose is unchanged.
            sm._world.facts.set_door_lock(sid, DOOR, False)
        assert project(sm, sid, p, "3,3") == baseline and count["compose"] == 7
        with sm._transaction(write=True):
            raw = sm._world.facts.spatial_scene(sid, A)
            raw["grid"]["width"] += 1
            sm._conn.execute(
                "UPDATE mechanical_scenes SET state_json=? WHERE session_id=? AND scene_id=?",
                (spatial_module.canonical_json(raw), sid, A),
            )
        assert project(sm, sid, p, "3,3") == baseline and count["compose"] == 8


@pytest.mark.parametrize("failure", ["rejected", "storage_error"])
def test_command_candidate_queries_cannot_publish_on_failure(
    tmp_path, context, monkeypatch, failure
):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        install(sm, sid, p, context)
        seed(sm, sid, p, context)
        baseline = project(sm, sid, p, "3,3")
        sizes = len(sm.environment._cells), len(sm.environment._projections)

        def fail(*a, **k):
            candidate = sm.environment.project_cell(sid, A, "3,3", p)
            assert value(candidate.temperature) == Temperature(-10)
            assert (len(sm.environment._cells), len(sm.environment._projections)) == sizes
            raise StateMachineError(failure, "injected after uncommitted projection")

        monkeypatch.setattr(sm._event_persistence, "_append_events", fail)
        op = SetEnvironmentBase(A, replace(WEATHER, temperature=Temperature(-10)))
        if failure == "storage_error":
            with pytest.raises(StateMachineError):
                submit(sm, sid, p, gm(context), op, "fail")
        else:
            assert submit(sm, sid, p, gm(context), op, "fail").status == "rejected"
        assert project(sm, sid, p, "3,3") == baseline
        assert (len(sm.environment._cells), len(sm.environment._projections)) == sizes


def test_read_rollback_commit_failure_and_clear_cancel_publication(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        install(sm, sid, p, context)
        seed(sm, sid, p, context)
        env = sm.environment
        with pytest.raises(RuntimeError), sm._transaction():
            env.project_cell(sid, A, "3,3", p)
            assert len(env._cells) == len(env._projections) == 0
            raise RuntimeError("aborted read")
        assert len(env._cells) == len(env._projections) == 0

        class FailCommit:
            def __getattr__(self, name):
                return getattr(connection, name)

            def commit(self):
                raise sqlite3.OperationalError("injected read COMMIT failure")

        connection = sm._store._conn
        sm._store._conn = FailCommit()
        try:
            with pytest.raises(StateMachineError), sm._transaction():
                env.project_cell(sid, A, "3,3", p)
        finally:
            sm._store._conn = connection
        assert len(env._cells) == len(env._projections) == 0
        with sm._transaction():
            env.project_cell(sid, A, "3,3", p)
            env.clear_snapshots()
        assert len(env._cells) == len(env._projections) == 0
        assert project(sm, sid, p, "3,3").ground_precipitation.status == "none"


def test_old_reader_cannot_replace_current_content_identity(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm, StateMachine(db, dnd_loader=lock_loader()) as writer:
        install(sm, sid, p, context)
        seed(sm, sid, p, context)
        with sm._transaction():
            sm._conn.execute("SELECT facts_json FROM scene_environments").fetchone()
            submit(
                writer,
                sid,
                p,
                gm(context),
                SetEnvironmentBase(A, Conditions(Temperature(-20))),
                "new",
            )
            assert value(sm.environment.project_cell(sid, A, "3,3", p).temperature) == Temperature(
                8
            )
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(-20)
        assert value(project(sm, sid, p, "3,3").temperature) == Temperature(-20)


def test_area_prepares_visibility_once_and_cached_results_still_authorized(
    tmp_path, context, monkeypatch
):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        _, effects = install(sm, sid, p, context)
        seed(sm, sid, p, context)
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        count = counters(monkeypatch, sm)
        area = EnvironmentArea(tuple(f"{x},0" for x in range(5)))

        def query(ctx=context):
            return sm.environment.query_area(sid, pc, A, area, p, context=ctx)

        baseline = query()
        assert count["visibility"] == count["light"] == 1 and count["target"] == 5
        assert query() == baseline
        assert count["visibility"] == 2 and count["light"] == 1 and count["compose"] == 5
        with pytest.raises(StateMachineError):
            query(replace(context, player_id="other"))
        effects.current = (
            Source(
                "hidden",
                Conditions(Temperature(-99)),
                EnvironmentArea(("0,0",), HeightInterval(2, 4)),
            ),
        )
        public = query()
        assert all(r.temperature.status == "unknown" for r in public)
        assert "hidden" not in repr(public)
        effects.current = (
            replace(effects.current[0], area=EnvironmentArea(("8,8",), HeightInterval(8, 9))),
        )
        assert query() == public
        # Re-prepare the existing Light path on a cache hit.
        with sm._transaction(write=True):
            raw = sm._world.facts.spatial_scene(sid, A)
            raw["grid"]["lighting"]["4,0"] = "dark"
            sm._conn.execute(
                "UPDATE mechanical_scenes SET state_json=? WHERE session_id=? AND scene_id=?",
                (spatial_module.canonical_json(raw), sid, A),
            )
        with pytest.raises(StateMachineError):
            query()
        # Actor visibility changes must reject the entire area even when its cells are warm.
        with sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE actors SET cell='99,99' WHERE session_id=? AND actor_id=?", (sid, pc)
            )
        with pytest.raises(StateMachineError):
            query()


def test_bounded_lru_eviction_clear_restart_and_external_optout(tmp_path, context, monkeypatch):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm:
        wo, _ = install(sm, sid, p, context)
        seed(sm, sid, p, context)
        count = counters(monkeypatch, sm)
        env = sm.environment
        # Small budgets exercise the same production eviction path deterministically.
        env._cells.capacity = env._projections.capacity = 2
        baseline = project(sm, sid, p, "3,3", height=0)
        for height in (1, 2, 3):
            project(sm, sid, p, "3,3", height=height)
        assert len(env._cells) == 2 and count["scene"] == 1
        assert project(sm, sid, p, "3,3", height=0) == baseline and count["compose"] == 5
        for index, kind in enumerate(("rain", "snow", "sleet", "hail")):
            wo.current = replace(
                wo.current, precipitation=PrecipitationProtection(((0, 0),), (kind,))
            )
            project(sm, sid, p, "3,3", height=index)
        assert len(env._cells) <= 2 and len(env._projections) <= 2
        current = project(sm, sid, p, "3,3")
        env.clear_snapshots()
        assert len(env._cells) == len(env._projections) == 0
        assert project(sm, sid, p, "3,3") == current
        wo.environment_cacheable = False
        before = count["compose"], count["scene"]
        project(sm, sid, p, "3,3")
        project(sm, sid, p, "3,3")
        assert (count["compose"], count["scene"]) == (before[0] + 2, before[1] + 2)
    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        wo.sm = restored
        restored._objects.spatial.protection_provider = wo
        restored.environment.effects_provider = Effects()
        assert project(restored, sid, p, "3,3") == current


def test_old_environment_command_fingerprints_omit_new_absent_field(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        op = SetEnvironmentProtection(A, Protection(("2,1",), ()))
        cmd = command(sm, sid, p, gm(context), op, "legacy")
        # Frozen original JSON shape (before the optional precipitation extension).
        import hashlib

        from trpg_state_machine.package import canonical_json

        raw = asdict(cmd)
        raw.pop("check_world_effects")
        raw.pop("time_wait")  # The frozen legacy shape predates optional waits.
        raw["payload"]["protection"].pop("precipitation")
        expected = hashlib.sha256(
            canonical_json(
                {
                    "command": raw,
                    "principal": context.principal_id,
                    "player": context.player_id,
                    "package": p.package_digest,
                    "binding": asdict(p.ruleset_binding),
                }
            ).encode()
        ).hexdigest()
        assert command_fingerprint(cmd, p, gm(context)) == expected


@pytest.mark.parametrize(
    "orientation,cell",
    [
        ("north", "4,3"),
        ("east", "3,4"),
        ("south", "2,3"),
        ("west", "3,2"),
    ],
)
def test_new_precipitation_projection_uses_existing_rotation(tmp_path, context, orientation, cell):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        oss = sm._objects.spatial
        candidate = replace(oss.state(sid, OBSTACLE, p), orientation=orientation)
        props = CurrentProtectionProperties(
            precipitation=PrecipitationProtection(((1, 0),), PRECIPITATION_KINDS)
        )
        result = oss.project_protection(
            sid, OBSTACLE, p, candidate=candidate, properties=props, wind_from="north"
        )
        assert {(c.cell, c.kind) for c in result.precipitation_cells} == {
            (cell, kind) for kind in PRECIPITATION_KINDS
        }
        assert {c.cell for c in result.rain_cells} == {cell}


@pytest.mark.parametrize("kinds", [(), ("rain", "rain"), ("mist",), ["snow"], (True,)])
def test_new_precipitation_rejects_unknown_duplicate_or_mutable_kinds(kinds):
    with pytest.raises(ValueError):
        PrecipitationProtection(((0, 0),), kinds)
    with pytest.raises(StateMachineError):
        StaticPrecipitationProtection(("0,0",), kinds)


def test_scene_preparation_keeps_explicit_property_validation_for_unsupported_objects(
    tmp_path, context
):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm, sm._transaction(), pytest.raises(ValueError):
        sm._objects.spatial.scene_protection_contributions(
            sid, A, p, wind_from="north", properties={DOOR: {}}
        )
