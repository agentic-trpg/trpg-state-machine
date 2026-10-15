"""Original explicit World definitions over the existing public station fixture."""

from lock_support import lock_data, lock_loader
from spatial_support import command, create, submit

from trpg_state_machine import StateMachine
from trpg_state_machine.package import _validate


def world_data():
    data = lock_data()
    data["manifest"].update(schema_version="state-machine-foundation/6", package_version="world.1")
    data["world_objects"] = [
        {
            "object_id": obj["object_id"],
            "definition_ref": "definition." + obj["object_id"].split(".", 1)[1],
            "display_name": obj["object_id"],
            "description_ref": None,
            "material": "iron" if obj["state"] is not None else "wood",
            "size_category": "large" if obj["state"] is not None else "tiny",
            "weight_lb": 40 if obj["state"] is not None else 1,
            "capabilities": [{"kind": "openable"}] if obj["state"] is not None else [],
        }
        for scene in data["scenes"]
        for obj in scene["objects"]
    ]
    return data


def setup(tmp_path, context, data=None):
    package = _validate(data or world_data())
    db = tmp_path / "world.db"
    sm = StateMachine(db, dnd_loader=lock_loader())
    sid = create(sm, package, context)
    return sm, sid, package, db


__all__ = ["command", "submit", "setup", "world_data"]
