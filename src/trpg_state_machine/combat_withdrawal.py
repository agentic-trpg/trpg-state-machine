"""Adopt the Session's fixed encounter boundary through ordinary paid walking."""

from dataclasses import dataclass
from itertools import pairwise

from dnd5e_engine.evaluation_delta import ActionBudgetUpdate, MovementLedgerUpdate
from dnd5e_engine.evaluation_opportunity import opportunity_dependencies, step_reactors
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene

from .combat import DistanceWithdrawal
from .errors import StateMachineError
from .grid import cell_in_bounds
from .spatial import SpatialPosition


@dataclass(frozen=True, slots=True)
class CombatWithdraw:
    """Choose a fixed-area exit or withdraw by distance from the current cell."""

    from_scene_id: str
    expected_cell: str
    target_cell: str | None = None
    passage_id: str | None = None


def validate_withdrawal(payload):
    """Validate the typed choice before taking any game-state snapshots."""
    values = (payload.from_scene_id, payload.expected_cell)
    target, passage = payload.target_cell, payload.passage_id
    if (target is not None and passage is not None) or any(
        type(value) is not str or not value.strip() or len(value) > 256
        for value in (*values, *(item for item in (target, passage) if item is not None))
    ):
        raise StateMachineError("invalid_operation", "Choose one outside cell or passage")


@dataclass(frozen=True, slots=True)
class PassageWithdrawal:
    """Freeze the live source edge and destination for a paid combat exit."""

    start: str
    threshold: str
    landing: SpatialPosition


def prepare_withdrawal(world, row, actor_id, payload, package, region):
    """Resolve the chosen source edge and permit only a one-step boundary exit."""
    sid = row["session_id"]
    if region is None:
        raise StateMachineError("combat.withdrawal_unavailable", "No active encounter region")
    before = world.characters.position(sid, actor_id)
    if (before.scene_id, before.cell) != (payload.from_scene_id, payload.expected_cell):
        raise StateMachineError("position_conflict", "Passage source precondition differs")
    if region.scene_id != before.scene_id or before.cell not in region.cells:
        raise StateMachineError("combat.withdrawal_unavailable", "Exit is outside the encounter")
    if payload.target_cell is not None:
        topology = world.grid.base_topology(sid, before.scene_id)
        if (
            payload.target_cell in region.cells
            or not cell_in_bounds(topology, payload.target_cell)
            or not topology.is_adjacent(before.cell, payload.target_cell)
        ):
            raise StateMachineError(
                "combat.withdrawal_route", "Choose an adjacent cell outside the encounter"
            )
        return payload.target_cell, None
    passages = world.facts.instances.passages(sid, passage_id=payload.passage_id)
    if not passages:
        raise StateMachineError("invalid_target", "No authorized passage")
    source = next((end for end in passages[0].endpoints if end.scene_id == before.scene_id), None)
    if source is None:
        raise StateMachineError("invalid_target", "No authorized passage")
    target = next(end for end in passages[0].endpoints if end.scene_id != before.scene_id)
    landing = world._passage_route(sid, actor_id, target.scene_id, package, payload.passage_id)
    return source.threshold_cell, PassageWithdrawal(
        source.standing_cell, source.threshold_cell, landing
    )


def crosses_boundary(region, position):
    """Check whether the verified route ever leaves the active fixed area."""
    if region.combat_id != position.combat_id or region.scene_id != position.scene_id:
        raise StateMachineError("invalid_rule_result", "Movement differs from the encounter")
    if position.expected not in region.cells:
        raise StateMachineError("invalid_mechanical_state", "Participant is outside the encounter")
    return any(cell not in region.cells for cell in position.path[1:])


def boundary_blockers(region, grid):
    """Build only the outside fringe needed to constrain an ordinary path."""
    exterior = set()
    for cell in region.cells:
        x, y = map(int, cell.split(","))
        exterior.update(
            f"{nx},{ny}"
            for nx in range(max(0, x - 1), min(grid.width, x + 2))
            for ny in range(max(0, y - 1), min(grid.height, y + 2))
        )
    return exterior - region.cells


def confine_walk(snapshot, blockers):
    """Add the fixed boundary to detached ordinary-walk geometry only."""
    if not blockers:
        return snapshot
    scene = snapshot.scene_state
    grid = scene.grid.model_copy(
        update={"blocked_cells": sorted(set(scene.grid.blocked_cells) | blockers)}
    )
    return snapshot.model_copy(update={"scene_state": scene.model_copy(update={"grid": grid})})


def require_no_unmasked_opportunity(snapshot, position, loader):
    """Keep artificial path blockers from masking an actual reaction on the paid route."""
    plan, failure = opportunity_dependencies(snapshot, loader)
    if failure:
        raise StateMachineError("movement.opportunity_attack", failure)
    grid = GridTopology(GridScene.model_validate(snapshot.scene_state.grid.model_dump()))
    for start, end in pairwise(position.path):
        fires, failure = step_reactors(
            snapshot, position.actor_id, start, end, loader, plan, grid=grid
        )
        if failure or fires:
            raise StateMachineError(
                "movement.opportunity_attack",
                failure or "The real movement route requires a reaction",
            )


def walking_position(changes):
    """Extract the sole guarded move after ordinary walking result validation."""
    return next((change[3] for change in changes if change[0] == "position"), None)


def require_walk_bystander_clearance(world, sid, snapshot, position):
    """Check authoritative non-roster occupants along the proposed combat path."""
    roster = snapshot.combat_state.actor_zone
    actors, _ = world.grid.reader.region_occupants(
        sid, position.scene_id, position.path[1:], include_actors=True, include_objects=False
    )
    if any(actor not in roster for actor, _ in actors):
        raise StateMachineError("movement.capability", "Route crosses a bystander's cell")


def check_withdrawal(snapshot, region, position, passage):
    """Require the chosen actual crossing and a compatible combat effect lifecycle."""
    if (
        position is None
        or len(position.path) != 2
        or (
            passage is None
            and not crosses_boundary(region, position)
            or passage is not None
            and tuple(position.path) != (passage.start, passage.threshold)
        )
    ):
        raise StateMachineError("combat.withdrawal_route", "Expected one paid step out of combat")
    require_withdrawable_effects(snapshot)


def require_withdrawable_effects(snapshot):
    """Preserve effects whose encounter clocks cannot continue in exploration."""
    combat = snapshot.combat_state
    # RUO-81-F1: combat-clock transitions need the disposition agreed in RUO-45.
    if (
        combat.effect_lifecycles
        or any(combat.concentration_rounds_remaining.values())
        or combat.pending_reactions
        or combat.reaction_effects_pending_expiry
    ):
        raise StateMachineError(
            "combat.withdrawal_effects_unsupported", "Withdrawal cannot suspend combat clocks"
        )


def distance_withdrawal_origin(world, row, snapshot, actor_id, payload, policy):
    """Verify the current participant and nearest living hostile using the real grid."""
    if not isinstance(policy, DistanceWithdrawal):
        raise StateMachineError("combat.withdrawal_unavailable", "Choose a permitted exit")
    combat = snapshot.combat_state
    if combat.ended or combat.combat_id != policy.combat_id:
        raise StateMachineError("combat.withdrawal_unavailable", "Encounter is not active")
    if combat.initiative_ids[combat.current_turn_index] != actor_id:
        raise StateMachineError("not_actor_turn", "Actor does not own this turn")
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    actor = actors[actor_id]
    if actor_id in combat.dead_ids or not actor.is_alive or actor.hp_current <= 0:
        raise StateMachineError("actor_incapacitated", "Actor cannot withdraw")
    cell = combat.actor_zone[actor_id]
    if (policy.scene_id, cell) != (payload.from_scene_id, payload.expected_cell):
        raise StateMachineError("position_conflict", "Withdrawal origin changed")
    require_withdrawable_effects(snapshot)
    opposition = combat.encounter_ids if actor_id in combat.party_ids else combat.party_ids
    grid = world.grid.base_topology(row["session_id"], policy.scene_id)
    distances = [
        grid.distance_ft(cell, combat.actor_zone[target])
        for target in opposition - combat.dead_ids
        if actors[target].is_alive and actors[target].hp_current > 0
    ]
    if not distances or any(
        distance is None or distance < policy.distance_ft for distance in distances
    ):
        raise StateMachineError(
            "combat.withdrawal_distance", "Withdrawal requires separation from the nearest hostile"
        )
    return SpatialPosition(policy.scene_id, cell)


def withdrawal_continues(snapshot, actor_id):
    """Determine whether a viable opposing roster remains after this departure."""
    combat = snapshot.combat_state
    return actor_id in combat.encounter_ids and bool(
        combat.encounter_ids - combat.dead_ids - {actor_id}
    )


def pass_snapshot_after_walk(snapshot, move_result, position):
    """Project the validated paid movement into the detached ordinary Pass input."""
    budget = next(
        op.value for op in move_result.state_delta.operations if type(op) is ActionBudgetUpdate
    )
    ledger = next(
        op.value for op in move_result.state_delta.operations if type(op) is MovementLedgerUpdate
    )
    actor_id = position.actor_id
    combat = snapshot.combat_state
    return snapshot.model_copy(
        update={
            "character_states": tuple(
                actor.model_copy(update=budget.model_dump())
                if actor.entity_id == actor_id
                else actor
                for actor in snapshot.character_states
            ),
            "combat_state": combat.model_copy(
                update={
                    "actor_zone": {**combat.actor_zone, actor_id: position.value},
                    "movement_ledgers": {**combat.movement_ledgers, actor_id: ledger},
                }
            ),
        }
    )


def commit_withdrawal(
    world, records, row, package, snapshot, actor_id, origin, next_actor, *, landing=None
):
    """Commit departure and an optional already-admitted cross-Scene landing."""
    sid = row["session_id"]
    combat_id = snapshot.combat_state.combat_id
    if next_actor is None:
        cells = {**snapshot.combat_state.actor_zone, actor_id: origin.cell}
        records.freeze_positions(sid, cells)
        records.update_rule_state(sid, "departed_actor_id", actor_id)
        records.update_rule_state(sid, "ended", True)
    else:
        records.withdraw_participant(sid, combat_id, actor_id, next_actor)
    events = []
    if landing is not None:
        events.extend(
            world.scenes.move_admitted(
                row,
                package,
                actor_id,
                landing,
                environment=world.environment,
                loader=world.loader,
            )
        )
    else:
        landing = origin
    events.append(
        world._capture_event(
            sid,
            package,
            "combat.withdrawn",
            {
                "actor_id": actor_id,
                "combat_id": combat_id,
                "scene_id": landing.scene_id,
                "from_scene_id": origin.scene_id,
                "cell": landing.cell,
                "reason": "withdrawal",
                "encounter_ended": next_actor is None,
            },
            phase="combat.after_withdrawal",
            source_actor_id=actor_id,
        )
    )
    return events
