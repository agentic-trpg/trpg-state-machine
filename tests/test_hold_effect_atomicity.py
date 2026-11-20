"""Transaction and old-reader checks through the public Hold Person command path."""

from dataclasses import replace

import pytest
from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_state import DeathSaveRecord
from dnd_support import NPC
from test_combat_closure import close_command
from test_dnd_ledger_scope import stored_state
from test_hold_effect_candidate import (
    SECOND_TARGET,
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


def _assert_result_committed(sm, sid, package, context, before_rng, receipt, proposal):
    """Compare one public receipt, ordered rules evidence and durable RNG transition."""
    assert (receipt.status, receipt.error_code) == ("committed", None)
    events = sm.read_events(sid, package, context=context)
    engine_events = [
        event
        for event in events
        if event.source_command_id == receipt.command_id and "engine_event" in event.payload
    ]
    assert [event.payload["engine_event"] for event in engine_events] == [
        event.model_dump(mode="json") for event in proposal.proposed_events
    ]
    assert (
        tuple(event.event_seq for event in events if event.source_command_id == receipt.command_id)
        == receipt.event_seqs
    )
    after_rng = sm.read_rng_context(sid, package, context=context)
    assert after_rng.version == before_rng.version + int(proposal.rng_transition.state_changed)
    assert after_rng.state == proposal.rng_transition.next_state
    assert sm.get_command_receipt(sid, receipt.command_id, package, context=context) == receipt


@pytest.mark.parametrize("linked_target", [None, SECOND_TARGET])
def test_hold_attack_knockout_ends_exact_group_atomically(
    tmp_path, context, monkeypatch, linked_target
):
    """Commit the real attack's knockout and any bundled first save after a late retry."""
    seed = 0 if linked_target else 9
    sm, sid, package, _, db = hold_world(
        tmp_path, context, seed=seed, caster_hp=1, two_targets=linked_target is not None
    )
    with sm:
        cast = hold_cast_command(
            sm, sid, package, context, target_ids=(linked_target,) if linked_target else None
        )
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        before_events = sm.read_events(sid, package, context=context)
        persisted = stored_state(db)
        caster = cast.actor_id
        attack = hold_attack_command(sid, caster, before.world_version)
        (effect,) = before.effect_states
        identity = (
            (linked_target, "effect:paralyzed", effect.origin)
            if linked_target
            else (caster, "effect:hold-person", f"cast:hold-person:{caster}")
        )
        assert (effect.target_id, effect.id, effect.origin) == identity
        assert before.combat_state.concentration_chain[caster] == [identity]
        assert _effect_counts(sm, sid) == (1, 1)

        evaluate = sm._rules._evaluate_dnd
        proposals = []

        async def observe(request):
            """Record the fixed engine result without changing public command evaluation."""
            result = await evaluate(request)
            proposals.append(result)
            return result

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", observe)
        # Receipt insertion must observe the knockout, graph deletion and, in the
        # two-participant encounter, the first failure from the bundled TurnStart.
        failures = "IS NULL" if linked_target else "=1"
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_knockout_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='hold.attack' AND "
            "(SELECT current_hp FROM characters WHERE session_id=NEW.session_id "
            f"AND actor_id='{caster}')=0 AND "
            "(SELECT death_save_failures FROM mechanical_life WHERE session_id=NEW.session_id "
            f"AND actor_id='{caster}') {failures} AND "
            "NOT EXISTS(SELECT 1 FROM effect_concentrations WHERE session_id=NEW.session_id "
            f"AND caster_id='{caster}') AND "
            "NOT EXISTS(SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id "
            f"AND target_id='{identity[0]}' AND effect_id='{identity[1]}' "
            f"AND origin='{identity[2]}') "
            "BEGIN SELECT RAISE(ABORT,'injected knockout receipt failure'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(attack, package, context=context)
        assert len(proposals) == 1 and proposals[0].status == "accepted"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == before_rng
        assert sm.read_events(sid, package, context=context) == before_events
        assert stored_state(db) == persisted
        assert sm.get_command_receipt(sid, attack.command_id, package, context=context) is None

        sm._conn.execute("DROP TRIGGER fail_knockout_receipt")
        receipt = sm.submit_command(attack, package, context=context)
        assert proposals[0] == proposals[1]
        _assert_result_committed(sm, sid, package, context, before_rng, receipt, proposals[1])
        assert receipt.world_version == before.world_version + 1
        after = sm.read_combat_snapshot(sid, package, context=context)
        actor = next(a for a in after.character_states if a.entity_id == caster)
        assert (
            actor.hp_current,
            actor.is_alive,
            actor.last_damaged_by,
            actor.concentration_effect_id,
        ) == (0, True, NPC, None)
        assert [c.condition for c in actor.conditions] == ["unconscious"]
        assert actor.death_saves == (
            None if linked_target else DeathSaveRecord(successes=0, failures=1, is_stable=False)
        )
        state = after.combat_state
        assert (state.dead_ids, state.deaths_recorded) == (set(), [])
        assert state.damage_instance_sequence == before.combat_state.damage_instance_sequence + 1
        instance = f"damage:{state.damage_instance_sequence}:{NPC}:{caster}"
        assert state.processed_zero_hp_damage_instances == {(caster, instance)}
        assert not state.concentration_chain and not state.concentration_rounds_remaining
        assert (
            not after.effect_states
            and not state.conditions_by_effect
            and not state.effect_lifecycles
        )
        assert _effect_counts(sm, sid) == (0, 0)
        assert before.effect_states == (effect,)
        assert before.combat_state.concentration_chain[caster] == [identity]
        if linked_target:
            target = next(a for a in after.character_states if a.entity_id == linked_target)
            assert not target.conditions and target.hp_current == 40
        assert state.turn_serial == before.combat_state.turn_serial + 1
        assert state.initiative_ids[state.current_turn_index] == (linked_target or caster)
        committed = stored_state(db)
        assert sm.submit_command(attack, package, context=context) == receipt
        assert len(proposals) == 2
        assert stored_state(db) == committed


@pytest.mark.parametrize(("seed", "revived"), [(11, False), (390, True)])
def test_hold_knockout_public_pass_adopts_nonterminal_saves(
    tmp_path, context, monkeypatch, seed, revived
):
    """Adopt an ordinary success or the reviewed natural-20 restoration at TurnStart."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=seed, caster_hp=1)
    with sm:
        cast = hold_cast_command(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = sm.read_combat_snapshot(sid, package, context=context)
        attack = hold_attack_command(sid, cast.actor_id, before.world_version)
        assert sm.submit_command(attack, package, context=context).status == "committed"
        after_attack = sm.read_combat_snapshot(sid, package, context=context)
        ready = pass_command(sid, after_attack, "hold.nonterminal.ready")
        assert sm.submit_command(ready, package, context=context).status == "committed"
        before_save = sm.read_combat_snapshot(sid, package, context=context)
        before_rng = sm.read_rng_context(sid, package, context=context)
        save = pass_command(sid, before_save, "hold.nonterminal.save")

        evaluate = sm._rules._evaluate_dnd
        proposals = []

        async def observe(request):
            """Retain the real RE save so the test can match its committed evidence."""
            result = await evaluate(request)
            proposals.append(result)
            return result

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", observe)
        receipt = sm.submit_command(save, package, context=context)
        (proposal,) = proposals
        _assert_result_committed(sm, sid, package, context, before_rng, receipt, proposal)
        after = sm.read_combat_snapshot(sid, package, context=context)
        pc = next(a for a in after.character_states if a.entity_id == cast.actor_id)
        assert (pc.hp_current, pc.is_alive) == (int(revived), True)
        assert pc.death_saves == DeathSaveRecord(
            successes=0 if revived else 2, failures=0, is_stable=False
        )
        assert [c.condition for c in pc.conditions] == ["prone" if revived else "unconscious"]
        if revived:
            assert pc.conditions[0].source_entity_id == "implied:revive"
            assert pc.movement_remaining == 30
        assert not after.combat_state.deaths_recorded and not after.combat_state.dead_ids
        assert (
            after.combat_state.initiative_ids[after.combat_state.current_turn_index]
            == cast.actor_id
        )
        assert after.combat_state.turn_serial == before_save.combat_state.turn_serial + 1
        if revived:
            continuation = pass_command(sid, after, "hold.revived.pass")
            assert continuation.actor_id == cast.actor_id
            rng = sm.read_rng_context(sid, package, context=context)
            assert sm.submit_command(continuation, package, context=context).status == "committed"
            assert sm.read_rng_context(sid, package, context=context) == rng


def test_hold_knockout_public_pass_reaches_terminal_death_atomically(
    tmp_path, context, monkeypatch
):
    """Pass away from dying, then adopt the next RE save and its unique death record."""
    sm, sid, package, _, db = hold_world(tmp_path, context, seed=9, caster_hp=1)
    with sm:
        cast = hold_cast_command(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        caster = cast.actor_id
        before = sm.read_combat_snapshot(sid, package, context=context)
        attack = hold_attack_command(sid, caster, before.world_version)
        assert sm.submit_command(attack, package, context=context).status == "committed"
        dying = sm.read_combat_snapshot(sid, package, context=context)
        dying_rng = sm.read_rng_context(sid, package, context=context)
        ready = pass_command(sid, dying, "hold.dying.pass")
        assert ready.actor_id == caster
        ready_receipt = sm.submit_command(ready, package, context=context)
        assert ready_receipt.status == "committed"
        after_ready = sm.read_combat_snapshot(sid, package, context=context)
        assert after_ready.combat_state.turn_serial == dying.combat_state.turn_serial + 1
        assert sm.read_rng_context(sid, package, context=context) == dying_rng
        assert next(
            a for a in after_ready.character_states if a.entity_id == caster
        ).death_saves == DeathSaveRecord(successes=0, failures=1, is_stable=False)

        save = pass_command(sid, after_ready, "hold.terminal.pass")
        assert save.actor_id == NPC
        before_events = sm.read_events(sid, package, context=context)
        persisted = stored_state(db)
        evaluate = sm._rules._evaluate_dnd
        proposals = []

        async def observe(request):
            """Record the genuine save outcome before the SQLite fault and retry."""
            result = await evaluate(request)
            proposals.append(result)
            return result

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", observe)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_save_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='hold.terminal.pass' AND "
            "(SELECT is_alive FROM mechanical_life WHERE session_id=NEW.session_id "
            f"AND actor_id='{caster}')=0 AND "
            "EXISTS(SELECT 1 FROM combats, json_each(combats.state_json, '$.dead_ids') "
            f"WHERE session_id=NEW.session_id AND value='{caster}') "
            "BEGIN SELECT RAISE(ABORT,'injected dying save receipt failure'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(save, package, context=context)
        assert len(proposals) == 1 and proposals[0].status == "accepted"
        assert sm.read_combat_snapshot(sid, package, context=context) == after_ready
        assert sm.read_rng_context(sid, package, context=context) == dying_rng
        assert sm.read_events(sid, package, context=context) == before_events
        assert stored_state(db) == persisted
        assert sm.get_command_receipt(sid, save.command_id, package, context=context) is None

        sm._conn.execute("DROP TRIGGER fail_save_receipt")
        receipt = sm.submit_command(save, package, context=context)
        assert proposals[0] == proposals[1]
        _assert_result_committed(sm, sid, package, context, dying_rng, receipt, proposals[1])
        after = sm.read_combat_snapshot(sid, package, context=context)
        actor = next(a for a in after.character_states if a.entity_id == caster)
        assert (actor.hp_current, actor.is_alive) == (0, False)
        assert actor.death_saves == DeathSaveRecord(successes=0, failures=3, is_stable=False)
        state = after.combat_state
        assert state.dead_ids == {caster}
        (record,) = state.deaths_recorded
        assert (
            record.target_id,
            record.target_kind,
            record.location_id,
            record.reason,
            record.killer_id,
        ) == (caster, "character", after.scene_state.scene_id, "death_saves", None)
        assert state.turn_serial == after_ready.combat_state.turn_serial + 1
        assert (
            state.processed_zero_hp_damage_instances
            == dying.combat_state.processed_zero_hp_damage_instances
        )
        committed = stored_state(db)
        assert sm.submit_command(save, package, context=context) == receipt
        assert len(proposals) == 2 and stored_state(db) == committed

        close = close_command(sm, sid, package, context, "hold.knockout.close")
        xp = sm.read_session(sid, package, context=context).character.xp
        clock = sm.world_time.read(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        closure_receipt = sm.submit_command(close, package, context=context)
        _assert_result_committed(sm, sid, package, context, rng, closure_receipt, proposals[2])
        (closure,) = proposals[2].state_delta.operations
        assert closure.reason == "defeat_tpk"
        assert closure.xp_increments == {}
        assert closure.historical.residual_hp == {caster: 0}
        assert sm.read_combat_snapshot(sid, package, context=context).combat_state.ended
        final = sm.read_session(sid, package, context=context)
        assert (final.phase, final.character.xp) == ("exploration", xp)
        assert sm.world_time.read(sid, package, context=context) == clock
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.submit_command(close, package, context=context) == closure_receipt
        assert len(proposals) == 3


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
