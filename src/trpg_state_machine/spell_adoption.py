"""Validate supported ordinary spell Deltas before SM adopts any facts."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_state import CombatSnapshot, MovementLedgerState
from dnd5e_engine.movement import project_speeds, speed_for_mode
from dnd5e_engine.rules.conditions import active_condition_names, exhaustion_level_of
from dnd5e_engine.turn_rules import next_turn_index, ordinary_action_payment, reset_turn_budget
from pydantic import ValidationError

from .damage_adoption import (
    DAMAGE_OPERATIONS,
    adopt_damage_operation,
    json_value,
    ordinary_damage_target,
    validate_damage_result,
)
from .errors import StateMachineError
from .package import canonical_json


class UnsupportedSpellConsequence(Exception):
    """Signal a valid but not yet adopted spell consequence before any write."""


def _invalid(message):
    """Reject a spell result that cannot be adopted as one complete payment."""
    raise StateMachineError("invalid_rule_result", message)


def ordinary_spell_changes(request, result, mechanism, activity_id):
    """Validate one immediate healing or save-damage result against fixed input.

    The returned edits are candidates for the caller's existing write transaction.
    Resource and combat expenditure guards must both be applied there; this
    function does not change persistent state.
    """
    snapshot = request.state_snapshot
    payload = request.payload
    caster = request.actor_id
    target = payload.target_id
    spell_id = payload.spell_id
    healing = mechanism == "healing"
    slot_paid = payload.slot_level not in (None, 0)
    if mechanism not in {"healing", "save_damage"}:
        _invalid("Unsupported ordinary spell mechanism")
    data = snapshot.model_dump(mode="json")
    actors = {actor["entity_id"]: actor for actor in data["character_states"]}
    combat = data["combat_state"]
    pools = data["resource_state"]["pools"]
    if caster not in snapshot.combat_state.party_ids or target not in actors:
        _invalid("Spell actor or target is outside the admitted encounter")
    if snapshot.combat_state.pending_reactions or snapshot.effect_states:
        _invalid("Spell result needs unsupported reaction or effect adoption")
    target_before = next(actor for actor in snapshot.character_states if actor.entity_id == target)
    combat_before = snapshot.combat_state
    if not healing and caster == target and target_before.last_damaged_by != caster:
        _invalid("Spell damage attribution is incomplete")
    if (
        not healing
        and not ordinary_damage_target(combat_before, target_before)
        and any(
            type(op)
            in {
                delta.TempHPSet,
                delta.DeathStateUpdate,
                delta.ConditionsUpdate,
                delta.ProcessedDamageUpdate,
                delta.DeathLedgerUpdate,
            }
            for op in result.state_delta.operations
        )
    ):
        raise UnsupportedSpellConsequence

    submitted = [event for event in result.proposed_events if event.type == "intent_submitted"]
    cast = [event for event in result.proposed_events if event.type == "spell_cast"]
    healed = [event for event in result.proposed_events if event.type == "healing_applied"]
    saved = [event for event in result.proposed_events if event.type == "save_rolled"]
    damaged = [event for event in result.proposed_events if event.type == "damage_applied"]
    if (
        len(submitted) != 1
        or submitted[0].actor_id != caster
        or submitted[0].intent_type != "cast_spell"
        or submitted[0].spell_id != spell_id
        or submitted[0].target_id != target
        or len(cast) != 1
        or cast[0].actor_id != caster
        or cast[0].spell_id != spell_id
        or cast[0].slot_level != (payload.slot_level if slot_paid else None)
        or (len(healed) != 1 if healing else bool(healed))
        or (healing and (healed[0].target_id != target or healed[0].amount < 0))
        or (
            bool(saved or damaged or any(e.type == "death" for e in result.proposed_events))
            if healing
            else len(saved) != 1 or len(damaged) != 1
        )
        or (not healing and saved[0].target_id != target)
        or any(
            event.type
            not in {
                "intent_submitted",
                "spell_cast",
                "healing_applied",
                "save_rolled",
                "damage_applied",
                "death",
                "turn_phase",
                "turn_ended",
                "turn_started",
                "round_started",
            }
            for event in result.proposed_events
        )
    ):
        _invalid("Spell events differ from the submitted caster and target")
    changes = []
    seen = set()

    def edit(component, identity, field, expected, value):
        """Apply a guarded field to the isolated candidate exactly once."""
        key = component, identity, field
        if key in seen:
            _invalid("Duplicate spell Delta field")
        seen.add(key)
        owner = actors[identity] if component == "actor" else combat
        original = owner[field]
        expected, value = json_value(expected), json_value(value)
        if field in {"dead_ids", "processed_zero_hp_damage_instances"}:
            original, expected = sorted(original), sorted(expected)
        if original != expected:
            _invalid("Spell Delta pre-value differs from fixed input")
        owner[field] = value
        changes.append((component, identity, field, value))

    resource = ledger = None
    hp = None
    paid_action = False
    newly_dead = (
        set()
        if healing
        else validate_damage_result(
            request,
            result,
            target_before,
            source_id=f"spell:{spell_id}:{activity_id}",
            is_crit=False,
        )
    )
    dead_ids = combat_before.dead_ids | newly_dead
    eligible = next_turn_index(
        combat_before.initiative_ids, dead_ids, combat_before.current_turn_index + 1
    )
    turn_advanced = any(type(op) is delta.TurnUpdate for op in result.state_delta.operations)
    turn = next(
        (
            op.value.current_turn_index
            for op in result.state_delta.operations
            if type(op) is delta.TurnUpdate
        ),
        snapshot.combat_state.current_turn_index,
    )
    if not 0 <= turn < len(snapshot.combat_state.initiative_ids):
        _invalid("Spell turn is outside encounter")
    next_actor = snapshot.combat_state.initiative_ids[turn]
    turn_update = next(
        (op for op in result.state_delta.operations if type(op) is delta.TurnUpdate), None
    )
    solo_wrap = (
        turn_update is not None
        and next_actor == caster
        and turn_update.value.round_number == snapshot.combat_state.round_number + 1
        and all(
            actor_id == caster or actor_id in dead_ids
            for actor_id in snapshot.combat_state.initiative_ids
        )
    )
    caster_before = next(actor for actor in snapshot.character_states if actor.entity_id == caster)
    if solo_wrap and (not caster_before.action_available or caster_before.action_taken_this_turn):
        _invalid("Solo caster lacked the ordinary action before its round wrap")

    def effective_speed(before, mode):
        """Use RE's fixed-actor speed projection for a turn ledger mode."""
        source = combatant(before)
        speeds = project_speeds(
            source.base_speed,
            source.movement_modes,
            condition_names=active_condition_names(source.conditions),
            exhaustion_level=exhaustion_level_of(source.conditions),
        )
        return speed_for_mode(speeds, mode)

    def ordinary_reset(actor_id):
        """Project RE's ordinary next-turn budget from fixed actor facts."""
        before = next(actor for actor in snapshot.character_states if actor.entity_id == actor_id)
        reset = reset_turn_budget(
            combatant(before), effective_speed=effective_speed(before, "walk"), attacks=1
        ).model_dump(mode="json")
        reset["sneak_attack_spent_this_turn"] = False
        return reset

    next_reset = ordinary_reset(next_actor)
    fresh_ledger = MovementLedgerState(
        spent_ft=0, distance_ft=0, active_mode="walk", dash_count=0
    ).model_dump(mode="json")

    for event in result.proposed_events:
        if event.type == "turn_ended" and event.actor_id != caster:
            _invalid("Turn end event belongs to another actor")
        if event.type == "turn_started" and event.actor_id != next_actor:
            _invalid("Turn start event belongs to another actor")
        if event.type == "turn_phase" and event.actor_id not in (None, caster, next_actor):
            _invalid("Turn phase event belongs to another actor")
    if turn_update is None:
        _invalid("Living caster requires a next-turn transition")
    rollover = turn_update.value.round_number != snapshot.combat_state.round_number
    if (
        sum(
            event.type == "turn_ended" and event.actor_id == caster
            for event in result.proposed_events
        )
        != 1
        or sum(
            event.type == "turn_started" and event.actor_id == next_actor
            for event in result.proposed_events
        )
        != 1
        or sum(event.type == "round_started" for event in result.proposed_events) != int(rollover)
        or any(
            event.type == "round_started" and event.round_number != turn_update.value.round_number
            for event in result.proposed_events
        )
    ):
        _invalid("Spell turn handoff events differ from the accepted transition")
    for op in result.state_delta.operations:
        if type(op) is delta.ResourceUpdate:
            if resource is not None or not slot_paid or op.owner_id != caster or op.amount != -1:
                _invalid("Spell requires one owned slot payment")
            if op.expected.kind != "spell_slot" or op.expected.level != payload.slot_level:
                _invalid("Spell slot level differs from the request")
            old = op.expected.model_dump(mode="json")
            new = op.value.model_dump(mode="json")
            if old not in pools or new["current"] != old["current"] - 1:
                _invalid("Spell slot differs from the fixed resource pool")
            pools[pools.index(old)] = new
            if (
                actors[caster]["spell_slots"].get(1, actors[caster]["spell_slots"].get("1"))
                != old["current"]
            ):
                _invalid("Caster slot mirror differs from the paid pool")
            actors[caster]["spell_slots"]["1"] = new["current"]
            resource = op
            changes.append(("resource", caster, op.pool_id, (old["current"], 1)))
        elif type(op) is delta.SlotSpendLedgerUpdate:
            if (
                ledger is not None
                or not slot_paid
                or op.owner_id != caster
                or op.combat_id != combat["combat_id"]
            ):
                _invalid("Slot spend ledger has a different owner or combat")
            if combat["expended_resources"].get(caster) != op.expected:
                _invalid("Slot spend ledger pre-value differs")
            combat["expended_resources"][caster] = op.value
            ledger = op
            changes.append(("slot_ledger", caster, "expended_resources", op))
        elif healing and type(op) is delta.HPDelta:
            if hp is not None or op.target_id != target or op.amount < 0:
                _invalid("Spell changed an unrelated HP value")
            if op.resulting_hp > actors[target]["hp_max"]:
                _invalid("Spell healing exceeds target maximum HP")
            edit("actor", target, "hp_current", op.expected_hp, op.resulting_hp)
            hp = op
        elif not healing and type(op) in DAMAGE_OPERATIONS:
            adopt_damage_operation(request, target_before.entity_type.lower(), op, edit)
        elif type(op) is delta.ActionBudgetUpdate:
            if op.actor_id not in ({caster, next_actor} if turn_advanced else {caster}):
                _invalid("Spell changed an unrelated action budget")
            expected_budget = op.expected.model_dump(mode="json")
            value_budget = op.value.model_dump(mode="json")
            changed = {
                field for field in value_budget if expected_budget[field] != value_budget[field]
            }
            if op.actor_id == caster and solo_wrap:
                paid_action = True
            elif op.actor_id == caster:
                permitted = {"action_available", "action_taken_this_turn"}
                if turn_advanced:
                    permitted |= {"movement_remaining", "sneak_attack_spent_this_turn"}
                if (
                    not changed <= permitted
                    or not {"action_available", "action_taken_this_turn"} <= changed
                ):
                    _invalid("Spell changed an unrelated caster budget")
                paid_action = (
                    op.expected.action_available
                    and not op.expected.action_taken_this_turn
                    and not op.value.action_available
                    and op.value.action_taken_this_turn
                )
            elif not turn_advanced:
                _invalid("Spell changed next actor without a turn transition")
            for field in value_budget:
                edit(
                    "actor",
                    op.actor_id,
                    field,
                    expected_budget[field],
                    value_budget[field],
                )
        elif type(op) is delta.TurnUpdate:
            if (
                op.combat_id != combat["combat_id"]
                or op.value.ended != op.expected.ended
                or op.value.turn_serial != op.expected.turn_serial + 1
                or op.value.last_ended_turn != (snapshot.combat_state.round_number, caster)
                or op.value.departed_actor_id != op.expected.departed_actor_id
                or op.value.monster_turn_start_done != op.expected.monster_turn_start_done
            ):
                _invalid("Spell turn transition differs from caster's ended turn")
            if eligible is None or op.value.current_turn_index != eligible[0]:
                _invalid("Spell turn skipped the next live actor")
            if op.value.round_number != op.expected.round_number + int(eligible[1]):
                _invalid("Spell round transition is inconsistent")
            expected_turn = op.expected.model_dump(mode="json")
            value_turn = op.value.model_dump(mode="json")
            for field in value_turn:
                edit(
                    "combat",
                    op.combat_id,
                    field,
                    expected_turn[field],
                    value_turn[field],
                )
        elif type(op) is delta.MovementLedgerUpdate:
            if (
                not turn_advanced
                or op.actor_id != next_actor
                or combat["movement_ledgers"].get(op.actor_id)
                != op.expected.model_dump(mode="json")
            ):
                _invalid("Spell changed movement outside the next turn")
            key = "movement_ledgers", op.actor_id
            if key in seen:
                _invalid("Duplicate spell movement ledger")
            seen.add(key)
            combat["movement_ledgers"][op.actor_id] = op.value.model_dump(mode="json")
            changes.append(
                ("ledger", op.actor_id, "movement_ledgers", op.value.model_dump(mode="json"))
            )
        else:
            _invalid("Unsupported ordinary spell consequence")
    if solo_wrap and not paid_action:
        paid_action = True
    if any(
        actors[next_actor][field] != next_reset[field]
        for field in delta.AttackBudgetState.model_fields
    ):
        _invalid("Next actor omitted or changed its ordinary turn budget")
    if not solo_wrap:
        payment = ordinary_action_payment(combatant(caster_before))
        if payment != {"action_available": False, "action_taken_this_turn": True}:
            _invalid("Caster lacked the ordinary action before the spell")
        outgoing = caster_before.model_dump(mode="json")
        outgoing.update(payment)
        caster_ledger = snapshot.combat_state.movement_ledgers[caster]
        outgoing["movement_remaining"] = caster_ledger.remaining(
            effective_speed(caster_before, caster_ledger.active_mode)
        )
        outgoing["sneak_attack_spent_this_turn"] = False
        if any(
            actors[caster][field] != outgoing[field]
            for field in delta.AttackBudgetState.model_fields
        ):
            _invalid("Outgoing caster budget differs from its paid turn end")
    if combat["movement_ledgers"].get(next_actor) != fresh_ledger:
        _invalid("Next actor omitted or changed its ordinary movement ledger")
    if not paid_action or (resource is None) == slot_paid or (ledger is None) == slot_paid:
        _invalid("Spell action and slot payment are incomplete")
    if resource is not None and (
        ledger.slot_kind != resource.value.kind or ledger.level != resource.value.level
    ):
        _invalid("Spell payment and ledger disagree")
    if healing:
        expected_healing = min(healed[0].amount, target_before.hp_max - target_before.hp_current)
        if hp is None and expected_healing != 0 or hp is not None and hp.amount != expected_healing:
            _invalid("Spell HP differs from the healing event")
    data.update(
        character_states=[actors[a.entity_id] for a in snapshot.character_states],
        combat_state=combat,
    )
    data["resource_state"]["pools"] = pools
    try:
        CombatSnapshot.model_validate_json(canonical_json(data))
    except ValidationError as exc:
        raise StateMachineError(
            "invalid_rule_result", "Spell Delta violates snapshot invariants"
        ) from exc
    return tuple(changes)
