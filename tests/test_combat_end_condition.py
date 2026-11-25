"""Configured encounter closure through trusted dynamic public commands."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatClosePayload, CombatIntentPayload
from dnd_support import NPC, create
from modern_support import modern_loader
from test_combat_start import opening_data, start_command

from trpg_state_machine import StateMachine, StateMachineError, TypedCommand
from trpg_state_machine.package import _validate


def _close(sm, sid, package, host, key):
    session = sm.read_session(sid, package, context=host)
    return TypedCommand(
        sid,
        key,
        session.pc_actor_id,
        "combat.close",
        CombatClosePayload(kind="combat.close"),
        session.world_version,
    )


def test_registered_end_condition_is_fixed_and_uses_committed_turn(tmp_path, package_data, context):
    assets = modern_loader()
    package = _validate(opening_data(package_data, assets))
    host = replace(context, can_start_combat=True)
    seen = []

    def after_npc_turn(snapshot):
        seen.append((snapshot.combat_state.combat_id, snapshot.combat_state.current_turn_index))
        return snapshot.combat_state.current_turn_index == 1

    path = tmp_path / "configured.db"
    with StateMachine(
        path, dnd_loader=assets, combat_end_conditions={"test.after_npc": after_npc_turn}
    ) as sm:
        sid = create(sm, package, host)
        unknown = start_command(sm, sid, package, host, "unknown", end_condition_id="test.absent")
        assert (
            sm.submit_command(unknown, package, context=host).error_code == "unknown_end_condition"
        )
        opening = start_command(sm, sid, package, host, end_condition_id="test.after_npc")
        assert sm.submit_command(opening, package, context=host).status == "committed"
        combat = sm.read_combat_snapshot(sid, package, context=host)
        combat_id = combat.combat_state.combat_id
        assert sm._combat_records.active_end_condition(sid) == (combat_id, "test.after_npc")
        unmet = sm.submit_command(_close(sm, sid, package, host, "unmet"), package, context=host)
        assert unmet.error_code == "closure.nonterminal"
        assert not sm.read_combat_snapshot(sid, package, context=host).combat_state.ended
        pass_command = TypedCommand(
            sid,
            "npc.pass",
            NPC,
            "combat.intent",
            CombatIntentPayload(intent_type="pass", source_id=NPC),
            sm.read_session(sid, package, context=host).world_version,
        )
        assert sm.submit_command(pass_command, package, context=host).status == "committed"
        close = _close(sm, sid, package, host, "condition.close")
        xp_before = sm.read_session(sid, package, context=host).character.xp
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_condition_receipt BEFORE INSERT ON command_receipts "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(close, package, context=host)
        assert error.value.code == "storage_error"
        assert not sm.read_combat_snapshot(sid, package, context=host).combat_state.ended
        assert sm.get_command_receipt(sid, close.command_id, package, context=host) is None
        sm._conn.execute("DROP TRIGGER fail_condition_receipt")
        receipt = sm.submit_command(close, package, context=host)
        assert receipt.status == "committed"
        assert sm.read_session(sid, package, context=host).phase == "exploration"
        assert sm.read_session(sid, package, context=host).character.xp == xp_before
        assert sm.submit_command(close, package, context=host) == receipt
        ended_event = sm.read_events(sid, package, context=host)[-1].payload["engine_event"]
        assert ended_event["reason"] == "forced"
        assert ended_event["condition_id"] == "test.after_npc"
        assert seen == [(combat_id, 0), (combat_id, 1), (combat_id, 1)]
    with StateMachine(
        path, dnd_loader=assets, combat_end_conditions={"test.after_npc": after_npc_turn}
    ) as reopened:
        assert reopened.get_command_receipt(sid, close.command_id, package, context=host) == receipt
        assert reopened.read_session(sid, package, context=host).phase == "exploration"
        second = start_command(reopened, sid, package, host, "second")
        assert reopened.submit_command(second, package, context=host).status == "committed"
        assert reopened._combat_records.active_end_condition(sid)[1] is None
        ended_events = [
            event.payload["engine_event"]
            for event in reopened.read_events(sid, package, context=host)
            if event.payload.get("engine_event", {}).get("reason") == "forced"
        ]
        assert ended_events[-1]["condition_id"] == "test.after_npc"
