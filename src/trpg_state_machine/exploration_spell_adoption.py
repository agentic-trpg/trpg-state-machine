"""Guard an exploration spell's payment and consequences against the fixed input."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.damage_rules import healing_balance
from dnd5e_engine.evaluation_beacon import is_reviewed_beacon_effect
from dnd5e_engine.evaluation_cast_activity import cast_activity_components, casting_activity_seconds
from dnd5e_engine.evaluation_charged_item import exploration_charged_cast_profile
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_exploration_healing import exploration_healing_activity
from dnd5e_engine.evaluation_identify import identify_object
from dnd5e_engine.evaluation_multi_spells import verify_multi_spell_definition
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_spell_materials import spell_material_payment
from dnd5e_engine.evaluation_spells import spell_event
from dnd5e_engine.evaluation_timed_spells import verify_timed_spell_effect
from dnd5e_engine.events import EffectApplied, HealingApplied, IntentSubmitted, SpellCast
from dnd5e_engine.spellcasting import spell_component_metadata

from .errors import StateMachineError


def _slot_change(request, spend, *, allow_pact=False):
    """Bind one slot payment to the caster's fixed pool and supported payment policy."""
    resources = request.state_snapshot.resource_state
    if (
        resources is None
        or spend.owner_id != request.actor_id
        or spend.expected not in resources.pools
        or spend.expected.kind
        not in (("spell_slot", "pact_slot") if allow_pact else ("spell_slot",))
        or spend.expected.level != request.payload.slot_level
        or spend.amount != -1
    ):
        raise StateMachineError("invalid_rule_result", "Exploration spell payment differs")
    if allow_pact and spend != spell_payment(
        resources, request.actor_id, request.payload.slot_level
    ):
        raise StateMachineError("invalid_rule_result", "Exploration spell slot choice differs")
    return ("resource", spend.owner_id, spend.pool_id, (spend.expected.current, 1))


def exploration_spell_changes(request, result, loader):
    """Select the bound spell's consequence profile before accepting any writes."""
    spell = loader.get_spell(request.payload.spell_id)
    if spell is not None and (activity := exploration_healing_activity(loader, spell)):
        return _healing_changes(request, result, spell, activity, loader)
    return _timed_spell_changes(request, result, spell, loader)


def casting_activity_changes(request, result, loader):
    """Verify the frozen spell definition and translate a finish to guarded edits."""
    from dnd5e_engine.evaluation_timed_spells import _passive_profile

    payload = request.payload
    operations = result.state_delta.operations
    spell = loader.get_spell(payload.spell_id) if loader is not None else None
    if spell is None:
        raise StateMachineError("invalid_rule_result", "Casting activity lacks a bound spell")
    try:
        seconds = casting_activity_seconds(spell, as_ritual=payload.as_ritual)
        expected = (
            identify_object(spell, payload.identify, request.actor_id, loader)
            if payload.identify is not None
            else None
        )
        if payload.phase == "start":
            if operations[0].duration_seconds != seconds:
                raise ValueError("Activity duration differs from its bound spell")
            return ()
        if payload.elapsed_seconds != seconds:
            raise ValueError("Casting elapsed time differs from its bound spell")
        intent, cast, consequence = result.proposed_events
        if payload.identify is None:
            activity, effect, _ = _passive_profile(
                spell,
                target_id=request.actor_id,
                caster_id=request.actor_id,
                casting_activity=True,
            )
            if operations[-1].value.effect != effect_state(effect):
                raise ValueError("Casting effect differs from its bound spell")
        else:
            if consequence.identification != expected:
                raise ValueError("Identify result differs from its bound item")
            activity = spell.activities[0]
        if intent.activity_id != activity.id or (
            cast.components,
            cast.material,
            cast.material_consumed,
            cast.material_cost_gp,
        ) != cast_activity_components(spell):
            raise ValueError("Casting evidence differs from its bound spell")
    except ValueError as exc:
        raise StateMachineError("invalid_rule_result", str(exc)) from exc
    edits = () if payload.as_ritual else (_slot_change(request, operations[0]),)
    if payload.identify is not None:
        return edits
    return (*edits, ("world_effect", request.actor_id, "create", operations[-1].value))


def multi_exploration_spell_changes(request, result, loader):
    """Bind the /33 proposal to the ruleset before translating candidate writes."""
    if loader is None:
        raise StateMachineError("invalid_rule_result", "Multi spell has no bound ruleset loader")
    try:
        verify_multi_spell_definition(result, request, loader)
    except ValueError as exc:
        raise StateMachineError("invalid_rule_result", str(exc)) from exc
    payment, *effects = result.state_delta.operations
    return (
        _slot_change(request, payment),
        *(
            ("world_effect", effect.value.effect.target_id, "create", effect.value)
            for effect in effects
        ),
    )


def environmental_spell_changes(request, result):
    """Bind the owned slot of a previously verified environmental cast."""
    # RuleEvaluationResult.verify_request checks the exact typed environmental
    # create operation, the payment (if any), and the unchanged RNG and events.
    operations = result.state_delta.operations
    return (
        (_slot_change(request, operations[0], allow_pact=request.payload.spell_id == "darkness"),)
        if request.payload.slot_level
        else ()
    )


def charged_item_changes(request, result, loader):
    """Adopt one verified item cast after matching its frozen asset and charge cost.

    The result envelope has already bound the typed payment and optional HP
    edit to the exact request and evidence.
    """
    spend, *healing = result.state_delta.operations
    entry = spend.expected
    asset = loader.get_item(entry.item_slug) if loader is not None else None
    profile = (
        exploration_charged_cast_profile(
            asset,
            loader,
            activity_id=request.payload.activity_id,
            cast_level=request.payload.cast_level,
        )
        if asset is not None
        else None
    )
    cast = result.proposed_events[1]
    if (
        profile is None
        or profile.activity.id != request.payload.activity_id
        or profile.spell.slug != cast.spell_id
        or profile.cost != spend.amount
        or (request.payload.area_target is not None) != (cast.spell_id == "mass-cure-wounds")
        or entry.charges_remaining_per_unit > profile.capacity
        or (cast.item_cast_level is not None) != profile.requires_held_attunement
        or (
            profile.requires_held_attunement
            and (
                entry.attuned_actor_id != request.actor_id
                or next(
                    a
                    for a in request.state_snapshot.character_states
                    if a.entity_id == request.actor_id
                ).held_weapon_instance_id
                != entry.instance_id
            )
        )
    ):
        raise StateMachineError("invalid_rule_result", "Item cast differs from its bound activity")
    return (
        (
            "item",
            entry.instance_id,
            "spend_charge" if spend.value.quantity else "depleted_staff",
            (entry.model_dump(mode="json"), spend.amount),
        ),
        *(("actor", edit.target_id, "hp_current", edit.resulting_hp) for edit in healing),
    )


def _healing_changes(request, result, spell, activity, loader):
    """Bind healing evidence and its RNG discipline to the guarded HP and slot edits."""
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
    expects_rng_draw = not any(
        effect.target_id == payload.target_id and is_reviewed_beacon_effect(effect, loader)
        for effect in snapshot.effect_states
    )
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
        or result.rng_transition.state_changed != expects_rng_draw
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


def _timed_spell_changes(request, result, spell, loader):
    """Return candidate writes for one paid cast and one terminal world effect.

    The caller validates the shared result envelope and rechecks the version
    and resource pre-value in its write transaction. The effect's relative
    duration is converted into a deadline only during that transaction.
    """
    payload = request.payload
    snapshot = request.state_snapshot
    operations = result.state_delta.operations
    material_changes = ()
    expected_types = {delta.ResourceUpdate, delta.WorldEffectCreate}
    if payload.material_instance_id is not None:
        expected_types.add(delta.SpellMaterialConsume)
    if len(operations) != len(expected_types) or {type(op) for op in operations} != expected_types:
        raise StateMachineError("invalid_rule_result", "Timed spell requires a slot and effect")

    if payload.material_instance_id is not None:
        if spell is None or len(spell.activities) != 1:
            raise StateMachineError("invalid_rule_result", "Material spell definition differs")
        try:
            material = spell_material_payment(
                spell,
                spell.activities[0].consumption,
                snapshot.inventory_state,
                actor_id=request.actor_id,
                instance_id=payload.material_instance_id,
                loader=loader,
            )
        except ValueError as exc:
            raise StateMachineError(
                "invalid_rule_result", "Spell material binding differs"
            ) from exc
        debit = next(op for op in operations if type(op) is delta.SpellMaterialConsume)
        if debit != material:
            raise StateMachineError("invalid_rule_result", "Spell material debit differs")
        material_changes = (
            ("item", material.expected.instance_id, "consume", material.expected.model_dump()),
        )
    spend = _slot_change(request, next(op for op in operations if type(op) is delta.ResourceUpdate))
    create = next(op for op in operations if type(op) is delta.WorldEffectCreate)
    if result.rng_transition.state_changed:
        raise StateMachineError("invalid_rule_result", "Timed spell RNG differs")

    proposal = create.value
    if spell is None:
        raise StateMachineError("invalid_rule_result", "Timed spell has no bound definition")
    try:
        verify_timed_spell_effect(request, spell, create)
    except ValueError as exc:
        raise StateMachineError(
            "invalid_rule_result", "Timed spell effect differs from its bound definition"
        ) from exc

    events = result.proposed_events
    if len(events) != 3 or tuple(type(event) for event in events) != (
        IntentSubmitted,
        SpellCast,
        EffectApplied,
    ):
        raise StateMachineError("invalid_rule_result", "Unexpected timed spell events")
    intent, cast, applied = events
    components, material_name, _, material_cost = spell_component_metadata(spell)
    if (
        intent.intent_type != "cast_spell"
        or intent.actor_id != request.actor_id
        or intent.spell_id != payload.spell_id
        or intent.source_id != payload.spell_source_id
        or intent.target_id != payload.target_id
        or not any(activity.id == intent.activity_id for activity in spell.activities)
        or cast.actor_id != request.actor_id
        or cast.spell_id != payload.spell_id
        or cast.slot_level != payload.slot_level
        or cast.ritual
        or cast.material_consumed != bool(material_changes)
        or (
            material_changes
            and (
                cast.components != list(components)
                or cast.material != material_name
                or cast.material_cost_gp != material_cost
            )
        )
        or effect_state(applied.effect) != proposal.effect
    ):
        raise StateMachineError("invalid_rule_result", "Timed spell events do not match the writes")

    return (
        spend,
        *material_changes,
        ("world_effect", payload.target_id, "create", proposal),
    )
