"""Plan uninterrupted host-authorized rests and adopt their typed recovery."""

from dataclasses import dataclass

from dnd5e_engine.evaluation_aid import reviewed_hp_bonus
from dnd5e_engine.evaluation_delta import HPDelta, ResourceUpdate, WorldRestEffectEnd
from dnd5e_engine.evaluation_rest import rest_inert_elapsed_effects
from dnd5e_engine.evaluation_wearables import (
    attune_cloak_reason,
    attunement_needs,
    reviewed_protective_cloak,
)

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
    attune_instance_id: str | None = None
    unattune_instance_id: str | None = None


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
    focus = (
        operation.attune_instance_id
        if operation.attune_instance_id is not None
        else operation.unattune_instance_id
    )
    if focus is not None and (
        type(focus) is not str
        or not focus
        or operation.hit_dice
        or (operation.attune_instance_id is not None and operation.unattune_instance_id is not None)
    ):
        raise StateMachineError(
            "invalid_operation", "Attunement needs one dedicated item-focused short rest"
        )


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


def prepare_rest_time_expiries(world, request, plan):
    """Fix absolute deadlines for the evaluator's rest-neutral elapsed effects."""
    start, target, _ = plan
    expiries = {}
    for effect in rest_inert_elapsed_effects(request, world.loader):
        identity = (effect.target_id, effect.id, effect.origin)
        application = world.characters.effects.spell_application(request.session_id, identity)
        if application is None or application.effect != effect or application.expires_tick is None:
            raise StateMachineError("rest_interrupted", "Elapsed spell application changed")
        if start <= application.expires_tick <= target:
            expiries[identity] = application.expires_tick
    return expiries


def rest_snapshot_after_time(snapshot, expiries, events):
    """Project only expected owner-clock removals before rechecking the fixed rest."""
    if not expiries:
        return snapshot
    settled = {
        tuple(event.payload["identity"]): event.payload["boundary_tick"]
        for event in events
        if event.event_type == "effect.expired"
        and event.mechanical_phase == "effects.after_expiry"
        and tuple(event.payload["identity"]) in expiries
    }
    if settled != expiries:
        raise StateMachineError("rest_interrupted", "An expected elapsed spell did not settle")
    return snapshot.model_copy(
        update={
            "effect_states": tuple(
                effect
                for effect in snapshot.effect_states
                if (effect.target_id, effect.id, effect.origin) not in expiries
            )
        }
    )


def prepare_attunement(row, actor_id, operation, world, snapshot):
    """Fix the exact item contact, existing bonds and actor facts for one short rest."""
    iid = (
        operation.attune_instance_id
        if operation.attune_instance_id is not None
        else operation.unattune_instance_id
    )
    if iid is None:
        return None, None
    subject, target = attunement_needs(actor_id, iid)
    actor = next((a for a in snapshot.character_states if a.entity_id == subject), None)
    if actor is None:
        return None, "invalid_actor"
    sid = row["session_id"]
    try:
        entry = world.objects._direct_item(sid, subject, target)
    except StateMachineError as exc:
        return None, exc.code
    asset = world.loader.get_item(entry["item_slug"])
    if asset is None:
        return None, "invalid_target"
    bonds = tuple(
        (r["object_id"], r["item_slug"])
        for r in world.facts._conn.execute(
            "SELECT object_id,item_slug FROM objects WHERE session_id=? "
            "AND item_attuned_actor_id=? ORDER BY object_id",
            (sid, subject),
        )
    )
    if operation.attune_instance_id is not None:
        if entry.get("attuned_actor_id") is not None:
            return None, "item_attunement_conflict"
        reason = attune_cloak_reason(actor, asset, tuple(slug for _, slug in bonds), world.loader)
        if reason is not None:
            return None, reason
        mode = "attune"
    else:
        if not reviewed_protective_cloak(asset):
            return None, "unsupported_item_binding"
        if entry.get("attuned_actor_id") != subject:
            return None, "item_attunement_conflict"
        mode = "unattune"
    return (mode, target, entry["item_slug"], bonds), None


def rest_changes(request, result):
    """Map verified self-recovery and Hit Dice costs into guarded character writes.

    The pinned evaluator owns healing, recovery and randomness. Compare the
    proposed operations with owned fixed inputs, declared dice and typed evidence
    before staging any time, resource, HP, effect or RNG changes.
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
    completions = [op for op in result.state_delta.operations if type(op) is WorldRestEffectEnd]
    expected_endings = (
        tuple(
            effect
            for effect in snapshot.effect_states
            if effect.target_id == actor_id and effect.duration.until_long_rest
        )
        if not short
        else ()
    )
    if len(completions) != bool(expected_endings):
        raise StateMachineError("invalid_rule_result", "Rest expiry differs from owned effects")
    ending = completions[0] if completions else None
    hp_pair = ending.hp if ending is not None else None
    if ending is not None and (
        ending.expected != expected_endings
        or ending.completed_at_tick != request.payload.completed_at_tick
        or (hp_pair is not None)
        != any(reviewed_hp_bonus(effect) is not None for effect in expected_endings)
        or (
            hp_pair is not None
            and (
                hp_pair.expected_current != actor.hp_current
                or hp_pair.expected_maximum != actor.hp_max
                or hp_pair.current != hp_pair.maximum
                or hp_pair.maximum > actor.hp_max
                or hp_pair.zero_consequence != "none"
            )
        )
    ):
        raise StateMachineError("invalid_rule_result", "Rest expiry changed unrelated facts")
    healed, changes = 0, []
    for op in result.state_delta.operations:
        if type(op) is WorldRestEffectEnd:
            changes.append(("rest_effect", actor_id, "expire", op.expected))
            if hp_pair is not None:
                changes.append(("actor", actor_id, "hp_pair", hp_pair))
            continue
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
            and hp_pair is None
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
    if hp_pair is not None and events:
        # The evaluator verifies healing from the post-expiry HP. Here the
        # paired write is guarded against the fixed pre-expiry HP facts.
        healed = getattr(events[0], "hp_regained", 0)
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
