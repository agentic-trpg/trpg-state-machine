"""Pinned history-free evaluator projection and controlled component delta adapter."""

import copy
import json
from dataclasses import asdict

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.attack_rules import attack_action_is_spent
from dnd5e_engine.evaluation import evaluate
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    ActionAvailabilityResult,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import (
    CheckPayload,
    CombatClosePayload,
    ExplorationItemUsePayload,
    ExplorationSpellCastPayload,
    ItemUsePayload,
    ReadVersion,
    RuleError,
    RuleEvaluationRequest,
    RuleEvaluationResult,
)
from dnd5e_engine.evaluation_haste import has_haste_effects, haste_speed
from dnd5e_engine.evaluation_movement import timed_walk_bonus, walk_speed
from dnd5e_engine.evaluation_preflight import template_support_failure
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_ruleset import (
    RulesetBinding,
    RulesetBindingError,
    get_default_ruleset,
    verify_ruleset,
)
from dnd5e_engine.evaluation_spells import ordinary_spell_mechanism
from dnd5e_engine.evaluation_state import (
    CharacterStateV2,
    CombatSnapshot,
    ConditionRecord,
    MovementLedgerState,
    NonCombatSnapshot,
)
from dnd5e_engine.events import HealingApplied, IntentSubmitted
from dnd5e_engine.movement import project_speeds, speed_for_mode
from dnd5e_engine.rules.conditions import active_condition_names, exhaustion_level_of
from dnd5e_engine.turn_rules import (
    next_turn_index,
    ordinary_action_payment,
    reset_turn_budget,
)
from pydantic import ValidationError

from .combat import DND_RULESET, _remap, process_id, validate_foundation
from .combat_movement import ordinary_walk_changes, require_walk_route
from .concentration_adoption import (
    concentration_release_changes,
    effect_identity,
    require_effect_applied,
)
from .damage_adoption import (
    DAMAGE_OPERATIONS,
    adopt_damage_operation,
    json_value,
    ordinary_damage_target,
    validate_damage_result,
)
from .dnd_checks import check_changes
from .dnd_closure import closure_changes
from .errors import StateMachineError
from .haste_adoption import haste_cast_admission, haste_pass_admission
from .package import canonical_json
from .scoped_reads import scoped_read
from .session_access import _binding
from .spatial import SpatialPosition
from .spell_adoption import ordinary_spell_changes
from .timed_spell_adoption import timed_spell_changes


def _invalid(message):
    """Raise a rules adapter validation error."""
    raise StateMachineError("invalid_rule_result", message)


class UnsupportedEffectConsequence(Exception):
    """A verified Hold Person attack reaches the unsupported zero-HP lifecycle."""


def _hold_source(snapshot):
    """Select the caster of a complete Hold Person group in the fixed encounter input."""
    state = snapshot.combat_state
    if (
        snapshot.snapshot_schema_version != "engine-snapshot/20"
        or not snapshot.effect_states
        or len(state.concentration_chain) != 1
    ):
        return None
    source, chain = next(iter(state.concentration_chain.items()))
    identities = {effect_identity(effect) for effect in snapshot.effect_states}
    if not (
        len(chain) == len(identities) == len(snapshot.effect_states) and set(chain) == identities
    ):
        return None
    if chain == [(source, "effect:hold-person", f"cast:hold-person:{source}")]:
        anchor = snapshot.effect_states[0]
        if anchor.lifecycle is None and anchor.flags.get("concentration_anchor") is True:
            return source
    if all(
        effect.target_id != source
        and effect.id == "effect:paralyzed"
        and effect.lifecycle is not None
        and effect.lifecycle.source_id == source
        and effect.lifecycle.source_kind == "spell"
        and effect.lifecycle.source_slug == "hold-person"
        for effect in snapshot.effect_states
    ):
        return source
    return None


def _has_combat_effects(snapshot):
    """Check for effect graph components even if the current effect list is empty."""
    state = snapshot.combat_state
    return bool(
        snapshot.effect_states
        or state.concentration_chain
        or state.concentration_rounds_remaining
        or state.effect_lifecycles
        or state.conditions_by_effect
    )


def delta_changes(request, result, *, effect_operations=(), effect_targets=()):
    """Check pre-values and ownership for an attack, turn advance or narrow potion Delta.

    A temporary candidate validates invariants; it is never persisted as a Snapshot.
    Whole-session OCC covers all Engine dependencies in this candidate API.
    """
    snapshot = request.state_snapshot
    item_use = type(request.payload) is ItemUsePayload
    turn_update = next(
        (op for op in result.state_delta.operations if type(op) is delta.TurnUpdate), None
    )
    turn_advanced = turn_update is not None
    data = snapshot.model_dump(mode="json")
    actors = {a["entity_id"]: a for a in data["character_states"]}
    combat = data["combat_state"]
    changes = []
    seen = set()
    consumed = False
    paid = False
    turn = (
        turn_update.value.current_turn_index
        if turn_advanced
        else snapshot.combat_state.current_turn_index
    )
    if not 0 <= turn < len(snapshot.combat_state.initiative_ids):
        _invalid("Turn delta is outside the encounter")
    budget_owners = {request.actor_id, snapshot.combat_state.initiative_ids[turn]}
    affected_actors = budget_owners | {request.payload.target_id}
    if effect_operations:
        affected_actors.update(effect.target_id for effect in snapshot.effect_states)
        affected_actors.update(effect_targets)
    inventory = snapshot.inventory_state.model_dump(mode="json") if item_use else None

    if (
        getattr(request.payload, "intent_type", None) == "attack"
        and request.actor_id in snapshot.combat_state.party_ids
        and request.payload.stat_block_action_id is None
        and not _has_combat_effects(snapshot)
    ):
        target = next(
            (
                actor
                for actor in snapshot.character_states
                if actor.entity_id == request.payload.target_id
            ),
            None,
        )
        if target is not None and ordinary_damage_target(snapshot.combat_state, target):
            source_id = request.payload.weapon_id or actors[request.actor_id]["weapon_in_hands"]
            rolls = [event for event in result.proposed_events if event.type == "attack_rolled"]
            if (
                source_id is None
                or len(rolls) != 1
                or rolls[0].attacker_id != request.actor_id
                or rolls[0].target_id != target.entity_id
                or rolls[0].is_opportunity_attack
                or rolls[0].is_hit
                != any(event.type == "damage_applied" for event in result.proposed_events)
            ):
                _invalid("Damage differs from the authorized weapon attack")
            newly_dead = validate_damage_result(
                request, result, target, source_id=source_id, is_crit=rolls[0].is_crit
            )
            if newly_dead and turn_advanced:
                state = snapshot.combat_state
                expected_turn = next_turn_index(
                    state.initiative_ids, state.dead_ids | newly_dead, state.current_turn_index + 1
                )
                if (
                    expected_turn is None
                    or turn != expected_turn[0]
                    or turn_update.value.round_number != state.round_number + int(expected_turn[1])
                ):
                    _invalid("Attack turn did not advance to the next living actor")

    def edit(component, identity, field, expected, value):
        """Validate one edit and update the isolated candidate and change list."""
        key = (component, identity, field)
        if key in seen:
            _invalid("Duplicate delta field")
        seen.add(key)
        current = actors[identity] if component == "actor" else combat
        # Set-valued schema fields have no serialization-order semantics.
        old = current[field]
        expected = json_value(expected)
        if field in ("dead_ids", "processed_zero_hp_damage_instances"):
            old = sorted(old)
            expected = sorted(expected)
        if old != expected:
            _invalid("Delta pre-value differs from authoritative input")
        current[field] = json_value(value)
        changes.append((component, identity, field, json_value(value)))

    for op in result.state_delta.operations:
        if type(op) in effect_operations:
            continue
        if effect_operations and type(op) is delta.ResourceUpdate:
            if (
                request.payload.intent_type != "cast_spell"
                or op.owner_id != request.actor_id
                or op.amount != -1
                or op.expected not in snapshot.resource_state.pools
                or op.expected.kind != "spell_slot"
                or op.expected.level != request.payload.slot_level
            ):
                _invalid("Effect cast payment differs from the bound caster")
            changes.append(("resource", op.owner_id, op.pool_id, (op.expected.current, 1)))
            continue
        if effect_operations and type(op) is delta.SlotSpendLedgerUpdate:
            if (
                request.payload.intent_type != "cast_spell"
                or op.owner_id != request.actor_id
                or op.combat_id != combat["combat_id"]
                or combat["expended_resources"].get(op.owner_id) != op.expected
            ):
                _invalid("Effect cast slot ledger differs from fixed combat")
            changes.append(("slot_ledger", op.owner_id, "expended_resources", op))
            continue
        if item_use and type(op) not in (
            delta.InventoryConsume,
            delta.HPDelta,
            delta.ActionBudgetUpdate,
            delta.TurnUpdate,
            delta.MovementLedgerUpdate,
        ):
            _invalid("Unsupported item-use consequence")
        if type(op) is delta.InventoryConsume:
            if not item_use or consumed or op.amount != 1:
                _invalid("Unexpected inventory consumption")
            consumed = True
            expected = op.expected.model_dump(mode="json")
            value = op.value.model_dump(mode="json")
            if (
                expected["instance_id"] != request.payload.instance_id
                or expected["owner_id"] != request.actor_id
                or expected["quantity"] != 1
                or expected["charges_remaining_per_unit"] != 1
                or not expected["accessible"]
                or value != {**expected, "quantity": 0}
                or expected not in inventory["entries"]
            ):
                _invalid("Item consumption differs from the selected potion")
            inventory["entries"] = [
                entry if entry["instance_id"] != expected["instance_id"] else value
                for entry in inventory["entries"]
            ]
            changes.append(("item", expected["instance_id"], "consume", expected))
            continue
        target = getattr(op, "target_id", None)
        actor_id = getattr(op, "actor_id", None)
        if target is not None and (
            target not in actors
            or (
                target != request.payload.target_id
                and not (
                    effect_operations
                    and type(op) is delta.ConditionsUpdate
                    and target in affected_actors
                )
            )
        ):
            _invalid("Damage/status delta must belong to the declared target")
        allowed_owners = budget_owners
        if type(op) is delta.ActionBudgetUpdate:
            # Damage can change the target's movement budget (e.g. unconscious),
            # even when a different Actor receives the next turn.
            allowed_owners = affected_actors
        if actor_id is not None and (actor_id not in actors or actor_id not in allowed_owners):
            _invalid("Action/movement delta actor is outside the authorized command")
        if hasattr(op, "combat_id") and op.combat_id != combat["combat_id"]:
            _invalid("Delta belongs to another combat")
        if item_use and type(op) is delta.HPDelta:
            if op.amount <= 0 or op.resulting_hp > actors[target]["hp_max"]:
                _invalid("Potion healing must increase HP within the actor maximum")
            edit("actor", target, "hp_current", op.expected_hp, op.resulting_hp)
        elif type(op) in DAMAGE_OPERATIONS:
            target_kind = actors.get(request.payload.target_id, {}).get("entity_type", "").lower()
            adopt_damage_operation(request, target_kind, op, edit)
        elif type(op) in (delta.ActionBudgetUpdate, delta.TurnUpdate):
            if item_use and type(op) is delta.ActionBudgetUpdate:
                changed_budget = {
                    field
                    for field in type(op.value).model_fields
                    if getattr(op.expected, field) != getattr(op.value, field)
                }
                if op.actor_id == request.actor_id:
                    permitted = {"bonus_action_available", "bonus_action_taken_this_turn"}
                    if turn_advanced:
                        permitted |= {"movement_remaining", "sneak_attack_spent_this_turn"}
                    if not changed_budget <= permitted or (
                        not turn_advanced and changed_budget != permitted
                    ):
                        _invalid("Potion changed an unrelated action budget")
                    paid = (
                        op.expected.bonus_action_available
                        and not op.expected.bonus_action_taken_this_turn
                        and not op.value.bonus_action_available
                        and op.value.bonus_action_taken_this_turn
                    )
                elif not turn_advanced or op.actor_id != snapshot.combat_state.initiative_ids[turn]:
                    _invalid("Potion changed another actor's budget outside turn start")
            if type(op) is delta.TurnUpdate:
                if item_use:
                    source = next(
                        actor
                        for actor in snapshot.character_states
                        if actor.entity_id == request.actor_id
                    )
                    if (
                        source.action_available
                        or source.movement_remaining > 0
                        or (source.attack_action_engaged and source.attacks_remaining > 0)
                        or op.value.last_ended_turn
                        != (snapshot.combat_state.round_number, request.actor_id)
                        or op.value.turn_serial != op.expected.turn_serial + 1
                    ):
                        _invalid("Potion turn advance differs from the spent turn")
                if op.value.ended != op.expected.ended:
                    _invalid("Combat completion is not supported by this attack slice")
                for field in ("monster_turn_start_done", "last_ended_turn", "departed_actor_id"):
                    reference = getattr(op.value, field)
                    if reference is None or reference == getattr(op.expected, field):
                        continue
                    owner = reference if field == "departed_actor_id" else reference[1]
                    if owner not in actors or owner not in affected_actors:
                        _invalid("Changed turn reference is outside the authorized attack")
            component = "combat" if type(op) is delta.TurnUpdate else "actor"
            identity = combat["combat_id"] if component == "combat" else (target or actor_id)
            for field in type(op.value).model_fields:
                edit(
                    component,
                    identity,
                    field,
                    getattr(op.expected, field),
                    getattr(op.value, field),
                )
        elif type(op) is delta.ConditionsUpdate:
            edit("actor", target, "conditions", op.expected, op.value)
        elif type(op) is delta.MovementLedgerUpdate:
            if item_use and not turn_advanced:
                _invalid("Potion changed movement outside turn start")
            field = "movement_ledgers"
            if combat[field][actor_id] != json_value(op.expected):
                _invalid("Movement ledger pre-value differs")
            if (field, actor_id) in seen:
                _invalid("Duplicate movement ledger")
            seen.add((field, actor_id))
            combat[field][actor_id] = json_value(op.value)
            changes.append(("ledger", actor_id, field, json_value(op.value)))
        else:
            _invalid("Unrecognized delta operation")
    data.update(
        character_states=[actors[a.entity_id] for a in snapshot.character_states],
        combat_state=combat,
        **({"inventory_state": inventory} if item_use else {}),
    )
    try:
        CombatSnapshot.model_validate_json(canonical_json(data))
    except ValidationError as exc:
        raise StateMachineError(
            "invalid_rule_result", "Delta violates mechanical invariants"
        ) from exc
    if not changes:
        _invalid("Event-only commit/version policy is not implemented")
    if item_use and (not consumed or not paid):
        _invalid("Potion use must consume its instance and bonus action")
    return tuple(changes)


def exploration_potion_changes(request, result):
    """Bind consumption and self-healing evidence to the exact selected bottle."""
    snapshot = request.state_snapshot
    actor_id = request.actor_id
    actor = next(a for a in snapshot.character_states if a.entity_id == actor_id)
    operations = result.state_delta.operations
    if len(operations) != 2 or {type(op) for op in operations} != {
        delta.InventoryConsume,
        delta.HPDelta,
    }:
        _invalid("Exploration potion must consume one bottle and heal its drinker")
    consume = next(op for op in operations if type(op) is delta.InventoryConsume)
    healing = next(op for op in operations if type(op) is delta.HPDelta)
    entry = next(
        (
            e
            for e in snapshot.inventory_state.entries
            if e.instance_id == request.payload.instance_id
        ),
        None,
    )
    if (
        entry is None
        or entry.owner_id != actor_id
        or entry.quantity != 1
        or entry.charges_remaining_per_unit != 1
        or not entry.accessible
        or consume.amount != 1
        or consume.expected != entry
        or consume.value != entry.model_copy(update={"quantity": 0})
    ):
        _invalid("Item consumption differs from the selected potion")

    expected_intent = IntentSubmitted(
        actor_id=actor_id, intent_type="use_item", target_id=actor_id, item_id=entry.item_slug
    )
    events = result.proposed_events
    if (
        len(events) != 2
        or events[0] != expected_intent
        or type(events[1]) is not HealingApplied
        or events[1].target_id != actor_id
        or events[1].amount <= 0
        or not result.rng_transition.state_changed
    ):
        _invalid("Exploration potion events or RNG differ from the expected profile")
    if (
        request.payload.target_id != actor_id
        or healing.target_id != actor_id
        or healing.expected_hp != actor.hp_current
        or healing.amount <= 0
        or healing.resulting_hp != min(actor.hp_max, actor.hp_current + events[1].amount)
    ):
        _invalid("Potion healing differs from the rolled event")
    return (
        ("item", entry.instance_id, "consume", entry.model_dump(mode="json")),
        ("actor", actor_id, "hp_current", healing.resulting_hp),
    )


def _turn_intent_scope(
    request, result, *, effect_operations=(), condition_changes=None, effect_movement=None
):
    """Bind the ordinary turn handoff and validate its affected target's movement ledger."""
    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    operations = result.state_delta.operations
    cast = request.payload.intent_type == "cast_spell"
    permitted = {delta.TurnUpdate, delta.ActionBudgetUpdate, delta.MovementLedgerUpdate}
    permitted.update(effect_operations)
    if cast:
        permitted.update({delta.ResourceUpdate, delta.SlotSpendLedgerUpdate})
    if any(type(op) not in permitted for op in operations):
        _invalid("Cast or Pass proposed a consequence outside its operation scope")

    actor_id = request.actor_id
    if combat.initiative_ids[combat.current_turn_index] != actor_id:
        _invalid("Cast or Pass actor does not own the fixed turn")
    actors = {actor.entity_id: combatant(actor) for actor in snapshot.character_states}
    outgoing = actors[actor_id]
    if cast:
        payment = ordinary_action_payment(outgoing)
        if not payment or outgoing.action_taken_this_turn:
            _invalid("Effect cast lacks an available ordinary Action")
        outgoing = outgoing.model_copy(update=payment)

    turns = [op for op in operations if type(op) is delta.TurnUpdate]
    advances = not cast or attack_action_is_spent(outgoing)
    if len(turns) != int(advances):
        _invalid("Cast or Pass omitted or duplicated its required turn transition")
    turn_events = [
        event
        for event in result.proposed_events
        if event.type in {"turn_phase", "turn_ended", "turn_started", "round_started"}
    ]
    actors[actor_id] = outgoing
    for target_id, value in (condition_changes or {}).items():
        actors[target_id] = actors[target_id].model_copy(update={"conditions": value})

    def speed(actor, mode):
        """Use the RE movement projection for the target's adopted effect or conditions."""
        if effect_movement is not None and actor.entity_id == effect_movement[0]:
            return haste_speed(actor, effect_movement[1], mode)
        return speed_for_mode(
            project_speeds(
                actor.base_speed,
                actor.movement_modes,
                condition_names=active_condition_names(actor.conditions),
                exhaustion_level=exhaustion_level_of(actor.conditions),
            ),
            mode,
        )

    ledgers = [op for op in operations if type(op) is delta.MovementLedgerUpdate]
    if advances:
        next_turn = next_turn_index(
            combat.initiative_ids, combat.dead_ids, combat.current_turn_index + 1
        )
        if next_turn is None:
            _invalid("Cast or Pass lacks a living next turn")
        index, rollover = next_turn
        next_actor = combat.initiative_ids[index]
        new_round = combat.round_number + int(rollover)
        turn = turns[0]
        if (
            turn.value.current_turn_index != index
            or turn.value.round_number != new_round
            or turn.value.turn_serial != combat.turn_serial + 1
            or turn.value.last_ended_turn != (combat.round_number, actor_id)
            or turn.value.ended != combat.ended
            or turn.value.departed_actor_id != combat.departed_actor_id
            or turn.value.monster_turn_start_done != combat.monster_turn_start_done
        ):
            _invalid("Cast or Pass turn does not match the fixed initiative handoff")
        expected_events = [("turn_phase", actor_id, "turn_end", combat.round_number)]
        expected_events.append(("turn_ended", actor_id, None, None))
        if rollover:
            expected_events.extend(
                [
                    ("round_started", None, None, new_round),
                    ("turn_phase", None, "round_start", new_round),
                ]
            )
        expected_events.extend(
            [
                ("turn_started", next_actor, None, None),
                ("turn_phase", next_actor, "turn_start", new_round),
            ]
        )
        if [
            (
                event.type,
                getattr(event, "actor_id", None),
                getattr(event, "phase", None),
                getattr(event, "round_number", None),
            )
            for event in turn_events
        ] != expected_events:
            _invalid("Cast or Pass lacks matching turn handoff events")
        fresh = MovementLedgerState(spent_ft=0, distance_ft=0, active_mode="walk", dash_count=0)
        old_ledger = combat.movement_ledgers[next_actor]
        if [(op.actor_id, op.value) for op in ledgers] != (
            [(next_actor, fresh)] if old_ledger != fresh else []
        ):
            _invalid("Cast or Pass changed movement outside the next turn reset")
        for identity, actor in actors.items():
            ledger = combat.movement_ledgers[identity]
            if identity == next_actor:
                actor = reset_turn_budget(actor, effective_speed=speed(actor, "walk"), attacks=1)
            else:
                actor = actor.model_copy(
                    update={
                        "movement_remaining": ledger.remaining(speed(actor, ledger.active_mode))
                    }
                )
            actors[identity] = actor.model_copy(update={"sneak_attack_spent_this_turn": False})
    elif turn_events or ledgers:
        _invalid("Effect cast proposed turn consequences without a turn transition")
    elif effect_movement is not None:
        target = effect_movement[0]
        actor = actors[target]
        ledger = combat.movement_ledgers[target]
        actors[target] = actor.model_copy(
            update={"movement_remaining": ledger.remaining(speed(actor, ledger.active_mode))}
        )

    fields = delta.AttackBudgetState.model_fields
    proposed_budgets = {
        op.actor_id: op.value.model_dump()
        for op in operations
        if type(op) is delta.ActionBudgetUpdate
    }
    expected_budgets = {}
    for actor in snapshot.character_states:
        identity = actor.entity_id
        values = {field: getattr(actors[identity], field) for field in fields}
        if any(values[field] != getattr(actor, field) for field in fields):
            expected_budgets[identity] = values
    if proposed_budgets != expected_budgets:
        _invalid("Cast or Pass changed budgets outside Action payment and turn reset")


def _concentration_cast_graph(request, result):
    """Select a single supported creation and bind its scope and paired slot operations."""
    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    payload = request.payload
    operations = result.state_delta.operations
    graphs = [
        op for op in operations if type(op) in (delta.HoldPersonGraphCreate, delta.HasteGraphCreate)
    ]
    if not graphs:
        return None
    payments = [op for op in operations if type(op) is delta.ResourceUpdate]
    ledgers = [op for op in operations if type(op) is delta.SlotSpendLedgerUpdate]
    if len(graphs) != 1 or len(payments) != 1 or len(ledgers) != 1 or _has_combat_effects(snapshot):
        _invalid("Concentration cast requires one new graph and one paired slot spend")
    graph, payment, ledger = graphs[0], payments[0], ledgers[0]
    if (
        graph.combat_id != combat.combat_id
        or graph.source_id != request.actor_id
        or graph.spell_source_id != payload.spell_source_id
        or graph.slot_level != payload.slot_level
        or graph.actor_ids != combat.initiative_ids
        or graph.applied_turn_serial != combat.turn_serial
        or graph.applied_round != combat.round_number
        or (ledger.slot_kind, ledger.level) != (payment.expected.kind, payment.expected.level)
    ):
        _invalid("Concentration creation or payment differs from the bound cast")
    return graph


def _hold_cast_conditions(request, result, graph):
    """Bind cast evidence and every graph member to the selected living Humanoids."""
    payload = request.payload
    targets = payload.target_ids or (payload.target_id,)
    if (
        payload.spell_id != graph.spell_slug
        or targets[0] != payload.target_id
        or graph.selected_target_ids != targets
    ):
        _invalid("Hold Person graph differs from the selected targets")
    actors = {actor.entity_id: actor for actor in request.state_snapshot.character_states}
    for target in targets:
        actor = actors.get(target)
        if (
            actor is None
            or actor.creature_type != "humanoid"
            or not actor.is_alive
            or actor.hp_current <= 0
        ):
            _invalid("Hold Person target is not a living Humanoid")
    submitted = [event for event in result.proposed_events if event.type == "intent_submitted"]
    casts = [event for event in result.proposed_events if event.type == "spell_cast"]
    saves = [event for event in result.proposed_events if event.type == "save_rolled"]
    applied = [event for event in result.proposed_events if event.type == "effect_applied"]
    conditions = [
        (event.target_id, event.condition)
        for event in result.proposed_events
        if event.type == "condition_applied"
    ]
    failed = tuple(save.target_id for save in saves if not save.succeeded)
    if (
        len(submitted) != 1
        or submitted[0].actor_id != request.actor_id
        or submitted[0].intent_type != "cast_spell"
        or submitted[0].spell_id != graph.spell_slug
        or submitted[0].target_id != payload.target_id
        or [(cast.actor_id, cast.spell_id, cast.slot_level) for cast in casts]
        != [(request.actor_id, graph.spell_slug, graph.slot_level)]
        or tuple(save.target_id for save in saves) != targets
        or any(save.ability != "wis" or save.dc != graph.save_dc for save in saves)
        or tuple(entry.effect.target_id for entry in graph.entries)
        != (failed or (request.actor_id,))
        or len(applied) != len(graph.entries)
        or conditions
        != [
            (entry.effect.target_id, entry.condition.condition)
            for entry in graph.entries
            if entry.condition is not None
        ]
    ):
        _invalid("Hold Person cast evidence differs from its graph")
    for event, entry in zip(applied, graph.entries, strict=True):
        require_effect_applied(event, entry.effect)
    return {
        entry.effect.target_id: (*actors[entry.effect.target_id].conditions, entry.condition)
        for entry in graph.entries
        if entry.condition is not None
    }


def _concentration_cast_changes(request, result, graph):
    """Validate the typed cast and use the shared turn and component adoption checks."""
    if type(graph) is delta.HoldPersonGraphCreate:
        component = "hold_graph"
        condition_changes = _hold_cast_conditions(request, result, graph)
        effect_movement = None
    else:
        component = "haste_graph"
        haste_cast_admission(request, result, graph)
        condition_changes = None
        effect_movement = (graph.target_id, (graph.effect,))
    _turn_intent_scope(
        request,
        result,
        effect_operations=(type(graph),),
        condition_changes=condition_changes,
        effect_movement=effect_movement,
    )
    base = delta_changes(
        request, result, effect_operations=(type(graph),), effect_targets=condition_changes or ()
    )
    return (*base, (component, graph.source_id, "create", graph))


def _hold_pass_boundaries(request, result, source, lifecycles):
    """Bind each member transition to the already-verified Pass TurnEnd and its evidence."""
    combat = request.state_snapshot.combat_state
    actor_id = request.actor_id
    expired = [event for event in result.proposed_events if event.type == "effect_expired"]
    reasons = {(event.target_id, event.effect_id, event.origin): event.reason for event in expired}
    if len(reasons) != len(expired) or reasons.keys() != {
        op.identity for op in lifecycles if op.effect is None
    }:
        _invalid("Hold Person expiry evidence differs from the removed members")
    saves = [event for event in result.proposed_events if event.type == "save_rolled"]
    caster_duration_ended = (
        actor_id == source and combat.concentration_rounds_remaining[source] == 1
    )
    if caster_duration_ended and reasons != dict.fromkeys(
        combat.concentration_chain[source], "duration"
    ):
        _invalid("Hold Person caster duration must expire every member at its final TurnEnd")
    for op in lifecycles:
        clock = op.expected_lifecycle.state if op.expected_lifecycle is not None else None
        reason = reasons.get(op.identity)
        if reason == "duration":
            if not caster_duration_ended and (
                clock is None
                or not clock.expires_at(actor_id, "end", combat.turn_serial, combat.round_number)
            ):
                _invalid("Hold Person member duration is outside its TurnEnd boundary")
            continue
        if (
            reason not in (None, "save_succeeded")
            or clock is None
            or not clock.repeats_at(actor_id, combat.turn_serial)
            or clock.application.save_ability != "wis"
            or [
                (save.ability, save.dc, save.succeeded)
                for save in saves
                if save.target_id == actor_id
            ]
            != [("wis", clock.application.save_dc, reason == "save_succeeded")]
        ):
            _invalid("Hold Person member transition lacks its due TurnEnd save evidence")


def hold_effect_changes(request, result, source):
    """Guard Hold Person lifecycle updates against the fixed encounter input."""
    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    payload = request.payload
    operations = result.state_delta.operations
    lifecycles = [op for op in operations if type(op) is delta.EffectLifecycleUpdate]
    concentrations = [op for op in operations if type(op) is delta.ConcentrationUpdate]
    old_effects = {(e.target_id, e.id, e.origin): e for e in snapshot.effect_states}
    old_clocks = {e.identity: e for e in combat.effect_lifecycles}
    old_links = {e.identity: e for e in combat.conditions_by_effect}
    if len(old_effects) != len(snapshot.effect_states) or len(concentrations) > 1:
        _invalid("Duplicate effect or concentration transition")
    actors = {a.entity_id: a for a in snapshot.character_states}
    zero_hp = (
        payload.intent_type == "attack"
        and payload.target_id == source
        and any(
            type(op) is delta.HPDelta
            and op.target_id == source
            and op.expected_hp > 0
            and op.resulting_hp == 0
            for op in operations
        )
    )
    knocked_out = zero_hp and any(
        event.type == "unconscious" and event.target_id == source
        for event in result.proposed_events
    )
    unconscious_added = False
    for operation in operations:
        if type(operation) is delta.ConditionsUpdate:
            if operation.target_id not in actors:
                _invalid("Hold Person condition target is outside encounter")
            before = actors[operation.target_id].conditions
            independent = [c for c in before if c.source_effect_id is None]
            after_independent = [c for c in operation.value if c.source_effect_id is None]
            if knocked_out and operation.target_id == source:
                independent.append(
                    ConditionRecord(
                        condition="unconscious",
                        source_entity_id="implied:event",
                        scope="combat",
                        duration_rounds=None,
                        save_dc=None,
                        applied_round=combat.round_number,
                        exhaustion_level=1,
                        source_effect_id=None,
                    )
                )
                unconscious_added = True
            if list(operation.expected) != list(before) or after_independent != independent:
                _invalid("Hold Person changed independent conditions")
    final_effects = dict(old_effects)
    for op in lifecycles:
        if (
            op.combat_id != combat.combat_id
            or op.identity not in old_effects
            or op.expected_effect != old_effects[op.identity]
            or op.expected_lifecycle != old_clocks.get(op.identity)
            or op.expected_lineage != old_links.get(op.identity)
        ):
            _invalid("Effect lifecycle pre-value differs from fixed input")
        if op.effect is None:
            if not any(
                event.type == "effect_expired"
                and (event.target_id, event.effect_id, event.origin) == op.identity
                for event in result.proposed_events
            ):
                _invalid("Effect removal lacks matching expiry evidence")
            final_effects.pop(op.identity)
        else:
            if (
                op.lifecycle.state.last_repeat_turn_serial != combat.turn_serial
                or not any(
                    event.type == "turn_phase"
                    and event.phase == "turn_end"
                    and event.actor_id == op.identity[0]
                    for event in result.proposed_events
                )
                or not any(
                    event.type == "save_rolled"
                    and event.target_id == op.identity[0]
                    and event.ability == "wis"
                    and not event.succeeded
                    for event in result.proposed_events
                )
            ):
                _invalid("Repeat-save clock lacks its target TurnEnd evidence")
            final_effects[op.identity] = op.effect
    if len({op.identity for op in lifecycles}) != len(lifecycles):
        _invalid("Duplicate effect lifecycle transition")
    if concentrations:
        op = concentrations[0]
        caster = next(a for a in snapshot.character_states if a.entity_id == source)
        if (
            op.combat_id != combat.combat_id
            or op.source_id != source
            or op.expected_effect_id != caster.concentration_effect_id
            or op.expected_chain != tuple(combat.concentration_chain[source])
            or op.expected_rounds != combat.concentration_rounds_remaining[source]
        ):
            _invalid("Concentration pre-value differs from fixed input")
        if op.rounds == op.expected_rounds - 1 and not any(
            event.type == "turn_phase" and event.phase == "turn_end" and event.actor_id == source
            for event in result.proposed_events
        ):
            _invalid("Concentration tick lacks caster TurnEnd")
        if (
            not op.chain
            and not any(
                event.type == "concentration_dropped" and event.target_id == source
                for event in result.proposed_events
            )
            and not any(event.type == "effect_expired" for event in result.proposed_events)
        ):
            _invalid("Concentration removal lacks expiry or drop evidence")
        chain = op.chain
    else:
        chain = tuple(combat.concentration_chain[source])
    if set(chain) != set(final_effects) or len(chain) != len(final_effects):
        _invalid("Hold Person graph transition is incomplete")
    if payload.intent_type == "pass":
        condition_changes = {
            op.target_id: op.value for op in operations if type(op) is delta.ConditionsUpdate
        }
        expected_conditions = {}
        for op in lifecycles:
            if op.effect is None and op.expected_lineage.statuses:
                target, effect_id, _ = op.identity
                expected_conditions[target] = tuple(
                    condition
                    for condition in actors[target].conditions
                    if condition.source_effect_id != effect_id
                )
        if condition_changes != expected_conditions:
            _invalid("Hold Person conditions do not match its graph transition")
        _turn_intent_scope(
            request,
            result,
            effect_operations=(
                delta.EffectLifecycleUpdate,
                delta.ConcentrationUpdate,
                delta.ConditionsUpdate,
            ),
            condition_changes=expected_conditions,
        )
        _hold_pass_boundaries(request, result, source, lifecycles)
    base = delta_changes(
        request,
        result,
        effect_operations=(delta.EffectLifecycleUpdate, delta.ConcentrationUpdate),
    )
    if zero_hp:
        evidence = result.proposed_events
        dead = any(
            type(op) is delta.DeathStateUpdate and op.target_id == source and not op.value.is_alive
            for op in operations
        ) and any(event.type == "death" and event.target_id == source for event in evidence)
        confirmed_knockout = unconscious_added and any(
            event.type == "condition_applied"
            and event.target_id == source
            and event.condition == "unconscious"
            for event in evidence
        )
        if (
            chain
            or not (confirmed_knockout or dead)
            or not any(
                event.type == "damage_applied"
                and event.target_id == source
                and event.source_actor_id == request.actor_id
                and event.amount > 0
                for event in evidence
            )
            or not any(
                event.type == "concentration_dropped" and event.target_id == source
                for event in evidence
            )
        ):
            _invalid(
                "Zero-HP concentration outcome lacks matching damage, life-state or drop evidence"
            )
        raise UnsupportedEffectConsequence
    return (
        *base,
        *(("hold_lifecycle", op.identity, "update", op) for op in lifecycles),
        *(("hold_concentration", source, "update", op) for op in concentrations),
    )


def haste_effect_changes(request, result):
    """Admit bounded Haste turn effects and the ordinary handoff."""
    adopted, condition_changes, target, final_effects = haste_pass_admission(request, result)
    effect_operations = (
        delta.EffectLifecycleUpdate,
        delta.ConcentrationUpdate,
        delta.EffectFollowUpCreate,
    )
    _turn_intent_scope(
        request,
        result,
        effect_operations=(*effect_operations, delta.ConditionsUpdate),
        condition_changes=condition_changes,
        effect_movement=(target, final_effects),
    )
    base = delta_changes(
        request, result, effect_operations=effect_operations, effect_targets=(target,)
    )
    return (*base, *adopted)


def validate_dnd_result(request, result, *, loader=None):
    """Strictly restore an RE result and validate request, binding, read version and Delta."""
    try:
        result = RuleEvaluationResult.model_validate(copy.deepcopy(result))
        # RE /24 verifies /2 stage coverage against this exact input. Uncovered
        # is a valid explicit limitation, never upgraded to sensory Evidence or
        # complete coverage by SM; existing operation admission remains unchanged.
        result.verify_request(request)
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateMachineError(
            "invalid_rule_result", "Invalid Engine result envelope/binding/RNG"
        ) from exc
    required = (
        ReadVersion(
            kind="world", ref=request.session_id, version=request.state_snapshot.world_version
        ),
    )
    if result.read_set != required:
        _invalid("Expected the pinned Engine's complete whole-session read fence")
    if result.status == "needs_choice":
        _invalid("Unexpected choice after explicit command capability admission")
    actors = {a.entity_id for a in request.state_snapshot.character_states}
    for event in result.proposed_events:
        for field in ("actor_id", "target_id", "source_actor_id", "attacker_id", "killer_id"):
            value = getattr(event, field, None)
            if value is not None and value not in actors:
                _invalid("Proposed mechanical event refers to an actor outside the snapshot")
    if result.status != "accepted":
        return result, ()
    if request.operation_kind == "rules.check":
        return result, check_changes(request, result)
    if type(request.payload) is ExplorationItemUsePayload:
        return result, exploration_potion_changes(request, result)
    if type(request.payload) is ExplorationSpellCastPayload:
        return result, timed_spell_changes(request, result, loader)
    if type(request.payload) is ItemUsePayload:
        changes = delta_changes(request, result)
        bottle = next(
            value
            for component, _, field, value in changes
            if component == "item" and field == "consume"
        )
        submitted = [event for event in result.proposed_events if event.type == "intent_submitted"]
        healed = [event for event in result.proposed_events if event.type == "healing_applied"]
        if (
            len(submitted) != 1
            or submitted[0].actor_id != request.actor_id
            or submitted[0].target_id != request.actor_id
            or submitted[0].intent_type != "use_item"
            or submitted[0].item_id != bottle["item_slug"]
            or len(healed) != 1
            or healed[0].target_id != request.actor_id
        ):
            _invalid("Potion events do not match the selected bottle and authorized drinker")
        actor = next(
            actor
            for actor in request.state_snapshot.character_states
            if actor.entity_id == request.actor_id
        )
        expected_healing = min(healed[0].amount, actor.hp_max - actor.hp_current)
        hp_changes = [
            value
            for component, identity, field, value in changes
            if component == "actor" and identity == request.actor_id and field == "hp_current"
        ]
        if hp_changes != ([actor.hp_current + expected_healing] if expected_healing else []):
            _invalid("Potion healing differs from the rolled event")
        return result, changes
    if getattr(request.payload, "intent_type", None) == "cast_spell":
        if (graph := _concentration_cast_graph(request, result)) is not None:
            return result, _concentration_cast_changes(request, result, graph)
        if loader is None:
            _invalid("Spell result requires its bound rules loader")
        mechanism = ordinary_spell_mechanism(loader, request.payload.spell_id)
        if mechanism is None:
            _invalid("Accepted spell is outside the bound ordinary mechanism")
        spell = loader.get_spell(request.payload.spell_id)
        return result, ordinary_spell_changes(request, result, mechanism, spell)
    if (
        request.operation_kind == "combat.intent"
        and request.payload.intent_type == "drop_concentration"
    ):
        return result, concentration_release_changes(request, result)
    if (
        request.operation_kind == "combat.intent"
        and request.payload.intent_type in {"pass", "attack"}
        and (source := _hold_source(request.state_snapshot)) is not None
        and (request.payload.intent_type == "pass" or request.payload.target_id == source)
    ):
        return result, hold_effect_changes(request, result, source)
    if request.operation_kind == "combat.intent" and request.payload.intent_type == "move":
        return result, ordinary_walk_changes(request, result)
    if request.operation_kind == "combat.intent" and request.payload.intent_type == "pass":
        if has_haste_effects(request.state_snapshot):
            return result, haste_effect_changes(request, result)
        _turn_intent_scope(request, result)
    return result, (
        closure_changes(request, result)
        if request.operation_kind == "combat.close"
        else delta_changes(request, result)
    )


class RulesAdapter:
    """Pinned history-free translation; facts and the outer transaction belong to SM."""

    def __init__(self, store, loader, characters, facts, records, world):
        """Bind the fixed rules loader and character, world and combat interfaces."""
        self.store = store
        self.loader = loader
        self.characters = characters
        self.facts = facts
        self.records = records
        self.world = world

    @property
    def _conn(self):
        """Return the shared rules adapter connection."""
        return self.store.connection

    def session_binding(self, row):
        """Translate SM's durable identity to the pinned RE contract."""
        return RulesetBinding(**asdict(_binding(row)))

    def _pinned_row(self, session_id, package):
        """Read the Session and verify its fixed Package pin."""
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise StateMachineError("session_not_found", "Unknown session")
        self.world.sessions._check_package_pin(row, package)
        return row

    def _verify_dnd_binding(self, package, row=None):
        """Verify the persisted rules binding against the frozen dataset."""
        if package.legacy_audit_only:
            raise StateMachineError(
                "legacy_rules_audit_only", "Old mechanical facts require explicit migration"
            )
        # Only creation has no Session yet. Every live path supplies its row.
        binding = (
            RulesetBinding(**asdict(package.ruleset_binding))
            if row is None
            else self.session_binding(row)
        )
        if binding.ruleset_id != DND_RULESET:
            raise StateMachineError("unsupported_ruleset", "Expected a D&D evaluation package")
        try:
            verify_ruleset(binding, self.loader)
        except RulesetBindingError as exc:
            raise StateMachineError(
                "ruleset_binding_mismatch", "Evaluator data/configuration differ"
            ) from exc

    @scoped_read
    def _noncombat_snapshot(self, row, package=None, *, actor_ids=None):
        """Read the required mechanical actors and the current scene in one scope.

        A named dependency set limits actor and component assembly to its members.
        Callers supplying a Package request its effective lighting projection.
        """
        self.records.require_noncombat_save(row)
        sid = row["session_id"]
        if row["mechanical_schema_version"] not in (
            "engine-snapshot/17",
            "engine-snapshot/18",
            "engine-snapshot/19",
            "engine-snapshot/20",
        ):
            raise StateMachineError(
                "unsupported_mechanical_version", "Complete noncombat state was not initialized"
            )
        if actor_ids is None:
            actor_ids = tuple(
                a[0]
                for a in self._conn.execute(
                    "SELECT actor_id FROM actors WHERE session_id=? AND current_scene_id=? "
                    "ORDER BY actor_id",
                    (sid, row["current_scene_id"]),
                )
            )
        owners = set(actor_ids)
        actors = self.characters.rule_actors(sid, owners)
        scene_data = self.facts.mechanical_scene(sid, row["current_scene_id"])
        if scene_data is not None and package is not None:
            projected, unknown, _ = self.world.objects.lights.current_project(
                sid, scene_data["scene_id"], package
            )
            if unknown:
                raise StateMachineError(
                    "unsupported_light_geometry", "Mechanical lighting contains uncertainty"
                )
            scene_data = {**scene_data, "grid": projected}
        try:
            return NonCombatSnapshot.model_validate_json(
                canonical_json(
                    {
                        "snapshot_kind": "non_combat",
                        "snapshot_schema_version": row["mechanical_schema_version"],
                        "session_id": sid,
                        "world_version": row["world_version"],
                        "character_states": [actors[a] for a in actor_ids],
                        **self.characters.rule_components(sid, owners),
                        "combat_setup": None,
                        "scene_state": scene_data,
                    }
                )
            )
        except (ValidationError, KeyError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Current scene lacks complete mechanical facts"
            ) from exc

    def validate_saved_mechanics(self, row):
        """Validate stored stocks without requiring every scene NPC to support RE.

        Resuming exploration is allowed in scenes containing ordinary NPCs with
        no mechanical profile. A later rule request still requires a complete
        local snapshot; recovery must not create those missing profiles.
        """
        if row["mechanical_schema_version"] not in (
            "engine-snapshot/17",
            "engine-snapshot/18",
            "engine-snapshot/19",
            "engine-snapshot/20",
        ):
            raise StateMachineError(
                "unsupported_mechanical_version", "Old mechanical facts need migration"
            )
        actors = self.characters.rule_actors(row["session_id"])
        if row["pc_actor_id"] not in actors:
            raise StateMachineError("incomplete_session", "Mechanical PC facts are missing")
        try:
            for actor in actors.values():
                CharacterStateV2.model_validate_json(canonical_json(actor))
        except (ValidationError, ValueError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Stored Actor facts are invalid"
            ) from exc
        self.characters.rule_components(row["session_id"], set(actors), full_audit=True)

    @scoped_read
    def _combat_snapshot(self, row, *, for_pass=False, for_walk=False):
        """Build fixed combat facts with the required Pass or walking projection."""
        sid = row["session_id"]
        if row["mechanical_schema_version"] not in (
            "engine-snapshot/17",
            "engine-snapshot/18",
            "engine-snapshot/19",
            "engine-snapshot/20",
        ):
            raise StateMachineError(
                "unsupported_mechanical_version", "Old save lacks proven current component facts"
            )
        combat = self._conn.execute("SELECT * FROM combats WHERE session_id=?", (sid,)).fetchone()
        if combat is None:
            raise StateMachineError("combat_not_initialized", "Session has no mechanical encounter")
        state = json.loads(combat["state_json"])
        roster = set(state["initiative_ids"])
        state.update(self.characters.effects.combat_fields(sid, roster))
        scene = self.facts.mechanical_scene(sid, combat["scene_id"])
        if not state["ended"]:
            state["actor_zone"] = self._current_cells(
                sid,
                combat["scene_id"],
                state["initiative_ids"],
                self.facts.maps.scene(sid, combat["scene_id"]).grid,
            )
        try:
            components = self.characters.rule_components(sid, roster)
            if for_pass:
                independent = self.characters.effects.world_passive_identities(sid, roster)
                components["effect_states"] = [
                    effect
                    for effect in components["effect_states"]
                    if (effect["target_id"], effect["id"], effect["origin"]) not in independent
                ]
            actors = self.characters.combat_rule_actors(
                sid, roster, components["inventory_state"]["entries"]
            )
            snapshot = CombatSnapshot.model_validate_json(
                canonical_json(
                    {
                        "snapshot_schema_version": row["mechanical_schema_version"],
                        "snapshot_kind": "combat",
                        "session_id": sid,
                        "world_version": row["world_version"],
                        "character_states": [actors[a] for a in state["initiative_ids"]],
                        **components,
                        "scene_state": scene,
                        "combat_state": state,
                    }
                )
            )
            return self._project_walk_budgets(snapshot) if for_walk else snapshot
        except (ValidationError, KeyError, TypeError) as exc:
            raise StateMachineError(
                "invalid_mechanical_state", "Stored Engine state is incomplete"
            ) from exc

    @staticmethod
    def _project_walk_budgets(snapshot):
        """For a reviewed timed walk, project its target's budget from the existing ledger."""
        try:
            timed = timed_walk_bonus(snapshot)
        except ValueError:
            # Keep the complete fixed input for other mechanics and RE refusal of walking.
            return snapshot
        if timed is None:
            return snapshot
        target, bonus = timed
        ledger = snapshot.combat_state.movement_ledgers[target]
        if ledger.active_mode != "walk":
            return snapshot
        return snapshot.model_copy(
            update={
                "character_states": tuple(
                    actor.model_copy(
                        update={"movement_remaining": ledger.remaining(walk_speed(actor, bonus))}
                    )
                    if actor.entity_id == target
                    else actor
                    for actor in snapshot.character_states
                )
            }
        )

    def _current_cells(self, sid, scene_id, roster, grid):
        """Read required actor cells and check map consistency and duplicate occupancy."""
        from .grid import cell_in_bounds

        topology = self.world.grid.reader.topology_for_grid(sid, scene_id, grid)
        positions = {}
        for actor_id in roster:
            position = self.characters.position(sid, actor_id)
            if (
                position.scene_id != scene_id
                or not cell_in_bounds(topology, position.cell)
                or not topology.is_valid_cell(position.cell)
            ):
                raise StateMachineError(
                    "invalid_spatial_state", "Current actor position is incomplete"
                )
            positions[actor_id] = position.cell
        if len(set(positions.values())) != len(positions):
            raise StateMachineError("invalid_spatial_state", "Encounter occupancy overlaps")
        return positions

    def _rng_context(self, row):
        """Delegate RNG context reading to the unified runtime."""
        return self.world.runtime.rng_context(row)

    def _prepare_combat(self, row, package):
        """Verify fixed bindings and combat foundations before preparing snapshot and RNG
        inputs.
        """
        self._verify_dnd_binding(package, row)
        if package.combat_foundation_json is None:
            raise StateMachineError("unsupported_operation", "Package has no reviewed encounter")
        data = json.loads(package.combat_foundation_json)
        original = validate_foundation(data)
        if original.scene_state.scene_id != row["current_scene_id"]:
            raise StateMachineError(
                "invalid_combat_foundation", "Encounter must be in its authored scene"
            )
        if original.combat_state.party_ids != {row["pc_template_id"]}:
            raise StateMachineError(
                "invalid_combat_foundation", "Encounter must use selected PC template"
            )
        data = _remap(data, row["pc_template_id"], row["pc_actor_id"])
        data["snapshot"]["session_id"] = row["session_id"]
        data["snapshot"]["world_version"] = row["world_version"]
        spatial = self.facts.spatial_scene(row["session_id"], row["current_scene_id"])
        if spatial is not None:
            self.world._spatial_combat_guard(row["session_id"], row["current_scene_id"], package)
            roster = data["snapshot"]["combat_state"]["initiative_ids"]
            present = {
                r[0]
                for r in self._conn.execute(
                    "SELECT actor_id FROM actors WHERE session_id=? AND current_scene_id=?",
                    (row["session_id"], row["current_scene_id"]),
                )
            }
            if present != set(roster):
                raise StateMachineError(
                    "invalid_spatial_state", "Encounter roster must cover present actors"
                )
            data["snapshot"]["scene_state"] = spatial
        if not self.facts.instances.session_value(row["session_id"], "has_noncombat"):
            exclude = (
                {"spells_known"}
                if original.snapshot_schema_version == "engine-snapshot/20"
                else None
            )
            data["snapshot"]["character_states"] = [
                self.characters.initial_rule_actor(row["session_id"], actor).model_dump(
                    mode="json", exclude=exclude
                )
                for actor in CombatSnapshot.model_validate_json(
                    canonical_json(data["snapshot"])
                ).character_states
            ]
        if self.facts.instances.session_value(row["session_id"], "has_noncombat"):
            try:
                roster = data["snapshot"]["combat_state"]["initiative_ids"]
                actors = self.characters.rule_actors(row["session_id"], set(roster))
                data["snapshot"]["character_states"] = [actors[a] for a in roster]
                data["snapshot"].update(
                    self.characters.rule_components(row["session_id"], set(roster))
                )
                data["snapshot"]["scene_state"] = self.facts.mechanical_scene(
                    row["session_id"], row["current_scene_id"]
                )
            except (KeyError, TypeError) as exc:
                raise StateMachineError(
                    "invalid_mechanical_state", "Encounter dependencies are missing"
                ) from exc
        data["snapshot"]["combat_state"]["actor_zone"] = self._current_cells(
            row["session_id"],
            row["current_scene_id"],
            data["snapshot"]["combat_state"]["initiative_ids"],
            data["snapshot"]["scene_state"]["grid"],
        )
        snapshot = CombatSnapshot.model_validate_json(canonical_json(data["snapshot"]))
        if snapshot.scene_state.scene_id != row["current_scene_id"]:
            raise StateMachineError(
                "invalid_combat_foundation", "Encounter must be in current scene"
            )
        if reason := template_support_failure(snapshot, self.loader):
            raise StateMachineError("unsupported_combat_foundation", reason)
        return (snapshot, RNGState.model_validate_json(canonical_json(data["rng_state"])))

    async def _evaluate_dnd(self, request):
        """Invoke stateless evaluation with the selected fixed rules loader."""
        return await evaluate(request, loader=self.loader)

    def _dnd_capability(self, payload, actor_id, snapshot):
        """Determine command support from actual actor, monster and operation state."""
        if type(payload) is CheckPayload:
            return "pending_choice_unsupported" if payload.dc is None else None
        if type(payload) is CombatClosePayload:
            return None
        if type(payload) in (ExplorationItemUsePayload, ExplorationSpellCastPayload):
            return None  # RE validates the fixed exploration operation.
        if type(payload) is ItemUsePayload:
            if payload.target_id != actor_id or actor_id not in snapshot.combat_state.party_ids:
                return "item_unauthorized"
            if not any(
                e.instance_id == payload.instance_id for e in snapshot.inventory_state.entries
            ):
                return "item_missing"
            return None
        combat = snapshot.combat_state
        if payload.intent_type == "drop_concentration":
            return None  # RE validates the caster's current group and the exact intent.
        if payload.intent_type == "cast_spell":
            if payload.spell_id is None:
                return "intent.capability"
            if payload.spell_id == "haste":
                basic = type(payload)(
                    intent_type="cast_spell",
                    source_id=payload.source_id,
                    spell_id="haste",
                    target_id=payload.target_id,
                    target_ids=payload.target_ids,
                    slot_level=payload.slot_level,
                    spell_source_id=payload.spell_source_id,
                    willing_target_ids=payload.willing_target_ids,
                )
                if (
                    payload != basic
                    or snapshot.snapshot_schema_version != "engine-snapshot/20"
                    or payload.source_id not in (None, actor_id)
                    or payload.target_id not in combat.initiative_ids
                    or payload.target_ids not in (None, (payload.target_id,))
                    or payload.willing_target_ids != (payload.target_id,)
                    or payload.spell_source_id is None
                    or actor_id not in combat.party_ids
                    or combat.pending_reactions
                    or combat.active_reaction_responses
                    or combat.reaction_effects_pending_expiry
                    or combat.sap_marks
                    or _has_combat_effects(snapshot)
                ):
                    return "spell.capability"
                return None
            if payload.spell_id == "hold-person":
                # The bound rules engine admits the requested slot level and target count.
                basic = type(payload)(
                    intent_type="cast_spell",
                    source_id=payload.source_id,
                    spell_id="hold-person",
                    target_id=payload.target_id,
                    target_ids=payload.target_ids,
                    slot_level=payload.slot_level,
                    spell_source_id=payload.spell_source_id,
                )
                if (
                    payload != basic
                    or snapshot.snapshot_schema_version != "engine-snapshot/20"
                    or payload.source_id not in (None, actor_id)
                    or payload.target_id is None
                    or payload.spell_source_id is None
                    or actor_id not in combat.party_ids
                    or combat.pending_reactions
                    or combat.active_reaction_responses
                    or combat.reaction_effects_pending_expiry
                    or combat.sap_marks
                    or _has_combat_effects(snapshot)
                ):
                    return "spell.capability"
                return None
            canonical = get_default_ruleset()
            mechanism = ordinary_spell_mechanism(canonical, payload.spell_id)
            if mechanism is None:
                return "intent.capability"
            spell = canonical.get_spell(payload.spell_id)
            if spell is None:
                return "spell.capability"
            level = spell.level
            basic = type(payload)(
                intent_type="cast_spell",
                source_id=payload.source_id,
                spell_id=payload.spell_id,
                target_id=payload.target_id,
                slot_level=payload.slot_level,
                spell_source_id=payload.spell_source_id,
            )
            if (
                payload != basic
                or payload.slot_level not in ((None, 0) if level == 0 else (level,))
                or payload.source_id not in (None, actor_id)
                or payload.spell_source_id is None
                or payload.target_id is None
                or actor_id not in combat.party_ids
                or combat.pending_reactions
                or snapshot.effect_states
            ):
                return "spell.capability"
            return None
        if payload.intent_type == "move":
            if (
                payload.source_id not in (None, actor_id)
                or combat.pending_reactions
                or combat.active_reaction_responses
                or combat.reaction_effects_pending_expiry
            ):
                return "intent.capability"
            if any(
                combat.actor_zone[identity] == payload.target_zone_id
                for identity in combat.dead_ids
            ):
                # RE leaves dead creatures out of its movement occupancy. SM
                # still holds their position and its World grid requires one actor per cell.
                return "position_conflict"
            return None
        if payload.intent_type != "attack":
            if payload.intent_type == "pass":
                basic = type(payload)(intent_type="pass", source_id=payload.source_id)
                if (
                    payload == basic
                    and payload.source_id in (None, actor_id)
                    and snapshot.snapshot_schema_version == "engine-snapshot/20"
                    and not (
                        combat.pending_reactions
                        or combat.active_reaction_responses
                        or combat.reaction_effects_pending_expiry
                        or combat.sap_marks
                    )
                    and all(
                        actor.is_alive and actor.hp_current > 0
                        for actor in snapshot.character_states
                    )
                    and (
                        not _has_combat_effects(snapshot)
                        or _hold_source(snapshot) is not None
                        or has_haste_effects(snapshot)
                    )
                ):
                    return None
            return "intent.capability"
        selected = (
            payload.weapon_instance_id
            if snapshot.snapshot_schema_version
            in ("engine-snapshot/18", "engine-snapshot/19", "engine-snapshot/20")
            else payload.weapon_id
        )
        if selected is None and payload.stat_block_action_id is None:
            return "pending_choice_unsupported"
        if payload.stat_block_action_id is not None:
            slug = snapshot.combat_state.monster_slug_by_entity.get(actor_id)
            monster = self.loader.get_monster(slug) if slug else None
            if monster is None or payload.stat_block_action_id not in {
                a.slug for a in monster.actions
            }:
                return "unsupported_stat_block_action"
        return None

    def _apply_dnd_changes(self, row, changes, *, command_id=None):
        """Adopt verified rules changes through domain interfaces in the shared write
        scope.
        """
        self.store.require_write()
        sid = row["session_id"]
        effect_updates = any(
            component
            in {
                "hold_graph",
                "haste_graph",
                "hold_lifecycle",
                "hold_concentration",
                "effect_follow_up",
            }
            for component, _, _, _ in changes
        )
        for component, _, _, operation in changes:
            if component == "hold_concentration":
                self.characters.effects.assert_concentration(sid, operation)
        for component, identity, field, value in changes:
            if component == "actor" and field == "conditions":
                independent = [
                    condition for condition in value if condition["source_effect_id"] is None
                ]
                if (
                    not effect_updates
                    and self.characters.effects.actor_conditions(sid, identity, independent)
                    != value
                ):
                    _invalid("Delta changed effect-linked conditions")
        for component, identity, field, value in changes:
            if component == "item" and field == "consume":
                before = self.facts.object(sid, identity)
                if before is None or self.facts.item_entry(before) != value or command_id is None:
                    _invalid("Selected inventory instance changed")
                self.facts.consume_item(sid, before, command_id)
            elif component == "xp":
                self.characters.award_xp(sid, identity, value)
            elif component == "resource":
                expected_current, amount = value
                self.characters.spend_resource(
                    sid, identity, field, amount, expected_current=expected_current
                )
            elif component == "world_effect" and field == "create":
                self.world.runtime.time.consumers["effects"].apply_verified(sid, value)
            elif component == "slot_ledger":
                self.records.spend_slot(
                    sid,
                    value.combat_id,
                    identity,
                    value.slot_kind,
                    value.level,
                    value.expected,
                    value.value,
                )
            elif component == "hold_graph":
                self.characters.effects.create_concentration(
                    sid,
                    command_id,
                    identity,
                    value.concentration_id,
                    value.rounds,
                    (
                        {
                            "effect": entry.effect.model_dump(mode="json"),
                            "conditions": list(entry.lineage.statuses),
                            "lifecycle": entry.clock.model_dump(mode="json")["state"]
                            if entry.clock
                            else None,
                        }
                        for entry in value.entries
                    ),
                )
            elif component == "haste_graph":
                self.characters.effects.create_concentration(
                    sid,
                    command_id,
                    identity,
                    value.concentration_id,
                    value.rounds,
                    (
                        {
                            "effect": value.effect.model_dump(mode="json"),
                            "conditions": value.lineage.statuses,
                            "lifecycle": value.lifecycle.model_dump(mode="json")["state"],
                        },
                    ),
                )
            elif component == "hold_lifecycle":
                self.characters.effects.apply_lifecycle(sid, value)
            elif component == "effect_follow_up":
                self.characters.effects.apply_follow_up(sid, value)
            elif component == "hold_concentration":
                self.characters.effects.apply_concentration(sid, value)
            elif component == "position":
                position = self.characters.position(sid, identity)
                if (position.scene_id, position.cell) != (value.scene_id, value.expected):
                    raise StateMachineError("version_conflict", "Walking position changed")
                require_walk_route(self.world, sid, value)
                self.characters.move(sid, identity, SpatialPosition(value.scene_id, value.value))
            elif component == "actor" and field == "hp_current":
                self.characters.set_hp(sid, identity, value)
            elif component == "actor":
                self.characters.update_rule_metadata(
                    sid,
                    identity,
                    field,
                    [condition for condition in value if condition["source_effect_id"] is None]
                    if field == "conditions"
                    else value,
                )
            elif component == "combat":
                if field == "ended" and value:
                    combat = self._conn.execute(
                        "SELECT scene_id,state_json FROM combats WHERE session_id=?", (sid,)
                    ).fetchone()
                    roster = json.loads(combat["state_json"])["initiative_ids"]
                    prepared = self.facts.maps.scene(sid, combat["scene_id"]).grid
                    cells = self._current_cells(sid, combat["scene_id"], roster, prepared)
                    self.records.freeze_positions(sid, cells)
                self.records.update_rule_state(sid, field, value)
            elif component == "ledger":
                self.records.update_ledger(sid, identity, value)
            else:
                _invalid("Unknown internal component edit")
        if effect_updates:
            for component, identity, field, value in changes:
                if component == "actor" and field == "conditions":
                    independent = [c for c in value if c["source_effect_id"] is None]
                    if (
                        self.characters.effects.actor_conditions(sid, identity, independent)
                        != value
                    ):
                        _invalid("Effect-linked conditions differ after adoption")

    def request(self, command, package, snapshot, rng, *, row=None, spell_positions=None):
        """Build a rules request with durable Session identity and isolated fixed inputs."""
        row = row if row is not None else self._pinned_row(command.session_id, package)
        return RuleEvaluationRequest(
            schema_version=(
                "engine-evaluation/30"
                if command.command_type == "combat.intent"
                and snapshot.snapshot_schema_version == "engine-snapshot/20"
                and (
                    command.payload.spell_id == "haste"
                    or (
                        command.payload.intent_type in {"pass", "drop_concentration"}
                        and has_haste_effects(snapshot)
                    )
                )
                else "engine-evaluation/29"
                if type(command.payload) is ExplorationSpellCastPayload
                else "engine-evaluation/28"
                if type(command.payload) is ExplorationItemUsePayload
                else "engine-evaluation/27"
                if snapshot.snapshot_schema_version == "engine-snapshot/20"
                else "engine-evaluation/26"
                if snapshot.snapshot_schema_version == "engine-snapshot/19"
                else "engine-evaluation/25"
                if snapshot.snapshot_schema_version == "engine-snapshot/18"
                else "engine-evaluation/24"
            ),
            session_id=command.session_id,
            command_id=command.command_id,
            operation_kind=command.command_type,
            actor_id=command.actor_id,
            payload=copy.deepcopy(command.payload),
            state_snapshot=snapshot,
            ruleset_binding=self.session_binding(row),
            rng_context=rng,
            spell_positions=spell_positions,
        )

    def query_availability(
        self, session_id, actor_id, payload, package, snapshot, owner, *, row=None
    ):
        """Query the pinned attack-only availability and validate its input association."""
        row = row if row is not None else self._pinned_row(session_id, package)
        self._verify_dnd_binding(package, row)
        try:
            request = ActionAvailabilityRequest(
                schema_version=(
                    "engine-availability/16"
                    if snapshot.snapshot_schema_version == "engine-snapshot/20"
                    else "engine-availability/15"
                    if snapshot.snapshot_schema_version == "engine-snapshot/19"
                    else "engine-availability/14"
                    if snapshot.snapshot_schema_version == "engine-snapshot/18"
                    else "engine-availability/13"
                ),
                session_id=session_id,
                operation_kind="combat.intent",
                actor_id=actor_id,
                payload=copy.deepcopy(payload),
                state_snapshot=snapshot,
                ruleset_binding=self.session_binding(row),
            )
            capability = (
                self._dnd_capability(payload, actor_id, snapshot)
                if payload.intent_type == "attack"
                else "intent.capability"
            )
            if owner != process_id() and not snapshot.combat_state.ended:
                capability = "combat_unrestorable"
            if capability is not None:
                result = ActionAvailabilityResult(
                    schema_version=request.schema_version,
                    session_id=session_id,
                    actor_id=actor_id,
                    operation_kind=request.operation_kind,
                    snapshot_world_version=snapshot.world_version,
                    ruleset_binding=request.ruleset_binding,
                    status="unknown",
                    reason=RuleError(code=capability, reason="Capability is not supported"),
                )
            else:
                result = query_action_availability(request, loader=self.loader)
        except RulesetBindingError as exc:
            raise StateMachineError(
                "ruleset_binding_mismatch", "Availability evaluator binding differs"
            ) from exc
        except ValidationError as exc:
            raise StateMachineError("rule_schema_error", "Invalid availability schema") from exc
        except Exception as exc:
            raise StateMachineError("rule_query_failed", "Availability query failed") from exc
        try:
            result = ActionAvailabilityResult.model_validate(copy.deepcopy(result))
            if (
                result.session_id != request.session_id
                or result.actor_id != request.actor_id
                or result.operation_kind != request.operation_kind
                or result.schema_version != request.schema_version
                or result.snapshot_world_version != snapshot.world_version
                or result.ruleset_binding != request.ruleset_binding
            ):
                raise ValueError("Availability response differs from request")
        except (ValidationError, ValueError, TypeError) as exc:
            raise StateMachineError("invalid_rule_result", "Invalid availability result") from exc
        # Free-text Engine preflight errors may mention hidden mechanical inputs.
        # Expose a bounded code and a generic status description to the authorized actor.
        return result.model_copy(
            update={
                "reason": RuleError(
                    code=result.reason.code,
                    reason={
                        "available": "Available now; execution revalidates permission and state.",
                        "unavailable": "Unavailable on this snapshot.",
                        "unknown": "This capability cannot currently be determined.",
                    }[result.status],
                )
            }
        )
