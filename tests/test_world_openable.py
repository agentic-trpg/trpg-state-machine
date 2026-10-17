from dataclasses import asdict

import pytest
from lock_support import DOOR, A
from world_support import command, setup, submit, world_data

from trpg_state_machine import BarObject, ObjectState, SetObjectState, UnbarObject
from trpg_state_machine.commands import world_command_from_data


def test_bar_separate_from_open_and_legacy_switch_cannot_bypass(tmp_path, context):
    d = world_data()
    d["lockable_doors"] = []
    d["world_objects"][0]["capabilities"][0].update(
        initial_barred=False, bar_operable_sides=[A], operation_sides=[A]
    )
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        op = BarObject(A, DOOR, "0,0")
        assert sm.query_world_action(sid, actor, op, p, context=context).status == "available"
        cmd = command(sm, sid, p, context, op, "bar")
        data = asdict(cmd)
        data.pop("check_world_effects")
        assert world_command_from_data(data) == cmd
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "committed"
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert (
            submit(
                sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "blocked"
            ).error_code
            == "object_barred"
        )
        assert (
            submit(sm, sid, p, context, UnbarObject(A, DOOR, "0,0"), "unbar").status == "committed"
        )
        assert sm.get_object(sid, actor, DOOR, p, context=context)["open_state"] == "closed"
        assert (
            submit(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "open").status
            == "committed"
        )
        assert submit(sm, sid, p, context, op, "bar.open").error_code == "object_must_be_closed"


@pytest.mark.parametrize("initial_jammed", [True, False])
def test_jam_policy_is_independent_of_lock(tmp_path, context, initial_jammed):
    d = world_data()
    d["lockable_doors"] = []
    d["world_objects"][0]["capabilities"][0]["initial_jammed"] = initial_jammed
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        receipt = submit(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "open")
        assert receipt.error_code == ("object_jammed" if initial_jammed else None)


def test_repeated_open_is_a_noop_and_queries_are_read_only(tmp_path, context):
    d = world_data()
    d["lockable_doors"] = []
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        op = SetObjectState(A, DOOR, ObjectState.OPEN)
        first = submit(sm, sid, p, context, op, "open.once")
        assert first.status == "committed" and first.event_seqs
        before = sm._conn.total_changes
        for _ in range(2):
            assert sm.query_world_action(sid, actor, op, p, context=context).status == "available"
            assert sm.get_object(sid, actor, DOOR, p, context=context)["open_state"] == "open"
        assert sm._conn.total_changes == before
        again = submit(sm, sid, p, context, op, "open.again")
        assert again.status == "committed" and not again.event_seqs
        assert again.world_version == first.world_version
