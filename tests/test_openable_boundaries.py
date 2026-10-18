"""Dependency refusals are safety/coverage evidence, not implemented gameplay."""

from dataclasses import asdict

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from test_dnd_runtime import authority, setup

from trpg_state_machine import (
    ActivateMechanism,
    BarObject,
    CheckMechanism,
    ClearJam,
    ForceOpen,
    ObjectState,
    PickLock,
    ServiceMechanism,
    SetObjectState,
    StateMachineError,
    TypedCommand,
    UnlockObject,
)
from trpg_state_machine.commands import OPERATIONS, world_command_from_data


@pytest.mark.parametrize(
    "op",
    [
        ActivateMechanism("scene.platform", "object.trap", "0,0", "up"),
        ServiceMechanism("scene.platform", "object.trap", "0,0", "disarm"),
        CheckMechanism("scene.platform", "object.trap", "0,0", "disarm"),
        SetObjectState("scene.platform", "object.platform_gate", ObjectState.OPEN),
        BarObject("scene.platform", "object.platform_gate", "0,0"),
        UnlockObject("scene.platform", "object.platform_gate", "lock.public", "object.key", "0,0"),
        PickLock("scene.platform", "object.platform_gate", "lock.public", "object.tools", "0,0"),
        ForceOpen("scene.platform", "object.platform_gate", "force", None, "0,0"),
        ClearJam("scene.platform", "object.platform_gate", "clear", None, "0,0"),
    ],
)
def test_world_api_cannot_bypass_active_combat(tmp_path, package_data, context, op, monkeypatch):
    sm, sid, p, _ = setup(tmp_path, package_data, context)
    with sm:
        import trpg_state_machine.rule_execution as execution

        async def forbidden(*args, **kwargs):
            pytest.fail("unsupported combat object action must not evaluate")

        monkeypatch.setattr(execution, "evaluate_openable", forbidden)
        state = sm.read_session(sid, p, context=context)
        before = authority(sm, sid)
        cmd = TypedCommand(
            sid,
            "object",
            state.pc_actor_id,
            next(k for k, v in OPERATIONS.items() if v is type(op)),
            op,
            state.world_version,
        )
        receipt = sm.submit_command(cmd, p, context=context)
        assert receipt.status == "rejected"
        assert receipt.error_code == "unsupported_combat_world_operation"
        assert not receipt.event_seqs
        after = authority(sm, sid)
        assert {k: v for k, v in after.items() if k != "command_receipts"} == {
            k: v for k, v in before.items() if k != "command_receipts"
        }
        assert sm.submit_command(cmd, p, context=context) == receipt


@pytest.mark.parametrize("spell", ["arcane-lock", "knock"])
def test_unsupported_magic_cannot_spend_resources(tmp_path, package_data, context, spell):
    sm, sid, p, _ = setup(tmp_path, package_data, context)
    with sm:
        state = sm.read_session(sid, p, context=context)
        before = authority(sm, sid)
        payload = CombatIntentPayload(
            intent_type="cast_spell", spell_id=spell, target_id="npc.caretaker"
        )
        receipt = sm.submit_command(
            TypedCommand(
                sid, "spell", state.pc_actor_id, "combat.intent", payload, state.world_version
            ),
            p,
            context=context,
        )
        assert receipt.status == "rejected" and receipt.error_code == "intent.capability"
        after = authority(sm, sid)
        assert {k: v for k, v in after.items() if k != "command_receipts"} == {
            k: v for k, v in before.items() if k != "command_receipts"
        }


@pytest.mark.parametrize("field", ["trusted", "bypass_checks"])
def test_client_cannot_supply_authority(field):
    op = PickLock("scene.platform", "object.gate", "lock.public", "object.tool", "0,0")
    cmd = TypedCommand("session.example", "op", "pc.example", "world.pick_lock", op, 0)
    data = asdict(cmd)
    data.pop("check_world_effects")
    data["payload"][field] = True
    with pytest.raises(StateMachineError):
        world_command_from_data(data)
