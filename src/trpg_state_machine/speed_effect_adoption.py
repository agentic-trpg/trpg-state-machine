"""Validate the bounded Pass that carries or expires one attack speed effect."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.events import EffectExpired, IntentSubmitted

from .concentration_adoption import effect_identity
from .errors import StateMachineError


def supports_pass_roster(actors):
    """Check the host's currently supported ordinary Pass roster."""
    return all(actor.is_alive and actor.hp_current > 0 for actor in actors)


def _invalid(message):
    """Reject a malformed speed effect transition before entering the write scope."""
    raise StateMachineError("invalid_rule_result", message)


def speed_effect_pass_admission(request, result):
    """Bind the exact removal, or lack of one, to the incoming actor's START.

    The fixed input has one independently admitted speed effect graph. The
    caller also validates the ordinary turn transition and movement budgets.
    """
    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    (clock,) = combat.effect_lifecycles
    (lineage,) = combat.conditions_by_effect
    effect = next(
        effect for effect in snapshot.effect_states if effect_identity(effect) == clock.identity
    )
    turns = [op for op in result.state_delta.operations if type(op) is delta.TurnUpdate]
    updates = [
        op for op in result.state_delta.operations if type(op) is delta.EffectLifecycleUpdate
    ]
    if (
        len(turns) != 1
        or not 0 <= turns[0].value.current_turn_index < len(combat.initiative_ids)
        or result.rng_transition.state_changed
    ):
        _invalid("Speed effect Pass requires one ordinary turn transition without RNG")
    incoming = turns[0].value
    next_actor = combat.initiative_ids[incoming.current_turn_index]
    expires = clock.state.expires_at(
        next_actor, "start", incoming.turn_serial, incoming.round_number
    )
    if len(updates) != int(expires):
        _invalid("Speed effect Pass omitted or added an expiry outside its START")

    events = result.proposed_events
    if (
        not events
        or events[0] != IntentSubmitted(actor_id=request.actor_id, intent_type="pass")
        or any(
            event.type
            not in {"turn_phase", "turn_ended", "round_started", "turn_started", "effect_expired"}
            for event in events[1:]
        )
    ):
        _invalid("Speed effect Pass proposed events outside its turn transition")
    expiry = [index for index, event in enumerate(events) if event.type == "effect_expired"]
    if len(expiry) != int(expires):
        _invalid("Speed effect Pass must pair expiry evidence with its typed removal")
    if not expires:
        return (), effect.target_id, snapshot.effect_states

    (op,) = updates
    if (
        op.combat_id != combat.combat_id
        or op.identity != clock.identity
        or op.expected_effect != effect
        or op.expected_lifecycle != clock
        or op.expected_lineage != lineage
        or op.effect is not None
        or op.lifecycle is not None
        or op.lineage is not None
    ):
        _invalid("Speed effect removal differs from the fixed effect, clock or lineage")
    (index,) = expiry
    if (
        events[index]
        != EffectExpired(
            target_id=effect.target_id, effect_id=effect.id, origin=effect.origin, reason="duration"
        )
        or index < 2
        or events[index - 2].type != "turn_started"
        or events[index - 1].type != "turn_phase"
        or events[index - 1].phase != "turn_start"
    ):
        _invalid("Speed effect expiry evidence differs from the incoming START")
    remaining = tuple(
        effect for effect in snapshot.effect_states if effect_identity(effect) != clock.identity
    )
    return (("effect_lifecycle", op.identity, "update", op),), effect.target_id, remaining
