"""Explicit component composition, step capability lifetime and closed knowledge."""

from copy import deepcopy
from dataclasses import replace

import pytest
from navigation_support import A, B, create, navigation_data, navigation_package, read, start
from test_domain_runtime import components, seed

from trpg_state_machine import (
    AdvanceNavigation,
    StartNavigation,
    StateMachine,
    StateMachineError,
    StepActor,
)
from trpg_state_machine.activity_handler import ActivityHandler
from trpg_state_machine.package import _validate


def test_navigation_composes_without_state_machine_and_step_scope_cannot_escape(tmp_path, context):
    c = components(tmp_path / "component.db")
    package = navigation_package()
    sid, actor = seed(c, package, context)
    handler = ActivityHandler(c.store, c.world.activities, c.characters, c.world, c.capture)
    with c.store.transaction(write=True):
        row = c.sessions._session(sid, package, context)
        handler.operate(
            row,
            package,
            StartNavigation("activity.direct", B, "4,2"),
            actor_id=actor,
            command_id="start",
        )
        record = c.world.activities.get(sid, "activity.direct")
        primitive = StepActor(A, "0,0", "1,0")
        scope = c.world.activities.execution(record, primitive)
        assert c.world.activities.qualifies(scope, sid, actor, primitive)
        assert not c.world.activities.qualifies(
            replace(scope, issuer=object()), sid, actor, primitive
        )
        assert not c.world.activities.qualifies(scope, sid, actor, StepActor(A, "0,0", "0,1"))
    with c.store.transaction(write=True):
        assert not c.world.activities.qualifies(scope, sid, actor, primitive)
        with pytest.raises(StateMachineError) as exc:
            c.world._operate(row, package, primitive, actor_id=actor, navigation_scope=scope)
        assert exc.value.code == "unauthorized"
        events = handler.operate(
            row, package, AdvanceNavigation("activity.direct", 1), actor_id=actor, command_id="step"
        )
        assert any(e.event_type == "actor.stepped" for e in events)
        assert c.characters.position(sid, actor).cell == "1,0"
        assert c.world.activities.get(sid, "activity.direct")["cursor"] == 1
    c.store.close()


@pytest.mark.parametrize(
    "fault",
    [
        "private",
        "abstract",
        "unknown_scene",
        "unknown_actor",
        "unknown_region",
        "duplicate",
        "secret_endpoint",
    ],
)
def test_navigation_configuration_fails_closed(fault):
    data = navigation_data()
    region = data["navigation"]["regions"][0]
    if fault == "private":
        data["scenes"][0]["access_policy"] = "internal_only"
    elif fault == "abstract":
        for scene in data["scenes"]:
            scene["grid"] = None
            for obj in scene["objects"]:
                obj.update(initial_cell=None, movable=False, blocks_passage=False)
            for exit_ in scene["exits"]:
                exit_["passage_id"] = None
        data["passages"] = []
        region["passage_ids"] = []
        for pc in data["pc_templates"]:
            pc["initial_cell"] = None
        for actor in data["actors"]:
            actor["initial_cell"] = None
    elif fault == "unknown_scene":
        region["scene_ids"].append("scene.unknown")
    elif fault == "unknown_actor":
        data["navigation"]["actor_grants"][0]["actor_id"] = "npc.unknown"
    elif fault == "unknown_region":
        data["navigation"]["actor_grants"][0]["region_ids"] = ["region.unknown"]
    elif fault == "duplicate":
        region["scene_ids"].append(region["scene_ids"][0])
    else:
        region["scene_ids"] = [A]
    with pytest.raises(StateMachineError) as exc:
        _validate(data)
    if fault in {"abstract", "private"}:
        assert exc.value.code == "unsupported_navigation"


@pytest.mark.parametrize("short_target", [True, False])
def test_long_unrelated_branch_is_pruned_without_rejecting_short_route(
    tmp_path, context, short_target
):
    data = navigation_data()
    other = "scene.other"
    workshop = data["scenes"][1]
    workshop["grid"].update(width=4096, height=1)
    workshop["objects"][0]["initial_cell"] = "4,0"
    for actor in data["actors"]:
        if actor["initial_scene_id"] == B:
            actor["initial_cell"] = "2,0"
    for endpoint in data["passages"][0]["endpoints"]:
        endpoint.update(standing_cell="0,0", threshold_cell="1,0")
    scene = deepcopy(data["scenes"][0])
    scene.update(
        scene_id=other,
        objects=[],
        actor_ids=[],
        event_definition_ids=[],
        initial_state={"local_flags": {}},
        exits=[{"to_scene_id": B, "passage_id": "passage.other"}],
    )
    scene["grid"].update(width=2, height=1)
    data["scenes"].append(scene)
    data["world"]["locations"][0]["scene_ids"].append(other)
    workshop["exits"].append({"to_scene_id": other, "passage_id": "passage.other"})
    data["passages"].append(
        {
            "passage_id": "passage.other",
            "door_object_id": None,
            "endpoints": [
                {"scene_id": B, "standing_cell": "4095,0", "threshold_cell": "4094,0"},
                {"scene_id": other, "standing_cell": "0,0", "threshold_cell": "1,0"},
            ],
        }
    )
    region = data["navigation"]["regions"][0]
    region["scene_ids"].append(other)
    region["passage_ids"].append("passage.other")
    package = _validate(data)
    with StateMachine(tmp_path / "bounded-plan.db") as sm:
        sid = create(sm, package, context)
        receipt = start(
            sm, sid, package, context, target=(A, "0,3") if short_target else (other, "0,0")
        )
        if short_target:
            assert receipt.status == "committed"
            assert read(sm, sid, package, context).plan_length == 3
        else:
            assert receipt.error_code == "navigation_plan_limit"
            assert sm._conn.execute("SELECT count(*) FROM navigation_activities").fetchone()[0] == 0
