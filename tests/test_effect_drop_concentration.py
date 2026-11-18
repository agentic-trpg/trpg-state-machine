"""Manual concentration release through the public combat command."""

from dataclasses import replace

import pytest
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from dnd_support import NPC
from test_hold_effect_atomicity import _effect_counts
from test_hold_effect_candidate import hold_cast_command, hold_world
from test_storage_conditions import _condition

from trpg_state_machine import StateMachine, StateMachineError


def _held_target(snapshot):
    """Read the target actor from the frozen combat snapshot."""
    return next(actor for actor in snapshot.character_states if actor.entity_id == NPC)


def _cast_then_prepare_drop(sm, sid, package, context):
    """Establish a real Hold Person graph and prepare its caster's release."""
    cast = hold_cast_command(sm, sid, package, context)
    assert sm.submit_command(cast, package, context=context).status == "committed"
    version = sm.read_session(sid, package, context=context).world_version
    return replace(
        cast,
        command_id="hold.drop",
        payload=CombatIntentPayload(intent_type="drop_concentration"),
        expected_world_version=version,
    )


@pytest.mark.parametrize("independent_source", [False, True])
def test_drop_concentration_outside_caster_turn(tmp_path, context, independent_source):
    """Release only the caster's graph, without using any part of another actor's turn."""
    sm, sid, package, loader, db = hold_world(tmp_path, context)
    with sm:
        command = _cast_then_prepare_drop(sm, sid, package, context)
        if independent_source:
            # There is no public command to apply a standalone condition in this fixture.
            with sm._store.transaction(write=True):
                sm._characters.add_condition(sid, NPC, _condition("paralyzed"))
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        tick = sm.read_time_sync(sid, package, context=context).committed_tick
        combat = before.combat_state
        assert combat.initiative_ids[combat.current_turn_index] == NPC
        (effect,) = before.effect_states
        assert combat.concentration_chain[command.actor_id] == [
            (effect.target_id, effect.id, effect.origin)
        ]
        target = _held_target(before)
        assert [cond.condition for cond in target.conditions] == (
            ["paralyzed", "paralyzed"] if independent_source else ["paralyzed"]
        )
        assert target.conditions[-1].source_effect_id == effect.id
        assert target.movement_remaining == 0
        assert _effect_counts(sm, sid) == (1, 1)

        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.world_version == before.world_version + 1 == receipt.world_version
        assert not after.effect_states
        assert after.combat_state == combat.model_copy(
            update={
                "concentration_chain": {},
                "concentration_rounds_remaining": {},
                "conditions_by_effect": (),
                "effect_lifecycles": (),
            }
        )
        actors = {actor.entity_id: actor for actor in after.character_states}
        for actor in before.character_states:
            expected = actor
            if actor.entity_id == NPC:
                movement_remaining = 0
                if not independent_source:
                    ledger = combat.movement_ledgers[NPC]
                    assert ledger.active_mode == "walk"
                    movement_remaining = ledger.remaining(actor.base_speed)
                    assert movement_remaining > 0
                expected = actor.model_copy(
                    update={
                        "conditions": target.conditions[:-1],
                        "movement_remaining": movement_remaining,
                    }
                )
            elif actor.entity_id == command.actor_id:
                assert actor.concentration_effect_id
                expected = actor.model_copy(update={"concentration_effect_id": None})
            assert actors[actor.entity_id] == expected
        assert after.resource_state == before.resource_state
        assert sm.read_time_sync(sid, package, context=context).committed_tick == tick
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert _effect_counts(sm, sid) == (0, 0)
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) == receipt
        assert sm.submit_command(command, package, context=context) == receipt
        assert sm.read_combat_snapshot(sid, package, context=context) == after
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert reopened.read_rng_context(sid, package, context=context) == rng


def test_drop_concentration_denies_other_player_and_unauthorized_npc(tmp_path, context):
    """A release obeys the session owner, NPC control and optimistic version checks."""
    sm, sid, package, _, _ = hold_world(tmp_path, context)
    with sm:
        command = _cast_then_prepare_drop(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        wrong_player = replace(context, principal_id="authenticated:bob", player_id="player.bob")
        npc_command = replace(command, actor_id=NPC, command_id="hold.drop.npc")
        for attempt, caller in (
            (command, wrong_player),
            (npc_command, replace(context, controlled_npc_ids=())),
        ):
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(attempt, package, context=caller)
            assert error.value.code == "unauthorized"
            assert sm.get_command_receipt(sid, attempt.command_id, package, context=context) is None
        stale = replace(
            command, command_id="hold.drop.stale", expected_world_version=before.world_version - 1
        )
        conflict = sm.submit_command(stale, package, context=context)
        assert (conflict.status, conflict.error_code) == ("conflict", "version_conflict")
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert _effect_counts(sm, sid) == (1, 1)


def test_drop_concentration_late_receipt_failure_rolls_back(tmp_path, context):
    """A final receipt failure restores the released graph and permits a retry."""
    sm, sid, package, loader, db = hold_world(tmp_path, context)
    with sm:
        command = _cast_then_prepare_drop(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_drop_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='hold.drop' AND "
            "NOT EXISTS(SELECT 1 FROM effect_concentrations WHERE session_id=NEW.session_id) AND "
            "NOT EXISTS(SELECT 1 FROM ongoing_effects WHERE session_id=NEW.session_id) "
            "BEGIN SELECT RAISE(ABORT,'injected drop receipt failure'); END"
        )
        with pytest.raises(StateMachineError):
            sm.submit_command(command, package, context=context)
        assert _effect_counts(sm, sid) == (1, 1)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, command.command_id, package, context=context) is None
        with StateMachine(db, dnd_loader=loader) as reopened:
            assert reopened.read_combat_snapshot(sid, package, context=context) == before

        sm._conn.execute("DROP TRIGGER fail_drop_receipt")
        receipt = sm.submit_command(command, package, context=context)
        assert receipt.status == "committed", receipt
        assert _effect_counts(sm, sid) == (0, 0)
        assert before.effect_states
        assert sm.submit_command(command, package, context=context) == receipt
