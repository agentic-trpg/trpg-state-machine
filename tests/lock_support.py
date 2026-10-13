"""Original public mechanisms, actual pinned amulets; no spell/focus use."""

from copy import deepcopy

from dnd5e_engine.evaluation_ruleset import ruleset_binding
from dnd5e_srd_data import BundledAssetLoader, MemoryAssetLoader
from item_support import NPC, A, B, item_data, item_loader
from spatial_support import command, create, submit

from trpg_state_machine import StateMachine
from trpg_state_machine.package import _validate

C = "scene.store"
DOOR, OTHER_DOOR = "object.platform_gate", "object.workshop_door"
CRED, TWIN, OTHER_CRED = "object.amulet_a", "object.amulet_b", "object.amulet_c"
PASSAGE = "passage.mechanical"


def lock_loader():
    bundled = BundledAssetLoader()
    return MemoryAssetLoader(
        items=[
            bundled.get_item(s)
            for s in (
                "rapier",
                "longsword",
                "dagger",
                "greatsword",
                "rope",
                "shield",
                "leather-armor",
                "wand-of-magic-missiles",
                "amulet",
            )
        ],
        monsters=[bundled.get_monster("bandit")],
    )


def lock_data():
    data = item_data()
    data["manifest"]["ruleset_binding"] = ruleset_binding(lock_loader()).model_dump(mode="json")
    data["manifest"].update(schema_version="state-machine-foundation/5", package_version="gc4.1")
    data["scenes"][0]["objects"] = [data["scenes"][0]["objects"][0]]
    data["scenes"][0]["description"] += (
        " The original public mechanism accepts one specific amulet."
    )
    data["actors"][0]["initial_cell"] = "2,2"
    data["item_transfers"] = []
    for iid in (CRED, TWIN, OTHER_CRED):
        data["scenes"][0]["objects"].append(
            {
                "object_id": iid,
                "state": None,
                "movable": False,
                "blocks_passage": False,
                "initial_cell": "2,0",
            }
        )
        data["item_transfers"].append(
            {
                "instance_id": iid,
                "item_slug": "amulet",
                "initial_owner_id": None,
                "quantity": 1,
                "charges_remaining_per_unit": 0,
                "policy": "ordinary_public",
            }
        )
    data["passages"][0]["door_object_id"] = DOOR
    scene = deepcopy(data["scenes"][1])
    scene.update(scene_id=C, actor_ids=[], objects=[], event_definition_ids=[])
    scene["initial_state"]["local_flags"] = {}
    scene["exits"] = [{"to_scene_id": B, "passage_id": "passage.store"}]
    data["scenes"].append(scene)
    data["world"]["locations"][0]["scene_ids"].append(C)
    data["scenes"][1]["exits"].append({"to_scene_id": C, "passage_id": "passage.store"})
    data["passages"].append(
        {
            "passage_id": "passage.store",
            "door_object_id": OTHER_DOOR,
            "endpoints": [
                {"scene_id": B, "standing_cell": "2,1", "threshold_cell": "2,2"},
                {"scene_id": C, "standing_cell": "0,0", "threshold_cell": "0,1"},
            ],
        }
    )
    region = data["navigation"]["regions"][0]
    region["scene_ids"].append(C)
    region["passage_ids"].append("passage.store")
    extra = deepcopy(data["rule_bindings"]["noncombat_foundation"]["additional_scenes"][0])
    extra["scene_id"] = C
    data["rule_bindings"]["noncombat_foundation"]["additional_scenes"].append(extra)
    data["lockable_doors"] = [
        {
            "object_id": oid,
            "initial_locked": True,
            "credential_instance_ids": ids,
            "policy": "public_owned_credential",
        }
        for oid, ids in ((DOOR, [CRED]), (OTHER_DOOR, [CRED, OTHER_CRED]))
    ]
    return data


def setup(tmp_path, context, data=None):
    package = _validate(data or lock_data())
    db = tmp_path / "locks.db"
    sm = StateMachine(db, dnd_loader=lock_loader())
    sid = create(sm, package, context)
    sm.grant_npc_access(sid, NPC, "agent:gc4", package, context=context)
    return sm, sid, package, db


__all__ = ["A", "B", "NPC", "command", "create", "submit", "item_loader"]
