"""Admit and record uninterrupted, host-authorized long rest."""

from dataclasses import dataclass

from dnd5e_engine.evaluation_delta import HPDelta, ResourceUpdate

from .errors import StateMachineError
from .time_binding import MAX_TICK, TimeBinding
from .time_coordination import AdvanceSessionTime

_DURATION = 8 * 3600
_COOLDOWN = 16 * 3600


@dataclass(frozen=True, slots=True)
class LongRest:
    """Confirm a safe uninterrupted eight-hour rest for the selected PC.

    A trusted host confirms the sleep and activity arrangement. The command carries
    no caller-supplied elapsed time, completion proof, or recovery values.
    """


def prepare_long_rest(row, actor_id, world, records):
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
    last = records.last_long_rest(sid, actor_id)
    if last is not None and start - last["completed_tick"] < _COOLDOWN:
        return None, "rest_cooldown"
    target = start + _DURATION
    if target > MAX_TICK:
        return None, "rest_time_range"
    return (start, target, place), None


def advance_long_rest(row, package, command_id, clock, plan):
    """Stage time and its bounded work in the caller's rest effects savepoint."""
    _, target, _ = plan
    events = clock.advance(row, AdvanceSessionTime(target, source="activity"), package, command_id)
    if clock.progress(row["session_id"])["status"] != "completed":
        raise StateMachineError("rest_time_unsettled", "Long rest cannot finish in one advance")
    return events


def long_rest_changes(request, result):
    """Map this actor's bounded increases from the verified, draw-free RE result."""
    snapshot = request.state_snapshot
    actor_id = request.actor_id
    actor = next(a for a in snapshot.character_states if a.entity_id == actor_id)
    if result.rng_transition.state_changed or request.payload.rest_type != "long":
        raise StateMachineError("invalid_rule_result", "Expected a draw-free Long Rest")
    if snapshot.resource_state is None:
        raise StateMachineError("invalid_rule_result", "Long Rest requires fixed owned pools")
    pools = {p.pool_id: p for p in snapshot.resource_state.pools if p.owner_id == actor_id}
    increases = {"hit_dice": {}, "spell_slot": {}, "pact_slot": {}, "feature_uses": {}}
    healed, changes = 0, []
    for op in result.state_delta.operations:
        if type(op) is ResourceUpdate and (
            op.owner_id == actor_id and pools.pop(op.pool_id, None) == op.expected and op.amount > 0
        ):
            kind = op.expected.kind
            key = (
                op.pool_id
                if kind == "hit_dice"
                else op.expected.feature_slug
                if kind == "feature_uses"
                else op.expected.level
            )
            increases[kind][key] = op.amount
            changes.append(
                ("resource_restore", actor_id, op.pool_id, (op.expected.current, op.value.current))
            )
        elif type(op) is HPDelta and (
            op.target_id == actor_id
            and not healed
            and op.expected_hp == actor.hp_current
            and op.amount > 0
            and op.resulting_hp <= actor.hp_max
        ):
            healed = op.amount
            changes.append(("actor", actor_id, "hp_current", op.resulting_hp))
        else:
            raise StateMachineError("invalid_rule_result", "Long Rest changed unrelated facts")
    events = result.proposed_events
    event_types = ["rest_resolved", "healing_applied"] if healed else ["rest_resolved"]
    if [event.type for event in events] != event_types:
        raise StateMachineError("invalid_rule_result", "Unexpected Long Rest events")
    rest = events[0]
    restored = {
        "hit_dice": rest.hit_dice_restored,
        "spell_slot": rest.spell_slots_restored,
        "pact_slot": rest.pact_slots_restored,
        "feature_uses": rest.feature_uses_restored,
    }
    if (
        rest.actor_id != actor_id
        or rest.rest_type != "long"
        or rest.hit_dice
        or rest.hp_regained != healed
        or any(
            {k: v for k, v in values.items() if v} != increases[kind]
            for kind, values in restored.items()
        )
        or (healed and (events[1].target_id != actor_id or events[1].amount != healed))
    ):
        raise StateMachineError("invalid_rule_result", "Long Rest events differ from its changes")
    return changes
