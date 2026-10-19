"""Real Environment service/SQLite/command tests; explicit external seam fixtures."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from lock_support import A, B, lock_loader
from spatial_support import create
from test_migrations import assert_original_rows_equal, stored_rows
from test_object_protection import large_data
from test_world_spatial import OBSTACLE
from world_support import command, setup, submit

from trpg_state_machine import (
    AddEnvironmentSource,
    BatchEnvironment,
    EnterScene,
    EnvironmentArea,
    HeightInterval,
    InitializeEnvironment,
    Obscurant,
    Precipitation,
    RemoveEnvironmentSource,
    ReplaceEnvironmentSource,
    RepositionObject,
    SetEnvironmentBase,
    SetEnvironmentProtection,
    StateMachine,
    StaticWindProtection,
    Temperature,
    TimeBinding,
    Wind,
    world_command_from_data,
)
from trpg_state_machine import (
    EnvironmentConditions as Conditions,
)
from trpg_state_machine import (
    EnvironmentSceneFacts as Facts,
)
from trpg_state_machine import (
    EnvironmentSource as Source,
)
from trpg_state_machine import (
    SceneEnvironmentProtection as Protection,
)
from trpg_state_machine.environment_commands import environment_operation_from_data
from trpg_state_machine.errors import StateMachineError
from trpg_state_machine.object_protection import (
    CurrentProtectionProperties,
    RainProtection,
    WindBoundary,
    WindProtection,
)
from trpg_state_machine.package import _validate

RAIN = Precipitation("rain", "heavy")
WEATHER = Conditions(Temperature(8), Wind(20, "north"), RAIN, Obscurant("fog", "thin"))


def gm(context):
    return replace(context, can_manage_environment=True)


def project(sm, sid, package, cell="2,1", *, scene=A, height=None):
    with sm._transaction():
        return sm.environment.project_cell(sid, scene, cell, package, height_ft=height)


def value(field):
    return field.contributions[0].value


def seed(sm, sid, p, ctx, facts=None, scene=A, key="init"):
    version = sm.read_session(sid, p, context=ctx).world_version
    return sm.environment.initialize(
        sid,
        (InitializeEnvironment(scene, facts or Facts(WEATHER)),),
        p,
        command_id=key,
        expected_world_version=version,
        context=gm(ctx),
    )


def test_controlled_initialization_public_queries_and_no_writes(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm:
        assert sm.environment is sm._world.environment
        before, count = stored_rows(db), sm._conn.total_changes
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        sm._conn.execute("PRAGMA query_only=ON")
        try:
            result = sm.environment.query_cell(sid, pc, A, "2,1", p, context=context)
            assert result.temperature.status == result.wind.status == "unconfigured"
            assert sm.environment.read_facts(sid, B, p, context=gm(context)) is None
            assert sm.environment.query_unit(sid, pc, p, context=context).status == "missing_input"
        finally:
            sm._conn.execute("PRAGMA query_only=OFF")
        assert stored_rows(db) == before and sm._conn.total_changes == count
        receipt = seed(sm, sid, p, context)
        assert receipt.status == "committed" and len(receipt.event_seqs) == 1
        event = sm.read_events(sid, p, context=context)[-1]
        assert event.event_type == "environment.changed"
        assert event.payload["scene_ids"] == [A]
        assert event.signals == ()  # Change coordination is not sensory evidence.
        before, count = stored_rows(db), sm._conn.total_changes
        sm._conn.execute("PRAGMA query_only=ON")
        try:
            result = sm.environment.query_cell(sid, pc, A, "2,1", p, context=context, height_ft=3)
            assert value(result.temperature) == Temperature(8)
            assert value(result.precipitation) == RAIN
            assert result.ground_precipitation.status == "unknown"  # shelter unconfigured
            assert result.effects_status == "unconfigured"
            assert all(c.source_ref == "" for c in result.temperature.contributions)
        finally:
            sm._conn.execute("PRAGMA query_only=OFF")
        assert stored_rows(db) == before and sm._conn.total_changes == count


def test_base_local_overlap_ties_unknown_none_and_remove(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    ctx = gm(context)
    with sm:
        seed(sm, sid, p, ctx)
        hot = Source(
            "hot",
            Conditions(temperature=Temperature(30)),
            EnvironmentArea(("2,1",)),
            priority=1,
            observable=True,
        )
        cold = replace(hot, source_id="cold", conditions=Conditions(temperature=Temperature(-10)))
        for op, key in (
            (AddEnvironmentSource(A, hot), "hot"),
            (AddEnvironmentSource(A, cold), "cold"),
        ):
            assert submit(sm, sid, p, ctx, op, key).status == "committed"
        here = project(sm, sid, p)
        assert here.temperature.status == "unsupported"
        assert {c.source_ref for c in here.temperature.contributions} == {"fact:hot", "fact:cold"}
        assert value(project(sm, sid, p, "2,2").temperature) == Temperature(8)
        cold = replace(cold, priority=2)
        assert (
            submit(sm, sid, p, ctx, ReplaceEnvironmentSource(A, cold), "prior").status
            == "committed"
        )
        assert value(project(sm, sid, p).temperature) == Temperature(-10)
        assert (
            submit(sm, sid, p, ctx, RemoveEnvironmentSource(A, "cold"), "remove").status
            == "committed"
        )
        assert value(project(sm, sid, p).temperature) == Temperature(30)
        assert (
            submit(sm, sid, p, ctx, RemoveEnvironmentSource(A, "hot"), "remove2").status
            == "committed"
        )
        assert value(project(sm, sid, p).temperature) == Temperature(8)
        unknown = Conditions(Temperature(), Wind(), Precipitation(), Obscurant())
        assert (
            submit(sm, sid, p, ctx, SetEnvironmentBase(A, unknown), "unknown").status == "committed"
        )
        result = project(sm, sid, p)
        assert all(
            getattr(result, k).status == "unknown"
            for k in ("temperature", "wind", "precipitation", "obscurant")
        )
        none = Conditions(
            wind=Wind(0),
            precipitation=Precipitation("none", "none"),
            obscurant=Obscurant("none", "none"),
        )
        submit(sm, sid, p, ctx, SetEnvironmentBase(A, none), "none")
        result = project(sm, sid, p)
        assert result.temperature.status == "unconfigured"
        assert (
            result.wind.status == result.precipitation.status == result.obscurant.status == "none"
        )


def test_noops_conflicts_permissions_fingerprints_and_closed_wire(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    ctx = gm(context)
    with sm:
        init = InitializeEnvironment(A, Facts(WEATHER))
        cmd = command(sm, sid, p, ctx, init, "init")
        data = asdict(cmd)
        data.pop("check_world_effects")
        assert world_command_from_data(data) == cmd
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as denied:
            sm.submit_command(cmd, p, context=context)
        assert denied.value.code == "unauthorized" and stored_rows(db) == before
        receipt = sm.submit_command(cmd, p, context=ctx)
        assert sm.submit_command(cmd, p, context=ctx) == receipt
        with pytest.raises(StateMachineError) as conflict:
            sm.submit_command(
                replace(cmd, payload=InitializeEnvironment(B, Facts(WEATHER))), p, context=ctx
            )
        assert conflict.value.code == "idempotency_conflict"
        changed = replace(cmd, command_id="stale", payload=SetEnvironmentBase(A, Conditions()))
        # Type must match registered command as well.
        changed = replace(changed, command_type="world.set_environment_base")
        assert sm.submit_command(changed, p, context=ctx).status == "conflict"
        start = sm.read_session(sid, p, context=ctx)
        for i, op in enumerate(
            (init, SetEnvironmentBase(A, WEATHER), RemoveEnvironmentSource(A, "absent"))
        ):
            result = submit(sm, sid, p, ctx, op, f"noop{i}")
            assert result.status == "committed" and result.event_seqs == ()
            assert result.world_version == start.world_version
        source = Source("one", Conditions(wind=Wind(1, "east")))
        assert submit(sm, sid, p, ctx, AddEnvironmentSource(A, source), "add").status == "committed"
        assert submit(sm, sid, p, ctx, AddEnvironmentSource(A, source), "add_same").event_seqs == ()
        different = replace(source, conditions=Conditions(wind=Wind(2, "west")))
        assert (
            submit(sm, sid, p, ctx, AddEnvironmentSource(A, different), "dup").error_code
            == "environment_source_exists"
        )
        assert (
            submit(sm, sid, p, ctx, ReplaceEnvironmentSource(B, source), "absent").error_code
            == "environment_source_not_found"
        )
        wire = {
            "changes": [
                {
                    "command_type": "world.set_environment_base",
                    "payload": {"scene_id": A, "conditions": asdict(WEATHER)},
                }
            ]
        }
        parsed = environment_operation_from_data("world.batch_environment", wire)
        assert parsed == BatchEnvironment((SetEnvironmentBase(A, WEATHER),))
        for raw in (
            {"changes": [{"scene_id": A}]},
            {"changes": []},
            {"changes": [], "can_manage_environment": True},
        ):
            with pytest.raises(StateMachineError):
                environment_operation_from_data("world.batch_environment", raw)


def test_batch_prevalidation_rollback_and_net_noop(tmp_path, context, monkeypatch):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    ctx = gm(context)
    with sm:
        seed(sm, sid, p, ctx)
        before = sm.environment.read_facts(sid, A, p, context=ctx)
        batch = BatchEnvironment(
            (
                SetEnvironmentBase(A, Conditions(Temperature(20))),
                AddEnvironmentSource(B, Source("bad", WEATHER, EnvironmentArea(("99,99",)))),
            )
        )
        calls = []
        write = sm._world_facts.set_environment
        monkeypatch.setattr(sm._world_facts, "set_environment", lambda *a: calls.append(a))
        receipt = submit(sm, sid, p, ctx, batch, "bad.batch")
        assert receipt.status == "rejected" and receipt.error_code == "invalid_cell"
        assert calls == []  # full validation before even the first write
        assert sm.environment.read_facts(sid, A, p, context=ctx) == before
        assert sm.environment.read_facts(sid, B, p, context=ctx) is None
        monkeypatch.setattr(sm._world_facts, "set_environment", write)
        good = BatchEnvironment(
            (
                SetEnvironmentBase(A, Conditions(Temperature(20))),
                SetEnvironmentBase(B, Conditions(Temperature(20))),
            )
        )
        snapshot = stored_rows(db)

        def fail(*args, **kwargs):
            raise StateMachineError("storage_error", "Injected event persistence failure")

        monkeypatch.setattr(sm, "_append_events", fail)
        with pytest.raises(StateMachineError):
            submit(sm, sid, p, ctx, good, "storage.failure")
        assert stored_rows(db) == snapshot  # facts, version, receipt, evidence, outbox, RNG
        monkeypatch.undo()
        success = submit(sm, sid, p, ctx, good, "good.batch")
        assert success.status == "committed" and len(success.event_seqs) == 1
        event = sm.read_events(sid, p, context=ctx)[-1]
        assert event.payload["scene_ids"] == sorted((A, B))
        noop = BatchEnvironment(
            (
                SetEnvironmentBase(A, Conditions(Temperature(7))),
                SetEnvironmentBase(A, Conditions(Temperature(20))),
            )
        )
        assert submit(sm, sid, p, ctx, noop, "net.noop").event_seqs == ()


def test_unvisited_scene_modification_session_isolation_revisit_and_restart(
    tmp_path, context, package
):
    p, db = package, tmp_path / "world.db"
    sm = StateMachine(db)
    sid = create(sm, p, context)
    ctx = gm(context)
    with sm:
        other = create(sm, p, context, "other")
        assert not sm._conn.execute(
            "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, B)
        ).fetchone()
        op = SetEnvironmentBase(B, Conditions(Temperature(-2), precipitation=RAIN))
        cmd = command(sm, sid, p, ctx, op, "before.visit")
        receipt = sm.submit_command(cmd, p, context=ctx)
        assert receipt.status == "committed"
        assert seed(sm, sid, p, ctx, Facts(WEATHER), scene=B, key="late.init").event_seqs == ()
        assert sm.environment.read_facts(sid, B, p, context=ctx).base.temperature == Temperature(-2)
        assert sm.environment.read_facts(other, B, p, context=ctx) is None
        assert B not in sm.read_session(sid, p, context=context).scenes
        environment = sm.environment.read_facts(sid, B, p, context=ctx)
        assert submit(sm, sid, p, context, EnterScene(B), "visit").status == "committed"
        assert sm.read_session(sid, p, context=context).scenes[B].visited_count == 1
        with sm._transaction(write=True):
            assert not sm.scene_lifecycle.ensure_scene(sid, B, p)
        assert sm.environment.read_facts(sid, B, p, context=ctx) == environment
        assert submit(sm, sid, p, context, EnterScene(A), "back").status == "committed"
        assert sm.read_session(sid, p, context=context).scenes[B].visited_count == 1
        before = stored_rows(db)
    # Genuine fresh interpreter, same approved Package and real resume/replay path.
    root = Path(__file__).resolve().parents[1]
    code = """
import sys,json
from dataclasses import asdict,replace
from test_object_protection import large_data
from lock_support import lock_loader
from trpg_state_machine.package import _validate
from trpg_state_machine import StateMachine,TrustedContext,world_command_from_data
from trpg_state_machine import load_adventure_package
p=load_adventure_package(sys.argv[5]).package; c=TrustedContext(**json.loads(sys.argv[3]))
with StateMachine(sys.argv[1]) as sm:
    state=sm.resume_session(sys.argv[2],p,context=c)
    cmd=world_command_from_data(json.loads(sys.argv[4]))
    r=sm.submit_command(cmd,p,context=c)
    f=sm.environment.read_facts(sys.argv[2],'scene.workshop',p,context=c)
    print(json.dumps([f.base.temperature.celsius,r.status,r.world_version,
                      state.scenes['scene.workshop'].visited_count,
                      state.scenes['scene.platform'].visited_count]))
"""
    data = asdict(cmd)
    data.pop("check_world_effects")
    context_data = asdict(replace(ctx, controlled_npc_ids=()))
    # Reconstitute immutable tuple on the wire.
    code = code.replace(
        "c=TrustedContext(**json.loads(sys.argv[3]))",
        "d=json.loads(sys.argv[3]); d['controlled_npc_ids']=(); c=TrustedContext(**d)",
    )
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(db),
            sid,
            json.dumps(context_data),
            json.dumps(data),
            str(root / "tests/fixtures/lantern_station.yaml"),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests")))},
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(process.stdout) == [-2, "committed", receipt.world_version, 1, 2]
    assert stored_rows(db) == before


def test_multiple_protection_owners_preserve_gaps_and_current_object_pose(tmp_path, context):
    data = large_data()
    data["time_binding"] = asdict(TimeBinding())
    for actor in data["actors"]:
        actor.pop("npc_state", None)
    p = _validate(data)

    class WOFixture:
        def current_protection(self, sid, oid, package, *, candidate):
            if oid == OBSTACLE:
                return CurrentProtectionProperties(
                    RainProtection(((0, 0),)),
                    WindProtection((WindBoundary((0, 0), "north", 2, 4, 6),)),
                    0,
                )
            return CurrentProtectionProperties(RainProtection(()), WindProtection(()), 0)

    class EffectsFixture:
        def environment_sources(self, sid, scene_id, package):
            return ()

    db = tmp_path / "injected.db"
    with StateMachine(
        db,
        dnd_loader=lock_loader(),
        object_protection_provider=WOFixture(),
        environment_effects_provider=EffectsFixture(),
    ) as sm:
        sid = create(sm, p, context)
        protection = Protection((), (StaticWindProtection("3,4", "north", HeightInterval(0, 2)),))
        seed(sm, sid, p, context, Facts(WEATHER, protection=protection))
        with sm._transaction():
            segments = sm.environment.project_height_range(sid, A, "3,4", HeightInterval(0, 6), p)
        assert [(s.heights.lower_ft, s.heights.upper_ft) for s in segments] == [
            (0, 2),
            (2, 4),
            (4, 6),
        ]
        assert {b.source_ref for b in segments[0].environment.protection.wind_bands} == {
            "static",
            "object:" + OBSTACLE,
        }
        assert segments[0].environment.wind_exposure.status == "none"
        assert segments[1].environment.wind_exposure.status != "none"
        assert segments[2].environment.wind_exposure.status == "none"
        ctx = replace(gm(context), can_place_world_objects=True)
        receipt = submit(sm, sid, p, ctx, RepositionObject(A, OBSTACLE, "3,3", "4,3"), "pose")
        assert receipt.status == "committed"
        assert project(sm, sid, p, "3,4", height=5).wind_exposure.status != "none"
        assert project(sm, sid, p, "4,4", height=5).wind_exposure.status == "none"
        assert sm.world_time.read(sid, p, context=context).game_tick == 0
        assert sm.world_time.read(sid, p, context=context).binding == TimeBinding()
        before = stored_rows(db)
    with StateMachine(
        db,
        dnd_loader=lock_loader(),
        object_protection_provider=WOFixture(),
        environment_effects_provider=EffectsFixture(),
    ) as sm:
        sm.resume_session(sid, p, context=context)
        assert project(sm, sid, p, "4,4", height=5).wind_exposure.status == "none"
        assert stored_rows(db) == before


@pytest.mark.parametrize(
    "field,left,right",
    [
        ("wind", Wind(20, "north"), Wind(20, "east")),
        ("precipitation", RAIN, Precipitation("snow", "heavy")),
    ],
)
def test_conflicting_scalar_sources_are_unsupported_not_blended(
    tmp_path, context, field, left, right
):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        seed(sm, sid, p, context)
        sources = (
            Source("left", Conditions(**{field: left}), observable=True),
            Source("right", Conditions(**{field: right}), observable=True),
        )
        batch = BatchEnvironment(tuple(AddEnvironmentSource(A, s) for s in sources))
        assert submit(sm, sid, p, gm(context), batch, "conflict.values").status == "committed"
        result = getattr(project(sm, sid, p), field)
        assert result.status == "unsupported" and len(result.contributions) == 2
        submit(sm, sid, p, gm(context), RemoveEnvironmentSource(A, "right"), "stop.right")
        assert value(getattr(project(sm, sid, p), field)) == left


def test_sqlite_evidence_fault_rolls_back_entire_environment_command(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm:
        seed(sm, sid, p, context)
        before = stored_rows(db)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_environment_evidence BEFORE INSERT ON event_evidence "
            "BEGIN SELECT RAISE(ABORT,'environment test fault'); END"
        )
        with pytest.raises(StateMachineError) as failed:
            submit(
                sm,
                sid,
                p,
                gm(context),
                BatchEnvironment(
                    (
                        SetEnvironmentBase(A, Conditions(Temperature(3))),
                        SetEnvironmentBase(B, Conditions(Temperature(3))),
                    )
                ),
                "sql.fault",
            )
        assert failed.value.code == "storage_error"
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fail_environment_evidence")


def test_source_movement_change_payload_and_public_region_differences(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        seed(sm, sid, p, context)
        source = Source(
            "temperature.local",
            Conditions(Temperature(40)),
            EnvironmentArea(("1,0",)),
            observable=True,
        )
        submit(sm, sid, p, gm(context), AddEnvironmentSource(A, source), "source.add")
        moved = replace(source, area=EnvironmentArea(("2,0",)))
        submit(sm, sid, p, gm(context), ReplaceEnvironmentSource(A, moved), "source.move")
        event = sm.read_events(sid, p, context=context)[-1]
        change = event.payload["changes"][0]
        assert change["source_id"] == source.source_id
        assert change["fields"] == ["area"]
        assert [a["cells"] for a in change["areas"]] == [["1,0"], ["2,0"]]
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        result = sm.environment.query_area(
            sid, pc, A, EnvironmentArea(("1,0", "2,0")), p, context=context
        )
        assert [value(c.temperature).celsius for c in result] == [8, 40]
        assert "temperature.local" not in repr(result)
        assert (
            len(sm.environment.query_area(sid, pc, A, EnvironmentArea(), p, context=context)) == 81
        )


def test_visibility_and_query_budgets_reject_without_writes(tmp_path, context):
    from test_object_spatial_service import changed_grid

    data = large_data()
    changed_grid(data, width=17, height=17, default_lighting="dark")
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        before = stored_rows(db)
        with pytest.raises(StateMachineError):
            sm.environment.query_cell(sid, pc, A, "2,1", p, context=context)
        with pytest.raises(StateMachineError) as bounded:
            sm.environment.query_area(sid, pc, A, EnvironmentArea(), p, context=context)
        assert bounded.value.code == "invalid_environment"
        assert stored_rows(db) == before


def test_hidden_vertical_source_cannot_disclose_height_boundaries(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm:
        hidden = Source(
            "script.secret",
            Conditions(Temperature(-20)),
            EnvironmentArea(("0,0",), HeightInterval(2, 4)),
        )
        seed(sm, sid, p, context, Facts(WEATHER, (hidden,)))
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        before = stored_rows(db)
        hidden_range = sm.environment.query_height_range(
            sid, pc, A, "0,0", HeightInterval(0, 6), p, context=context
        )
        assert tuple(segment.heights for segment in hidden_range) == (HeightInterval(0, 6),)
        assert all(segment.environment.temperature.status == "unknown" for segment in hidden_range)
        public = sm.environment.query_cell(sid, pc, A, "0,0", p, context=context, height_ft=3)
        assert public.temperature.status == "unknown" and public.temperature.contributions == ()
        assert "script.secret" not in repr(public)
        assert stored_rows(db) == before


@pytest.mark.parametrize(
    "direction,target", [("north", "3,4"), ("east", "2,3"), ("south", "3,2"), ("west", "4,3")]
)
def test_real_oss_wind_direction_static_rain_and_height_gaps(tmp_path, context, direction, target):
    sm, sid, p, db = setup(tmp_path, context, large_data())

    class WOFixture:
        # Test contract fixture, no production WO protection adapter claimed.
        def current_protection(self, sid, oid, package, *, candidate):
            if oid != OBSTACLE:
                return CurrentProtectionProperties(RainProtection(()), WindProtection(()), 0)
            return CurrentProtectionProperties(
                RainProtection(((0, 0),)),
                WindProtection(
                    (
                        WindBoundary((0, 0), direction, 2, 0, 2),
                        WindBoundary((0, 0), direction, 2, 4, 6),
                    )
                ),
                0,
            )

    with sm:
        sm._objects.spatial.protection_provider = WOFixture()
        weather = replace(WEATHER, wind=Wind(20, direction))
        protection = Protection(("2,1",), ())
        seed(sm, sid, p, context, Facts(weather, protection=protection))
        before, count = stored_rows(db), sm._conn.total_changes
        sm._conn.execute("PRAGMA query_only=ON")
        try:
            assert project(sm, sid, p).ground_precipitation.status == "none"  # static roof
            assert project(sm, sid, p, "3,3").ground_precipitation.status == "none"  # real OSS roof
            assert project(sm, sid, p, target, height=1).wind_exposure.status == "none"
            assert project(sm, sid, p, target, height=3).wind_exposure.status != "none"
            assert project(sm, sid, p, target, height=4).wind_exposure.status == "none"
            assert project(sm, sid, p, target, height=6).wind_exposure.status != "none"
            with sm._transaction():
                segments = sm.environment.project_height_range(
                    sid, A, target, HeightInterval(0, 6), p
                )
            assert [(s.heights.lower_ft, s.heights.upper_ft) for s in segments] == [
                (0, 2),
                (2, 4),
                (4, 6),
            ]
            assert [s.environment.wind_exposure.status for s in segments][::2] == ["none", "none"]
            assert segments[1].environment.wind_exposure.status != "none"
        finally:
            sm._conn.execute("PRAGMA query_only=OFF")
        assert stored_rows(db) == before and sm._conn.total_changes == count


def test_static_directional_protection_and_local_rain_under_roof(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        protection = Protection(
            ("2,1",),
            (
                StaticWindProtection("2,1", "north", HeightInterval(10, 12)),
                StaticWindProtection("2,1", "north", HeightInterval(14, 16)),
            ),
        )
        seed(sm, sid, p, context, Facts(WEATHER, protection=protection))
        assert project(sm, sid, p, height=11).wind_exposure.status == "none"
        assert project(sm, sid, p, height=13).wind_exposure.status != "none"
        assert project(sm, sid, p, height=14).wind_exposure.status == "none"
        source = Source(
            "indoor.rain",
            Conditions(precipitation=RAIN),
            EnvironmentArea(("2,1",)),
            observable=True,
        )
        submit(sm, sid, p, gm(context), AddEnvironmentSource(A, source), "indoor")
        assert project(sm, sid, p).ground_precipitation.status == "ready"
        submit(sm, sid, p, gm(context), RemoveEnvironmentSource(A, source.source_id), "stop.indoor")
        assert project(sm, sid, p).ground_precipitation.status == "none"
        submit(
            sm,
            sid,
            p,
            gm(context),
            SetEnvironmentBase(A, replace(WEATHER, wind=Wind(20, "east"))),
            "east",
        )
        assert project(sm, sid, p, height=11).wind_exposure.status != "none"
        submit(sm, sid, p, gm(context), SetEnvironmentProtection(A, Protection()), "unknown.roof")
        assert project(sm, sid, p).ground_precipitation.status == "unknown"


def test_effect_fixture_moves_ends_and_preserves_natural_fog_without_persistence(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())

    class EffectsFixture:
        current = (
            Source(
                "spell.secret",
                Conditions(obscurant=Obscurant("fog", "dense")),
                EnvironmentArea(("2,1",)),
                observable=False,
            ),
        )

        def environment_sources(self, sid, scene_id, package):
            return self.current if scene_id == A else ()

    provider = EffectsFixture()
    with sm:
        seed(sm, sid, p, context)
        sm.environment.effects_provider = provider
        before, count = stored_rows(db), sm._conn.total_changes
        result = project(sm, sid, p)
        assert {c.value.density for c in result.obscurant.contributions} == {"thin", "dense"}
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        public = sm.environment.query_cell(sid, pc, A, "2,1", p, context=context)
        assert public.obscurant.status == "unknown" and public.obscurant.contributions == ()
        assert "spell.secret" not in repr(public)
        provider.current = (
            replace(provider.current[0], area=EnvironmentArea(("2,2",)), observable=True),
        )
        assert len(project(sm, sid, p).obscurant.contributions) == 1
        assert len(project(sm, sid, p, "2,2").obscurant.contributions) == 2
        public = sm.environment.query_area(
            sid, pc, A, EnvironmentArea(("2,1", "2,2")), p, context=context
        )
        assert [len(c.obscurant.contributions) for c in public] == [1, 2]
        provider.current = ()
        assert value(project(sm, sid, p, "2,2").obscurant) == Obscurant("fog", "thin")
        assert stored_rows(db) == before and sm._conn.total_changes == count


def test_public_authorization_scope_visibility_height_and_package_pin(tmp_path, context):
    data = large_data()
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        seed(sm, sid, p, context)
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        before = stored_rows(db)
        for actor, scene, ctx in (
            (pc, B, context),
            ("npc.unknown", A, context),
            (pc, A, replace(context, player_id="other")),
        ):
            with pytest.raises(StateMachineError):
                sm.environment.query_cell(sid, actor, scene, "2,1", p, context=ctx)
        with pytest.raises(StateMachineError):
            sm.environment.read_facts(sid, A, p, context=context)
        changed = large_data()
        changed["manifest"]["title"] += " changed"
        with pytest.raises(StateMachineError) as pin:
            sm.environment.query_cell(sid, pc, A, "2,1", _validate(changed), context=context)
        assert pin.value.code == "package_digest_mismatch"
        with pytest.raises(StateMachineError):
            sm.environment.query_area(sid, pc, A, EnvironmentArea(("99,99",)), p, context=context)
        assert stored_rows(db) == before


def test_height_scoped_sources_and_unit_height_readonly_seam(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, large_data())

    class HeightFixture:
        def environment_heights(self, sid, actor_id, package, *, position):
            assert position.cell == "0,0"
            return HeightInterval(10, 16)

    with sm:
        high = Source(
            "high",
            Conditions(temperature=Temperature(-10)),
            EnvironmentArea(("0,0",), HeightInterval(12, 14)),
            observable=True,
        )
        seed(sm, sid, p, context, Facts(WEATHER, (high,)))
        assert project(sm, sid, p, "0,0").temperature.status == "unknown"
        assert value(project(sm, sid, p, "0,0", height=11).temperature) == Temperature(8)
        assert value(project(sm, sid, p, "0,0", height=12).temperature) == Temperature(-10)
        assert value(project(sm, sid, p, "0,0", height=14).temperature) == Temperature(8)
        sm.environment.unit_height_provider = HeightFixture()
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        unit = sm.environment.query_unit(sid, pc, p, context=context)
        assert unit.status == "ready"
        assert [value(s.environment.temperature).celsius for s in unit.segments] == [8, -10, 8]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Temperature(float("nan")),
        lambda: Temperature(True),
        lambda: Temperature(-274),
        lambda: Wind(-1, "north"),
        lambda: Wind(2, "northeast"),
        lambda: Wind(0, "north"),
        lambda: Precipitation("rain", "none"),
        lambda: Obscurant("fog", "none"),
        lambda: EnvironmentArea(["1,1"]),
        lambda: EnvironmentArea(("01,1",)),
        lambda: EnvironmentArea(("1,1", "1,1")),
        lambda: EnvironmentArea(tuple(f"{x},0" for x in range(257))),
        lambda: HeightInterval(2, 2),
        lambda: HeightInterval(0, float("inf")),
        lambda: Source("base", WEATHER),
        lambda: Source("x", WEATHER, priority=True),
        lambda: Source("x", WEATHER, observable=1),
        lambda: Conditions(wind={"speed_mph": 1}),
        lambda: Facts(sources=tuple(Source(str(i), WEATHER) for i in range(65))),
    ],
)
def test_strong_types_limits_and_unknown_input(factory):
    with pytest.raises(StateMachineError):
        factory()


def test_real_schema33_migration_preserves_all_rows_bindings_and_atomicity(
    tmp_path, context, monkeypatch
):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "9ce60cb", "src"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        z.extractall(source)
    code = """
from pathlib import Path
from world_support import setup,submit
from test_object_protection import large_data
from dataclasses import replace
from trpg_state_machine import TrustedContext,AdvanceGameTime
import sys,json
c=TrustedContext('authenticated:alice','player.alice')
sm,sid,p,db=setup(Path(sys.argv[1]),c,large_data())
r=submit(sm,sid,p,replace(c,can_advance_time=True),AdvanceGameTime(0,3),'old.tick')
print(json.dumps([sid,sm._conn.execute('PRAGMA user_version').fetchone()[0]]));sm.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(source / "src"), str(root / "tests"))),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    sid, version = json.loads(result.stdout)
    assert version == 33
    db = tmp_path / "world.db"
    before = stored_rows(db)
    read = Path.read_text

    def broken(path, *args, **kwargs):
        content = read(path, *args, **kwargs)
        return (
            content.replace("PRAGMA user_version = 35;", "INVALID MIGRATION;")
            if path.name == "_migration_35.sql"
            else content
        )

    monkeypatch.setattr(Path, "read_text", broken)
    with pytest.raises(StateMachineError):
        StateMachine(db, dnd_loader=lock_loader())
    assert stored_rows(db) == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 33
    monkeypatch.undo()
    p = _validate(large_data())
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        sm.resume_session(sid, p, context=context)
        assert_original_rows_equal(before, stored_rows(db))
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 36
        assert sm._conn.execute("SELECT * FROM scene_environments").fetchall() == []
        assert sm.environment.read_facts(sid, A, p, context=gm(context)) is None
        assert sm.world_time.read(sid, p, context=context).binding.schema_version == "game-clock/1"
        assert sm.world_time.read(sid, p, context=context).game_tick == 3
