"""Original public-region fixtures and high-level public navigation calls."""

from pathlib import Path

import yaml
from spatial_support import PASSAGE, A, B, command, create, spatial_data, submit

from trpg_state_machine import AdvanceNavigation, StartNavigation, pump_navigation
from trpg_state_machine.package import _validate

NPC = "npc.caretaker"


def navigation_data(base=None):
    if base is None:
        base = yaml.safe_load(
            (Path(__file__).parent / "fixtures/npc_station.yaml").read_text("utf-8")
        )
    data = spatial_data(base)
    data["manifest"].update(schema_version="state-machine-foundation/3", package_version="gc2.1")
    data["pc_templates"][0]["initial_cell"] = "0,0"
    data["scenes"][0]["objects"][0]["state"] = "closed"
    for actor in data["actors"]:
        if "npc_state" in actor:
            actor["npc_state"]["runtime"]["current_activity"] = None
    data["navigation"] = {
        "regions": [{"region_id": "region.public", "scene_ids": [A, B], "passage_ids": [PASSAGE]}],
        "actor_grants": [
            {"actor_id": a["actor_id"], "region_ids": ["region.public"]} for a in data["actors"]
        ],
        "pc_template_grants": [
            {"pc_template_id": p["pc_template_id"], "region_ids": ["region.public"]}
            for p in data["pc_templates"]
        ],
    }
    return data


def navigation_package(base=None):
    return _validate(navigation_data(base))


def start(
    sm, sid, package, context, aid="activity.route", target=(B, "4,2"), actor=None, key="start"
):
    return submit(sm, sid, package, context, StartNavigation(aid, *target), key, actor)


def read(sm, sid, package, context, aid="activity.route", actor=None):
    actor = actor or sm.read_session(sid, package, context=context).pc_actor_id
    return sm.read_navigation_activity(sid, actor, aid, package, context=context)


def advance_command(sm, sid, package, context, key, aid="activity.route", actor=None):
    activity = read(sm, sid, package, context, aid, actor)
    return command(
        sm, sid, package, context, AdvanceNavigation(aid, activity.activity_version), key, actor
    )


def advance(sm, sid, package, context, key, aid="activity.route", actor=None):
    return sm.submit_command(
        advance_command(sm, sid, package, context, key, aid, actor), package, context=context
    )


def pump(sm, sid, package, context, request_id="pump", budget=32, aid="activity.route", actor=None):
    actor = actor or sm.read_session(sid, package, context=context).pc_actor_id
    return pump_navigation(
        sm.submit_command,
        sm.read_navigation_activity,
        sm.get_command_receipt,
        sid,
        actor,
        aid,
        package,
        context=context,
        request_id=request_id,
        budget=budget,
    )


__all__ = ["A", "B", "NPC", "PASSAGE", "create", "submit"]
