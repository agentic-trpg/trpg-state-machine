"""Behavior at the shared rule commit boundary, with actual Engine outcomes."""

import json
import os
import subprocess
import sys
from dataclasses import asdict, replace

from lock_support import CRED, DOOR, A
from spatial_support import create
from test_dnd_ledger_scope import stored_state
from test_openable_checks import check_data, setup_check
from world_support import command, submit

from trpg_state_machine import (
    CheckWorldEffects,
    ForceOpen,
    ObjectState,
    PickLock,
    SetObjectState,
    SetSceneFlag,
    StateMachine,
    TypedCommand,
)
from trpg_state_machine.package import _validate


def test_async_evaluation_holds_no_write_scope_and_rechecks_external_commit(
    tmp_path, context, monkeypatch
):
    import trpg_state_machine.rule_execution as execution

    sm, sid, p, loader = setup_check(tmp_path, context)
    with sm:
        original = execution.evaluate_openable
        before_rng = sm.read_rng_context(sid, p, context=context)

        async def concurrent(spec, **kwargs):
            assert not sm._conn.in_transaction
            result = await original(spec, **kwargs)
            with StateMachine(tmp_path / "check.db", dnd_loader=loader) as other:
                assert (
                    submit(
                        other, sid, p, context, SetSceneFlag(A, "flag.lamp_checked", True), "other"
                    ).status
                    == "committed"
                )
            return result

        monkeypatch.setattr(execution, "evaluate_openable", concurrent)
        receipt = submit(sm, sid, p, context, PickLock(A, DOOR, "lock.outer", CRED, "0,0"), "pick")
        assert receipt.status == "conflict" and receipt.error_code == "version_conflict"
        assert not receipt.event_seqs
        assert sm.read_rng_context(sid, p, context=context) == before_rng
        assert all(lock[1] for lock in sm._world_facts.locks_for_object(sid, DOOR))


def obstructed_data(*, jammed):
    d, loader = check_data(jammed=jammed)
    c = d["world_objects"][0]["capabilities"][0]
    c["force_methods"] = [c["force_methods"][0]]
    c["force_methods"][0]["open_after"] = True
    d["world_objects"][0]["capabilities"] = [
        c,
        {
            "kind": "spatial",
            "footprint": [[0, 0], [1, 0]],
            "mobility_policy": "fixed",
            "open_blocks_movement": True,
            "closed_blocks_movement": False,
            "destroyed_blocks_movement": False,
        },
    ]
    d["passages"][0]["door_object_id"] = None
    d["scenes"][0]["objects"][0]["initial_cell"] = "1,0"
    d["actors"][0]["initial_cell"] = "2,0"
    return d, loader


def test_force_success_final_space_conflict_rolls_back_every_effect(tmp_path, context):
    d, loader = obstructed_data(jammed=True)
    p = _validate(d)
    with StateMachine(tmp_path / "space.db", dnd_loader=loader) as sm:
        sid = create(sm, p, context)
        before = stored_state(tmp_path / "space.db")
        receipt = submit(sm, sid, p, context, ForceOpen(A, DOOR, "free-jam", None, "0,0"), "force")
        assert receipt.status == "rejected" and receipt.error_code == "invalid_spatial_state"
        assert not receipt.event_seqs
        after = stored_state(tmp_path / "space.db")
        assert {k: v for k, v in before.items() if k != "command_receipts"} == {
            k: v for k, v in after.items() if k != "command_receipts"
        }
        assert sm._world_facts.openable_state(sid, DOOR)["jammed"]
        assert sm._world_facts.object(sid, DOOR)["state"] == "closed"


def test_generic_check_consequence_uses_the_same_final_space_check(tmp_path, context):
    from dnd5e_engine.evaluation_contracts import CheckPayload

    d, loader = obstructed_data(jammed=False)
    p = _validate(d)
    gm = replace(context, can_adjudicate_checks=True)
    with StateMachine(tmp_path / "consequence.db", dnd_loader=loader) as sm:
        sid = create(sm, p, context)
        state = sm.read_session(sid, p, context=context)
        payload = CheckPayload(
            actor_id=state.pc_actor_id,
            ability="str",
            skill="athletics",
            tool=None,
            dc=0,
            target_id=None,
            context="generic",
            required_sense="none",
            social_interaction=False,
            advantage=(),
            disadvantage=(),
            redeem_granted_die=None,
        )
        cmd = TypedCommand(
            sid,
            "check",
            state.pc_actor_id,
            "rules.check",
            payload,
            state.world_version,
            check_world_effects=CheckWorldEffects(
                "gm.opening", on_success=(SetObjectState(A, DOOR, ObjectState.OPEN),)
            ),
        )
        before = stored_state(tmp_path / "consequence.db")
        result = sm.submit_command(cmd, p, context=gm)
        assert result.status == "rejected" and result.error_code == "invalid_spatial_state"
        after = stored_state(tmp_path / "consequence.db")
        assert {k: v for k, v in before.items() if k != "command_receipts"} == {
            k: v for k, v in after.items() if k != "command_receipts"
        }


def test_queries_hide_method_conditions_and_credentials_and_never_write(tmp_path, context):
    sm, sid, p, _ = setup_check(tmp_path, context)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        before = stored_state(tmp_path / "check.db")
        view = sm.get_object(sid, actor, DOOR, p, context=context)
        assert all(
            field not in repr(view)
            for field in ("credential_instance_ids", "pick_dc", "break-outer", "force_methods")
        )
        query = sm.query_world_action(
            sid, actor, PickLock(A, DOOR, "lock.secret", CRED, "0,0"), p, context=context
        )
        assert query.reason == "invalid_target"
        assert stored_state(tmp_path / "check.db") == before


def test_fresh_process_replays_new_check_receipt_without_rng_or_events(tmp_path, context):
    sm, sid, p, _ = setup_check(tmp_path, context)
    with sm:
        cmd = command(sm, sid, p, context, PickLock(A, DOOR, "lock.outer", CRED, "0,0"), "pick")
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "committed"
        before = stored_state(tmp_path / "check.db")
        data = asdict(cmd)
        data.pop("check_world_effects")
        code = (
            "import json,sys; from test_openable_checks import check_data; "
            "from trpg_state_machine import StateMachine,TrustedContext,world_command_from_data; "
            "from trpg_state_machine.package import _validate; from dataclasses import asdict; "
            "d,l=check_data(); p=_validate(d); c=json.loads(sys.argv[3]); "
            "c['controlled_npc_ids']=tuple(c['controlled_npc_ids']); c=TrustedContext(**c); "
            "s=StateMachine(sys.argv[1],dnd_loader=l); "
            "cmd=world_command_from_data(json.loads(sys.argv[2])); "
            "print(json.dumps(asdict(s.submit_command(cmd,p,context=c)))); s.close()"
        )
        run = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(tmp_path / "check.db"),
                json.dumps(data),
                json.dumps(asdict(context)),
            ],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(("src", "tests"))},
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(run.stdout) == json.loads(json.dumps(asdict(receipt)))
        assert stored_state(tmp_path / "check.db") == before


def test_invalid_force_lock_reference_is_rejected_by_schema():
    import pytest
    from test_openable_checks import check_data

    from trpg_state_machine import StateMachineError

    d, _ = check_data()
    d["world_objects"][0]["capabilities"][0]["force_methods"][1]["unlock_lock_id"] = ["lock.outer"]
    with pytest.raises(StateMachineError):
        _validate(d)
