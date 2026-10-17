"""Public container commands with real pinned item instances and inventories."""

from copy import deepcopy
from dataclasses import replace

import pytest
from lock_support import CRED, DOOR, A
from test_lockable_passages import acquire
from world_support import command, setup, submit, world_data

from trpg_state_machine import (
    DropItem,
    ObjectState,
    PickupItem,
    PutItem,
    SetObjectState,
    StateMachine,
    TakeItem,
    UnlockDoor,
)

BOX, BAG = "object.case", "object.pouch"


def container_data():
    d = world_data()
    # Original scenario cases mechanically represented as approved mundane units;
    # no D&D item capacity/weight inference from their mechanical slugs.
    for oid in (BOX, BAG):
        obj = deepcopy(d["scenes"][0]["objects"][1])
        obj.update(object_id=oid, initial_cell="1,0", state="open")
        d["scenes"][0]["objects"].append(obj)
        item = deepcopy(d["item_transfers"][0])
        item.update(instance_id=oid)
        d["item_transfers"].append(item)
        profile = deepcopy(d["world_objects"][1])
        profile.update(
            object_id=oid,
            definition_ref="definition." + oid.split(".")[1],
            display_name="Original scenario case",
            weight_lb=2,
            capabilities=[
                {"kind": "openable"},
                {
                    "kind": "container",
                    "capacity_weight_lb": 10,
                    "capacity_volume_cu_ft": 10,
                    "opening_profiles": ["station.small"],
                    "allowed_content_kinds": ["station.unit"],
                    "nesting_limit": 2,
                    "light_transmission_open": "transmit",
                    "light_transmission_closed": "block",
                },
            ],
        )
        d["world_objects"].append(profile)
    for profile in d["world_objects"]:
        profile.update(volume_cu_ft=1, fit_profile="station.small", content_kind="station.unit")
    return d


def test_nested_carried_location_closed_key_access_and_restore(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, container_data())
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        acquire(sm, sid, p, context)
        assert submit(sm, sid, p, context, PickupItem(BOX, A, "1,0"), "box").status == "committed"
        assert submit(sm, sid, p, context, PickupItem(BAG, A, "1,0"), "bag").status == "committed"
        assert (
            submit(sm, sid, p, context, PutItem(A, CRED, BAG, "0,0"), "key.in").status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, PutItem(A, BAG, BOX, "0,0"), "bag.in").status == "committed"
        )
        assert sm.get_object(sid, actor, CRED, p, context=context)["location"]["ancestry"] == (
            BAG,
            BOX,
        )
        assert (
            submit(sm, sid, p, context, SetObjectState(A, BOX, ObjectState.CLOSED), "closed").status
            == "committed"
        )
        entries = sm.read_carried_items(sid, actor, p, context=context).entries
        assert CRED not in {e.instance_id for e in entries}
        internal = sm.read_noncombat_snapshot(sid, p, context=context)
        assert (
            next(e for e in internal.inventory_state.entries if e.instance_id == CRED).accessible
            is False
        )
        assert sm.get_object(sid, actor, BOX, p, context=context)["contents"] is None
        assert sm.get_object(sid, actor, BOX, p, context=context)["contents_weight_lb"] is None
        assert (
            submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "closed.key").error_code
            == "credential_unavailable"
        )
        assert (
            submit(sm, sid, p, context, TakeItem(A, CRED, BAG, "0,0"), "closed.take").error_code
            == "container_inaccessible"
        )
        assert (
            submit(sm, sid, p, context, SetObjectState(A, BOX, ObjectState.OPEN), "opened").status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "key.works").status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, PutItem(A, BOX, BAG, "0,0"), "cycle").error_code
            == "container_cycle"
        )
        cmd = command(sm, sid, p, context, TakeItem(A, CRED, BAG, "0,0"), "take")
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "committed"
        assert sm.submit_command(cmd, p, context=context) == receipt
        stale = sm.submit_command(
            replace(cmd, command_id="stale", expected_world_version=0), p, context=context
        )
        assert stale.error_code == "version_conflict"
        assert submit(sm, sid, p, context, DropItem(BOX, A, "1,0"), "drop").status == "committed"
        assert sm.get_object(sid, actor, BAG, p, context=context)["location"]["cell"] == "1,0"
    import json
    import subprocess
    import sys

    code = (
        "from test_world_containers import container_data; from lock_support import lock_loader; "
        "from trpg_state_machine import StateMachine,TrustedContext; "
        "from trpg_state_machine.package import _validate; import sys,json; "
        "p=_validate(container_data()); c=TrustedContext('authenticated:alice','player.alice'); "
        "s=StateMachine(sys.argv[1],dnd_loader=lock_loader()); "
        "a=s.read_session(sys.argv[2],p,context=c).pc_actor_id; "
        "print(json.dumps(s.get_object(sys.argv[2],a,'object.pouch',p,context=c))); s.close()"
    )
    import os

    restored_process = subprocess.run(
        [sys.executable, "-c", code, str(db), sid],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "src;tests"},
    )
    assert json.loads(restored_process.stdout)["location"]["container_id"] == BOX
    from lock_support import lock_loader

    with StateMachine(db, dnd_loader=lock_loader()) as restored:
        assert (
            restored.get_object(sid, actor, BAG, p, context=context)["location"]["container_id"]
            == BOX
        )


@pytest.mark.parametrize("failure", ["weight", "volume", "fit", "capacity", "self"])
def test_rejected_transfers_have_no_partial_location_or_projection(tmp_path, context, failure):
    d = container_data()
    profile = next(x for x in d["world_objects"] if x["object_id"] == CRED)
    box = next(x for x in d["world_objects"] if x["object_id"] == BOX)
    if failure == "weight":
        profile["weight_lb"] = None
    elif failure == "volume":
        profile["volume_cu_ft"] = None
    elif failure == "fit":
        profile["fit_profile"] = None
    elif failure == "capacity":
        box["capabilities"][1]["capacity_weight_lb"] = 0
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        before = sm.read_carried_items(sid, actor, p, context=context)
        op = PutItem(A, BOX if failure == "self" else CRED, BOX, "0,0")
        receipt = submit(sm, sid, p, context, op, "bad")
        assert receipt.status == "rejected"
        assert sm.read_carried_items(sid, actor, p, context=context) == before


def test_nested_container_transfer_changes_all_root_owners(tmp_path, context):
    from item_support import navigate
    from lock_support import NPC

    sm, sid, p, _ = setup(tmp_path, context, container_data())
    with sm:
        sm.grant_npc_access(sid, NPC, "agent:world", p, context=context)
        acquire(sm, sid, p, context)
        for oid in (BOX, BAG):
            assert submit(sm, sid, p, context, PickupItem(oid, A, "1,0"), oid).status == "committed"
        assert (
            submit(sm, sid, p, context, PutItem(A, CRED, BAG, "0,0"), "key.in").status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, PutItem(A, BAG, BOX, "0,0"), "bag.in").status == "committed"
        )
        assert submit(sm, sid, p, context, DropItem(BOX, A, "1,0"), "drop").status == "committed"
        navigate(sm, sid, p, context, (A, "1,1"), "npc.near", NPC)
        result = submit(sm, sid, p, context, PickupItem(BOX, A, "1,0"), "npc.pick", NPC)
        assert result.status == "committed", result
        inventory = sm.read_carried_items(sid, NPC, p, context=context)
        assert {e.instance_id for e in inventory.entries} >= {BOX, BAG, CRED}
        assert all(e.owner_id == NPC for e in inventory.entries)


def test_destroyed_container_retains_contents_and_allows_access(tmp_path, context):
    from test_world_durability import adopt

    from trpg_state_machine.object_durability import _ObjectHPSettlement

    d = container_data()
    profile = next(x for x in d["world_objects"] if x["object_id"] == BOX)
    profile["capabilities"].append(
        {
            "kind": "durability",
            "armor_class": 10,
            "max_hp": 5,
            "initial_hp": 5,
            "damage_profile_ref": "scenario.explicit_settled_damage",
            "damage_threshold": None,
            "destruction_policy_ref": "disable_preserve_obstruction",
        }
    )
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        assert (
            submit(sm, sid, p, context, PutItem(A, CRED, BOX, "0,0"), "key.in").status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, SetObjectState(A, BOX, ObjectState.CLOSED), "close").status
            == "committed"
        )
        version = sm.read_session(sid, p, context=context).world_version
        adopt(sm, sid, p, context, _ObjectHPSettlement(sid, BOX, "destroy.case", version, 5, 0))
        result = submit(sm, sid, p, context, TakeItem(A, CRED, BOX, "0,0"), "recover")
        assert result.status == "committed", result


def test_unknown_container_limit_is_distinct_from_unlimited(tmp_path, context):
    d = container_data()
    next(x for x in d["world_objects"] if x["object_id"] == BOX)["capabilities"][1][
        "capacity_weight_lb"
    ] = "unknown"
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        acquire(sm, sid, p, context)
        result = submit(sm, sid, p, context, PutItem(A, CRED, BOX, "0,0"), "unknown.limit")
        assert result.error_code == "unknown_container_capacity"
