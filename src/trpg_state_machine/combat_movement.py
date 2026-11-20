"""Verify closed ordinary walking results before the shared combat adoption."""

from itertools import pairwise

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_movement import timed_walk_bonus, walk_speed
from dnd5e_engine.events import ActorMoved

from .errors import StateMachineError


def _invalid(message):
    """Report a malformed walking proposal without committing a rules refusal."""
    raise StateMachineError("invalid_rule_result", message)


def ordinary_walk_changes(request, result):
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
        timed = timed_walk_bonus(snapshot)
        speed = walk_speed(actor, timed[1] if timed is not None and timed[0] == actor_id else 0)
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
    """Validate live path edges and exclude unadopted region transitions before writes.

    The command's OCC and fixed snapshot checks have already matched the current
    map. Reuse its prepared topology and query only the proposed path's cells.
    """
    scene = position.scene_id
    grid = world.facts.maps.scene(sid, scene).grid.topology
    path = position.path
    if any(grid.edge_distance(start, end) is None for start, end in pairwise(path)):
        _invalid("Walking route contains an invalid grid edge")
    environment = world.environment
    initial = environment.region_condition_sources(sid, scene, path[0])
    if any(
        environment.region_condition_sources(sid, scene, end) != initial for end in path[1:]
    ) or world.objects.mechanisms.has_armed_region_entry(sid, scene, path):
        raise StateMachineError("movement.capability", "Route exceeds supported combat movement")
