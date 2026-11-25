"""Bind Haste casting, weapon strikes and turn transitions to encounter facts."""

from dnd5e_engine import attack_rules
from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.action_economy_rules import action_economy_gate_failure
from dnd5e_engine.action_policy import available_grant, grant_payment_for_effect
from dnd5e_engine.damage_rules import concentration_dc
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_haste import haste_speed
from dnd5e_engine.events import (
    ConcentrationDropped,
    ConditionRemoved,
    EffectExpired,
    IntentSubmitted,
)
from dnd5e_engine.intents import IntentRejectedError
from dnd5e_engine.turn_rules import ordinary_action_payment, spend_attack_budget

from .concentration_adoption import (
    _indexed_operations,
    condition_transition,
    effect_identity,
    follow_up_creations,
    require_effect_applied,
)
from .damage_adoption import damage_result_balances
from .errors import StateMachineError


def _invalid(message):
    """Reject a proposal outside its fixed encounter or allowed component writes."""
    raise StateMachineError("invalid_rule_result", message)


def haste_cast_admission(request, result, graph):
    """Bind the typed Haste graph to the willing target and ordered cast evidence."""
    payload = request.payload
    if (
        payload.spell_id != "haste"
        or graph.target_id != payload.target_id
        or payload.willing_target_ids != (graph.target_id,)
        or result.rng_transition.state_changed
    ):
        _invalid("Haste creation differs from the bound spell, willing target or RNG")
    events = result.proposed_events
    if (
        len(events) < 3
        or events[0]
        != IntentSubmitted(
            actor_id=request.actor_id,
            intent_type="cast_spell",
            target_id=graph.target_id,
            spell_id=payload.spell_id,
        )
        or events[1].type != "spell_cast"
        or events[1].actor_id != graph.source_id
        or events[1].spell_id != payload.spell_id
        or events[1].slot_level != graph.slot_level
        or any(
            event.type not in {"turn_phase", "turn_ended", "turn_started", "round_started"}
            for event in events[3:]
        )
    ):
        _invalid("Haste cast events differ from the submitted spell or turn handoff")
    require_effect_applied(events[2], graph.effect)


HASTE_LIFECYCLE_OPERATIONS = (
    delta.EffectLifecycleUpdate,
    delta.ConcentrationUpdate,
    delta.EffectFollowUpCreate,
)


def _attack_release(request, result, actors, caster, events, *, ends):
    """Adopt exactly the active Haste graph's captured Lethargy on a failed save."""
    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    (effect,) = snapshot.effect_states
    identity = effect_identity(effect)
    operations = result.state_delta.operations
    removals = _indexed_operations(operations, delta.EffectLifecycleUpdate, lambda op: op.identity)
    groups = _indexed_operations(operations, delta.ConcentrationUpdate, lambda op: op.source_id)
    conditions = _indexed_operations(operations, delta.ConditionsUpdate, lambda op: op.target_id)
    follow_ups = follow_up_creations(request, result, removals, {identity: effect})
    if not ends:
        if removals or groups or conditions or follow_ups or events:
            _invalid("A retained Haste graph cannot emit a release transition")
        return (), {}

    (clock,) = combat.effect_lifecycles
    (link,) = combat.conditions_by_effect
    if (
        removals.keys() != {identity}
        or groups.keys() != {caster}
        or follow_ups.keys() != {identity}
    ):
        _invalid("Failed Haste concentration must end the group and apply its follow-up")
    removal = removals[identity]
    group = groups[caster]
    child = follow_ups[identity]
    if (
        removal.combat_id != combat.combat_id
        or removal.expected_effect != effect
        or removal.expected_lifecycle != clock
        or removal.expected_lineage != link
        or removal.effect is not None
        or removal.lifecycle is not None
        or removal.lineage is not None
        or group.combat_id != combat.combat_id
        or group.expected_effect_id != actors[caster].concentration_effect_id
        or group.expected_chain != tuple(combat.concentration_chain[caster])
        or group.expected_rounds != combat.concentration_rounds_remaining[caster]
        or (group.effect_id, group.chain, group.rounds) != (None, (), None)
        or (child.effect,) != effect.end_effects
    ):
        _invalid("Attack release differs from the fixed Haste group or captured follow-up")
    expected, removed_conditions, applied = condition_transition(
        actors, {identity: effect}, {identity: link}, removals, follow_ups
    )
    target = effect.target_id
    prefix = (
        ConcentrationDropped(target_id=caster, effect_name=effect.id),
        EffectExpired(
            target_id=target, effect_id=effect.id, origin=effect.origin, reason="concentration_drop"
        ),
        *(
            ConditionRemoved(target_id=target, condition=name)
            for name in sorted(removed_conditions.get(target, ()))
        ),
    )
    if (
        {target_id: (op.expected, op.value) for target_id, op in conditions.items()} != expected
        or events[: len(prefix)] != prefix
        or len(events) <= len(prefix)
        or tuple(events[len(prefix) + 1 :]) != applied[identity]
    ):
        _invalid("Attack release condition and expiry events differ from the effect transition")
    require_effect_applied(events[len(prefix)], child.effect)
    after = (
        actors[target].model_copy(update={"conditions": list(expected[target][1])})
        if target in expected
        else actors[target]
    )
    ledger = combat.movement_ledgers[target]
    movement = ledger.remaining(haste_speed(after, (child.effect,), ledger.active_mode))
    return (
        ("effect_lifecycle", identity, "update", removal),
        ("effect_follow_up", effect_identity(child.effect), "create", child),
        ("hold_concentration", caster, "update", group),
    ), {target: movement}


def _strike_payment(payload, actor, effect):
    """Return the attacker after applying the exact permitted funding source."""
    grant = payload.action_grant
    if grant is not None and (
        payload.stat_block_action_id is not None
        or grant != (effect.id, effect.origin)
        or not available_grant(actor, effect, "attack")
    ):
        _invalid("Attack attempted to spend an unauthorized Haste grant")
    try:
        failure = action_economy_gate_failure(
            actor,
            payload,
            is_bonus_action=False,
            is_reaction_cast=False,
            extra_action=grant is not None,
        )
    except IntentRejectedError as exc:
        raise StateMachineError("invalid_rule_result", "Attack has no permitted action") from exc
    if failure is not None:
        _invalid("Attack has no permitted action")
    if grant is not None:
        return actor.model_copy(update=grant_payment_for_effect(actor, effect))
    return spend_attack_budget(
        actor,
        new_action=attack_rules.attack_action_is_spent(actor),
        attacks=1,
        payment=ordinary_action_payment(actor),
    )


def haste_attack_admission(request, result):
    """Bind a single positive-HP strike, its payment and any Haste release before adoption."""
    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    actors = {a.entity_id: a for a in snapshot.character_states}
    payload = request.payload
    actor_state = actors.get(request.actor_id)
    target = actors.get(payload.target_id)
    if (
        actor_state is None
        or target is None
        or not target.is_alive
        or target.hp_current <= 0
        or combat.initiative_ids[combat.current_turn_index] != request.actor_id
        or (request.actor_id in combat.party_ids) == (target.entity_id in combat.party_ids)
        or len(snapshot.effect_states) != 1
        or (
            payload.stat_block_action_id is None
            and (
                payload.weapon_instance_id is None
                or payload.weapon_instance_id != actor_state.held_weapon_instance_id
            )
        )
    ):
        _invalid("Haste attack requires a fixed target and the admitted single effect graph")
    actor = combatant(actor_state)
    (effect,) = snapshot.effect_states
    events = result.proposed_events
    rolls = [event for event in events if event.type == "attack_rolled"]
    packets = [event for event in events if event.type == "damage_applied"]
    permitted = {
        delta.HPDelta,
        delta.TempHPSet,
        delta.DamageAttributionUpdate,
        delta.DamageSequenceUpdate,
        delta.ActionBudgetUpdate,
        delta.ConditionsUpdate,
        *HASTE_LIFECYCLE_OPERATIONS,
    }
    if (
        any(type(op) not in permitted for op in result.state_delta.operations)
        or not events
        or events[0]
        != IntentSubmitted(
            actor_id=actor.entity_id, intent_type="attack", target_id=target.entity_id
        )
        or len(rolls) != 1
        or rolls[0].attacker_id != actor.entity_id
        or rolls[0].target_id != target.entity_id
        or rolls[0].is_opportunity_attack
        or rolls[0].is_hit != bool(packets)
    ):
        _invalid("Haste attack evidence differs from the submitted single strike")
    hp, _, _ = damage_result_balances(
        request,
        result,
        target,
        source_id=None if payload.stat_block_action_id else actor.weapon_in_hands,
        is_crit=rolls[0].is_crit,
    )
    caster = next(iter(combat.concentration_chain), None)
    amount = sum(packet.amount for packet in packets)
    saves = [event for event in events if event.type == "concentration_check"]
    needs_save = amount > 0 and target.entity_id == caster
    prefix = (events[0], *rolls, *packets, *saves)
    if (
        hp <= 0
        or len(saves) != int(needs_save)
        or (
            needs_save
            and (
                saves[0].target_id != caster
                or saves[0].dc != concentration_dc(amount)
                or saves[0].succeeded != (saves[0].roll_total >= saves[0].dc)
            )
        )
        or events[: len(prefix)] != prefix
    ):
        _invalid("Haste attack exceeds the supported damage or concentration evidence")
    adopted, movement = _attack_release(
        request,
        result,
        actors,
        caster,
        events[len(prefix) :],
        ends=needs_save and not saves[0].succeeded,
    )
    paid = _strike_payment(payload, actor, effect)
    budgets = {
        actor.entity_id: {"attack_rolls_made_this_turn": actor.attack_rolls_made_this_turn + 1}
    }
    for owner, remaining in movement.items():
        budgets.setdefault(owner, {})["movement_remaining"] = remaining
    expected = {}
    fields = delta.AttackBudgetState.model_fields
    for owner, changes in budgets.items():
        before = {field: getattr(actors[owner], field) for field in fields}
        after = (
            {field: getattr(paid, field) for field in fields}
            if owner == actor.entity_id
            else before
        )
        value = {**after, **changes}
        if value != before:
            expected[owner] = (before, value)
    updates = _indexed_operations(
        result.state_delta.operations, delta.ActionBudgetUpdate, lambda op: op.actor_id
    )
    if {
        owner: (op.expected.model_dump(), op.value.model_dump()) for owner, op in updates.items()
    } != expected:
        _invalid("Haste attack budget differs from the selected funding and movement ledger")
    return adopted, effect.target_id


def haste_pass_admission(request, result):
    """Guard terminal lifecycle edits, linked conditions and concentration during Pass."""
    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    actor_id = request.actor_id
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    if (
        len(snapshot.effect_states) != 1
        or len(combat.effect_lifecycles) != 1
        or len(combat.conditions_by_effect) != 1
        or len(combat.concentration_chain) > 1
    ):
        _invalid("Haste Pass requires one fixed target effect and its clock and lineage")
    (effect,) = snapshot.effect_states
    identity = effect_identity(effect)
    effects = {identity: effect}
    (clock,) = combat.effect_lifecycles
    (link,) = combat.conditions_by_effect
    if effect.target_id not in actors or clock.identity != identity or link.identity != identity:
        _invalid("Haste Pass effect identity differs from the fixed roster, clock or lineage")
    operations = result.state_delta.operations
    lifecycles = _indexed_operations(
        operations, delta.EffectLifecycleUpdate, lambda op: op.identity
    )
    groups = _indexed_operations(operations, delta.ConcentrationUpdate, lambda op: op.source_id)
    conditions = _indexed_operations(operations, delta.ConditionsUpdate, lambda op: op.target_id)
    removed = lifecycles.get(identity)
    if result.rng_transition.state_changed or set(lifecycles) - {identity}:
        _invalid("Haste Pass exceeds its effect scope or consumes RNG")
    if removed is not None and (
        removed.combat_id != combat.combat_id
        or removed.expected_effect != effect
        or removed.expected_lifecycle != clock
        or removed.expected_lineage != link
        or any(value is not None for value in (removed.effect, removed.lifecycle, removed.lineage))
    ):
        _invalid("Haste Pass effect removal differs from the fixed effect, clock or lineage")

    group = None
    if combat.concentration_chain:
        source, chain = next(iter(combat.concentration_chain.items()))
        rounds = combat.concentration_rounds_remaining.get(source)
        if source not in actors or tuple(chain) != (identity,) or rounds is None:
            _invalid("Haste effect differs from the fixed concentration membership")
        if actor_id == source:
            group = groups.get(source)
            expected_after = (
                (None, (), None)
                if removed is not None
                else (actors[source].concentration_effect_id, (identity,), rounds - 1)
            )
            if (
                group is None
                or group.combat_id != combat.combat_id
                or group.expected_effect_id != actors[source].concentration_effect_id
                or group.expected_chain != tuple(chain)
                or group.expected_rounds != rounds
                or (removed is not None) != (rounds == 1)
                or (group.effect_id, group.chain, group.rounds) != expected_after
            ):
                _invalid("Haste concentration differs from the fixed caster turn or group")
        elif removed is not None:
            _invalid("Haste concentration ended outside its caster's TurnEnd")
    else:
        target_end = (
            clock.state.expiry_actor_id == actor_id
            and clock.state.applied_turn_serial < combat.turn_serial
        )
        if (removed is not None) != target_end:
            _invalid("Follow-up expiration differs from its later target TurnEnd")
    if set(groups) != ({group.source_id} if group else set()):
        _invalid("Haste Pass changed an unrelated concentration group")

    follow_ups = follow_up_creations(request, result, lifecycles, effects)
    expected, ended, applied = condition_transition(
        actors, effects, {identity: link}, lifecycles, follow_ups
    )
    if {target: (op.expected, op.value) for target, op in conditions.items()} != expected:
        _invalid("Haste Pass conditions do not match the changed effect lineage")

    events = result.proposed_events
    if (
        len(events) < 5
        or events[0] != IntentSubmitted(actor_id=actor_id, intent_type="pass")
        or events[1].type != "turn_phase"
        or events[1].phase != "turn_end"
    ):
        _invalid("Haste Pass has no submitted intent and TurnEnd boundary")
    boundary = []
    adopted = []
    if removed is not None:
        if group is not None:
            boundary.append(ConcentrationDropped(target_id=actor_id, effect_name=effect.id))
        boundary.append(
            EffectExpired(
                target_id=effect.target_id,
                effect_id=effect.id,
                origin=effect.origin,
                reason="duration",
            )
        )
        boundary.extend(
            ConditionRemoved(target_id=effect.target_id, condition=name)
            for name in sorted(ended.get(effect.target_id, ()))
        )
        adopted.append(("effect_lifecycle", identity, "update", removed))
        if follow_up := follow_ups.get(identity):
            position = 2 + len(boundary)
            if position >= len(events):
                _invalid("Haste Pass follow-up lacks application evidence")
            require_effect_applied(events[position], follow_up.effect)
            boundary.append(events[position])
            boundary.extend(applied[identity])
            adopted.append(
                ("effect_follow_up", effect_identity(follow_up.effect), "create", follow_up)
            )
    if group is not None:
        adopted.append(("hold_concentration", group.source_id, "update", group))
    end = 2 + len(boundary)
    if (
        tuple(boundary) != events[2:end]
        or end >= len(events)
        or events[end].type != "turn_ended"
        or any(
            event.type not in {"turn_ended", "turn_started", "round_started", "turn_phase"}
            for event in events[end:]
        )
    ):
        _invalid("Haste Pass effects do not match its ordered TurnEnd evidence")
    after = (effect,) if removed is None else tuple(op.effect for op in follow_ups.values())
    return adopted, {target: pair[1] for target, pair in expected.items()}, effect.target_id, after


def lethargy_pass_expiry(snapshot, result, endings):
    """Verify a standalone Haste follow-up against the actual TurnEnd clock and lineage."""
    combat = snapshot.combat_state
    if (
        len(snapshot.effect_states) != 1
        or len(combat.effect_lifecycles) != 1
        or len(combat.conditions_by_effect) != 1
        or combat.concentration_chain
    ):
        _invalid("TurnStart damage requires a standalone Lethargy effect graph")
    (effect,) = snapshot.effect_states
    identity = effect_identity(effect)
    (clock,) = combat.effect_lifecycles
    (link,) = combat.conditions_by_effect
    if clock.identity != identity or link.identity != identity:
        _invalid("Lethargy clock or lineage differs from the fixed effect")
    phase = next(
        (
            (actor, "turn_end", round_number)
            for actor, round_number, serial in endings
            if clock.state.expires_at(actor, "end", serial, round_number)
        ),
        None,
    )
    updates = _indexed_operations(
        result.state_delta.operations, delta.EffectLifecycleUpdate, lambda op: op.identity
    )
    if set(updates) != ({identity} if phase else set()):
        _invalid("Lethargy expiry does not match its actual TurnEnd")
    if phase is None:
        return (), {}, None, (), effect.target_id, (effect,)

    removal = updates[identity]
    if (
        removal.combat_id != combat.combat_id
        or removal.expected_effect != effect
        or removal.expected_lifecycle != clock
        or removal.expected_lineage != link
        or any(value is not None for value in (removal.effect, removal.lifecycle, removal.lineage))
    ):
        _invalid("Lethargy expiry rewrites the fixed effect, clock or lineage")
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    conditions, ended, _ = condition_transition(
        actors, {identity: effect}, {identity: link}, updates, {}
    )
    events = (
        EffectExpired(
            target_id=effect.target_id, effect_id=effect.id, origin=effect.origin, reason="duration"
        ),
        *(
            ConditionRemoved(target_id=effect.target_id, condition=name)
            for name in sorted(ended.get(effect.target_id, ()))
        ),
    )
    changes = (("effect_lifecycle", identity, "update", removal),)
    return (
        changes,
        {target: pair[1] for target, pair in conditions.items()},
        phase,
        events,
        effect.target_id,
        (),
    )
