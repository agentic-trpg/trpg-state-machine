"""Bind typed Haste creation and turn transitions to fixed encounter facts."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.events import (
    ConcentrationDropped,
    ConditionRemoved,
    EffectExpired,
    IntentSubmitted,
)

from .concentration_adoption import (
    _indexed_operations,
    condition_transition,
    effect_identity,
    follow_up_creations,
    require_effect_applied,
)
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
        adopted.append(("hold_lifecycle", identity, "update", removed))
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
