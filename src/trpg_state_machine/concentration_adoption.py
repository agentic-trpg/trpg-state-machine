"""Validate the Engine's voluntary concentration release before shared adoption."""

from collections import defaultdict

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.events import (
    ConcentrationDropped,
    ConditionRemoved,
    EffectExpired,
    IntentSubmitted,
)

from .errors import StateMachineError


def _invalid(message):
    """Reject an open or mismatched release proposal."""
    raise StateMachineError("invalid_rule_result", message)


def _indexed_operations(operations, operation_type, key):
    """Index operations of one kind and reject repeated edits to the same component."""
    selected = {}
    for operation in operations:
        if type(operation) is operation_type:
            identity = key(operation)
            if identity in selected:
                _invalid("Duplicate concentration release operation")
            selected[identity] = operation
    return selected


def _linked_removals(links, effects, selected):
    """Project the released provenance keys while retaining same-key providers."""
    released = defaultdict(set)
    for identity in selected:
        target, effect_id, _ = identity
        released[target].update((effect_id, status) for status in links[identity].statuses)
    for link in links.values():
        target, effect_id, _ = link.identity
        keys = released.get(target)
        if not keys or link.identity in selected:
            continue
        effect = effects.get(link.identity)
        if effect is not None and not effect.disabled:
            keys.difference_update(
                (effect_id, status) for status in effect.statuses.intersection(link.statuses)
            )
    return released


def concentration_release_changes(request, result):
    """Admit only the requesting caster's complete terminal transition.

    The envelope and request binding have already been verified. The Effects owner
    applies the guarded removals in the same write scope as the optional movement
    projection. Linked conditions derive from surviving providers at read time.
    """
    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    source = request.actor_id
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    caster = actors.get(source)
    chain = tuple(combat.concentration_chain.get(source, ()))
    selected = set(chain)
    operations = result.state_delta.operations
    permitted = {
        delta.ConcentrationUpdate,
        delta.EffectLifecycleUpdate,
        delta.ConditionsUpdate,
        delta.ActionBudgetUpdate,
    }
    if (
        caster is None
        or not chain
        or len(selected) != len(chain)
        or any(target not in actors for target, _, _ in chain)
        or any(
            selected.intersection(members)
            for other, members in combat.concentration_chain.items()
            if other != source
        )
        or result.rng_transition.state_changed
        or any(type(op) not in permitted for op in operations)
    ):
        _invalid("Concentration release exceeds the fixed caster scope")

    groups = _indexed_operations(operations, delta.ConcentrationUpdate, lambda op: op.source_id)
    if groups.keys() != {source}:
        _invalid("Concentration release must remove exactly the caster's group")
    group = groups[source]
    if (
        group.combat_id != combat.combat_id
        or group.expected_effect_id != caster.concentration_effect_id
        or group.expected_chain != chain
        or group.expected_rounds != combat.concentration_rounds_remaining.get(source)
        or group.effect_id is not None
        or group.chain
        or group.rounds is not None
    ):
        _invalid("Concentration release differs from the fixed group")

    lifecycles = _indexed_operations(
        operations, delta.EffectLifecycleUpdate, lambda op: op.identity
    )
    if lifecycles.keys() != selected:
        _invalid("Concentration release must remove all and only the group's members")
    effects = {
        (effect.target_id, effect.id, effect.origin): effect for effect in snapshot.effect_states
    }
    clocks = {record.identity: record for record in combat.effect_lifecycles}
    links = {record.identity: record for record in combat.conditions_by_effect}
    for identity in chain:
        operation = lifecycles[identity]
        if (
            operation.combat_id != combat.combat_id
            or operation.expected_effect != effects.get(identity)
            or operation.expected_lifecycle != clocks.get(identity)
            or operation.expected_lineage != links.get(identity)
            or any(
                value is not None
                for value in (operation.effect, operation.lifecycle, operation.lineage)
            )
        ):
            _invalid("Concentration member removal differs from the fixed effect, clock or lineage")

    conditions = _indexed_operations(operations, delta.ConditionsUpdate, lambda op: op.target_id)
    budgets = _indexed_operations(operations, delta.ActionBudgetUpdate, lambda op: op.actor_id)
    removals = _linked_removals(links, effects, selected)
    expected_conditions = {}
    ended_conditions = {}
    for target, keys in removals.items():
        before = tuple(actors[target].conditions)
        after = tuple(c for c in before if (c.source_effect_id, c.condition) not in keys)
        if before != after:
            expected_conditions[target] = (before, after)
            ended_conditions[target] = {c.condition for c in before} - {c.condition for c in after}
    if {
        target: (op.expected, op.value) for target, op in conditions.items()
    } != expected_conditions:
        _invalid("Concentration release changed conditions outside the removed lineage")

    changes = []
    for target, operation in budgets.items():
        if (
            not ended_conditions.get(target)
            or any(
                getattr(operation.expected, field) != getattr(actors[target], field)
                for field in delta.AttackBudgetState.model_fields
            )
            or operation.value == operation.expected
            or operation.value
            != operation.expected.model_copy(
                update={"movement_remaining": operation.value.movement_remaining}
            )
        ):
            _invalid("Concentration release changed an unrelated action or movement field")
        changes.append(("actor", target, "movement_remaining", operation.value.movement_remaining))

    events = [IntentSubmitted(actor_id=source, intent_type="drop_concentration")]
    for target, effect_id, origin in chain:
        events.extend(
            (
                ConcentrationDropped(target_id=source, effect_name=effect_id),
                EffectExpired(
                    target_id=target,
                    effect_id=effect_id,
                    origin=origin,
                    reason="concentration_drop",
                ),
            )
        )
        events.extend(
            ConditionRemoved(target_id=target, condition=condition)
            for condition in sorted(ended_conditions.get(target, ()))
        )
    if tuple(events) != result.proposed_events:
        _invalid("Concentration release events differ from the adopted graph transition")

    changes.extend(
        ("hold_lifecycle", identity, "update", lifecycles[identity]) for identity in chain
    )
    changes.append(("hold_concentration", source, "update", group))
    return tuple(changes)
