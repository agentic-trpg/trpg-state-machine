"""Verify closed ordinary walking results before the shared combat adoption."""

from itertools import pairwise

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_movement import timed_walk_bonuses, walk_speed
from dnd5e_engine.evaluation_region_effect import supports_fixed_combat_regions
from dnd5e_engine.evaluation_speed_effects import (
    has_speed_effects,
    is_bound_attack_speed_effect,
    speed_effect_support_failure,
    speed_with_effects,
)
from dnd5e_engine.evaluation_state import CharacterStateV2, MovementLedgerState
from dnd5e_engine.evaluation_world_spell_modifiers import world_spell_combat_modifiers
from dnd5e_engine.events import ActorMoved

from .errors import StateMachineError
from .package import canonical_json


def _invalid(message):
    """Report a malformed walking proposal without committing a rules refusal."""
    raise StateMachineError("invalid_rule_result", message)


def refresh_world_walk_expiry(world, sid, expired):
    """Refresh one combat movement mirror after a supported elapsed walk modifier ends."""
    if not any(change.key == "system.attributes.movement.walk" for change in expired.changes):
        return
    try:
        world_spell_combat_modifiers((expired,))
    except ValueError:
        return
    target = expired.target_id
    ledger_json = world.combats.active_movement_ledger(sid, target)
    if ledger_json is None:
        return
    ledger = MovementLedgerState.model_validate_json(ledger_json)
    if ledger.active_mode != "walk":
        return
    remaining = tuple(
        EffectState.model_validate_json(canonical_json(data))
        for data in world.characters.effects.for_targets(sid, (target,))
    )
    facts = world.characters.rule_actors(sid, (target,))[target]
    actor = CharacterStateV2.model_validate_json(canonical_json(facts))
    if len(remaining) == 1 and is_bound_attack_speed_effect(remaining[0], world.loader):
        speed = speed_with_effects(actor, remaining, ledger.active_mode)
    else:
        try:
            bonus, _ = world_spell_combat_modifiers(remaining).get(target, (0, None))
        except ValueError:
            return
        speed = walk_speed(actor, bonus)
    budget = ledger.remaining(speed)
    if budget != actor.movement_remaining:
        world.characters.update_rule_metadata(sid, target, "movement_remaining", budget)


def ordinary_walk_changes(request, result, *, loader):
    """Bind the event and guarded position, expenditure and budget to the fixed route.

    RE owns routing, pricing and reaction admission. The host correlates the
    event and guarded changes. The write scope checks current World route dependencies.
    """
    snapshot, actor_id = request.state_snapshot, request.actor_id
    combat = snapshot.combat_state
    operations = {type(op): op for op in result.state_delta.operations}
    if (
        len(result.state_delta.operations) != 3
        or operations.keys()
        != {delta.PositionUpdate, delta.MovementLedgerUpdate, delta.ActionBudgetUpdate}
        or result.rng_transition.state_changed
        or combat.ended
        or combat.initiative_ids[combat.current_turn_index] != actor_id
        or combat.pending_reactions
        or combat.active_reaction_responses
        or combat.reaction_effects_pending_expiry
    ):
        _invalid("Ordinary walk must change only its mover's position and movement budget")
    position = operations[delta.PositionUpdate]
    ledger = operations[delta.MovementLedgerUpdate]
    budget = operations[delta.ActionBudgetUpdate]
    if (
        position.actor_id != actor_id
        or position.combat_id != combat.combat_id
        or position.scene_id != snapshot.scene_state.scene_id
        or position.expected != combat.actor_zone[actor_id]
        or position.value != request.payload.target_zone_id
        or position.value in combat.actor_zone.values()
        or ledger.actor_id != actor_id
        or ledger.expected != combat.movement_ledgers[actor_id]
        or ledger.expected.active_mode != "walk"
        or budget.actor_id != actor_id
    ):
        _invalid("Walking edit differs from the requested actor, scene, route or ledger")
    actor = next(actor for actor in snapshot.character_states if actor.entity_id == actor_id)
    if actor_id in combat.dead_ids or not actor.is_alive or actor.hp_current <= 0:
        _invalid("Incapacitated actor cannot walk")
    events = result.proposed_events
    distance = (len(position.path) - 1) * snapshot.scene_state.grid.cell_size_ft
    if (
        len(events) != 1
        or not isinstance(event := events[0], ActorMoved)
        or event.actor_id != actor_id
        or event.from_zone != position.expected
        or event.to_zone != position.value
        or event.path != position.path
        or event.forced
        or event.movement_mode != "walk"
        or event.distance_ft != distance
        or event.movement_cost_ft < distance
    ):
        _invalid("Walking event differs from the adopted route")
    try:
        if has_speed_effects(snapshot):
            if loader is None or speed_effect_support_failure(snapshot, loader) is not None:
                _invalid("Walking result requires the bound speed effect graph")
            speed = speed_with_effects(actor, snapshot.effect_states)
        else:
            bonus = (
                0
                if loader is not None and supports_fixed_combat_regions(snapshot, loader)
                else timed_walk_bonuses(snapshot).get(actor_id, 0)
            )
            speed = walk_speed(actor, bonus)
        expected_ledger = ledger.expected.spend(
            cost_ft=event.movement_cost_ft,
            distance_ft=distance,
            mode="walk",
            effective_speed=speed,
        )
    except ValueError as exc:
        raise StateMachineError(
            "invalid_rule_result", "Walking route or allowance differs"
        ) from exc
    expected_budget = delta.AttackBudgetState.model_validate(
        {field: getattr(actor, field) for field in delta.AttackBudgetState.model_fields}
    )
    remaining = expected_ledger.remaining(speed)
    if (
        actor.movement_remaining != ledger.expected.remaining(speed)
        or ledger.value != expected_ledger
        or budget.expected != expected_budget
        or budget.value != expected_budget.model_copy(update={"movement_remaining": remaining})
    ):
        _invalid("Walking expenditure or remaining budget differs from the route")
    return (
        ("position", actor_id, "cell", position),
        ("ledger", actor_id, "movement", ledger.value.model_dump(mode="json")),
        ("actor", actor_id, "movement_remaining", remaining),
    )


def require_walk_route(world, sid, position):
    """Validate live path edges and return supported source membership transitions.

    The command's OCC and fixed snapshot checks have already matched the current
    map. Reuse its prepared topology and query only the proposed path's cells.
    """
    scene = position.scene_id
    grid = world.facts.maps.scene(sid, scene).grid.topology
    path = position.path
    if any(grid.edge_distance(start, end) is None for start, end in pairwise(path)):
        _invalid("Walking route contains an invalid grid edge")
    environment = world.environment
    if world.objects.mechanisms.has_armed_region_entry(sid, scene, path):
        raise StateMachineError("movement.capability", "Route exceeds supported combat movement")
    memberships = [
        (
            set(environment.region_condition_sources(sid, scene, cell)),
            set(environment.region_condition_sources(sid, scene, cell, combat=True)),
        )
        for cell in path
    ]
    edges = []
    for (before, combat_before), (after, combat_after) in pairwise(memberships):
        if (before - combat_before) != (after - combat_after):
            raise StateMachineError("movement.capability", "Route crosses an unadopted fixed area")
        edges.extend((False, source, rule) for source, rule in sorted(combat_before - combat_after))
        edges.extend((True, source, rule) for source, rule in sorted(combat_after - combat_before))
    return edges
