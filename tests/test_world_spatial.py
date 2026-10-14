from copy import deepcopy
from dataclasses import replace

import pytest
from lock_support import A
from world_support import command, setup, submit, world_data

from trpg_state_machine import RepositionObject, StateMachineError, StepActor

OBSTACLE = "object.barrier"


def spatial_world_data():
    d = world_data()
    obj = deepcopy(d["scenes"][0]["objects"][1])
    obj.update(object_id=OBSTACLE, state=None, initial_cell="2,0")
    d["scenes"][0]["objects"].append(obj)
    profile = deepcopy(d["world_objects"][1])
    profile.update(
        object_id=OBSTACLE,
        definition_ref="definition.barrier",
        weight_lb=80,
        capabilities=[
            {
                "kind": "spatial",
                "footprint": [[0, 0], [0, 1]],
                "mobility_policy": "gm_placement",
                "open_blocks_movement": True,
                "closed_blocks_movement": True,
                "destroyed_blocks_movement": False,
            }
        ],
    )
    d["world_objects"].append(profile)
    return d


def test_multicell_current_projection_gm_authorization_collision_and_replay(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, spatial_world_data())
    gm = replace(context, can_place_world_objects=True)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert sm.get_object(sid, actor, OBSTACLE, p, context=context)["occupied_cells"] == (
            "2,0",
            "2,1",
        )
        op = RepositionObject(A, OBSTACLE, "2,0", "1,0")
        cmd = command(sm, sid, p, context, op, "gm.place")
        with pytest.raises(StateMachineError, match="grant"):
            sm.submit_command(cmd, p, context=context)
        receipt = sm.submit_command(cmd, p, context=gm)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(cmd, p, context=gm) == receipt
        q = sm.query_world_action(sid, actor, StepActor(A, "0,0", "1,0"), p, context=context)
        assert q.status == "unavailable"
        assert (
            submit(
                sm, sid, p, gm, RepositionObject(A, OBSTACLE, "1,0", "0,0"), "actor.collision"
            ).error_code
            == "object_placement_blocked"
        )
        assert (
            submit(sm, sid, p, gm, RepositionObject(A, OBSTACLE, "1,0", "2,2"), "bounds").error_code
            == "object_placement_blocked"
        )
        assert (
            submit(sm, sid, p, gm, RepositionObject(A, OBSTACLE, "1,0", "2,0"), "clear").status
            == "committed"
        )
        assert (
            sm.query_world_action(sid, actor, StepActor(A, "0,0", "1,0"), p, context=context).status
            == "available"
        )


def test_object_placement_sql_fault_rolls_back_projection(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, spatial_world_data())
    gm = replace(context, can_place_world_objects=True)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        before = sm.get_object(sid, actor, OBSTACLE, p, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_evidence BEFORE INSERT ON event_evidence "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        cmd = command(sm, sid, p, gm, RepositionObject(A, OBSTACLE, "2,0", "1,0"), "fault")
        with pytest.raises(StateMachineError):
            sm.submit_command(cmd, p, context=gm)
        assert sm.get_object(sid, actor, OBSTACLE, p, context=context) == before
        sm._conn.execute("DROP TRIGGER fail_evidence")
        assert sm.submit_command(cmd, p, context=gm).status == "committed"


def test_navigation_execution_rechecks_changed_footprint(tmp_path, context):
    from lock_support import NPC
    from test_lockable_passages import pump

    from trpg_state_machine import CancelNavigation, StartNavigation

    sm, sid, p, _ = setup(tmp_path, context, spatial_world_data())
    gm = replace(context, can_place_world_objects=True)
    with sm:
        sm.grant_npc_access(sid, NPC, "agent:world", p, context=context)
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        assert (
            submit(sm, sid, p, context, StartNavigation("activity.route", A, "1,0"), "plan").status
            == "committed"
        )
        assert (
            submit(
                sm, sid, p, gm, RepositionObject(A, OBSTACLE, "2,0", "1,0"), "gm.block", NPC
            ).status
            == "committed"
        )
        result = pump(sm, sid, p, context, pc, "activity.route", "blocked")
        assert result.activity.status == "blocked"
        assert sm.read_actor_position(sid, pc, p, context=context).cell == "0,0"
        assert (
            submit(
                sm,
                sid,
                p,
                context,
                CancelNavigation("activity.route", result.activity.activity_version),
                "cancel",
            ).status
            == "committed"
        )
        assert (
            submit(
                sm, sid, p, gm, RepositionObject(A, OBSTACLE, "1,0", "2,0"), "gm.clear", NPC
            ).status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, StartNavigation("activity.retry", A, "1,0"), "retry").status
            == "committed"
        )
        assert (
            pump(sm, sid, p, context, pc, "activity.retry", "done").activity.status == "completed"
        )
