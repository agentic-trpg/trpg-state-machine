"""Transaction and old-reader checks through the public Hold Person command path."""

from dataclasses import replace

import pytest
from dnd5e_engine import evaluation_delta as delta
from dnd_support import NPC
from test_hold_effect_candidate import (
    hold_attack_command,
    hold_cast_command,
    hold_world,
    pass_command,
)

from trpg_state_machine.errors import StateMachineError


def _effect_counts(sm, sid):
    """Read all authoritative effect rows without using a snapshot projection."""
    return tuple(
        sm._conn.execute(f"SELECT count(*) FROM {table} WHERE session_id=?", (sid,)).fetchone()[0]
        for table in ("effect_concentrations", "ongoing_effects")
    )


@pytest.mark.parametrize("slot_level", [2, 3])
def test_hold_late_receipt_failure_rolls_back_then_replay_and_stale_conflict(
    tmp_path, context, slot_level
):
    """A failed final receipt leaves no graph, payment or RNG; retry commits once."""
    sm, sid, package, _, _ = hold_world(tmp_path, context)
    with sm:
        command = hold_cast_command(
            sm, sid, package, context, command_id="hold.atomic", slot_level=slot_level
        )
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
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(command, package, context=context)
        assert error.value.code == "storage_error"
        assert str(error.value.__cause__) == "injected hold receipt failure"
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
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed"
        committed = sm.read_combat_snapshot(sid, package, context=context)
        committed_rng = sm.read_rng_context(sid, package, context=context)
        assert _effect_counts(sm, sid) == (1, 1)
        assert old_snapshot.effect_states == ()
        assert old_snapshot.combat_state.concentration_chain == {}
        assert old_snapshot.world_version == command.expected_world_version
        assert len(committed.effect_states) == 1
        assert committed_rng != old_rng
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) == receipt
        assert _effect_counts(sm, sid) == (1, 1)

        stale = replace(command, command_id="hold.atomic.stale")
        conflict = sm.submit_command(stale, package, context=context)
        assert (conflict.status, conflict.error_code) == ("conflict", "version_conflict")
        assert sm.get_command_receipt(sid, stale.command_id, package, context=context) == conflict
        assert sm.read_combat_snapshot(sid, package, context=context) == committed
        assert sm.read_rng_context(sid, package, context=context) == committed_rng
        assert _effect_counts(sm, sid) == (1, 1)


def test_hold_forged_graph_caster_association_is_rejected(tmp_path, context):
    """Reject a real RE graph reassigned to an unrelated actor before any write."""
    sm, sid, package, _, _ = hold_world(tmp_path, context)
    with sm:
        command = hold_cast_command(sm, sid, package, context)
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
            sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert _effect_counts(sm, sid) == (0, 0)
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None


def test_hold_forged_graph_slot_level_is_rejected(tmp_path, context):
    """A typed third-level graph cannot accompany a second-level request and payment."""
    sm, sid, package, _, _ = hold_world(tmp_path, context)
    with sm:
        command = hold_cast_command(sm, sid, package, context)
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
            sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert _effect_counts(sm, sid) == (0, 0)
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None


def test_hold_damage_and_concentration_drop_roll_back_together(tmp_path, context):
    """A late fault restores HP and concentration before one successful retry."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=9)
    with sm:
        cast = hold_cast_command(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        caster = cast.actor_id
        hp_before = next(a for a in before.character_states if a.entity_id == caster).hp_current
        assert before.effect_states[0].target_id == caster
        assert _effect_counts(sm, sid) == (1, 1)

        attack = hold_attack_command(
            sid,
            caster,
            before.world_version,
            command_id="hold.damage.atomic",
        )
        # This fires only if the attack has already lowered HP and removed the
        # caster's group, immediately before the command receipt is persisted.
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_hold_damage_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='hold.damage.atomic' AND "
            "(SELECT current_hp FROM characters WHERE session_id=NEW.session_id "
            f"AND actor_id='{caster}')<{hp_before} AND "
            "NOT EXISTS(SELECT 1 FROM effect_concentrations WHERE session_id=NEW.session_id "
            f"AND caster_id='{caster}') "
            "BEGIN SELECT RAISE(ABORT,'injected damage receipt failure'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(attack, package, context=context)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert _effect_counts(sm, sid) == (1, 1)
        assert sm.get_command_receipt(sid, attack.command_id, package, context=context) is None

        sm._conn.execute("DROP TRIGGER fail_hold_damage_receipt")
        receipt = sm.submit_command(attack, package, context=context)
        assert receipt.status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        hp_after = next(a for a in after.character_states if a.entity_id == caster).hp_current
        assert 0 < hp_after < hp_before
        assert not after.effect_states
        assert not after.combat_state.concentration_chain
        assert _effect_counts(sm, sid) == (0, 0)
        assert before.effect_states and before.combat_state.concentration_chain[caster]
        assert sm.submit_command(attack, package, context=context) == receipt


def test_hold_attack_knockout_refused_without_partial_write(tmp_path, context, monkeypatch):
    """A real zero-HP proposal is outside the adopted damage and concentration path."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=9, caster_hp=1)
    with sm:
        cast = hold_cast_command(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm.read_events(sid, package, context=context)
        attack = hold_attack_command(sid, cast.actor_id, before.world_version)
        assert _effect_counts(sm, sid) == (1, 1)

        evaluate = sm._rules._evaluate_dnd
        proposals = []

        async def observe(request):
            """Capture the real RE result while preserving normal public submission."""
            result = await evaluate(request)
            proposals.append(result)
            return result

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", observe)
        receipt = sm.submit_command(attack, package, context=context)
        (proposal,) = proposals
        assert proposal.status == "accepted"
        assert any(
            type(operation) is delta.HPDelta
            and operation.target_id == cast.actor_id
            and operation.resulting_hp == 0
            for operation in proposal.state_delta.operations
        )
        assert (receipt.status, receipt.error_code) == ("rejected", "effect.capability")
        assert receipt.world_version == before.world_version
        assert receipt.event_seqs == ()
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_events(sid, package, context=context) == before_events
        assert _effect_counts(sm, sid) == (1, 1)
        assert sm.get_command_receipt(sid, attack.command_id, package, context=context) == receipt
        assert sm.submit_command(attack, package, context=context) == receipt
        assert len(proposals) == 1


@pytest.mark.parametrize("mutation", ["cast_hp", "pass_damage", "pass_omits_turn", "plain_pass"])
def test_hold_command_rejects_out_of_scope_delta(tmp_path, context, monkeypatch, mutation):
    """A genuine proposal cannot add damage or let Pass omit the turn handoff."""
    sm, sid, package, _, _ = hold_world(tmp_path, context)
    with sm:
        cast = hold_cast_command(sm, sid, package, context)
        if mutation in {"pass_damage", "pass_omits_turn"}:
            assert sm.submit_command(cast, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm.read_events(sid, package, context=context)
        before_counts = _effect_counts(sm, sid)
        command = cast if mutation == "cast_hp" else pass_command(sid, before, "hold.delta")
        evaluate = sm._rules._evaluate_dnd

        async def forge(request):
            """Alter only the typed operations returned by the real pinned evaluator."""
            result = await evaluate(request)
            assert result.status == "accepted"
            operations = result.state_delta.operations
            if mutation == "cast_hp":
                target = next(actor for actor in before.character_states if actor.entity_id == NPC)
                operations += (
                    delta.HPDelta(
                        kind="actor.hp_delta",
                        target_id=NPC,
                        expected_hp=target.hp_current,
                        amount=-1,
                        resulting_hp=target.hp_current - 1,
                    ),
                )
            elif mutation == "pass_omits_turn":
                assert sum(type(op) is delta.TurnUpdate for op in operations) == 1
                operations = tuple(op for op in operations if type(op) is not delta.TurnUpdate)
            else:
                combat = before.combat_state
                operations += (
                    delta.DamageSequenceUpdate(
                        kind="combat.damage_sequence_update",
                        combat_id=combat.combat_id,
                        expected=combat.damage_instance_sequence,
                        value=combat.damage_instance_sequence + 1,
                    ),
                )
            state_delta = delta.StateDelta.model_validate_json(
                result.state_delta.model_copy(update={"operations": operations}).model_dump_json()
            )
            return result.model_copy(update={"state_delta": state_delta})

        with monkeypatch.context() as patch:
            patch.setattr(sm._rules, "_evaluate_dnd", forge)
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(command, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_events(sid, package, context=context) == before_events
        assert _effect_counts(sm, sid) == before_counts
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None

        assert sm.submit_command(command, package, context=context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.combat_state.turn_serial == before.combat_state.turn_serial + 1
