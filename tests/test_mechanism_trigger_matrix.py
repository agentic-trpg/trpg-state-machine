"""Public commands exercise authored automatic mechanism event selectors."""

from copy import deepcopy

import pytest
from lock_support import CRED, DOOR, A
from test_lockable_passages import acquire
from test_mechanism_service import extended_data, trigger
from test_world_containers import BOX, container_data
from world_support import setup, submit

from trpg_state_machine import (
    BarObject,
    ClearJam,
    DropItem,
    LockDoor,
    PickupItem,
    PutItem,
    StateMachine,
    UnbarObject,
    UnlockDoor,
)
from trpg_state_machine.package import _validate

LEVER = "object.lever"


def trigger_data(event_type, target, value=None, *, container=False, lock=False, bar=False):
    """Install one authored trigger and an observable no-op-safe operation."""
    d = extended_data(
        triggers=[
            trigger("object_operation", target_object_id=target, event_type=event_type, value=value)
        ]
    )
    if container:
        base = container_data()
        base["manifest"]["schema_version"] = "state-machine-foundation/10"
        base["scenes"][0]["objects"].append(deepcopy(d["scenes"][0]["objects"][-1]))
        base["world_objects"].append(deepcopy(d["world_objects"][-1]))
        d = base
    if lock:
        from test_world_mechanisms import mechanism_data

        d["lockable_doors"] = mechanism_data()["lockable_doors"]
    if bar:
        d["world_objects"][0]["capabilities"][0].update(
            initial_barred=False, bar_operable_sides=[A], operation_sides=[A]
        )
    for operation in d["world_objects"][-1]["capabilities"][0]["operations"]:
        operation["actions"] = [dict(kind="set_open_state", target_object_id=DOOR, value="closed")]
        operation["consume_on_noop"] = True
    return d


@pytest.mark.parametrize(
    "event_type,value,actions,container,lock,bar",
    [
        (
            "door.lock_changed",
            False,
            [UnlockDoor(A, DOOR, CRED, "0,0"), LockDoor(A, DOOR, CRED, "0,0")],
            False,
            True,
            False,
        ),
        (
            "object.bar_changed",
            True,
            [BarObject(A, DOOR, "0,0"), UnbarObject(A, DOOR, "0,0")],
            False,
            False,
            True,
        ),
        (
            "item.picked_up",
            None,
            [PickupItem(CRED, A, "2,0"), DropItem(CRED, A, "2,0")],
            False,
            False,
            False,
        ),
        (
            "item.dropped",
            None,
            [PickupItem(CRED, A, "2,0"), DropItem(CRED, A, "2,0")],
            False,
            False,
            False,
        ),
        (
            "item.container_transferred",
            None,
            [PickupItem(CRED, A, "2,0"), PutItem(A, CRED, BOX, "0,0")],
            True,
            False,
            False,
        ),
    ],
)
def test_public_object_event_trigger_matches_exact_source(
    tmp_path, context, event_type, value, actions, container, lock, bar
):
    target = DOOR if lock or bar else CRED
    d = trigger_data(event_type, target, value, container=container, lock=lock, bar=bar)
    sm, sid, package, _ = setup(tmp_path, context, d)
    with sm:
        if lock:
            acquire(sm, sid, package, context)
        elif container:
            from item_support import navigate

            navigate(sm, sid, package, context, (A, "1,0"), "near.case")
            assert (
                submit(sm, sid, package, context, PickupItem(BOX, A, "1,0"), "case").status
                == "committed"
            )
        elif not bar:
            from item_support import navigate

            navigate(sm, sid, package, context, (A, "1,0"), "near.source")
        receipts = []
        for number, action in enumerate(actions):
            if container and number == 1:
                navigate(sm, sid, package, context, (A, "0,0"), "return.case")
            receipt = submit(sm, sid, package, context, action, f"source.{number}")
            assert receipt.status == "committed", receipt
            receipts.append(receipt)
        events = sm.read_events(sid, package, context=context)
        sources = [
            e
            for e in events
            if e.event_type == event_type
            and e.payload.get("object_id", e.payload.get("instance_id")) == target
        ]
        activations = [e for e in events if e.event_type == "mechanism.activated"]
        assert len(activations) == 1
        expected_receipt = receipts[1] if event_type == "item.dropped" else receipts[0]
        if event_type == "item.container_transferred":
            expected_receipt = receipts[1]
        matching = [e for e in sources if e.event_seq in expected_receipt.event_seqs]
        if event_type == "item.container_transferred":
            matching = [e for e in matching if e.payload["phase"] == "after"]
        assert len(matching) == 1
        assert activations[0].payload["source_event_seq"] == matching[0].event_seq
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1


def test_clear_jam_event_trigger_uses_real_check_result(tmp_path, context):
    from spatial_support import create
    from test_openable_checks import check_data

    data, loader = check_data(dc=0, jammed=True)
    mechanism = trigger_data("object.jam_cleared", DOOR)
    data["manifest"]["schema_version"] = "state-machine-foundation/10"
    data["scenes"][0]["objects"].append(mechanism["scenes"][0]["objects"][-1])
    data["world_objects"].append(mechanism["world_objects"][-1])
    package = _validate(data)
    with StateMachine(tmp_path / "jam-trigger.db", dnd_loader=loader) as sm:
        sid = create(sm, package, context)
        receipt = submit(
            sm, sid, package, context, ClearJam(A, DOOR, "free-jam", None, "0,0"), "clear"
        )
        assert receipt.status == "committed", receipt
        events = sm.read_events(sid, package, context=context)
        source = [
            e
            for e in events
            if e.event_type == "object.jam_cleared" and e.event_seq in receipt.event_seqs
        ]
        activations = [e for e in events if e.event_type == "mechanism.activated"]
        assert len(source) == len(activations) == 1
        assert activations[0].payload["source_event_seq"] == source[0].event_seq
        assert sm._world_facts.mechanism_state(sid, LEVER).activation_count == 1
