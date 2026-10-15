import pytest
from lock_support import CRED, DOOR, OTHER_CRED, TWIN, A
from test_lockable_passages import acquire
from world_support import setup, submit, world_data

from trpg_state_machine import LockObject, ObjectState, SetObjectState, UnlockDoor, UnlockObject


def multi_data():
    d = world_data()
    d["lockable_doors"] = [lock for lock in d["lockable_doors"] if lock["object_id"] != DOOR]
    d["world_objects"][0]["capabilities"].append(
        {
            "kind": "locks",
            "locks": [
                {
                    "lock_id": lid,
                    "initial_locked": True,
                    "credential_instance_ids": [iid],
                    "operable_sides": [],
                    "policy": "public_owned_credential",
                }
                for lid, iid in (("lock.outer", CRED), ("lock.inner", OTHER_CRED))
            ],
        }
    )
    return d


@pytest.mark.parametrize("shared_passage", [True, False])
def test_multiple_locks_require_separate_actual_credentials(tmp_path, context, shared_passage):
    data = multi_data()
    if not shared_passage:
        data["passages"][0]["door_object_id"] = None
        data["scenes"][0]["objects"][0]["initial_cell"] = "0,1"
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        acquire(sm, sid, p, context, iid=CRED, key="key1")
        acquire(sm, sid, p, context, iid=OTHER_CRED, key="key2")
        acquire(sm, sid, p, context, iid=TWIN, key="twin")
        legacy = submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "legacy.ambiguous")
        assert legacy.error_code == (
            "ambiguous_lock" if shared_passage else "unsupported_object_capability"
        )
        assert (
            submit(
                sm, sid, p, context, UnlockObject(A, DOOR, "lock.outer", TWIN, "0,0"), "wrong"
            ).error_code
            == "credential_unavailable"
        )
        assert (
            submit(
                sm, sid, p, context, UnlockObject(A, DOOR, "lock.outer", CRED, "0,0"), "outer"
            ).status
            == "committed"
        )
        assert (
            submit(
                sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "still.locked"
            ).error_code
            == "door_locked"
        )
        assert (
            submit(
                sm, sid, p, context, UnlockObject(A, DOOR, "lock.inner", OTHER_CRED, "0,0"), "inner"
            ).status
            == "committed"
        )
        assert (
            submit(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "open").status
            == "committed"
        )
        assert (
            submit(
                sm,
                sid,
                p,
                context,
                LockObject(A, DOOR, "lock.inner", OTHER_CRED, "0,0"),
                "lock.open",
            ).error_code
            == "door_must_be_closed"
        )
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        view = sm.get_object(sid, actor, DOOR, p, context=context)
        assert len(view["locks_for_object"]) == 2 and not view["effective_locked"]
        assert "credential_instance_ids" not in repr(view)
