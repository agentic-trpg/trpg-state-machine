"""Exercise a spell hit rider through public cast, walking and Pass commands."""

from dataclasses import replace

import pytest
from dnd5e_engine import evaluation_delta as delta
from item_support import NPC
from test_cure_wounds import actor
from test_dnd_runtime import authority
from test_hold_effect_candidate import pass_command
from test_ordinary_spells import assert_committed_events, observe_evaluation
from test_spell_attack_cast import OTHER_ENEMY, cast_command
from test_spell_attack_cast import open_spell_combat as open_spell_combat
from test_timed_spell_cast import move_command

from trpg_state_machine import StateMachine, StateMachineError

RAY = "ray-of-frost"


def effect_rows(sm, sid):
    """Read both effect-owned tables from SQLite after a public operation."""
    return tuple(
        tuple(
            tuple(row)
            for row in sm._conn.execute(f"SELECT * FROM {table} WHERE session_id=?", (sid,))
        )
        for table in ("ongoing_effects", "effect_concentrations")
    )


@pytest.mark.parametrize("other_enemy_turn", [False, True], ids=["target-next", "target-later"])
def test_hit_slow_walk_and_expiry_at_casters_start(
    open_spell_combat, context, monkeypatch, other_enemy_turn
):
    """Link a persisted hit effect to ordinary movement and the next caster START."""
    sm, sid, package, loader, db = open_spell_combat(
        temp_hp=0, extra_spells=(RAY,), walking=True, other_enemy_turn=other_enemy_turn
    )
    with sm:
        command = cast_command(sm, sid, package, context, RAY)
        observed = observe_evaluation(sm, monkeypatch)
        before = sm.read_combat_snapshot(sid, package, context=context)
        original_rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        result = observed[-1]
        assert_committed_events(sm, sid, package, context, receipt, result)
        hit = sm.read_combat_snapshot(sid, package, context=context)
        (effect,) = hit.effect_states
        (clock,) = hit.combat_state.effect_lifecycles
        (lineage,) = hit.combat_state.conditions_by_effect
        (creation,) = [
            op for op in result.state_delta.operations if type(op) is delta.CombatEffectCreate
        ]
        (damage,) = [event for event in result.proposed_events if event.type == "damage_applied"]
        state = hit.combat_state
        assert state.initiative_ids[state.current_turn_index] == (
            OTHER_ENEMY if other_enemy_turn else NPC
        )
        assert effect == creation.effect and effect.target_id == NPC
        assert clock == creation.lifecycle and lineage == creation.lineage and not lineage.statuses
        assert clock.state.expiry_actor_id == command.actor_id
        assert clock.state.applied_turn_serial == before.combat_state.turn_serial
        assert clock.state.application.source_slug == RAY
        assert clock.state.application.spec.expiry_boundary == "source_next_turn_start"
        assert actor(hit, NPC).hp_current == actor(before, NPC).hp_current - damage.amount
        assert damage.damage_type == "cold"
        assert actor(hit, NPC).movement_remaining == actor(hit, NPC).base_speed - 10 == 20
        assert (
            actor(hit, command.actor_id).spell_slots == actor(before, command.actor_id).spell_slots
        )
        assert state.expended_resources == before.combat_state.expended_resources
        assert sm.read_rng_context(sid, package, context=context) != original_rng
        assert sm.submit_command(command, package, context=context) == receipt
        assert effect_rows(sm, sid)[0][0][1:4] == creation.identity
        assert effect_rows(sm, sid)[1] == ()
        assert not before.effect_states and actor(before, NPC).hp_current == 40
        with StateMachine(db, dnd_loader=loader) as reopened:
            assert reopened.read_combat_snapshot(sid, package, context=context) == hit

        if other_enemy_turn:
            earlier = pass_command(sid, hit, "ray.before-target")
            receipt = sm.submit_command(earlier, package, context=context)
            assert receipt.status == "committed", receipt
            assert earlier.actor_id == OTHER_ENEMY
            assert not any(e.type == "effect_expired" for e in observed[-1].proposed_events)
            hit = sm.read_combat_snapshot(sid, package, context=context)
            assert hit.effect_states == (effect,) and hit.combat_state.effect_lifecycles == (clock,)
            assert actor(hit, NPC).movement_remaining == 20

        movement = sm, sid, package, context
        longer = move_command(movement, "11,0", "ray.walk-25")
        refused = sm.submit_command(longer, package, context=context)
        assert (refused.status, refused.error_code) == ("rejected", "movement.insufficient")
        assert sm.read_combat_snapshot(sid, package, context=context) == hit
        walk = sm.submit_command(
            move_command(movement, "10,0", "ray.walk-20"), package, context=context
        )
        assert walk.status == "committed", walk
        moved = sm.read_combat_snapshot(sid, package, context=context)
        assert moved.combat_state.actor_zone[NPC] == "10,0"
        assert moved.combat_state.movement_ledgers[NPC].spent_ft == 20
        assert actor(moved, NPC).movement_remaining == 0
        assert moved.effect_states == (effect,)

        end_target = pass_command(sid, moved, "ray.caster-next-start")
        receipt = sm.submit_command(end_target, package, context=context)
        assert receipt.status == "committed", receipt
        expired = sm.read_combat_snapshot(sid, package, context=context)
        assert effect_rows(sm, sid) == ((), ())
        assert not expired.effect_states
        assert not expired.combat_state.effect_lifecycles
        assert not expired.combat_state.conditions_by_effect
        assert expired.combat_state.turn_serial == clock.state.applied_turn_serial + 2 + int(
            other_enemy_turn
        )
        assert_committed_events(sm, sid, package, context, receipt, observed[-1])
        events = observed[-1].proposed_events
        (index,) = [i for i, event in enumerate(events) if event.type == "effect_expired"]
        assert events[1].type == "turn_phase" and events[1].phase == "turn_end"
        assert events[1].actor_id == NPC
        assert events[index - 2].type == "turn_started"
        assert events[index - 2].actor_id == command.actor_id
        assert events[index - 1].type == "turn_phase" and events[index - 1].phase == "turn_start"
        assert events[index - 1].actor_id == command.actor_id
        assert events[index].reason == "duration" and events[index].origin == effect.origin
        assert moved.effect_states == (effect,) and moved.combat_state.effect_lifecycles == (clock,)

        reset = expired
        for i in range(1 + int(other_enemy_turn)):
            receipt = sm.submit_command(
                pass_command(sid, reset, f"ray.target-next-start-{i}"), package, context=context
            )
            assert receipt.status == "committed", receipt
            reset = sm.read_combat_snapshot(sid, package, context=context)
        assert actor(reset, NPC).movement_remaining == 30
        restored = sm.submit_command(
            move_command(movement, "15,0", "ray.walk-25-restored"), package, context=context
        )
        assert restored.status == "committed", restored


def test_miss_commits_cast_without_effect_or_damage(open_spell_combat, context, monkeypatch):
    """A real miss spends the Action and roll with no effect or target HP edit."""
    sm, sid, package, _, _ = open_spell_combat(seed=31, temp_hp=0, extra_spells=(RAY,))
    with sm:
        command = cast_command(sm, sid, package, context, RAY)
        observed = observe_evaluation(sm, monkeypatch)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        result = observed[-1]
        (roll,) = [event for event in result.proposed_events if event.type == "attack_rolled"]
        assert not roll.is_hit
        assert not any(
            e.type in {"effect_applied", "damage_applied"} for e in result.proposed_events
        )
        assert_committed_events(sm, sid, package, context, receipt, result)
        assert effect_rows(sm, sid) == ((), ())
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert not after.effect_states
        assert not after.combat_state.effect_lifecycles
        assert actor(after, NPC).hp_current == actor(before, NPC).hp_current
        assert actor(after, NPC).movement_remaining == 30
        assert actor(after, command.actor_id).action_taken_this_turn
        assert sm.read_rng_context(sid, package, context=context) != rng


def test_speed_effect_with_unrelated_overlap_stays_unsupported(open_spell_combat, context):
    """An existing speed effect admits only the graph that Pass and walk can adopt."""
    sm, sid, package, _, _ = open_spell_combat(temp_hp=0, extra_spells=(RAY,), walking=True)
    with sm:
        command = cast_command(sm, sid, package, context, RAY)
        assert sm.submit_command(command, package, context=context).status == "committed"
        hit = sm.read_combat_snapshot(sid, package, context=context)
        (effect,) = hit.effect_states
        unrelated = effect.model_dump(mode="json")
        unrelated.update(id="effect:unrelated", origin="fixture", lifecycle=None, changes=[])
        with sm._store.transaction(write=True):
            sm._characters.effects.add_effect(sid, unrelated)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        stored = effect_rows(sm, sid)
        assert len(before.effect_states) == 2
        inputs = (
            (pass_command(sid, before, "ray.overlap-pass"), "turn.capability"),
            (
                move_command((sm, sid, package, context), "7,0", "ray.overlap-walk"),
                "movement.capability",
            ),
        )
        for command, code in inputs:
            receipt = sm.submit_command(command, package, context=context)
            assert (receipt.status, receipt.error_code) == ("rejected", code), receipt
            assert receipt.event_seqs == ()
            assert sm.read_combat_snapshot(sid, package, context=context) == before
            assert sm.read_rng_context(sid, package, context=context) == rng
            assert effect_rows(sm, sid) == stored


def test_nonfinal_lethal_rider_requires_supported_pass(open_spell_combat, context, monkeypatch):
    """Reject an accepted RE hit whose clock could not advance by public Pass."""
    sm, sid, package, _, _ = open_spell_combat(
        hp=1, temp_hp=0, extra_spells=(RAY,), walking=True, other_enemy_turn=True
    )
    with sm:
        command = cast_command(sm, sid, package, context, RAY)
        observed = observe_evaluation(sm, monkeypatch)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        receipt = sm.submit_command(command, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "spell.capability")
        assert receipt.event_seqs == ()
        assert observed[-1].status == "accepted"
        assert any(
            type(op) is delta.CombatEffectCreate for op in observed[-1].state_delta.operations
        )
        assert any(event.type == "death" for event in observed[-1].proposed_events)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert effect_rows(sm, sid) == ((), ())
        assert sm.submit_command(command, package, context=context) == receipt


@pytest.mark.parametrize("removal", [False, True], ids=["create", "expire"])
def test_hit_effect_rolls_back_at_receipt(open_spell_combat, context, removal):
    """Inject a real SQL failure after the effect and related facts were written."""
    sm, sid, package, _, _ = open_spell_combat(temp_hp=0, extra_spells=(RAY,))
    with sm:
        command = cast_command(sm, sid, package, context, RAY)
        if removal:
            assert sm.submit_command(command, package, context=context).status == "committed"
            snapshot = sm.read_combat_snapshot(sid, package, context=context)
            command = pass_command(sid, snapshot, "ray.rollback-expire")
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        stored = authority(sm, sid), effect_rows(sm, sid)
        condition = "NOT EXISTS" if removal else "EXISTS"
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_speed_receipt BEFORE INSERT ON command_receipts "
            f"WHEN NEW.command_id='{command.command_id}' AND {condition} "
            "(SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id) "
            "BEGIN SELECT RAISE(ABORT,'injected after speed effect write'); END"
        )
        with pytest.raises(StateMachineError) as failure:
            sm.submit_command(command, package, context=context)
        assert failure.value.code == "storage_error"
        assert "injected after speed effect write" in str(failure.value.__cause__)
        assert (authority(sm, sid), effect_rows(sm, sid)) == stored
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        sm._conn.execute("DROP TRIGGER fail_speed_receipt")
        assert sm.submit_command(command, package, context=context).status == "committed"
        assert bool(effect_rows(sm, sid)[0]) is not removal


@pytest.mark.parametrize("omit", ["effect_applied", "combat.effect_create"])
def test_cast_requires_complete_hit_effect_evidence(open_spell_combat, context, monkeypatch, omit):
    """A distorted accepted result discards the whole hit and consumes no RNG."""
    sm, sid, package, _, _ = open_spell_combat(temp_hp=0, extra_spells=(RAY,))
    with sm:
        command = replace(cast_command(sm, sid, package, context, RAY), command_id="ray.forge")
        before = sm.read_combat_snapshot(sid, package, context=context)
        stored = authority(sm, sid), effect_rows(sm, sid)
        evaluate = sm._rules._evaluate_dnd

        async def incomplete(request):
            """Drop just one side of the real result's effect evidence."""
            result = await evaluate(request)
            return result.model_copy(
                update={
                    "proposed_events": tuple(e for e in result.proposed_events if e.type != omit),
                    "state_delta": result.state_delta.model_copy(
                        update={
                            "operations": tuple(
                                op for op in result.state_delta.operations if op.kind != omit
                            )
                        }
                    ),
                }
            )

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", incomplete)
        with pytest.raises(StateMachineError) as failure:
            sm.submit_command(command, package, context=context)
        assert failure.value.code == "invalid_rule_result"
        assert (authority(sm, sid), effect_rows(sm, sid)) == stored
        assert sm.read_combat_snapshot(sid, package, context=context) == before
