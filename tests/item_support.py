"""Original GC3 public API fixtures with actual pinned item assets."""

from pathlib import Path

import yaml
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from modern_support import modern_data
from spatial_support import command, create, submit

from trpg_state_machine.package import _validate

A, B, NPC = "scene.platform", "scene.workshop", "npc.caretaker"
DAGGER, SECOND, ROPE = "object.dagger_a", "object.dagger_b", "object.rope"


def item_loader():
    bundled = BundledAssetLoader()
    return MemoryAssetLoader(
        items=[
            bundled.get_item(slug)
            for slug in (
                "rapier",
                "longsword",
                "dagger",
                "greatsword",
                "rope",
                "shield",
                "leather-armor",
                "wand-of-magic-missiles",
            )
        ],
        monsters=[bundled.get_monster("bandit")],
    )


def item_data():
    base = yaml.safe_load((Path(__file__).parent / "fixtures/npc_station.yaml").read_text("utf-8"))
    # This new complete D&D Package contains exactly the PC and one mechanical NPC.
    # Do not carry forward the legacy fixture's nonmechanical workshop actor.
    base["actors"] = [a for a in base["actors"] if a["actor_id"] == NPC]
    base["actors"][0]["npc_state"]["relationships"] = [
        r
        for r in base["actors"][0]["npc_state"]["relationships"]
        if r["target_id"] != "npc.attendant"
    ]
    base["world"]["world_facts"] = [
        f for f in base["world"]["world_facts"] if f["fact_id"] != "fact.route"
    ]
    for scene in base["scenes"]:
        scene["actor_ids"] = [a for a in scene["actor_ids"] if a == NPC]
    data = modern_data(base, item_loader(), npc_hp=50, spatial=True)
    data["manifest"].update(schema_version="state-machine-foundation/4", package_version="gc3.1")
    for actor in data["actors"]:
        if actor["actor_id"] == NPC:
            actor["initial_cell"] = "2,0"
        if "npc_state" in actor:
            actor["npc_state"]["runtime"]["current_activity"] = None
    data["navigation"] = {
        "regions": [
            {
                "region_id": "region.public",
                "scene_ids": [A, B],
                "passage_ids": ["passage.mechanical"],
            }
        ],
        "actor_grants": [
            {"actor_id": a["actor_id"], "region_ids": ["region.public"]} for a in data["actors"]
        ],
        "pc_template_grants": [
            {"pc_template_id": p["pc_template_id"], "region_ids": ["region.public"]}
            for p in data["pc_templates"]
        ],
    }
    data["item_transfers"] = []
    for iid, slug, cell in (
        (DAGGER, "dagger", "2,1"),
        (SECOND, "dagger", "2,1"),
        (ROPE, "rope", "1,0"),
    ):
        data["scenes"][0]["objects"].append(
            {
                "object_id": iid,
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": cell,
            }
        )
        data["item_transfers"].append(
            {
                "instance_id": iid,
                "item_slug": slug,
                "initial_owner_id": None,
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "policy": "ordinary_public",
            }
        )
    return data


def item_package():
    return _validate(item_data())


def navigate(sm, sid, package, context, target, key, actor=None):
    from trpg_state_machine import StartNavigation, pump_navigation

    actor = actor or sm.read_session(sid, package, context=context).pc_actor_id
    aid = "activity." + key
    receipt = submit(sm, sid, package, context, StartNavigation(aid, *target), key, actor)
    assert receipt.status == "committed", receipt
    result = pump_navigation(
        sm.submit_command,
        sm.read_navigation_activity,
        sm.get_command_receipt,
        sid,
        actor,
        aid,
        package,
        context=context,
        request_id=key + ".pump",
        budget=32,
    )
    assert result.activity.status == "completed", result
    return result


__all__ = ["A", "B", "NPC", "DAGGER", "SECOND", "ROPE", "command", "create", "submit"]
