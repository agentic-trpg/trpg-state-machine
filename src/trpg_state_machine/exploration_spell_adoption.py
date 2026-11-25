"""Guard an exploration spell's payment and consequences against the fixed input."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.damage_rules import healing_balance
from dnd5e_engine.evaluation_aid import (
    effective_hp_bonus,
    paired_hp_transition,
    reviewed_hp_bonus,
)
from dnd5e_engine.evaluation_beacon import is_reviewed_beacon_effect
from dnd5e_engine.evaluation_cast_activity import cast_activity_components, casting_activity_seconds
from dnd5e_engine.evaluation_charged_item import exploration_charged_cast_profile
from dnd5e_engine.evaluation_condition_choice import (
    condition_choice_profile,
    independent_condition_matches,
)
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_exploration_healing import (
    exploration_healing_activity,
    exploration_healing_life_delta,
)
from dnd5e_engine.evaluation_identify import identify_object
from dnd5e_engine.evaluation_multi_spells import verify_multi_spell_definition
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_spell_materials import spell_material_payment
from dnd5e_engine.evaluation_spells import spell_event
from dnd5e_engine.evaluation_timed_spells import verify_timed_spell_effect
from dnd5e_engine.events import (
    ConditionRemoved,
    EffectApplied,
    HealingApplied,
    IntentSubmitted,
    SpellCast,
)
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
    if request.payload.has_condition_choice:
        return _condition_choice_changes(request, result, spell)
    return _timed_spell_changes(request, result, spell, loader)


def _condition_choice_changes(request, result, spell):
    """Bind one exact independent removal and leave the other records in order."""
    profile = condition_choice_profile(spell) if spell is not None else None
    target = next(
        (
            a
            for a in request.state_snapshot.character_states
            if a.entity_id == request.payload.target_id
        ),
        None,
    )
    if profile is None or target is None:
        raise StateMachineError("invalid_rule_result", "Condition spell definition differs")
    matches = independent_condition_matches(request, target, profile[1])
    if len(matches) != 1:
        raise StateMachineError(
            "invalid_rule_result", "Selected condition does not have one independent source"
        )
    index = matches[0]
    selected = target.conditions[index]
    expected = (*target.conditions[:index], *target.conditions[index + 1 :])
    operations = result.state_delta.operations
    if (
        len(operations) != 2
        or type(operations[0]) is not delta.ResourceUpdate
        or operations[1]
        != delta.ConditionsUpdate(
            kind="actor.conditions_update",
            target_id=target.entity_id,
            expected=tuple(target.conditions),
            value=expected,
        )
        or result.rng_transition.state_changed
    ):
        raise StateMachineError("invalid_rule_result", "Condition choice writes differ")
    if result.proposed_events != (
        IntentSubmitted(
            actor_id=request.actor_id,
            intent_type="cast_spell",
            spell_id=spell.slug,
            target_id=target.entity_id,
            source_id=request.payload.spell_source_id,
            activity_id=profile[0].id,
        ),
        spell_event(request.actor_id, spell, request.payload.slot_level),
        ConditionRemoved(target_id=target.entity_id, condition=selected.condition),
    ):
        raise StateMachineError("invalid_rule_result", "Condition choice evidence differs")
    remaining = [condition.model_dump(mode="json") for condition in expected]
    payment = _slot_change(request, operations[0])
    choice = request.independent_condition
    if choice is not None:
        return (
            payment,
            (
                "independent_condition",
                target.entity_id,
                choice.condition_id,
                (selected.model_dump(mode="json"), remaining),
            ),
        )
    return payment, ("actor", target.entity_id, "conditions", remaining)


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
            if (
                operations[-1].value.effect != effect_state(effect)
                or operations[-1].value.concentration != spell.concentration
            ):
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


def ward_exploration_spell_changes(request, result, loader):
    """Adopt one bound ward payment and its linked concentration members."""
    if loader is None:
        raise StateMachineError("invalid_rule_result", "Ward has no bound ruleset loader")
    # validate_dnd_result has already asked RE to verify the exact result with
    # this loader; only translate its typed, paid proposal into owner writes.
    payment, *effects = result.state_delta.operations
    changes = (_slot_change(request, payment, allow_pact=request.payload.spell_id == "bane"),)
    if effects:
        changes += (
            (
                "world_effect_group",
                request.actor_id,
                "create",
                tuple(effect.value for effect in effects),
            ),
        )
    return changes


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
    """Bind healing, payment and zero-HP recovery to one guarded candidate."""
    snapshot, payload = request.state_snapshot, request.payload
    target = next((a for a in snapshot.character_states if a.entity_id == payload.target_id), None)
    events = result.proposed_events
    if len(events) not in (3, 4) or tuple(type(event) for event in events[:3]) != (
        IntentSubmitted,
        SpellCast,
        HealingApplied,
    ):
        raise StateMachineError("invalid_rule_result", "Unexpected exploration healing events")
    intent, cast, healing = events[:3]
    expects_rng_draw = not any(
        effect.target_id == payload.target_id and is_reviewed_beacon_effect(effect, loader)
        for effect in snapshot.effect_states
    )
    if (
        target is None
        or not target.is_alive
        or target.hp_current < 0
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
    life, removed = exploration_healing_life_delta(target, hp)
    expected_events = (
        (ConditionRemoved(target_id=target.entity_id, condition="unconscious"),) if removed else ()
    )
    if tuple(events[3:]) != expected_events:
        raise StateMachineError("invalid_rule_result", "Exploration recovery events differ")
    operations = result.state_delta.operations
    if (
        not operations
        or type(operations[0]) is not delta.ResourceUpdate
        or len(operations) != 1 + int(hp != target.hp_current) + len(life)
    ):
        raise StateMachineError("invalid_rule_result", "Unexpected exploration healing writes")
    spend = _slot_change(request, operations[0])
    if tuple(operations[1 + int(hp != target.hp_current) :]) != life:
        raise StateMachineError("invalid_rule_result", "Exploration recovery writes differ")
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
    changes = [spend, ("actor", target.entity_id, "hp_current", hp)]
    for operation in life:
        if type(operation) is delta.DeathStateUpdate:
            changes.append(
                (
                    "actor",
                    target.entity_id,
                    "death_saves",
                    None
                    if operation.value.death_saves is None
                    else operation.value.death_saves.model_dump(mode="json"),
                )
            )
        elif type(operation) is delta.ConditionsUpdate:
            changes.append(
                (
                    "actor",
                    target.entity_id,
                    "conditions",
                    [condition.model_dump(mode="json") for condition in operation.value],
                )
            )
    return tuple(changes)


def _timed_spell_changes(request, result, spell, loader):
    """Return candidate writes for one cast and one terminal world effect.

    The caller validates the shared result envelope and rechecks the version
    and resource pre-value in its write transaction. The effect's relative
    duration is converted into a deadline only during that transaction.
    """
    payload = request.payload
    snapshot = request.state_snapshot
    operations = result.state_delta.operations
    material_changes = ()
    cantrip = spell is not None and spell.level == 0 and payload.slot_level == 0
    expected_types = {delta.WorldEffectCreate}
    if not cantrip:
        expected_types.add(delta.ResourceUpdate)
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
    spend = (
        None
        if cantrip
        else _slot_change(
            request, next(op for op in operations if type(op) is delta.ResourceUpdate)
        )
    )
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
        or cast.slot_level != (payload.slot_level if spell.level else None)
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

    hp_changes = ()
    if reviewed_hp_bonus(proposal.effect) is not None:
        target = next(
            actor for actor in snapshot.character_states if actor.entity_id == payload.target_id
        )
        active = tuple(
            effect
            for effect in snapshot.effect_states
            if effect.target_id == payload.target_id and effect.id == proposal.effect.id
        )
        before = effective_hp_bonus(active)
        after = effective_hp_bonus((*active, proposal.effect))
        transition = paired_hp_transition(
            target.hp_current, target.hp_max, before, after, entity_type=target.entity_type
        )
        hp_changes = (("actor", payload.target_id, "hp_pair", transition),)
    return (
        *((spend,) if spend is not None else ()),
        *material_changes,
        *hp_changes,
        ("world_effect", payload.target_id, "create", proposal),
    )
