import subprocess
import sys
from dataclasses import replace

import pytest
from lock_support import CRED, DOOR
from world_support import setup, world_data

from trpg_state_machine import StateMachineError
from trpg_state_machine.package import _validate


def test_public_definition_identity_pin_and_process_restore(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        door = sm.get_object(sid, actor, DOOR, p, context=context)
        item = sm.get_object(sid, actor, CRED, p, context=context)
        assert (door["material"], door["size_category"], door["weight_lb"]) == ("iron", "large", 40)
        assert (item["material"], item["size_category"], item["weight_lb"]) == ("wood", "tiny", 1)
        assert DOOR in {
            o["object_id"] for o in sm.list_scene_objects(sid, actor, p, context=context)
        }
        with pytest.raises(StateMachineError):
            sm.get_object(sid, actor, DOOR, p, context=replace(context, principal_id="intruder"))
    code = (
        "from trpg_state_machine import StateMachine, TrustedContext; "
        "from world_support import world_data; from lock_support import lock_loader; "
        "from trpg_state_machine.package import _validate; "
        "import sys,json; p=_validate(world_data()); "
        "s=StateMachine(sys.argv[1],dnd_loader=lock_loader()); "
        "c=TrustedContext('authenticated:alice','player.alice'); "
        "a=s.read_session(sys.argv[2],p,context=c).pc_actor_id; "
        "print(json.dumps(s.get_object(sys.argv[2],a,sys.argv[3],p,context=c))); s.close()"
    )
    import json
    import os

    run = subprocess.run(
        [sys.executable, "-c", code, str(db), sid, DOOR],
        env={**os.environ, "PYTHONPATH": "src;tests"},
        check=True,
        capture_output=True,
        text=True,
    )
    restored = json.loads(run.stdout)
    assert restored["definition_ref"] == door["definition_ref"]
    assert restored["package_revision"] == door["package_revision"]


@pytest.mark.parametrize("mutation", ["reference", "capability", "weight", "combination"])
def test_invalid_definitions_rejected(mutation):
    data = world_data()
    obj = data["world_objects"][0]
    if mutation == "reference":
        obj["object_id"] = "object.absent"
    elif mutation == "capability":
        obj["capabilities"] = [{"kind": "invented"}]
    elif mutation == "weight":
        obj["weight_lb"] = True
    else:
        obj["capabilities"] = []
    with pytest.raises(StateMachineError):
        _validate(data)
