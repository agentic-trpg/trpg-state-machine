"""Closed spatial query support agrees with real commands and never writes."""

from dataclasses import replace

import pytest
from lock_support import CRED, DOOR, TWIN
from lock_support import setup as lock_setup
from spatial_support import PASSAGE, A, B, submit
from test_events import event_data
from test_gc14_review_fixes import MOVER, departure_data, setup
from test_lockable_passages import acquire
from test_migrations import stored_rows

from trpg_state_machine import (
    AdvanceGameTime,
    DropItem,
    EnterScene,
    LockDoor,
    MoveActor,
    ObjectState,
    PickupItem,
    PlaceObject,
    SetObjectState,
    SetSceneFlag,
    StartNavigation,
    StateMachineError,
    StepActor,
    TraversePassage,
    UnlockDoor,
)
from trpg_state_machine._operations import FoundationOperation


@pytest.mark.parametrize(
    "kind",
    ["step", "enter", "move", "passage", "place", "door", "pickup", "drop", "unlock", "lock"],
)
@pytest.mark.parametrize("available", [True, False])
def test_every_supported_query_matches_real_admission_and_is_read_only(
    tmp_path, context, kind, available
):
    if kind in ("pickup", "drop", "unlock", "lock"):
        sm, sid, p, db = lock_setup(tmp_path, context)
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        if kind == "pickup":
            assert (
                submit(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "near").status
                == "committed"
            )
        else:
            acquire(sm, sid, p, context)
        if kind == "lock":
            assert (
                submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock").status
                == "committed"
            )
        operation = {
            "pickup": PickupItem(CRED, A, "2,0"),
            "drop": DropItem(CRED, A, "1,0"),
            "unlock": UnlockDoor(A, DOOR, CRED, "0,0"),
            "lock": LockDoor(A, DOOR, CRED, "0,0"),
        }[kind]
        if not available:
            operation = replace(
                operation,
                **(
                    {"credential_instance_id": TWIN}
                    if kind in ("unlock", "lock")
                    else {"instance_id": "object.unknown"}
                ),
            )
    else:
        data = departure_data()
        if kind in ("enter", "passage", "place", "door"):
            data["actors"][-1]["initial_cell"] = "2,0"
            for pc in data["pc_templates"]:
                pc["initial_cell"] = "2,1"
        sm, sid, p, db, context = setup(tmp_path, context, data)
        actor = MOVER if kind == "move" else sm.read_session(sid, p, context=context).pc_actor_id
        operation = {
            "step": StepActor(A, "0,0", "1,0"),
            "enter": EnterScene(B),
            "move": MoveActor(MOVER, B),
            "passage": TraversePassage(PASSAGE, A, "2,1"),
            "place": PlaceObject("object.crate", A, "3,2", "2,2"),
            "door": SetObjectState(A, "object.platform_gate", ObjectState.CLOSED),
        }[kind]
        if not available:
            operation = {
                "step": StepActor(A, "0,0", "4,4"),
                "enter": EnterScene("scene.unknown"),
                "move": MoveActor(MOVER, "scene.unknown"),
                "passage": TraversePassage("passage.unknown", A, "2,1"),
                "place": PlaceObject("object.unknown", A, "3,2", "2,2"),
                "door": SetObjectState(A, "object.unknown", ObjectState.CLOSED),
            }[kind]
    with sm:
        before = stored_rows(db)
        query = sm.query_world_action(sid, actor, operation, p, context=context)
        assert stored_rows(db) == before
        assert query.status == ("available" if available else "unavailable")
        receipt = submit(sm, sid, p, context, operation, "actual", actor)
        assert receipt.status == ("committed" if available else "rejected"), receipt
        if not available:
            assert receipt.error_code == query.reason and receipt.event_seqs == ()
            assert {k: v for k, v in stored_rows(db).items() if k != "command_receipts"} == {
                k: v for k, v in before.items() if k != "command_receipts"
            }


@pytest.mark.parametrize(
    "operation",
    [
        SetSceneFlag(A, "flag.lamp_checked", True),
        ObjectState.OPEN,
        FoundationOperation,
        AdvanceGameTime(0, 1),
        StartNavigation("activity.query", B, "1,1"),
        {"scene_id": A},
        object(),
    ],
)
def test_unsupported_query_types_fail_at_entry_without_mutation(tmp_path, context, operation):
    sm, sid, p, db, context = setup(tmp_path, context)
    with sm:
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as exc:
            sm.query_world_action(sid, pc, operation, p, context=context)
        assert exc.value.code == "unsupported_operation"
        assert stored_rows(db) == before


def test_flag_submission_and_valid_package_consequence_keep_their_contract(tmp_path, context):
    data = departure_data()
    data["scenes"][0]["objects"][0]["state"] = "closed"
    data["events"] = [event_data()]
    data["scenes"][0]["event_definition_ids"] = ["event.open_gate"]
    sm, sid, p, db, context = setup(tmp_path, context, data)
    with sm:
        receipt = submit(sm, sid, p, context, SetSceneFlag(A, "flag.lamp_checked", True), "flag")
        assert receipt.status == "committed"
        snapshot = sm.read_session(sid, p, context=context)
        assert snapshot.scenes[A].local_flags["flag.lamp_checked"] is True
        assert snapshot.scenes[A].objects["object.platform_gate"] == "open"
        events = [
            e for e in sm.read_events(sid, p, context=context) if e.event_seq in receipt.event_seqs
        ]
        assert [e.event_type for e in events] == [
            "scene.flag_changed",
            "object.state_changed",
            "package.event_fired",
        ]
        assert events[1].payload["execution"] == {
            "kind": "package",
            "reference_id": "event.open_gate",
        }


def test_unseen_remote_and_unknown_query_targets_share_generic_refusal(tmp_path, context):
    sm, sid, p, db, context = setup(tmp_path, context)
    with sm:
        pc = sm.read_session(sid, p, context=context).pc_actor_id
        before = stored_rows(db)
        outcomes = [
            sm.query_world_action(
                sid, pc, SetObjectState(A, oid, ObjectState.OPEN), p, context=context
            )
            for oid in ("object.workshop_door", "object.absent")
        ]
        assert outcomes[0] == outcomes[1]
        assert outcomes[0].reason == "invalid_target"
        assert stored_rows(db) == before
