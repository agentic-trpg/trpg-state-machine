"""Plan uninterrupted host-authorized rests and adopt their typed recovery."""

from dataclasses import dataclass

from dnd5e_engine.evaluation_delta import HPDelta, ResourceUpdate

from .errors import StateMachineError
from .time_binding import MAX_TICK, TimeBinding
from .time_coordination import AdvanceSessionTime

_SHORT_DURATION = 3600
_LONG_DURATION = 8 * 3600
_LONG_COOLDOWN = 16 * 3600


@dataclass(frozen=True, slots=True)
class LongRest:
    """Confirm a safe uninterrupted eight-hour rest for the selected PC.

    A trusted host confirms the sleep and activity arrangement. The command carries
    no caller-supplied elapsed time, completion proof, or recovery values.
    """


@dataclass(frozen=True, slots=True)
class HitDiceSpend:
    """Choose a positive count from one of the resting actor's exact Hit Dice pools."""

    pool_id: str
    amount: int


@dataclass(frozen=True, slots=True)
class ShortRest:
    """Confirm a single uninterrupted hour of light activity for the selected PC.

    The caller preselects the dice to spend in roll order. An empty tuple requests
    only the recovery the pinned rules grant without spending Hit Dice.
    """

    hit_dice: tuple[HitDiceSpend, ...] = ()


def validate_short_rest(operation: ShortRest) -> None:
    """Check command shape while leaving ownership and affordability to the evaluator."""
    selections = operation.hit_dice
    if type(selections) is not tuple or any(
        type(spend) is not HitDiceSpend
        or type(spend.pool_id) is not str
        or not spend.pool_id
        or type(spend.amount) is not int
        or spend.amount <= 0
        for spend in selections
    ):
        raise StateMachineError("invalid_operation", "Expected exact Hit Dice pools and counts")
    try:
        pool_ids = {spend.pool_id.encode("utf-8", errors="strict") for spend in selections}
    except UnicodeEncodeError as exc:
        raise StateMachineError("invalid_operation", "Invalid Unicode Hit Dice pool ID") from exc
    if len(pool_ids) != len(selections):
        raise StateMachineError("invalid_operation", "Each Hit Dice pool can only be selected once")


def prepare_rest(row, actor_id, world, records, operation):
    """Return an authorized activity's prospective tick range or a refusal code."""
    sid = row["session_id"]
    if world._active_combat(sid):
        return None, "unsupported_combat_world_operation"
    binding, start = world.facts.time_state(sid)
    if type(binding) is not TimeBinding:
        return None, "unsupported_time_protocol"
    if row["mechanical_schema_version"] != "engine-snapshot/20":
        return None, "unsupported_mechanical_version"
    clock = world.runtime.time
    pending = clock.progress(sid)
    if (pending is not None and pending["status"] != "completed") or clock.waits.active(sid):
        return None, "time_advance_pending"
    if records.active(sid, actor_id) is not None:
        return None, "rest_activity_active"
    place = world.facts.carrier_position(sid, actor_id)
    if place is None or place[0] != row["current_scene_id"]:
        return None, "rest_location"
    long_rest = type(operation) is LongRest
    if long_rest:
        last = records.last_long_rest(sid, actor_id)
        if last is not None and start - last["completed_tick"] < _LONG_COOLDOWN:
            return None, "rest_cooldown"
    target = start + (_LONG_DURATION if long_rest else _SHORT_DURATION)
    if target > MAX_TICK:
        return None, "rest_time_range"
    return (start, target, place), None


def advance_rest(row, package, command_id, clock, plan):
    """Stage time and its bounded work in the caller's rest effects savepoint."""
    _, target, _ = plan
    events = clock.advance(row, AdvanceSessionTime(target, source="activity"), package, command_id)
    if clock.progress(row["session_id"])["status"] != "completed":
        raise StateMachineError("rest_time_unsettled", "Rest cannot finish in one advance")
    return events


def rest_changes(request, result):
    """Map verified self-recovery and Hit Dice costs into guarded character writes.

    The pinned evaluator owns healing, recovery and randomness. Compare the
    proposed operations with owned fixed inputs, declared dice and typed evidence
    before staging any time, resource, HP or RNG changes.
    """
    snapshot = request.state_snapshot
    actor_id = request.actor_id
    actor = next(a for a in snapshot.character_states if a.entity_id == actor_id)
    rest_type = request.payload.rest_type
    short = rest_type == "short"
    choices = {spend.pool_id: spend.amount for spend in request.payload.hit_dice}
    if (not short and choices) or result.rng_transition.state_changed != bool(choices):
        raise StateMachineError("invalid_rule_result", "Rest RNG differs from the chosen dice")
    if snapshot.resource_state is None:
        raise StateMachineError("invalid_rule_result", "Rest requires fixed owned pools")
    pools = {p.pool_id: p for p in snapshot.resource_state.pools if p.owner_id == actor_id}
    increases = {"hit_dice": {}, "spell_slot": {}, "pact_slot": {}, "feature_uses": {}}
    spent = {}
    healed, changes = 0, []
    for op in result.state_delta.operations:
        if type(op) is ResourceUpdate and (
            op.owner_id == actor_id and pools.pop(op.pool_id, None) == op.expected
        ):
            kind = op.expected.kind
            if short and kind == "hit_dice" and choices.get(op.pool_id) == -op.amount:
                spent[op.pool_id] = (op.expected.die_size, -op.amount)
                changes.append(
                    ("resource", actor_id, op.pool_id, (op.expected.current, -op.amount))
                )
                continue
            if op.amount > 0 and (not short or kind in ("pact_slot", "feature_uses")):
                key = (
                    op.pool_id
                    if kind == "hit_dice"
                    else op.expected.feature_slug
                    if kind == "feature_uses"
                    else op.expected.level
                )
                increases[kind][key] = op.amount
                changes.append(
                    (
                        "resource_restore",
                        actor_id,
                        op.pool_id,
                        (op.expected.current, op.value.current),
                    )
                )
                continue
        elif type(op) is HPDelta and (
            op.target_id == actor_id
            and not healed
            and (not short or choices)
            and op.expected_hp == actor.hp_current
            and op.amount > 0
            and op.resulting_hp <= actor.hp_max
        ):
            healed = op.amount
            changes.append(("actor", actor_id, "hp_current", op.resulting_hp))
            continue
        raise StateMachineError("invalid_rule_result", "Rest changed unrelated facts")
    events = result.proposed_events
    event_types = ["rest_resolved", "healing_applied"] if healed else ["rest_resolved"]
    if [event.type for event in events] != event_types:
        raise StateMachineError("invalid_rule_result", "Unexpected Rest events")
    rest = events[0]
    restored = {
        "hit_dice": rest.hit_dice_restored,
        "spell_slot": rest.spell_slots_restored,
        "pact_slot": rest.pact_slots_restored,
        "feature_uses": rest.feature_uses_restored,
    }
    if (
        rest.actor_id != actor_id
        or rest.rest_type != rest_type
        or rest.hp_regained != healed
        or tuple((spend.pool_id, spend.amount) for spend in rest.hit_dice) != tuple(choices.items())
        or {spend.pool_id: (spend.die_size, spend.amount) for spend in rest.hit_dice} != spent
        or any(
            {k: v for k, v in values.items() if v} != increases[kind]
            for kind, values in restored.items()
        )
        or (healed and (events[1].target_id != actor_id or events[1].amount != healed))
    ):
        raise StateMachineError("invalid_rule_result", "Rest events differ from its changes")
    return changes
