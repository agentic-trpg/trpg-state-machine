"""Ordinary sight through authorized object, spatial and environment reads."""

from dataclasses import replace

import pytest
from lock_support import NPC, A
from test_object_spatial_service import changed_grid, oriented_data
from test_world_spatial import OBSTACLE
from world_support import setup

from trpg_state_machine import EnvironmentArea, StateMachineError
from trpg_state_machine.perception import NPCContext


@pytest.mark.parametrize(
    "obstruction",
    [
        {"wall_segments": [{"x1": 1, "y1": 0, "x2": 1, "y2": 1}]},
        {"blocked_cells": ["1,0"]},
        {"cover_cells": {"1,0": "total"}},
    ],
    ids=["wall", "solid-terrain", "total-cover"],
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

        def track(sid, position, package, *, cells=None):
            preparations.append((position.cell, cells))
            return prepare(sid, position, package, cells=cells)

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
