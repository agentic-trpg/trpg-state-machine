"""Scene daylight configuration and fixed reads of the committed and candidate clock."""

from dataclasses import asdict, replace

import pytest
from lock_support import A, B, lock_loader
from world_support import setup, submit, world_data

from trpg_state_machine import AdvanceGameTime, StateMachine, StateMachineError, TimeBinding
from trpg_state_machine.package import _validate
from trpg_state_machine.world_daylight import scene_ambient_source

DAYLIGHT_TIME = TimeBinding(day_length=10, start_day_tick=1, day_start=2, night_start=7)


def daylight_data(scope):
    data = world_data()
    data["scenes"][0]["daylight"] = scope
    data["time_binding"] = asdict(DAYLIGHT_TIME)
    for actor in data["actors"]:
        actor.pop("npc_state", None)
    return data


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (None, "schema_error"),
        ([], "schema_error"),
        (["0,0", "0,0"], "duplicate_id"),
        (["00,0"], "invalid_reference"),
        (["999,0"], "invalid_reference"),
        ([["0,0"]], "invalid_reference"),
    ],
)
def test_daylight_requires_an_explicit_valid_scope(value, code):
    with pytest.raises(StateMachineError) as exc:
        _validate(daylight_data(value))
    assert exc.value.code == code
    assert exc.value.issue.path == "scenes[0].daylight"


def test_static_sunlight_and_day_night_configuration_cannot_both_own_ambient_sun():
    data = daylight_data("scene")
    data["scenes"][0]["grid"]["sunlight"] = True
    with pytest.raises(StateMachineError) as exc:
        _validate(data)
    assert exc.value.code == "schema_error"
    assert exc.value.issue.path == "scenes[0].daylight"


@pytest.mark.parametrize("with_default", [False, True])
def test_public_creation_checks_effective_daylight_clock(
    tmp_path, context, with_default, request_for
):
    data = daylight_data("scene")
    if not with_default:
        del data["time_binding"]
    package = _validate(data)
    request = replace(
        request_for(),
        adventure_id=package.adventure_id,
        package_version=package.package_version,
        package_digest=package.package_digest,
        time_binding=TimeBinding(cycle="none", day_start=None, night_start=None)
        if with_default
        else None,
    )
    with StateMachine(tmp_path / "daylight.db", dnd_loader=lock_loader()) as sm:
        with pytest.raises(StateMachineError) as exc:
            sm.create_session(request, package, context=context)
        assert exc.value.code == "invalid_time_binding"
        result = sm.create_session(
            replace(request, time_binding=DAYLIGHT_TIME), package, context=context
        )
        assert sm.world_time.read(result.session_id, package, context=context).phase == "night"


@pytest.mark.parametrize("scope", ["scene", ["0,0", "0,1"]])
def test_daylight_source_uses_persisted_scope_and_fixed_caller_clock(
    tmp_path, context, monkeypatch, scope
):
    ctx = replace(context, can_advance_time=True)
    sm, sid, package, db = setup(tmp_path, ctx, daylight_data(scope))

    def read(sm, scene, package):
        world = sm._world
        with world.grid.read_scope():
            grid = world.grid.reader.current_grid(sid, scene, package)
            return scene_ambient_source(world, sid, scene, grid)

    with sm:
        assert sm._world.facts.instances.session_value(sid, "evidence_schema") == "event-evidence/6"
        original = read(sm, A, package)
        assert (original.phase, original.level, original.sunlight) == ("night", "dark", False)
        assert original.covers("0,0")
        assert original.covers("1,0") == (scope == "scene")
        assert original.cells == (None if scope == "scene" else frozenset(scope))
        assert not original.covers("999,0")

        def unexpected_phase(sid):
            pytest.fail("A fixed indoor scene must not read the time")

        with monkeypatch.context() as m:
            m.setattr(sm.world_time, "current_phase", unexpected_phase)
            assert read(sm, B, package) is None
            assert read(sm, B, package) is None

        with sm._store.transaction(write=True):
            with pytest.raises(RuntimeError, match="rollback"), sm._store.savepoint():
                sm._world.facts.set_tick(sid, 1)
                candidate = read(sm, A, package)
                assert candidate.phase == "day"
                raise RuntimeError("rollback")
            assert read(sm, A, package) == original

        dawn = submit(sm, sid, package, ctx, AdvanceGameTime(0, 1), "dawn")
        assert dawn.status == "committed"
        changed = replace(
            package, scenes=(replace(package.scenes[0], daylight=None), *package.scenes[1:])
        )
        day = read(sm, A, changed)
        assert (day.phase, day.level, day.sunlight) == ("day", "bright", True)
        assert day.cells is original.cells
        assert original.phase == "night"
        assert sm.save_session(sid, package, context=ctx).world.game_tick == 1

    with StateMachine(db, dnd_loader=lock_loader()) as reopened:
        assert reopened.resume_session(sid, package, context=ctx).world.game_tick == 1
        assert read(reopened, A, package) == day
        dusk = submit(reopened, sid, package, ctx, AdvanceGameTime(1, 6), "dusk")
        assert dusk.status == "committed"
        assert read(reopened, A, package) == original
