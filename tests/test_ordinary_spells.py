"""Public instant spell adoption with the pinned stateless evaluator."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd5e_engine.outcome import DeathRecord
from test_combat_closure import close_command
from test_cure_wounds import BYSTANDER, NPC, SLOT, SOURCE, actor, cast_command, cure_world, pool
from test_dnd_runtime import authority

from trpg_state_machine import StateMachine, StateMachineError


def damage_command(sm, sid, package, context, *, key, spell_id="sacred-flame", slot=None):
    """Build a single-target spell intent through the public combat entry point."""
    command = cast_command(sm, sid, package, context, target=NPC, key=key)
    return replace(
        command,
        payload=CombatIntentPayload(
            intent_type="cast_spell",
            spell_id=spell_id,
            target_id=NPC,
            slot_level=slot,
            spell_source_id=SOURCE,
        ),
    )


def observe_evaluation(sm, monkeypatch):
    """Capture the accepted RE proposal without altering its result."""
    observed = []
    evaluate = sm._rules._evaluate_dnd

    async def observe(request):
        """Keep the real result for comparison to committed facts."""
        result = await evaluate(request)
        observed.append(result)
        return result

    monkeypatch.setattr(sm._rules, "_evaluate_dnd", observe)
    return observed


def assert_committed_events(sm, sid, package, context, receipt, result):
    """Compare the exact engine events linked to the successful command receipt."""
    committed = tuple(
        e.payload["engine_event"]
        for e in sm.read_events(sid, package, context=context)
        if e.event_seq in receipt.event_seqs and e.event_type == "combat.engine_event"
    )
    assert committed == tuple(event.model_dump(mode="json") for event in result.proposed_events)


def assert_npc_death(before, after, result, caster):
    """Match NPC death, its damage instance, and the new combat ledger entry."""
    assert result.status == "accepted"
    (damage,) = [e for e in result.proposed_events if e.type == "damage_applied"]
    (death,) = [e for e in result.proposed_events if e.type == "death"]
    sequence = before.combat_state.damage_instance_sequence + 1
    assert after.combat_state.damage_instance_sequence == sequence
    assert damage.damage_instance_id == f"damage:{sequence}:{caster}:{NPC}"
    assert damage.source_actor_id == caster
    assert damage.target_id == death.target_id == NPC
    assert death.reason == "damage"
    assert after.combat_state.dead_ids == before.combat_state.dead_ids | {NPC}
    assert after.combat_state.deaths_recorded == [
        *before.combat_state.deaths_recorded,
        DeathRecord(
            target_id=NPC,
            target_kind=actor(before, NPC).entity_type.lower(),
            location_id=before.scene_state.scene_id,
            reason=death.reason,
            killer_id=caster,
        ),
    ]
    assert actor(before, NPC).hp_current == 1
    assert actor(before, NPC).is_alive
    victim = actor(after, NPC)
    assert victim.hp_current == 0
    assert not victim.is_alive
    assert victim.last_damaged_by == caster
    assert victim.death_saves == actor(before, NPC).death_saves


@pytest.mark.parametrize("seed", [0, 2])
def test_save_damage_commits_or_saves_without_slot(tmp_path, context, seed):
    """Adopt either real RE save outcome and retain the prior fixed input."""
    sm, sid, package, loader, db = cure_world(
        tmp_path,
        context,
        extra_spells=("sacred-flame",),
        rng_seed=seed,
        target_hp=40,
        prepared=False,
    )
    with sm:
        command = cast_command(sm, sid, package, context, target=NPC, key=f"flame.{seed}")
        command = replace(
            command,
            payload=CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="sacred-flame",
                target_id=NPC,
                slot_level=None,
                spell_source_id=SOURCE,
            ),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        old_rng = sm.read_rng_context(sid, package, context=context)
        observed = []
        evaluate = sm._rules._evaluate_dnd

        async def observe(request):
            """Record the genuine RE outcome without changing its result."""
            result = await evaluate(request)
            observed.append(result)
            return result

        sm._rules._evaluate_dnd = observe
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        saves = [event for event in observed[0].proposed_events if event.type == "save_rolled"]
        damage = [event for event in observed[0].proposed_events if event.type == "damage_applied"]
        assert len(saves) == len(damage) == 1
        assert saves[0].succeeded is (seed == 0)
        assert saves[0].target_id == damage[0].target_id == NPC
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current - damage[0].amount
        assert (
            actor(after, command.actor_id).spell_slots
            == actor(before, command.actor_id).spell_slots
        )
        assert after.combat_state.expended_resources == before.combat_state.expended_resources
        assert sm.read_rng_context(sid, package, context=context) != old_rng
        assert sm.submit_command(command, package, context=context) == receipt
        assert actor(before, NPC).hp_current == 40
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )


def test_save_damage_commits_death_and_skips_dead_enemy(tmp_path, context, monkeypatch):
    """Adopt lethal Sacred Flame while retaining the caster's active turn."""
    sm, sid, package, loader, db = cure_world(
        tmp_path,
        context,
        extra_spells=("sacred-flame",),
        rng_seed=2,
        target_hp=1,
        second_enemy=True,
    )
    with sm:
        command = damage_command(sm, sid, package, context, key="flame.lethal")
        observed = observe_evaluation(sm, monkeypatch)
        before = sm.read_combat_snapshot(sid, package, context=context)
        assert before.combat_state.initiative_ids == (command.actor_id, NPC, BYSTANDER)
        receipt = sm.submit_command(command, package, context=context)
        (result,) = observed
        assert result.status == "accepted"
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert_npc_death(before, after, result, command.actor_id)
        assert after.combat_state.current_turn_index == before.combat_state.current_turn_index
        assert not any(event.type == "turn_started" for event in result.proposed_events)
        assert actor(after, BYSTANDER).hp_current == actor(before, BYSTANDER).hp_current
        assert actor(after, BYSTANDER).is_alive
        assert after.resource_state == before.resource_state
        assert after.combat_state.expended_resources == before.combat_state.expended_resources
        assert (
            sm.read_rng_context(sid, package, context=context).state
            == result.rng_transition.next_state
        )
        assert_committed_events(sm, sid, package, context, receipt, result)
        committed = authority(sm, sid)
        assert sm.submit_command(command, package, context=context) == receipt
        assert len(observed) == 1
        assert authority(sm, sid) == committed
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert (
            reopened.get_command_receipt(sid, command.command_id, package, context=context)
            == receipt
        )


def test_last_enemy_spell_defeat_allows_close_without_pass(tmp_path, context):
    """The current actor can close a decided encounter after a lethal spell."""
    sm, sid, package, _, _ = cure_world(
        tmp_path, context, extra_spells=("sacred-flame",), rng_seed=2, target_hp=1
    )
    with sm:
        cast = damage_command(sm, sid, package, context, key="last-enemy")
        assert sm.submit_command(cast, package, context=context).status == "committed"
        after_cast = sm.read_combat_snapshot(sid, package, context=context)
        assert NPC in after_cast.combat_state.dead_ids
        assert after_cast.combat_state.current_turn_index == 0
        close = close_command(sm, sid, package, context, "close-after-spell")
        receipt = sm.submit_command(close, package, context=context)
        assert receipt.status == "committed", receipt
        assert sm.submit_command(close, package, context=context) == receipt
        assert sm.read_session(sid, package, context=context).phase == "exploration"


@pytest.mark.parametrize(
    "seed,temp_hp,hp,fully_absorbed",
    [(2, 20, 1, True), (4, 1, 40, False)],
    ids=["absorbed", "split"],
)
def test_save_damage_applies_temporary_hp(
    tmp_path, context, monkeypatch, seed, temp_hp, hp, fully_absorbed
):
    """Adopt RE's temporary HP debit with or without an ordinary HP debit."""
    sm, sid, package, _, _ = cure_world(
        tmp_path,
        context,
        extra_spells=("sacred-flame",),
        rng_seed=seed,
        target_hp=hp,
        target_temp_hp=temp_hp,
    )
    with sm:
        command = damage_command(sm, sid, package, context, key="flame.temp")
        observed = observe_evaluation(sm, monkeypatch)
        before = sm.read_combat_snapshot(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        (result,) = observed
        (damage,) = [e for e in result.proposed_events if e.type == "damage_applied"]
        (temp,) = [op for op in result.state_delta.operations if op.kind == "actor.temp_hp_set"]
        assert result.status == "accepted"
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        target = actor(after, NPC)
        assert temp.target_id == damage.target_id == NPC
        assert damage.amount > 0
        assert temp.expected_temp_hp == actor(before, NPC).temp_hp == temp_hp
        assert actor(before, NPC).hp_current == hp
        hp_deltas = [op for op in result.state_delta.operations if op.kind == "actor.hp_delta"]
        if fully_absorbed:
            assert not hp_deltas
            assert target.temp_hp == temp.temp_hp == temp_hp - damage.amount > 0
            assert target.hp_current == hp
        else:
            (hp_delta,) = hp_deltas
            assert hp_delta.target_id == NPC
            assert target.temp_hp == temp.temp_hp == 0
            assert target.hp_current == hp_delta.resulting_hp == hp - (damage.amount - temp_hp)
            assert target.hp_current < hp
        assert target.last_damaged_by == command.actor_id
        assert target.is_alive
        assert after.combat_state.dead_ids == before.combat_state.dead_ids
        assert after.combat_state.deaths_recorded == before.combat_state.deaths_recorded
        assert all(e.type != "death" for e in result.proposed_events)
        assert_committed_events(sm, sid, package, context, receipt, result)


def test_lethal_slotted_damage_receipt_failure_rolls_back_and_replays(
    tmp_path, context, monkeypatch
):
    """Roll back a lethal cast and replay its same death, slot, RNG and events once."""
    sm, sid, package, loader, db = cure_world(
        tmp_path, context, extra_spells=("inflict-wounds",), rng_seed=2, target_hp=1
    )
    with sm:
        command = damage_command(
            sm, sid, package, context, key="inflict.rollback", spell_id="inflict-wounds", slot=1
        )
        observed = observe_evaluation(sm, monkeypatch)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        persisted = authority(sm, sid)
        events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            f"""
            CREATE TEMP TRIGGER fail_inflict_receipt BEFORE INSERT ON command_receipts
            WHEN NEW.command_id='inflict.rollback'
            BEGIN
                SELECT CASE WHEN
                    EXISTS (
                        SELECT 1 FROM characters
                        WHERE session_id=NEW.session_id AND actor_id='{NPC}' AND current_hp=0
                    ) AND EXISTS (
                        SELECT 1 FROM character_resources
                        WHERE session_id=NEW.session_id AND actor_id='{command.actor_id}'
                            AND resource_id='{SLOT}' AND current=1
                    ) AND EXISTS (
                        SELECT 1 FROM event_records
                        WHERE session_id=NEW.session_id AND source_command_id=NEW.command_id
                    )
                    THEN RAISE(ABORT, 'injected after damage, payment and events')
                    ELSE RAISE(ABORT, 'receipt attempted before domain writes')
                END;
            END
            """
        )
        with pytest.raises(StateMachineError) as failure:
            sm.submit_command(command, package, context=context)
        (first,) = observed
        assert first.status == "accepted"
        assert str(failure.value.__cause__) == "injected after damage, payment and events"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        assert authority(sm, sid) == persisted
        assert (
            sm._conn.execute(
                "SELECT current FROM character_resources "
                "WHERE session_id=? AND actor_id=? AND resource_id=?",
                (sid, command.actor_id, SLOT),
            ).fetchone()[0]
            == pool(before).current
        )

        sm._conn.execute("DROP TRIGGER fail_inflict_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        retried = observed[1]
        assert retried == first
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert_npc_death(before, after, retried, command.actor_id)
        assert after.combat_state.initiative_ids[after.combat_state.current_turn_index] == (
            command.actor_id
        )
        assert after.combat_state.round_number == before.combat_state.round_number
        assert pool(after).current == pool(before).current - 1 == 1
        assert actor(after, command.actor_id).spell_slots == {1: 1}
        assert after.combat_state.expended_resources == {command.actor_id: {"spell_slot:1": 1}}
        assert sm.read_rng_context(sid, package, context=context).state == (
            retried.rng_transition.next_state
        )
        assert_committed_events(sm, sid, package, context, receipt, retried)
        committed = authority(sm, sid)
        assert sm.submit_command(command, package, context=context) == receipt
        assert len(observed) == 2
        assert authority(sm, sid) == committed
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert reopened.submit_command(command, package, context=context) == receipt
        assert authority(reopened, sid) == committed


def test_lethal_spell_without_death_ledger_rolls_back(tmp_path, context, monkeypatch):
    """Reject an otherwise genuine lethal result with its death ledger removed."""
    sm, sid, package, _, _ = cure_world(
        tmp_path, context, extra_spells=("sacred-flame",), rng_seed=2, target_hp=1
    )
    with sm:
        command = damage_command(sm, sid, package, context, key="flame.missing_ledger")
        evaluate = sm._rules._evaluate_dnd

        async def omit_death_ledger(request):
            """Remove one required operation while preserving real events and other edits."""
            result = await evaluate(request)
            assert result.status == "accepted"
            delta = result.state_delta
            (ledger,) = [op for op in delta.operations if op.kind == "combat.death_ledger_update"]
            return result.model_copy(
                update={
                    "state_delta": delta.model_copy(
                        update={
                            "operations": tuple(op for op in delta.operations if op is not ledger)
                        }
                    )
                }
            )

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", omit_death_ledger)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        persisted = authority(sm, sid)
        with pytest.raises(StateMachineError) as failure:
            sm.submit_command(command, package, context=context)
        assert failure.value.code == "invalid_rule_result"
        assert authority(sm, sid) == persisted
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None


@pytest.mark.parametrize("seed", [0, 2])
def test_second_save_damage_spell_uses_same_adoption(tmp_path, context, seed):
    """A newly admitted save spell spends its slot without SM spell-ID dispatch."""
    sm, sid, package, _, _ = cure_world(
        tmp_path, context, extra_spells=("inflict-wounds",), rng_seed=seed, target_hp=40
    )
    with sm:
        command = cast_command(sm, sid, package, context, target=NPC, key=f"inflict.{seed}")
        command = replace(
            command,
            payload=CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="inflict-wounds",
                target_id=NPC,
                slot_level=1,
                spell_source_id=SOURCE,
            ),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        observed = []
        evaluate = sm._rules._evaluate_dnd

        async def observe(request):
            """Retain genuine RE event evidence for the proposed change."""
            result = await evaluate(request)
            observed.append(result)
            return result

        sm._rules._evaluate_dnd = observe
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        damage = [event for event in observed[0].proposed_events if event.type == "damage_applied"]
        assert len(damage) == 1
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current - damage[0].amount
        assert actor(after, command.actor_id).spell_slots == {1: 1}
        assert after.combat_state.expended_resources[command.actor_id]["spell_slot:1"] == 1
        assert sm.submit_command(command, package, context=context) == receipt


def test_damage_attribution_may_already_equal_caster(tmp_path, context):
    """A repeated source need not appear as a redundant RE field change."""
    sm, sid, package, _, _ = cure_world(
        tmp_path,
        context,
        extra_spells=("sacred-flame",),
        rng_seed=2,
        target_hp=40,
        same_attribution=True,
    )
    with sm:
        command = cast_command(sm, sid, package, context, target=NPC, key="same.source")
        command = replace(
            command,
            payload=CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="sacred-flame",
                target_id=NPC,
                spell_source_id=SOURCE,
            ),
        )
        assert sm.submit_command(command, package, context=context).status == "committed"


def test_unprepared_spell_receipt_precedes_unknown_target(tmp_path, context):
    """Retain SPELL-01's source rejection before RE inspects an invalid target."""
    sm, sid, package, _, _ = cure_world(tmp_path, context, prepared=False)
    with sm:
        command = cast_command(sm, sid, package, context, target="npc.unknown")
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "spell.source")
        assert sm.submit_command(command, package, context=context) == receipt


@pytest.mark.parametrize("field", ["source_id", "damage_instance_id"])
def test_forged_damage_provenance_rolls_back(tmp_path, context, field):
    """Uncovered stage evidence cannot relabel a spell or damage instance."""
    sm, sid, package, _, _ = cure_world(
        tmp_path, context, extra_spells=("sacred-flame",), rng_seed=2, target_hp=40
    )
    with sm:
        command = cast_command(sm, sid, package, context, target=NPC, key=f"forged.{field}")
        command = replace(
            command,
            payload=CombatIntentPayload(
                intent_type="cast_spell",
                spell_id="sacred-flame",
                target_id=NPC,
                spell_source_id=SOURCE,
            ),
        )
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        evaluate = sm._rules._evaluate_dnd

        async def forge(request):
            """Relabel one real event without changing its valid RE Delta."""
            result = await evaluate(request)
            events = list(result.proposed_events)
            index = next(i for i, event in enumerate(events) if event.type == "damage_applied")
            events[index] = events[index].model_copy(update={field: "forged"})
            return result.model_copy(update={"proposed_events": tuple(events)})

        sm._rules._evaluate_dnd = forge
        with pytest.raises(StateMachineError) as failure:
            sm.submit_command(command, package, context=context)
        assert failure.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
