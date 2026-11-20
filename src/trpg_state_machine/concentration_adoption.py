"""Validate the Engine's voluntary concentration release before shared adoption."""

from collections import defaultdict

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.activities.effects import applicable_condition_statuses
from dnd5e_engine.effect_lifecycle import conditions_after_effect_application
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_haste import haste_speed
from dnd5e_engine.evaluation_state import ConditionRecord
from dnd5e_engine.events import (
    ConcentrationDropped,
    ConditionApplied,
    ConditionRemoved,
    EffectApplied,
    EffectExpired,
    IntentSubmitted,
)
from dnd5e_engine.types.conditions import ActiveCondition

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
                _invalid("Duplicate effect graph operation")
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


def effect_identity(effect):
    """Return the effect's full target, ID and origin key."""
    return effect.target_id, effect.id, effect.origin


def require_effect_applied(event, expected):
    """Bind application evidence to the complete typed effect result."""
    if not isinstance(event, EffectApplied) or effect_state(event.effect) != expected:
        _invalid("Effect application differs from the typed change")


def follow_up_creations(request, result, lifecycles, effects):
    """Validate only explicit creations linked to exact removals in this result."""
    combat = request.state_snapshot.combat_state
    follow_ups = _indexed_operations(
        result.state_delta.operations, delta.EffectFollowUpCreate, lambda op: op.ended_identity
    )
    started = set()
    for identity, follow_up in follow_ups.items():
        removal = lifecycles.get(identity)
        child = follow_up.effect
        child_identity = effect_identity(child)
        if (
            removal is None
            or removal.effect is not None
            or follow_up.combat_id != combat.combat_id
            or child not in removal.expected_effect.end_effects
            or child.target_id != identity[0]
            or child_identity in effects
            or child_identity in started
            or follow_up.lineage.identity != child_identity
            or follow_up.lifecycle.identity != child_identity
            or follow_up.lifecycle.state.applied_turn_serial != combat.turn_serial
            or follow_up.lifecycle.state.applied_round != combat.round_number
        ):
            _invalid("Follow-up creation exceeds the removed effect and fixed combat scope")
        started.add(child_identity)
    return follow_ups


def condition_transition(actors, effects, links, removed, follow_ups):
    """Project exact condition revisions and events from removed and added lineage."""
    removals = _linked_removals(links, effects, removed)
    conditions = {}
    ended = {}
    applied = {}
    for target, keys in removals.items():
        before = tuple(actors[target].conditions)
        after = tuple(c for c in before if (c.source_effect_id, c.condition) not in keys)
        ended[target] = {c.condition for c in before} - {c.condition for c in after}
        conditions[target] = (before, after)
    for identity, operation in follow_ups.items():
        target = operation.effect.target_id
        actor = actors[target]
        before = tuple(actor.conditions)
        previous = conditions.get(target, (before, before))[1]
        expected_statuses = applicable_condition_statuses(
            operation.effect.statuses,
            condition_immunities=actor.condition_immunities,
            condition_names=[condition.condition for condition in previous],
        )
        if operation.lineage.statuses != expected_statuses:
            _invalid("Follow-up condition lineage differs from the fixed target's immunities")
        projected, names = conditions_after_effect_application(
            [ActiveCondition.model_validate(condition.model_dump()) for condition in previous],
            operation.effect.id,
            operation.lineage.statuses,
        )
        conditions[target] = (
            before,
            tuple(ConditionRecord.model_validate(c.model_dump()) for c in projected),
        )
        applied[identity] = tuple(ConditionApplied(target_id=target, condition=c) for c in names)
    return (
        {target: values for target, values in conditions.items() if values[0] != values[1]},
        ended,
        applied,
    )


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
        delta.EffectFollowUpCreate,
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
    effects = {effect_identity(effect): effect for effect in snapshot.effect_states}
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
    follow_ups = follow_up_creations(request, result, lifecycles, effects)
    expected_conditions, ended_conditions, applied = condition_transition(
        actors, effects, links, selected, follow_ups
    )
    if {
        target: (op.expected, op.value) for target, op in conditions.items()
    } != expected_conditions:
        _invalid("Concentration release changed conditions outside the removed lineage")

    changes = []
    follow_up_targets = {identity[0] for identity in selected if effects[identity].end_effects}
    movement_affected = set(follow_up_targets)
    movement_affected.update(target for target, names in ended_conditions.items() if names)
    if follow_up_targets:
        final_effects = [effect for identity, effect in effects.items() if identity not in selected]
        final_effects.extend(op.effect for op in follow_ups.values())
        for target in follow_up_targets:
            actor = actors[target]
            if target in expected_conditions:
                actor = actor.model_copy(
                    update={"conditions": list(expected_conditions[target][1])}
                )
            ledger = combat.movement_ledgers[target]
            remaining = ledger.remaining(haste_speed(actor, final_effects, ledger.active_mode))
            op = budgets.get(target)
            candidate = op.value.movement_remaining if op is not None else actor.movement_remaining
            if candidate != remaining:
                _invalid("Concentration release movement differs from retained effects and ledger")
    for target, operation in budgets.items():
        if (
            target not in movement_affected
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
        identity = (target, effect_id, origin)
        if follow_up := follow_ups.get(identity):
            if len(result.proposed_events) <= len(events):
                _invalid("Follow-up creation lacks application evidence")
            event = result.proposed_events[len(events)]
            require_effect_applied(event, follow_up.effect)
            events.append(event)
            events.extend(applied[identity])
    if tuple(events) != result.proposed_events:
        _invalid("Concentration release events differ from the adopted graph transition")

    for identity in chain:
        changes.append(("effect_lifecycle", identity, "update", lifecycles[identity]))
        if follow_up := follow_ups.get(identity):
            changes.append(
                ("effect_follow_up", effect_identity(follow_up.effect), "create", follow_up)
            )
    changes.append(("hold_concentration", source, "update", group))
    return tuple(changes)
