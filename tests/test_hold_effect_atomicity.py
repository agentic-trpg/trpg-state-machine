"""Transaction and old-reader checks for the opt-in Hold Person candidate."""

from dataclasses import replace

import pytest
from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import NPC
from test_hold_effect_candidate import candidate_submit, hold_world

from trpg_state_machine import TypedCommand
from trpg_state_machine.errors import StateMachineError


def _cast(sm, sid, package, context):
    """Bind one cast to the current world version before rule evaluation."""
    state = sm.read_session(sid, package, context=context)
    return TypedCommand(
        sid,
        "hold.atomic",
        state.pc_actor_id,
        "combat.intent",
        CombatIntentPayload(
            intent_type="cast_spell",
            spell_id="hold-person",
            target_id=NPC,
            slot_level=2,
            spell_source_id="class:wizard",
        ),
        state.world_version,
    )


def _effect_counts(sm, sid):
    """Read all authoritative effect rows without using a snapshot projection."""
    return tuple(
        sm._conn.execute(f"SELECT count(*) FROM {table} WHERE session_id=?", (sid,)).fetchone()[0]
        for table in ("effect_concentrations", "ongoing_effects")
    )


def test_hold_late_receipt_failure_rolls_back_then_replay_and_stale_conflict(tmp_path, context):
    """A failed final receipt leaves no graph, payment or RNG; retry commits once."""
    sm, sid, package, _, _ = hold_world(tmp_path, context)
    with sm:
        command = _cast(sm, sid, package, context)
        old_snapshot = sm.read_combat_snapshot(sid, package, context=context)
        old_rng = sm.read_rng_context(sid, package, context=context)
        old_effect_rows = _effect_counts(sm, sid)
        old_events = sm._conn.execute(
            "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
        ).fetchone()[0]
        assert old_effect_rows == (0, 0)

        # The WHEN clause proves the injected failure occurs after both effect
        # rows have been written, at the final receipt boundary.
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_hold_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='hold.atomic' AND "
            "(SELECT count(*) FROM effect_concentrations WHERE session_id=NEW.session_id)=1 "
            "AND (SELECT count(*) FROM ongoing_effects WHERE session_id=NEW.session_id)=1 "
            "BEGIN SELECT RAISE(ABORT,'injected hold receipt failure'); END"
        )
        with pytest.raises(StateMachineError):
            candidate_submit(sm, command, package, context)
        assert _effect_counts(sm, sid) == old_effect_rows
        assert sm.read_combat_snapshot(sid, package, context=context) == old_snapshot
        assert sm.read_rng_context(sid, package, context=context) == old_rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert (
            sm._conn.execute(
                "SELECT count(*) FROM event_records WHERE session_id=?", (sid,)
            ).fetchone()[0]
            == old_events
        )

        sm._conn.execute("DROP TRIGGER fail_hold_receipt")
        receipt = candidate_submit(sm, command, package, context)
        assert receipt.status == "committed"
        committed = sm.read_combat_snapshot(sid, package, context=context)
        committed_rng = sm.read_rng_context(sid, package, context=context)
        assert _effect_counts(sm, sid) == (1, 1)
        assert old_snapshot.effect_states == ()
        assert old_snapshot.combat_state.concentration_chain == {}
        assert old_snapshot.world_version == command.expected_world_version
        assert len(committed.effect_states) == 1
        assert committed_rng != old_rng
        assert candidate_submit(sm, command, package, context) == receipt
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) == receipt
        assert _effect_counts(sm, sid) == (1, 1)

        stale = replace(command, command_id="hold.atomic.stale")
        conflict = candidate_submit(sm, stale, package, context)
        assert (conflict.status, conflict.error_code) == ("conflict", "version_conflict")
        assert sm.get_command_receipt(sid, stale.command_id, package, context=context) == conflict
        assert sm.read_combat_snapshot(sid, package, context=context) == committed
        assert sm.read_rng_context(sid, package, context=context) == committed_rng
        assert _effect_counts(sm, sid) == (1, 1)


def test_hold_forged_graph_caster_association_is_rejected(tmp_path, context):
    """Reject a real RE graph reassigned to an unrelated actor before any write."""
    sm, sid, package, _, _ = hold_world(tmp_path, context)
    with sm:
        command = _cast(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        original = sm._rules._evaluate_dnd

        async def forged(request):
            """Retain the accepted typed result but misbind its graph to the target."""
            result = await original(request)
            assert result.status == "accepted"
            operations = list(result.state_delta.operations)
            index = next(
                i
                for i, operation in enumerate(operations)
                if type(operation) is delta.HoldPersonGraphCreate
            )
            operations[index] = operations[index].model_copy(update={"source_id": NPC})
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    )
                }
            )

        sm._rules._evaluate_dnd = forged
        with pytest.raises(StateMachineError) as error:
            candidate_submit(sm, command, package, context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert _effect_counts(sm, sid) == (0, 0)
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None


def test_hold_forged_graph_slot_level_is_rejected(tmp_path, context):
    """A typed third-level graph cannot accompany a second-level request and payment."""
    sm, sid, package, _, _ = hold_world(tmp_path, context)
    with sm:
        command = _cast(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        original = sm._rules._evaluate_dnd

        async def forged(request):
            """Round-trip the changed graph through the actual RE DTO schema."""
            result = await original(request)
            assert result.status == "accepted"
            operations = []
            for operation in result.state_delta.operations:
                if type(operation) is delta.HoldPersonGraphCreate:
                    assert operation.slot_level == 2
                    changed = operation.model_copy(update={"slot_level": 3})
                    operation = delta.HoldPersonGraphCreate.model_validate_json(
                        changed.model_dump_json()
                    )
                operations.append(operation)
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    )
                }
            )

        sm._rules._evaluate_dnd = forged
        with pytest.raises(StateMachineError) as error:
            candidate_submit(sm, command, package, context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert _effect_counts(sm, sid) == (0, 0)
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
