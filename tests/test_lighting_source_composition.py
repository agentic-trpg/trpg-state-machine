"""Public light results from held cones, ambient light and fixed projections."""

from dataclasses import asdict, replace

import pytest
from dnd5e_engine.spatial import GridTopology
from dnd_support import initialize
from lock_support import CRED, A
from test_lamp_profiles import GRID_CONE, configure_light
from test_object_spatial_service import changed_grid
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_daylight import DAYLIGHT_TIME
from test_world_lights import lighting_data
from test_world_magic_light import ITEM, cast
from test_world_magic_light import magic_world as magic_world
from world_support import setup, submit

from trpg_state_machine import (
    AdvanceGameTime,
    AimLight,
    DropWorldConcentration,
    EnvironmentArea,
    SetLightEnabled,
    StateMachineError,
)


def test_held_cone_turns_and_keeps_bright_and_dim_polygons(tmp_path, context, monkeypatch):
    data = lighting_data()
    configure_light(data, emission_shape=GRID_CONE, initial_enabled=True)
    changed_grid(data, width=8, height=8)
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        geometry_calls = []
        occluders = sm._world.grid.light_occluders

        def count_geometry(*args, **kwargs):
            geometry_calls.append(args[3])
            return occluders(*args, **kwargs)

        monkeypatch.setattr(sm._world.grid, "light_occluders", count_geometry)

        def read():
            return sm.get_illumination_area(
                sid, actor, ("1,0", "0,1"), package, context=context, include_geometry=True
            )

        assert tuple(row["illumination"] for row in read()) == ("dark", "dark")
        assert not geometry_calls
        east = submit(sm, sid, package, context, AimLight(A, CRED, "0,0", "north", "east"), "east")
        assert east.status == "committed"
        lit_east = read()
        assert tuple(row["illumination"] for row in lit_east) == ("bright", "dark")
        assert lit_east[0]["geometry"]["bright"]
        assert lit_east[0]["geometry"]["dim"]
        south = submit(
            sm, sid, package, context, AimLight(A, CRED, "0,0", "east", "south"), "south"
        )
        assert south.status == "committed"
        assert tuple(row["illumination"] for row in read()) == ("dark", "bright")
        assert lit_east[0]["illumination"] == "bright"


@pytest.mark.parametrize("scope", ["scene", ["0,0", "1,0"]])
def test_phase_lighting_combines_fixed_cells_and_held_lamp(tmp_path, context, scope):
    data = lighting_data()
    data["scenes"][0]["daylight"] = scope
    data["time_binding"] = asdict(DAYLIGHT_TIME)
    for npc in data["actors"]:
        npc.pop("npc_state", None)
    changed_grid(data, lighting={"0,0": "dim"})
    ctx = replace(context, can_advance_time=True)
    sm, sid, package, _ = setup(tmp_path, ctx, data)
    with sm:
        actor = sm.read_session(sid, package, context=ctx).pc_actor_id

        def read():
            rows = sm.get_illumination_area(sid, actor, ("0,0", "1,0"), package, context=ctx)
            return tuple((row["illumination"], row["sunlight"]) for row in rows)

        assert read() == (("dim", False), ("dark", False))
        dawn = submit(sm, sid, package, ctx, AdvanceGameTime(0, 1), "dawn")
        assert dawn.status == "committed"
        assert read() == (("dim", False), ("bright", True))
        day = sm.read_noncombat_snapshot(sid, package, context=ctx)
        topology = GridTopology(day.scene_state.grid)
        assert topology.sunlight_on_cell("1,0")
        assert not topology.sunlight_on_cell("0,0")
        dusk = submit(sm, sid, package, ctx, AdvanceGameTime(1, 6), "dusk")
        assert dusk.status == "committed"
        assert read() == (("dim", False), ("dark", False))
        light_on = submit(
            sm, sid, package, ctx, SetLightEnabled(A, CRED, "0,0", True), "after-dusk"
        )
        assert light_on.status == "committed"
        assert read() == (("bright", False), ("bright", False))
        assert day.scene_state.grid.lighting.get("1,0", day.scene_state.grid.default_lighting) == (
            "bright"
        )


def test_combat_admission_cannot_replace_night_with_static_light(timed_spell_world):
    def configure(data):
        data["scenes"][0]["daylight"] = "scene"
        data["time_binding"] = asdict(DAYLIGHT_TIME)

    sm, sid, package, context = timed_spell_world(walking=True, configure=configure)
    with sm:
        before = sm.read_noncombat_snapshot(sid, package, context=context)
        assert GridTopology(before.scene_state.grid).light_on_cell("0,0") == "dark"
        with pytest.raises(StateMachineError) as unavailable:
            initialize(sm, sid, package, context)
        assert unavailable.value.code == "unsupported_combat_dynamic_lighting"
        after = sm.read_noncombat_snapshot(sid, package, context=context)
        assert GridTopology(after.scene_state.grid).light_on_cell("0,0") == "dark"


def test_public_projection_distinguishes_magical_darkness_and_checks_sight_ray(magic_world):
    sm, sid, package, context = magic_world
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id

        def lights():
            return sm.get_illumination_area(sid, actor, ("2,0", "8,0"), package, context=context)

        def visible_target():
            return sm.environment.query_area(
                sid, actor, A, EnvironmentArea(("8,0",)), package, context=context
            )

        original = lights()
        assert visible_target()[0].cell == "8,0"
        _, light = cast(magic_world, "light-proof", "light", 0, {"kind": "item", "item_id": ITEM})
        assert light.status == "committed"
        assert ITEM in lights()[0]["contributing_sources"]
        _, dark = cast(
            magic_world, "dark-proof", "darkness", 2, {"kind": "point", "cell_id": "2,0"}
        )
        assert dark.status == "committed"
        covered, far = lights()
        assert covered["illumination"] == "dark" and covered["magical_darkness"]
        assert ITEM not in covered["contributing_sources"]
        assert far["illumination"] == original[1]["illumination"]
        assert not far.get("magical_darkness")
        with pytest.raises(StateMachineError) as denied:
            visible_target()
        assert denied.value.code == "unsupported_target_visibility"
        fixed = sm.read_noncombat_snapshot(sid, package, context=context)
        assert GridTopology(fixed.scene_state.grid).magical_darkness_on_cell("2,0")
        dropped = submit(sm, sid, package, context, DropWorldConcentration(), "end-dark-proof")
        assert dropped.status == "committed"
        assert lights() == original
        assert visible_target()[0].cell == "8,0"
        assert GridTopology(fixed.scene_state.grid).magical_darkness_on_cell("2,0")
