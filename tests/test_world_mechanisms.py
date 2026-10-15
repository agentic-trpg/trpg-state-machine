from copy import deepcopy

import pytest
from lock_support import CRED, DOOR, A
from world_support import command, setup, submit, world_data

from trpg_state_machine import ActivateMechanism, StateMachineError
from trpg_state_machine.package import _validate

LEVER = "object.lever"


def mechanism_data():
    from test_world_lights import light_config

    d = world_data()
    d["scenes"][0]["objects"].append(
        {
            "object_id": LEVER,
            "state": None,
            "initial_cell": "1,0",
            "movable": False,
            "blocks_passage": False,
        }
    )
    next(x for x in d["world_objects"] if x["object_id"] == CRED)["capabilities"].append(
        light_config()
    )
    profile = deepcopy(d["world_objects"][1])
    profile.update(
        object_id=LEVER,
        definition_ref="definition.lever",
        capabilities=[
            {
                "kind": "mechanism",
                "trigger_kind": "manual",
                "trigger_conditions": "actor_adjacent",
                "initial_status": "armed",
                "reset_policy": "none",
                "actions": [
                    {"kind": "set_light_enabled", "target_object_id": CRED, "value": True},
                    {"kind": "set_open_state", "target_object_id": DOOR, "value": "open"},
                ],
            }
        ],
    )
    d["world_objects"].append(profile)
    return d


def test_locked_target_preflight_is_atomic_and_success_is_one_shot_replayable(tmp_path, context):
    from test_lockable_passages import acquire

    from trpg_state_machine import UnlockDoor

    sm, sid, p, _ = setup(tmp_path, context, mechanism_data())
    with sm:
        receipt = submit(sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0"), "locked")
        assert receipt.error_code == "door_locked"
        assert sm._world_facts.light_state(sid, CRED)["enabled"] is False
        assert sm._world_facts.mechanism_status(sid, LEVER) == "armed"
        acquire(sm, sid, p, context)
        assert (
            submit(sm, sid, p, context, UnlockDoor(A, DOOR, CRED, "0,0"), "unlock").status
            == "committed"
        )
        cmd = command(sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0"), "activate")
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(cmd, p, context=context) == receipt
        assert sm._world_facts.light_state(sid, CRED)["enabled"] is True
        assert sm._world_facts.object(sid, DOOR)["state"] == "open"
        assert sm._world_facts.mechanism_status(sid, LEVER) == "spent"
        adopted = sm._conn.execute(
            "SELECT source_command_id,source_event_seq FROM trigger_history "
            "WHERE session_id=? AND event_definition_id=?",
            (sid, "mechanism." + LEVER),
        ).fetchone()
        assert tuple(adopted) == ("activate", receipt.event_seqs[0])
        assert (
            submit(sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0"), "again").error_code
            == "mechanism_spent"
        )
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_link_chain_and_adoption_rollback_on_storage_fault(tmp_path, context):
    d = mechanism_data()
    d["lockable_doors"] = []
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_chain BEFORE INSERT ON trigger_history "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        cmd = command(sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0"), "activate")
        with pytest.raises(StateMachineError):
            sm.submit_command(cmd, p, context=context)
        assert sm._world_facts.object(sid, DOOR)["state"] == "closed"
        assert sm._world_facts.light_state(sid, CRED)["enabled"] is False
        assert sm._world_facts.mechanism_status(sid, LEVER) == "armed"
        assert sm.get_command_receipt(sid, "activate", p, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_chain")
        assert sm.submit_command(cmd, p, context=context).status == "committed"


@pytest.mark.parametrize("invalid", ["recursive", "missing", "script", "wrong_capability"])
def test_invalid_link_graphs_rejected_before_session_creation(invalid):
    d = mechanism_data()
    a = d["world_objects"][-1]["capabilities"][0]["actions"][0]
    if invalid == "recursive":
        a["target_object_id"] = LEVER
    elif invalid == "missing":
        a["target_object_id"] = "object.absent"
    elif invalid == "script":
        a["kind"] = "eval_python"
    else:
        a["target_object_id"] = DOOR
    with pytest.raises(StateMachineError):
        _validate(d)


@pytest.mark.parametrize("inhibitor", ["jammed", "barred", "occupied", "destroyed"])
def test_link_origin_preserves_target_capability_invariants(tmp_path, context, inhibitor):
    d = mechanism_data()
    d["lockable_doors"] = []
    openable = d["world_objects"][0]["capabilities"][0]
    expected = {
        "jammed": "object_jammed",
        "barred": "object_barred",
        "occupied": "passage_unavailable",
        "destroyed": "mechanism_target_unavailable",
    }[inhibitor]
    if inhibitor == "jammed":
        openable["initial_jammed"] = True
    elif inhibitor == "barred":
        openable["initial_barred"] = True
    elif inhibitor == "occupied":
        d["scenes"][0]["objects"][0]["state"] = "open"
        d["actors"][0]["initial_cell"] = "0,1"
        d["world_objects"][-1]["capabilities"][0]["actions"][-1]["value"] = "closed"
    else:
        d["world_objects"][0]["capabilities"].append(
            {
                "kind": "durability",
                "armor_class": 10,
                "max_hp": 5,
                "initial_hp": 0,
                "damage_profile_ref": "scenario.explicit_settled_damage",
                "damage_threshold": None,
                "destruction_policy_ref": "disable_preserve_obstruction",
            }
        )
    sm, sid, p, _ = setup(tmp_path, context, d)
    with sm:
        result = submit(sm, sid, p, context, ActivateMechanism(A, LEVER, "0,0"), "blocked")
        assert result.error_code == expected, result
        assert sm._world_facts.mechanism_status(sid, LEVER) == "armed"
        assert not sm._world_facts.light_state(sid, CRED)["enabled"]
