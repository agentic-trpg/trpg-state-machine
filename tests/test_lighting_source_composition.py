"""Public light results from held cones, ambient light and fixed projections."""

from dataclasses import asdict, replace

import pytest
from lock_support import CRED, A
from test_lamp_profiles import GRID_CONE, configure_light
from test_object_spatial_service import changed_grid
from test_world_daylight import DAYLIGHT_TIME
from test_world_lights import lighting_data
from world_support import setup, submit

from trpg_state_machine import AdvanceGameTime, AimLight, SetLightEnabled


def test_held_cone_turns_and_keeps_bright_and_dim_polygons(tmp_path, context):
    data = lighting_data()
    configure_light(data, emission_shape=GRID_CONE, initial_enabled=True)
    changed_grid(data, width=8, height=8)
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id

        def read():
            return sm.get_illumination_area(
                sid, actor, ("1,0", "0,1"), package, context=context, include_geometry=True
            )

        assert tuple(row["illumination"] for row in read()) == ("dark", "dark")
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
