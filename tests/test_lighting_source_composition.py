"""Public light results from held cones, ambient light and fixed projections."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from dnd5e_engine.spatial import GridTopology
from dnd_support import initialize
from item_support import NPC
from lock_support import CRED, A
from test_lamp_profiles import GRID_CONE, configure_light
from test_light_geometry import coverage_area
from test_object_spatial_service import changed_grid
from test_timed_spell_cast import timed_spell_world as timed_spell_world
from test_world_daylight import DAYLIGHT_TIME
from test_world_lights import light_config, lighting_data
from test_world_magic_light import ITEM, cast
from test_world_magic_light import magic_world as magic_world
from world_support import setup, submit

from trpg_state_machine import (
    AdvanceGameTime,
    AimLight,
    DropItem,
    DropWorldConcentration,
    EnvironmentArea,
    SetLightEnabled,
    StateMachine,
    StateMachineError,
    StepActor,
)
from trpg_state_machine import grid as grid_module
from trpg_state_machine.spatial import SpatialPosition


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


def test_lamp_and_spell_share_partial_cell_and_shadow(timed_spell_world, monkeypatch):
    lamp_id = "object.fixture_lamp"

    def configure(data):
        data["manifest"]["schema_version"] = "state-machine-foundation/6"
        data["lockable_doors"] = []
        data["rule_bindings"].pop("combat_foundation")
        data["world_objects"] = [
            {
                "object_id": lamp_id,
                "definition_ref": "definition.component_lamp",
                "display_name": "Lamp",
                "description_ref": None,
                "material": None,
                "size_category": "tiny",
                "weight_lb": 2,
                "capabilities": [
                    {
                        **light_config(),
                        "initial_enabled": True,
                        "modes": [
                            {"mode_id": "normal", "bright_radius_ft": 20, "dim_outer_radius_ft": 40}
                        ],
                    }
                ],
            }
        ]
        data["scenes"][0]["objects"].append(
            {
                "object_id": lamp_id,
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": "0,1",
            }
        )
        changed_grid(
            data,
            width=8,
            height=5,
            default_lighting="dark",
            lighting={"0,1": "bright"},
            wall_segments=[{"x1": 1, "y1": 1, "x2": 2, "y2": 2}],
        )

    world = timed_spell_world(additional_spells=("light", "darkness"), configure=configure)
    sm, sid, package, context = world
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id

        def read():
            return sm.get_illumination_area(
                sid, actor, ("3,2", "3,1", "5,3"), package, context=context, include_geometry=True
            )

        lamp = read()
        partial, shadow, dim = lamp
        assert partial["illumination"] == "bright"
        assert coverage_area(partial["geometry"]["bright"]) == pytest.approx(0.5)
        assert shadow["illumination"] == "dark" and shadow["geometry"]["bright"] == ()
        assert dim["illumination"] == "dim" and dim["geometry"]["dim"]
        switch = submit(sm, sid, package, context, SetLightEnabled(A, lamp_id, "0,0", False), "off")
        assert switch.status == "committed", switch
        assert all(cell["illumination"] == "dark" for cell in read())
        _, light = cast(world, "spell-light", "light", 0, {"kind": "item", "item_id": lamp_id})
        assert light.status == "committed", light
        assert read() == lamp

        switch = submit(sm, sid, package, context, SetLightEnabled(A, lamp_id, "0,0", True), "on")
        assert switch.status == "committed", switch
        _, dark = cast(world, "spell-dark", "darkness", 2, {"kind": "point", "cell_id": "0,0"})
        assert dark.status == "committed", dark

        from trpg_state_machine.light_geometry import LightGeometry

        def unwanted_projection(*args):
            pytest.fail("Known Darkness suppressed this cell's only remaining emitter")

        monkeypatch.setattr(LightGeometry, "project", unwanted_projection)
        (masked,) = sm.get_illumination_area(
            sid, actor, ("0,1",), package, context=context, include_geometry=True
        )
        assert masked["illumination"] == "dark" and masked["magical_darkness"]
        assert masked["geometry"] == {"base": "dark", "dim": (), "bright": ()}


@pytest.mark.parametrize(("occluder", "unrelated"), [(None, 0), ("pc", 0), ("npc", 0), ("npc", 32)])
def test_actor_opacity_is_opt_in_and_follows_committed_space(
    tmp_path, context, monkeypatch, occluder, unrelated
):
    data = lighting_data()
    configured = data["actors"][0]
    configured["initial_cell"] = "2,1" if occluder == "pc" else "1,0"
    if occluder == "pc":
        data["pc_templates"][0]["light_opaque"] = True
    if occluder == "npc":
        configured["light_opaque"] = True
    if unrelated:
        changed_grid(data, width=40, height=8)
        for i in range(unrelated):
            actor = deepcopy(configured)
            actor.update(actor_id=f"npc.far_{i}", initial_cell=f"{i},7")
            actor.pop("npc_state", None)
            data["actors"].append(actor)
            data["scenes"][0]["actor_ids"].append(actor["actor_id"])

    sm, sid, package, database = setup(tmp_path, context, data)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        source = submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "on")
        assert source.status == "committed", source
        dropped = submit(sm, sid, package, context, DropItem(CRED, A, "0,0"), "put-on-floor")
        assert dropped.status == "committed", dropped

        def read(session):
            return session.get_illumination_area(
                sid, pc, ("2,0",), package, context=context, include_geometry=True
            )[0]

        configured_ids = []
        opacity = grid_module.actor_light_opaque

        def count_opacity(store, session, actor):
            configured_ids.append(actor)
            return opacity(store, session, actor)

        with monkeypatch.context() as patch:
            patch.setattr(grid_module, "actor_light_opaque", count_opacity)
            held = read(sm)
            assert set(configured_ids) == ({pc} if occluder == "pc" else {pc, NPC})
            assert len(configured_ids) <= 3
            configured_ids.clear()
            assert read(sm) == held
            assert configured_ids == []
        expected_before = "dark" if occluder == "npc" else "dim"
        expected_after = "dark" if occluder == "pc" else "dim"
        assert held["illumination"] == expected_before
        if occluder == "pc":
            actor, start, end = pc, "0,0", "1,0"
        else:
            actor, start, end = NPC, "1,0", "1,1"
        operation = StepActor(A, start, end)
        moved = submit(sm, sid, package, context, operation, "occluder-step", actor)
        assert moved.status == "committed", moved
        assert read(sm)["illumination"] == expected_after
        assert held["illumination"] == expected_before

        # A tentative position changes the same lighting field until rollback.
        def candidate_level():
            return sm._objects.lights.current_cells(sid, A, package, ("2,0",))["2,0"].level

        with sm._transaction(write=True):
            with pytest.raises(ValueError, match="discard"), sm._store.savepoint():
                if occluder == "pc":
                    sm._world.characters.set_hp(sid, pc, 0)
                    assert candidate_level() == "dark"
                    sm._world.characters.move(sid, pc, SpatialPosition(A, None))
                    assert candidate_level() == "dim"
                sm._world.characters.move(sid, actor, SpatialPosition(A, start))
                assert candidate_level() == expected_before
                raise ValueError("discard")
            assert candidate_level() == expected_after
        loader = sm._rules.loader

    with StateMachine(database, dnd_loader=loader) as recovered:
        assert read(recovered)["illumination"] == expected_after
