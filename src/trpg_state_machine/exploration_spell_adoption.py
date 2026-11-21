"""Guard an exploration spell's payment and consequences against the fixed input."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.damage_rules import healing_balance
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_exploration_healing import exploration_healing_activity
from dnd5e_engine.evaluation_spells import spell_event
from dnd5e_engine.evaluation_world_effect import WorldEffectApplicationRequest
from dnd5e_engine.events import EffectApplied, HealingApplied, IntentSubmitted, SpellCast

from .errors import StateMachineError


def _slot_change(request, spend):
    """Bind a single ordinary slot payment to the caster's fixed pool and level."""
    resources = request.state_snapshot.resource_state
    if (
        resources is None
        or spend.owner_id != request.actor_id
        or spend.expected not in resources.pools
        or spend.expected.kind != "spell_slot"
        or spend.expected.level != request.payload.slot_level
        or spend.amount != -1
    ):
        raise StateMachineError("invalid_rule_result", "Exploration spell payment differs")
    return ("resource", spend.owner_id, spend.pool_id, (spend.expected.current, 1))


def exploration_spell_changes(request, result, loader):
    """Select the bound spell's consequence profile before accepting any writes."""
    spell = loader.get_spell(request.payload.spell_id)
    if spell is not None and (activity := exploration_healing_activity(loader, spell)):
        return _healing_changes(request, result, spell, activity)
    return _timed_spell_changes(request, result, spell)


def environmental_spell_changes(request, result):
    """Bind the payment of a previously verified environmental cast to its one owned slot."""
    # RuleEvaluationResult.verify_request checks the exact typed environmental
    # create operation, the payment (if any), and the unchanged RNG and events.
    operations = result.state_delta.operations
    return (_slot_change(request, operations[0]),) if request.payload.slot_level else ()


def _healing_changes(request, result, spell, activity):
    """Bind the rolled HEAL event to the guarded HP edit and slot spend."""
    snapshot, payload = request.state_snapshot, request.payload
    target = next((a for a in snapshot.character_states if a.entity_id == payload.target_id), None)
    events = result.proposed_events
    if len(events) != 3 or tuple(type(event) for event in events) != (
        IntentSubmitted,
        SpellCast,
        HealingApplied,
    ):
        raise StateMachineError("invalid_rule_result", "Unexpected exploration healing events")
    intent, cast, healing = events
    if (
        target is None
        or not target.is_alive
        or target.hp_current <= 0
        or intent
        != IntentSubmitted(
            actor_id=request.actor_id,
            intent_type="cast_spell",
            spell_id=payload.spell_id,
            target_id=payload.target_id,
            source_id=payload.spell_source_id,
            activity_id=activity.id,
        )
        or cast != spell_event(request.actor_id, spell, payload.slot_level)
        or healing.target_id != payload.target_id
        or healing.amount < 0
        or not result.rng_transition.state_changed
    ):
        raise StateMachineError("invalid_rule_result", "Exploration healing evidence differs")
    hp = healing_balance(target.hp_current, target.hp_max, healing.amount)
    operations = result.state_delta.operations
    if (
        not operations
        or type(operations[0]) is not delta.ResourceUpdate
        or len(operations) != 1 + int(hp != target.hp_current)
    ):
        raise StateMachineError("invalid_rule_result", "Unexpected exploration healing writes")
    spend = _slot_change(request, operations[0])
    if hp == target.hp_current:
        return (spend,)
    if operations[1] != delta.HPDelta(
        kind="actor.hp_delta",
        target_id=target.entity_id,
        expected_hp=target.hp_current,
        amount=hp - target.hp_current,
        resulting_hp=hp,
    ):
        raise StateMachineError("invalid_rule_result", "Exploration heal differs from its evidence")
    return (spend, ("actor", target.entity_id, "hp_current", hp))


def _timed_spell_changes(request, result, spell):
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

    spend = _slot_change(request, next(op for op in operations if type(op) is delta.ResourceUpdate))
    create = next(op for op in operations if type(op) is delta.WorldEffectCreate)
    if result.rng_transition.state_changed:
        raise StateMachineError("invalid_rule_result", "Timed spell RNG differs")

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
        spend,
        ("world_effect", payload.target_id, "create", proposal),
    )
