"""Public Hold Person adoption for the pinned engine's named target list."""

from dataclasses import replace

import pytest
from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_contracts import CombatIntentPayload
from item_support import NPC
from test_hold_effect_atomicity import _effect_counts
from test_hold_effect_candidate import (
    SECOND_TARGET,
    hold_attack_command,
    hold_cast_command,
    hold_world,
    pass_command,
)

from trpg_state_machine import StateMachine, StateMachineError


def _cast(sm, sid, package, context, *, slot_level=3, targets=(NPC, SECOND_TARGET)):
    """Construct a named-target command through the same fixture as single targets."""
    return hold_cast_command(sm, sid, package, context, slot_level=slot_level, target_ids=targets)


def _pass_cast_turn(sm, sid, package, context, cast):
    """End the caster's paid turn before testing an NPC turn or release."""
    snapshot = sm.read_combat_snapshot(sid, package, context=context)
    assert (
        snapshot.combat_state.initiative_ids[snapshot.combat_state.current_turn_index]
        == cast.actor_id
    )
    assert snapshot.combat_state.concentration_rounds_remaining == {cast.actor_id: 10}
    turn = pass_command(sid, snapshot, f"{cast.command_id}.pass")
    assert sm.submit_command(turn, package, context=context).status == "committed"
    after = sm.read_combat_snapshot(sid, package, context=context)
    assert after.combat_state.initiative_ids[after.combat_state.current_turn_index] == NPC
    assert after.combat_state.concentration_rounds_remaining == {cast.actor_id: 9}
    return after


def _engine_events(sm, sid, package, context, receipt):
    """Read the durable mechanical event evidence for exactly one receipt."""
    events = [
        event
        for event in sm.read_events(sid, package, context=context)
        if event.source_command_id == receipt.command_id
    ]
    assert tuple(event.event_seq for event in events) == receipt.event_seqs
    return [event.payload["engine_event"] for event in events if "engine_event" in event.payload]


@pytest.mark.parametrize(
    ("seed", "targets", "failed_targets"),
    [
        (0, (NPC, SECOND_TARGET), (NPC, SECOND_TARGET)),
        (0, (SECOND_TARGET, NPC), (SECOND_TARGET, NPC)),
        (1, (NPC, SECOND_TARGET), (NPC,)),
        (5, (NPC, SECOND_TARGET), (SECOND_TARGET,)),
        (9, (NPC, SECOND_TARGET), ()),
    ],
)
def test_hold_cast_two_targets(tmp_path, context, seed, targets, failed_targets):
    """Commit one slot, one group and effects for exactly the failed saves."""
    sm, sid, package, loader, db = hold_world(tmp_path, context, seed=seed, two_targets=True)
    with sm:
        cast = _cast(sm, sid, package, context, targets=targets)
        before = sm.read_combat_snapshot(sid, package, context=context)
        old_rng = sm.read_rng_context(sid, package, context=context)
        assert before.combat_state.initiative_ids == (cast.actor_id, NPC, SECOND_TARGET)
        receipt = sm.submit_command(cast, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        combat = after.combat_state
        assert tuple(effect.target_id for effect in after.effect_states) == (
            failed_targets or (cast.actor_id,)
        )
        chain = tuple(
            (effect.target_id, effect.id, effect.origin) for effect in after.effect_states
        )
        assert combat.concentration_chain == {cast.actor_id: list(chain)}
        assert combat.concentration_rounds_remaining == {cast.actor_id: 10}
        assert tuple(clock.identity for clock in combat.effect_lifecycles) == (
            chain if failed_targets else ()
        )
        assert [(link.identity, link.statuses) for link in combat.conditions_by_effect] == [
            (identity, ["paralyzed"] if failed_targets else []) for identity in chain
        ]
        actors = {actor.entity_id: actor for actor in after.character_states}
        for target in targets:
            conditions = [(c.condition, c.source_effect_id) for c in actors[target].conditions]
            assert conditions == (
                [("paralyzed", "effect:paralyzed")] if target in failed_targets else []
            )
        assert actors[cast.actor_id].concentration_effect_id == (
            "effect:paralyzed" if failed_targets else "effect:hold-person"
        )
        expected_slots = {1: 4, 2: 3, 3: 1}
        assert actors[cast.actor_id].spell_slots == expected_slots
        assert {
            pool.level: pool.current
            for pool in after.resource_state.pools
            if pool.owner_id == cast.actor_id and pool.kind == "spell_slot"
        } == expected_slots
        assert combat.expended_resources == {cast.actor_id: {"spell_slot:3": 1}}
        events = _engine_events(sm, sid, package, context, receipt)
        assert [
            (event["target_id"], event["ability"], event["succeeded"])
            for event in events
            if event["type"] == "save_rolled"
        ] == [(target, "wis", target not in failed_targets) for target in targets]
        assert [
            (event["effect"]["target_id"], event["effect"]["id"], event["effect"]["origin"])
            for event in events
            if event["type"] == "effect_applied"
        ] == list(chain)
        assert _effect_counts(sm, sid) == (1, len(chain))
        rng = sm.read_rng_context(sid, package, context=context)
        assert rng != old_rng
        assert sm.submit_command(cast, package, context=context) == receipt
        assert sm.read_combat_snapshot(sid, package, context=context) == after
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert not before.effect_states
    with StateMachine(db, dnd_loader=loader) as reopened:
        assert reopened.read_combat_snapshot(sid, package, context=context) == after
        assert reopened.read_rng_context(sid, package, context=context) == rng
        assert (
            reopened.get_command_receipt(sid, cast.command_id, package, context=context) == receipt
        )


@pytest.mark.parametrize("targets", [(NPC, SECOND_TARGET), (SECOND_TARGET, NPC)])
def test_hold_two_targets_pass_keeps_other_member_and_ticks_on_caster_turn(
    tmp_path, context, targets
):
    """Remove the first saving target, tick the other member, then tick the group once."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=15, two_targets=True)
    with sm:
        cast = _cast(sm, sid, package, context, targets=targets)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = _pass_cast_turn(sm, sid, package, context, cast)
        assert len(before.effect_states) == 2
        original_chain = before.combat_state.concentration_chain[cast.actor_id]
        assert tuple(identity[0] for identity in original_chain) == targets
        (retained,) = [identity for identity in original_chain if identity[0] == SECOND_TARGET]
        retained_index = targets.index(SECOND_TARGET)
        turn = pass_command(sid, before, "hold.pass.first")
        assert turn.actor_id == NPC
        receipt = sm.submit_command(turn, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.effect_states == (before.effect_states[retained_index],)
        combat = after.combat_state
        assert combat.concentration_chain == {cast.actor_id: [retained]}
        assert combat.concentration_rounds_remaining == {cast.actor_id: 9}
        assert combat.conditions_by_effect == (
            before.combat_state.conditions_by_effect[retained_index],
        )
        assert combat.effect_lifecycles == (before.combat_state.effect_lifecycles[retained_index],)
        actors = {actor.entity_id: actor for actor in after.character_states}
        assert not actors[NPC].conditions and actors[SECOND_TARGET].conditions
        assert [
            (event["target_id"], event["reason"])
            for event in _engine_events(sm, sid, package, context, receipt)
            if event["type"] == "effect_expired"
        ] == [(NPC, "save_succeeded")]
        assert _effect_counts(sm, sid) == (1, 1)

        turn = pass_command(sid, after, "hold.pass.second")
        assert turn.actor_id == SECOND_TARGET
        receipt = sm.submit_command(turn, package, context=context)
        assert receipt.status == "committed", receipt
        next_snapshot = sm.read_combat_snapshot(sid, package, context=context)
        (clock,) = next_snapshot.combat_state.effect_lifecycles
        assert clock.state.last_repeat_turn_serial == combat.turn_serial
        assert next_snapshot.combat_state.concentration_rounds_remaining == {cast.actor_id: 9}

        turn = pass_command(sid, next_snapshot, "hold.pass.caster")
        assert turn.actor_id == cast.actor_id
        receipt = sm.submit_command(turn, package, context=context)
        assert receipt.status == "committed", receipt
        final = sm.read_combat_snapshot(sid, package, context=context)
        assert final.combat_state.concentration_rounds_remaining == {cast.actor_id: 8}
        assert final.combat_state.concentration_chain == {cast.actor_id: [retained]}
        assert final.resource_state == before.resource_state
        assert len(before.effect_states) == 2


def test_hold_two_target_caster_duration_expires_both_members(tmp_path, context, monkeypatch):
    """The ninth caster TurnEnd expires the complete group after all repeat saves fail."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=1332, two_targets=True)
    with sm:
        cast = _cast(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        snapshot = _pass_cast_turn(sm, sid, package, context, cast)
        order = (NPC, SECOND_TARGET, cast.actor_id)
        for serial in range(26):
            rounds = 9 - serial // len(order)
            assert snapshot.combat_state.concentration_rounds_remaining == {cast.actor_id: rounds}
            assert len(snapshot.effect_states) == 2
            turn = pass_command(sid, snapshot, f"hold.duration.two.{serial}")
            assert turn.actor_id == order[serial % len(order)]
            receipt = sm.submit_command(turn, package, context=context)
            assert receipt.status == "committed", receipt
            snapshot = sm.read_combat_snapshot(sid, package, context=context)

        final_turn = pass_command(sid, snapshot, "hold.duration.two.final")
        assert final_turn.actor_id == cast.actor_id
        assert snapshot.combat_state.concentration_rounds_remaining == {cast.actor_id: 1}
        _, retained = snapshot.combat_state.concentration_chain[cast.actor_id]
        assert retained[0] == SECOND_TARGET
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        evaluate = sm._rules._evaluate_dnd

        async def forge_partial_expiry(request):
            """Keep the second member, its condition and its movement after final TurnEnd."""
            result = await evaluate(request)
            assert result.status == "accepted"
            operations = []
            for op in result.state_delta.operations:
                if type(op) is delta.ConcentrationUpdate:
                    assert not op.chain
                    op = op.model_copy(
                        update={
                            "effect_id": op.expected_effect_id,
                            "chain": (retained,),
                            "rounds": 1,
                        }
                    )
                elif (
                    type(op) is delta.EffectLifecycleUpdate
                    and op.identity == retained
                    or type(op) is delta.ConditionsUpdate
                    and op.target_id == SECOND_TARGET
                    or type(op) is delta.ActionBudgetUpdate
                    and op.actor_id == SECOND_TARGET
                ):
                    continue
                operations.append(op)
            return result.model_copy(
                update={
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    ),
                    "proposed_events": tuple(
                        event
                        for event in result.proposed_events
                        if event.type != "concentration_dropped"
                        and not (
                            event.type in {"effect_expired", "condition_removed"}
                            and event.target_id == SECOND_TARGET
                        )
                    ),
                }
            )

        with monkeypatch.context() as patch:
            patch.setattr(sm._rules, "_evaluate_dnd", forge_partial_expiry)
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(final_turn, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == snapshot
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, final_turn.command_id, package, context=context) is None
        assert _effect_counts(sm, sid) == (1, 2)

        receipt = sm.submit_command(final_turn, package, context=context)
        assert receipt.status == "committed", receipt
        snapshot = sm.read_combat_snapshot(sid, package, context=context)
        assert not snapshot.effect_states
        assert not snapshot.combat_state.concentration_chain
        assert not snapshot.combat_state.concentration_rounds_remaining
        assert not snapshot.combat_state.conditions_by_effect
        assert not snapshot.combat_state.effect_lifecycles
        assert all(not actor.conditions for actor in snapshot.character_states)
        assert [
            (event["target_id"], event["reason"])
            for event in _engine_events(sm, sid, package, context, receipt)
            if event["type"] == "effect_expired"
        ] == [(NPC, "duration"), (SECOND_TARGET, "duration")]
        assert _effect_counts(sm, sid) == (0, 0)


@pytest.mark.parametrize("reason", ["save_succeeded", "duration"])
def test_hold_pass_rejects_expiry_of_another_member(tmp_path, context, monkeypatch, reason):
    """A coherent second removal cannot use a different actor's TurnEnd."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=15, two_targets=True)
    with sm:
        cast = _cast(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = _pass_cast_turn(sm, sid, package, context, cast)
        rng = sm.read_rng_context(sid, package, context=context)
        committed_events = sm.read_events(sid, package, context=context)
        turn = pass_command(sid, before, "hold.pass.forged")
        assert turn.actor_id == NPC
        _, other_effect = before.effect_states
        _, other_clock = before.combat_state.effect_lifecycles
        _, other_link = before.combat_state.conditions_by_effect
        other = next(actor for actor in before.character_states if actor.entity_id == SECOND_TARGET)
        evaluate = sm._rules._evaluate_dnd

        async def forge(request):
            """Start with the real Pass and add a fully linked removal of the other member."""
            result = await evaluate(request)
            assert result.status == "accepted"
            operations = list(result.state_delta.operations)
            (group,) = [op for op in operations if type(op) is delta.ConcentrationUpdate]
            assert group.chain == (other_clock.identity,)
            operations[operations.index(group)] = group.model_copy(
                update={"effect_id": None, "chain": (), "rounds": None}
            )
            operations.extend(
                (
                    delta.EffectLifecycleUpdate(
                        kind="combat.effect_lifecycle_update",
                        combat_id=group.combat_id,
                        identity=other_clock.identity,
                        expected_effect=other_effect,
                        effect=None,
                        expected_lifecycle=other_clock,
                        lifecycle=None,
                        expected_lineage=other_link,
                        lineage=None,
                    ),
                    delta.ConditionsUpdate(
                        kind="actor.conditions_update",
                        target_id=SECOND_TARGET,
                        expected=tuple(other.conditions),
                        value=(),
                    ),
                )
            )
            # Removing the incoming actor's paralysis also restores their movement.
            budget = delta.AttackBudgetState.model_validate(
                {name: getattr(other, name) for name in delta.AttackBudgetState.model_fields}
            )
            existing_budget = next(
                (
                    op
                    for op in operations
                    if type(op) is delta.ActionBudgetUpdate and op.actor_id == SECOND_TARGET
                ),
                None,
            )
            value = existing_budget.value if existing_budget is not None else budget
            if existing_budget is not None:
                operations.remove(existing_budget)
            operations.append(
                delta.ActionBudgetUpdate(
                    kind="combat.action_budget_update",
                    actor_id=SECOND_TARGET,
                    expected=budget,
                    value=value.model_copy(update={"movement_remaining": other.base_speed}),
                )
            )
            events = list(result.proposed_events)
            (save,) = [event for event in events if event.type == "save_rolled"]
            (expiry,) = [event for event in events if event.type == "effect_expired"]
            assert save.succeeded and expiry.reason == "save_succeeded"
            if reason == "save_succeeded":
                events.append(save.model_copy(update={"target_id": SECOND_TARGET}))
            events.append(expiry.model_copy(update={"target_id": SECOND_TARGET, "reason": reason}))
            return result.model_copy(
                update={
                    "proposed_events": tuple(events),
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    ),
                }
            )

        with monkeypatch.context() as patch:
            patch.setattr(sm._rules, "_evaluate_dnd", forge)
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(turn, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == committed_events
        assert sm.get_command_receipt(sid, turn.command_id, package, context=context) is None
        assert _effect_counts(sm, sid) == (1, 2)
        assert sm.submit_command(turn, package, context=context).status == "committed"
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.effect_states == (other_effect,)


@pytest.mark.parametrize(("seed", "succeeded"), [(6, False), (10, True)])
def test_hold_two_target_cast_preserves_positive_hp_caster_damage(
    tmp_path, context, seed, succeeded
):
    """The saving target can attack the caster while the other holds the group."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, seed=seed, two_targets=True)
    with sm:
        cast = _cast(sm, sid, package, context)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = _pass_cast_turn(sm, sid, package, context, cast)
        assert tuple(effect.target_id for effect in before.effect_states) == (SECOND_TARGET,)
        attack = hold_attack_command(sid, cast.actor_id, before.world_version)
        receipt = sm.submit_command(attack, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        old_caster = next(
            actor for actor in before.character_states if actor.entity_id == cast.actor_id
        )
        caster = next(actor for actor in after.character_states if actor.entity_id == cast.actor_id)
        assert 0 < caster.hp_current < old_caster.hp_current
        assert after.resource_state == before.resource_state
        events = _engine_events(sm, sid, package, context, receipt)
        (check,) = [event for event in events if event["type"] == "concentration_check"]
        assert (check["target_id"], check["succeeded"]) == (cast.actor_id, succeeded)
        assert after.effect_states == (before.effect_states if succeeded else ())
        assert bool(after.combat_state.concentration_chain) == succeeded


@pytest.mark.parametrize("targets", [(NPC, SECOND_TARGET), (SECOND_TARGET, NPC)])
def test_hold_drop_releases_both_members_outside_caster_turn(tmp_path, context, targets):
    """Drop the complete two-member group without spending or advancing the turn."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, two_targets=True)
    with sm:
        cast = _cast(sm, sid, package, context, targets=targets)
        assert sm.submit_command(cast, package, context=context).status == "committed"
        before = _pass_cast_turn(sm, sid, package, context, cast)
        rng = sm.read_rng_context(sid, package, context=context)
        assert len(before.effect_states) == 2
        assert before.combat_state.initiative_ids[before.combat_state.current_turn_index] == NPC
        drop = replace(
            cast,
            command_id="hold.drop.two",
            payload=CombatIntentPayload(intent_type="drop_concentration"),
            expected_world_version=before.world_version,
        )
        receipt = sm.submit_command(drop, package, context=context)
        assert receipt.status == "committed", receipt
        after = sm.read_combat_snapshot(sid, package, context=context)
        assert after.combat_state == before.combat_state.model_copy(
            update={
                "concentration_chain": {},
                "concentration_rounds_remaining": {},
                "conditions_by_effect": (),
                "effect_lifecycles": (),
            }
        )
        assert not after.effect_states
        assert all(
            not actor.conditions and not actor.concentration_effect_id
            for actor in after.character_states
        )
        assert _effect_counts(sm, sid) == (0, 0)
        assert after.resource_state == before.resource_state
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert [
            (event["target_id"], event["reason"])
            for event in _engine_events(sm, sid, package, context, receipt)
            if event["type"] == "effect_expired"
        ] == [(target, "concentration_drop") for target in targets]


@pytest.mark.parametrize(("slot_level", "targets"), [(2, (NPC, SECOND_TARGET)), (3, (NPC, NPC))])
def test_hold_rejects_illegal_target_count_in_pinned_engine(
    tmp_path, context, monkeypatch, slot_level, targets
):
    """An over-capacity or duplicate target list leaves the encounter intact."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, two_targets=True)
    with sm:
        cast = _cast(sm, sid, package, context, slot_level=slot_level, targets=targets)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        evaluate = sm._rules._evaluate_dnd
        observed = []

        async def observe(request):
            """Record the real evaluator's ruling on the submitted target list."""
            result = await evaluate(request)
            observed.append((request.payload.target_ids, result.error.code))
            return result

        monkeypatch.setattr(sm._rules, "_evaluate_dnd", observe)
        receipt = sm.submit_command(cast, package, context=context)
        assert (receipt.status, receipt.error_code) == ("rejected", "target_invalid")
        assert observed == [(targets, "target_invalid")]
        assert receipt.event_seqs == ()
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert _effect_counts(sm, sid) == (0, 0)


@pytest.mark.parametrize("tamper", ["duplicate_save", "missing_member", "effect_evidence"])
def test_hold_rejects_cast_evidence_that_differs_from_bound_graph(
    tmp_path, context, monkeypatch, tamper
):
    """Reject incomplete membership and mismatched effect/save evidence before writes."""
    sm, sid, package, _, _ = hold_world(tmp_path, context, two_targets=True)
    with sm:
        cast = _cast(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        committed_events = sm.read_events(sid, package, context=context)
        evaluate = sm._rules._evaluate_dnd

        async def forge(request):
            """Amend only the accepted proposal's graph or per-target events."""
            result = await evaluate(request)
            assert result.status == "accepted"
            events = list(result.proposed_events)
            operations = list(result.state_delta.operations)
            if tamper == "duplicate_save":
                first, second = [i for i, event in enumerate(events) if event.type == "save_rolled"]
                events[second] = events[first]
            else:
                applied = [i for i, event in enumerate(events) if event.type == "effect_applied"]
                event = events[applied[1]]
                if tamper == "effect_evidence":
                    events[applied[1]] = event.model_copy(
                        update={"effect": event.effect.model_copy(update={"name": "Forged"})}
                    )
                else:
                    del events[applied[1]]
                    index = next(
                        i
                        for i, op in enumerate(operations)
                        if type(op) is delta.HoldPersonGraphCreate
                    )
                    graph = operations[index]
                    operations[index] = graph.model_copy(
                        update={"entries": graph.entries[:1], "chain": graph.chain[:1]}
                    )
            return result.model_copy(
                update={
                    "proposed_events": tuple(events),
                    "state_delta": result.state_delta.model_copy(
                        update={"operations": tuple(operations)}
                    ),
                }
            )

        with monkeypatch.context() as patch:
            patch.setattr(sm._rules, "_evaluate_dnd", forge)
            with pytest.raises(StateMachineError) as error:
                sm.submit_command(cast, package, context=context)
        assert error.value.code == "invalid_rule_result"
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == committed_events
        assert sm.get_command_receipt(sid, cast.command_id, package, context=context) is None
        assert _effect_counts(sm, sid) == (0, 0)
        assert sm.submit_command(cast, package, context=context).status == "committed"


def test_hold_two_target_cast_late_failure_and_retry(tmp_path, context):
    """Roll back both effect members and the third-level spend at final receipt failure."""
    sm, sid, package, loader, db = hold_world(tmp_path, context, two_targets=True)
    with sm:
        cast = _cast(sm, sid, package, context)
        before = sm.read_combat_snapshot(sid, package, context=context)
        rng = sm.read_rng_context(sid, package, context=context)
        events = sm.read_events(sid, package, context=context)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_hold_two_receipt BEFORE INSERT ON command_receipts "
            "WHEN NEW.command_id='hold.cast' "
            "AND (SELECT count(*) FROM effect_concentrations WHERE session_id=NEW.session_id)=1 "
            "AND (SELECT count(*) FROM ongoing_effects WHERE session_id=NEW.session_id)=2 "
            "AND EXISTS(SELECT 1 FROM character_resources WHERE session_id=NEW.session_id "
            "AND actor_id=(SELECT caster_id FROM effect_concentrations "
            "WHERE session_id=NEW.session_id) AND kind='spell_slot' "
            "AND resource_id='spell:3' AND current=1) "
            "BEGIN SELECT RAISE(ABORT,'injected two-target Hold receipt failure'); END"
        )
        with pytest.raises(StateMachineError) as error:
            sm.submit_command(cast, package, context=context)
        assert error.value.code == "storage_error"
        assert str(error.value.__cause__) == "injected two-target Hold receipt failure"
        assert _effect_counts(sm, sid) == (0, 0)
        assert sm.read_combat_snapshot(sid, package, context=context) == before
        assert sm.read_rng_context(sid, package, context=context) == rng
        assert sm.read_events(sid, package, context=context) == events
        assert sm.get_command_receipt(sid, cast.command_id, package, context=context) is None
        with StateMachine(db, dnd_loader=loader) as reopened:
            assert reopened.read_combat_snapshot(sid, package, context=context) == before
            assert reopened.read_rng_context(sid, package, context=context) == rng
            assert reopened.read_events(sid, package, context=context) == events
        sm._conn.execute("DROP TRIGGER fail_hold_two_receipt")
        receipt = sm.submit_command(cast, package, context=context)
        assert receipt.status == "committed", receipt
        committed = sm.read_combat_snapshot(sid, package, context=context)
        assert len(committed.effect_states) == 2
        assert committed.combat_state.expended_resources == {cast.actor_id: {"spell_slot:3": 1}}
        assert _effect_counts(sm, sid) == (1, 2)
        assert sm.submit_command(cast, package, context=context) == receipt
        assert sm.read_combat_snapshot(sid, package, context=context) == committed
