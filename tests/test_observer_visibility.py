"""Ordinary sight through authorized object, spatial and environment reads."""

from dataclasses import replace

import pytest
from dnd5e_engine import spatial as re_spatial
from dnd5e_engine.spatial import GridTopology
from lock_support import CRED, NPC, A
from test_object_spatial_service import changed_grid, oriented_data
from test_world_containers import BOX
from test_world_lights import lighting_data
from test_world_spatial import OBSTACLE
from world_support import setup, submit

from trpg_state_machine import EnvironmentArea, SetLightEnabled, StateMachineError
from trpg_state_machine.grid_maps import ReadOnlyTopology
from trpg_state_machine.perception import NPCContext


@pytest.mark.parametrize(
    "obstruction",
    [
        {"wall_segments": [{"x1": 1, "y1": 0, "x2": 1, "y2": 1}]},
        {"blocked_cells": ["1,0"]},
        {"cover_cells": {"1,0": "total"}},
        {"obscurement_cells": {"1,0": "heavy"}},
    ],
    ids=["wall", "solid-terrain", "total-cover", "heavy-obscurement"],
)
def test_world_queries_share_ordinary_sight_for_distinct_observers(tmp_path, context, obstruction):
    data = oriented_data(anchor="2,0", footprint=[[0, 0]], blocking=False)
    changed_grid(data, lighting={"2,0": "dim", "1,2": "dark"}, **obstruction)
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        sm.grant_npc_access(sid, NPC, "npc:observer", package, context=context)
        npc = NPCContext("npc:observer", sid, NPC)

        def query_environment(actor, cell, ctx):
            return sm.environment.query_area(
                sid, actor, A, EnvironmentArea((cell,)), package, context=ctx
            )[0]

        assert sm.get_object(sid, NPC, OBSTACLE, package, context=npc)["object_id"] == OBSTACLE
        assert OBSTACLE in {
            row[0] for row in sm.get_spatial_view(sid, NPC, package, context=npc).objects
        }
        assert query_environment(NPC, "2,0", npc).cell == "2,0"

        with pytest.raises(StateMachineError) as hidden:
            sm.get_object(sid, pc, OBSTACLE, package, context=context)
        assert hidden.value.code == "invalid_target"
        assert OBSTACLE not in {
            row[0] for row in sm.get_spatial_view(sid, pc, package, context=context).objects
        }
        with pytest.raises(StateMachineError) as blocked:
            query_environment(pc, "2,0", context)
        assert blocked.value.code == "unsupported_target_visibility"

        with pytest.raises(StateMachineError) as dark:
            query_environment(NPC, "1,2", npc)
        assert dark.value.code == "unsupported_target_visibility"
        (diagnostic,) = sm.get_illumination_area(sid, NPC, ("1,2",), package, context=npc)
        assert diagnostic["illumination"] == "dark"

        for granted_actor, bad_context in (
            (pc, npc),
            (NPC, replace(npc, principal_id="npc:other")),
        ):
            with pytest.raises(StateMachineError) as denied:
                sm.get_object(sid, granted_actor, OBSTACLE, package, context=bad_context)
            assert denied.value.code == "unauthorized"


@pytest.mark.parametrize("width", [44, 111])
def test_environment_area_prepares_one_observer_for_only_requested_targets(
    tmp_path, context, monkeypatch, width
):
    data = oriented_data(anchor="2,0", footprint=[[0, 0]], blocking=False)
    changed_grid(data, width=width, lighting={"2,0": "dim", "1,2": "dark"})
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        sm.grant_npc_access(sid, NPC, "npc:observer", package, context=context)
        npc = NPCContext("npc:observer", sid, NPC)
        prepare = sm._world._prepare_visibility
        preparations = []

        def track(sid, actor, position, package, *, cells=None):
            preparations.append((position.cell, cells))
            return prepare(sid, actor, position, package, cells=cells)

        monkeypatch.setattr(sm._world, "_prepare_visibility", track)
        # Distance alone does not hide a lit, unoccluded cell within the map.
        visible = EnvironmentArea(("2,0", "2,1", "40,2"))
        for _ in range(2):
            preparations.clear()
            results = sm.environment.query_area(sid, NPC, A, visible, package, context=npc)
            assert tuple(result.cell for result in results) == visible.cells
            assert preparations == [("2,2", visible.cells)]

        preparations.clear()
        partly_dark = EnvironmentArea((*visible.cells, "1,2"))
        with pytest.raises(StateMachineError) as blocked:
            sm.environment.query_area(sid, NPC, A, partly_dark, package, context=npc)
        assert blocked.value.code == "unsupported_target_visibility"
        assert preparations == [("2,2", partly_dark.cells)]

        preparations.clear()
        with pytest.raises(StateMachineError) as denied:
            sm.environment.query_area(
                sid, NPC, A, visible, package, context=replace(npc, principal_id="npc:other")
            )
        assert denied.value.code == "unauthorized"
        assert preparations == []


def test_committed_lamp_changes_only_unobstructed_observer_queries(tmp_path, context):
    data = lighting_data()
    changed_grid(data, wall_segments=[{"x1": 1, "y1": 1, "x2": 2, "y2": 1}])
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        pc = sm.read_session(sid, package, context=context).pc_actor_id
        sm.grant_npc_access(sid, NPC, "npc:observer", package, context=context)
        npc = NPCContext("npc:observer", sid, NPC)

        def environment(actor_id, ctx):
            return sm.environment.query_area(
                sid, actor_id, A, EnvironmentArea(("1,0",)), package, context=ctx
            )

        def assert_target_hidden(actor_id, ctx):
            with pytest.raises(StateMachineError) as hidden:
                environment(actor_id, ctx)
            assert hidden.value.code == "unsupported_target_visibility"
            with pytest.raises(StateMachineError) as obj:
                sm.get_object(sid, actor_id, BOX, package, context=ctx)
            assert obj.value.code == "invalid_target"
            assert BOX not in {
                row[0] for row in sm.get_spatial_view(sid, actor_id, package, context=ctx).objects
            }

        assert_target_hidden(pc, context)
        assert_target_hidden(NPC, npc)

        lit = submit(sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "lamp.on")
        assert lit.status == "committed", lit
        assert environment(pc, context)[0].cell == "1,0"
        assert sm.get_object(sid, pc, BOX, package, context=context)["object_id"] == BOX
        assert BOX in {
            row[0] for row in sm.get_spatial_view(sid, pc, package, context=context).objects
        }
        assert_target_hidden(NPC, npc)

        unlit = submit(
            sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", False), "lamp.off"
        )
        assert unlit.status == "committed", unlit
        assert_target_hidden(pc, context)
        assert_target_hidden(NPC, npc)


@pytest.mark.parametrize("width", [44, 111])
def test_environment_filters_geometry_before_light_and_reuses_surviving_rays(
    tmp_path, context, monkeypatch, width
):
    data = oriented_data(anchor="2,0", footprint=[[0, 0]], blocking=False)
    walls = [{"x1": 4, "y1": 2, "x2": 4, "y2": 3}]
    walls.extend({"x1": x, "y1": 5, "x2": x, "y2": 6} for x in range(44, width))
    changed_grid(
        data,
        width=width,
        height=8,
        cover_cells={"3,0": "total", "2,0": "half"},
        wall_segments=walls,
    )
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        sm.grant_npc_access(sid, NPC, "npc:observer", package, context=context)
        npc = NPCContext("npc:observer", sid, NPC)
        lights = sm._objects.lights
        compose = lights.current_cells
        geometry = ReadOnlyTopology.has_line_of_sight
        cover = GridTopology.cover_between
        ordinary = GridTopology.can_see
        intersect = re_spatial._segments_intersect
        work = []
        walls_tested = []

        def check_geometry(topology, a, b):
            work.append(("geometry", a, b))
            return geometry(topology, a, b)

        def check_cover(topology, a, b, occupied_cells=()):
            result = cover(topology, a, b, occupied_cells)
            work.append(("cover", a, b, result))
            return result

        def compose_light(sid, scene_id, package, cells):
            work.append(("light", cells))
            return compose(sid, scene_id, package, cells)

        def check_ordinary(topology, a, b, senses=None):
            work.append(("sight", a, b))
            return ordinary(topology, a, b, senses)

        def check_intersection(a, b, c, d):
            walls_tested.append((c, d))
            return intersect(a, b, c, d)

        monkeypatch.setattr(ReadOnlyTopology, "has_line_of_sight", check_geometry)
        monkeypatch.setattr(GridTopology, "cover_between", check_cover)
        monkeypatch.setattr(lights, "current_cells", compose_light)
        monkeypatch.setattr(GridTopology, "can_see", check_ordinary)
        monkeypatch.setattr(re_spatial, "_segments_intersect", check_intersection)

        with pytest.raises(StateMachineError) as blocked:
            sm.environment.query_area(
                sid, NPC, A, EnvironmentArea(("2,0", "3,0", "4,2")), package, context=npc
            )
        assert blocked.value.code == "unsupported_target_visibility"
        assert work == [
            ("geometry", "2,2", "2,0"),
            ("cover", "2,2", "2,0", "half"),
            ("geometry", "2,2", "3,0"),
            ("cover", "2,2", "3,0", "total"),
            ("geometry", "2,2", "4,2"),
            ("light", ("2,0",)),
            ("sight", "2,2", "2,0"),
        ]
        assert walls_tested == [((4.0, 2.0), (4.0, 3.0))]

        work.clear()
        with pytest.raises(StateMachineError) as all_blocked:
            sm.environment.query_area(sid, NPC, A, EnvironmentArea(("4,2",)), package, context=npc)
        assert all_blocked.value.code == "unsupported_target_visibility"
        assert work == [("geometry", "2,2", "4,2")]

        work.clear()
        with pytest.raises(StateMachineError) as over_budget:
            sm.environment.query_area(sid, NPC, A, EnvironmentArea(), package, context=npc)
        assert over_budget.value.code == "invalid_environment"
        assert work == []
