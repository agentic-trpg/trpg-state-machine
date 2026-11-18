"""Guard a timed spell's slot spend and effect against the fixed RE input."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_world_effect import WorldEffectApplicationRequest
from dnd5e_engine.events import EffectApplied, IntentSubmitted, SpellCast

from .errors import StateMachineError


def timed_spell_changes(request, result, loader):
    """Return candidate writes for one paid cast and one terminal world effect.

    The caller validates the shared result envelope and rechecks the version
    and resource pre-value in its write transaction. The effect's relative
    duration is converted into a deadline only during that transaction.
    """
    payload = request.payload
    snapshot = request.state_snapshot
    operations = result.state_delta.operations
    if len(operations) != 2 or {type(op) for op in operations} != {
        delta.ResourceUpdate,
        delta.WorldEffectCreate,
    }:
        raise StateMachineError("invalid_rule_result", "Timed spell requires a slot and effect")

    spend = next(op for op in operations if type(op) is delta.ResourceUpdate)
    create = next(op for op in operations if type(op) is delta.WorldEffectCreate)
    if (
        spend.owner_id != request.actor_id
        or spend.expected not in snapshot.resource_state.pools
        or spend.expected.kind != "spell_slot"
        or spend.expected.level != payload.slot_level
        or spend.amount != -1
        or result.rng_transition.state_changed
    ):
        raise StateMachineError("invalid_rule_result", "Timed spell payment or RNG differs")

    proposal = create.value
    association = WorldEffectApplicationRequest(
        schema_version="engine-world-effect/1",
        session_id=request.session_id,
        command_id=request.command_id,
        source_id=request.actor_id,
        target_id=payload.target_id,
        ruleset_binding=request.ruleset_binding,
    )
    try:
        proposal.verify_request(association)
    except ValueError as exc:
        raise StateMachineError(
            "invalid_rule_result", "Timed spell effect association differs"
        ) from exc

    if (
        not create.expected_absent
        or not any(actor.entity_id == payload.target_id for actor in snapshot.character_states)
        or any(
            (effect.target_id, effect.id, effect.origin) == proposal.identity
            for effect in snapshot.effect_states
        )
    ):
        raise StateMachineError("invalid_rule_result", "Timed spell effect precondition differs")

    events = result.proposed_events
    spell = loader.get_spell(payload.spell_id)
    if len(events) != 3 or tuple(type(event) for event in events) != (
        IntentSubmitted,
        SpellCast,
        EffectApplied,
    ):
        raise StateMachineError("invalid_rule_result", "Unexpected timed spell events")
    intent, cast, applied = events
    if (
        intent.intent_type != "cast_spell"
        or intent.actor_id != request.actor_id
        or intent.spell_id != payload.spell_id
        or intent.source_id != payload.spell_source_id
        or intent.target_id != payload.target_id
        or spell is None
        or not any(activity.id == intent.activity_id for activity in spell.activities)
        or cast.actor_id != request.actor_id
        or cast.spell_id != payload.spell_id
        or cast.slot_level != payload.slot_level
        or cast.ritual
        or cast.material_consumed
        or effect_state(applied.effect) != proposal.effect
    ):
        raise StateMachineError("invalid_rule_result", "Timed spell events do not match the writes")

    return (
        ("resource", spend.owner_id, spend.pool_id, (spend.expected.current, 1)),
        ("world_effect", payload.target_id, "create", proposal),
    )
