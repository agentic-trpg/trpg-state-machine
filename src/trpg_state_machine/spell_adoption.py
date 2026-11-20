"""Validate supported ordinary spell Deltas before SM adopts any facts."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.areas import creature_count
from dnd5e_engine.attack_rules import can_retain_turn
from dnd5e_engine.damage_rules import healing_balance
from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_speed_effects import speed_cast_support_failure, speed_with_effects
from dnd5e_engine.evaluation_state import CombatSnapshot, MovementLedgerState
from dnd5e_engine.movement import project_speeds, speed_for_mode
from dnd5e_engine.rules.conditions import active_condition_names, exhaustion_level_of
from dnd5e_engine.turn_rules import (
    bonus_action_payment,
    next_turn_index,
    ordinary_action_payment,
    reset_turn_budget,
)
from dnd5e_srd_data.schema.spell import CastingTimeUnit
from pydantic import ValidationError

from .concentration_adoption import require_effect_applied
from .damage_adoption import (
    DAMAGE_OPERATIONS,
    adopt_damage_operation,
    json_value,
    ordinary_damage_target,
    validate_damage_result,
)
from .errors import StateMachineError
from .package import canonical_json
from .speed_effect_adoption import supports_pass_roster


class UnsupportedSpellConsequence(Exception):
    """Signal a valid but not yet adopted spell consequence before any write."""


def _invalid(message):
    """Reject a spell result that cannot be adopted as one complete payment."""
    raise StateMachineError("invalid_rule_result", message)


def ordinary_spell_changes(request, result, mechanism, spell, *, hit_effect=False):
    """Validate ordinary healing, save or attack consequences against fixed input.

    The bound spell supplies casting timing, damage source and declared type.
    Attack evidence is linked to the damage instance and to the caster's budget.
    An admitted speed rider requires a complete effect, clock and lineage on a hit
    and uses any admitted elapsed movement modifier in the resulting budgets.
    Each selected creature needs ordered healing evidence and the matching capped
    HP change, with one slot and action payment for the entire cast.
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
    attack = mechanism == "attack_damage"
    activity = spell.activities[0]
    bonus = spell.casting_time.unit == CastingTimeUnit.BONUS
    slot_paid = payload.slot_level not in (None, 0)
    if mechanism not in {"healing", "save_damage", "attack_damage"} or (attack and slot_paid):
        _invalid("Unsupported ordinary spell mechanism")
    data = snapshot.model_dump(mode="json")
    actors = {actor["entity_id"]: actor for actor in data["character_states"]}
    before_actors = {actor.entity_id: actor for actor in snapshot.character_states}
    combat = data["combat_state"]
    pools = data["resource_state"]["pools"]
    combat_before = snapshot.combat_state
    if caster not in snapshot.combat_state.party_ids or target not in actors:
        _invalid("Spell actor or target is outside the admitted encounter")
    if combat_before.pending_reactions or (
        speed_cast_support_failure(snapshot, target) is not None
        if hit_effect
        else bool(snapshot.effect_states)
    ):
        _invalid("Spell result needs unsupported reaction or effect adoption")
    target_before = before_actors[target]
    selected = payload.target_ids if payload.target_ids is not None else (target,)
    if healing and (
        not selected
        or selected[0] != target
        or len(set(selected)) != len(selected)
        or len(selected) > (creature_count(activity) or 1)
        or any(
            (actor := before_actors.get(identity)) is None
            or not actor.is_alive
            or actor.hp_current <= 0
            or identity in combat_before.dead_ids
            for identity in selected
        )
    ):
        _invalid("Healing targets differ from the admitted living creatures")
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
    rolled = [event for event in result.proposed_events if event.type == "attack_rolled"]
    damaged = [event for event in result.proposed_events if event.type == "damage_applied"]
    deaths = [event for event in result.proposed_events if event.type == "death"]
    applied = [event for event in result.proposed_events if event.type == "effect_applied"]
    creations = [op for op in result.state_delta.operations if type(op) is delta.CombatEffectCreate]
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
        or tuple(event.target_id for event in healed) != (selected if healing else ())
        or any(event.amount < 0 for event in healed)
        or (healing and bool(damaged or deaths))
        or len(saved) != int(mechanism == "save_damage")
        or (saved and (saved[0].target_id != target or len(damaged) != 1))
        or len(rolled) != int(attack)
        or (not hit_effect and (applied or creations))
        or any(
            event.type
            not in {
                "intent_submitted",
                "spell_cast",
                "healing_applied",
                "save_rolled",
                "attack_rolled",
                "damage_applied",
                "effect_applied",
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
    healing_order = ["intent_submitted", "spell_cast", *("healing_applied" for _ in healed)]
    if healing and [e.type for e in result.proposed_events[: len(healing_order)]] != healing_order:
        _invalid("Healing events precede or interrupt the submitted cast")
    healing_results = {
        event.target_id: healing_balance(
            before_actors[event.target_id].hp_current,
            before_actors[event.target_id].hp_max,
            event.amount,
        )
        for event in healed
    }
    hp_changes = {
        identity: value
        for identity, value in healing_results.items()
        if value != before_actors[identity].hp_current
    }
    creation = None
    if attack:
        roll = rolled[0]
        if (
            roll.natural is None
            or not 1 <= roll.natural <= 20
            or roll.modifier is None
            or not result.rng_transition.state_changed
        ):
            _invalid("Spell attack lacks complete d20 or RNG evidence")
        if (
            roll.attacker_id != caster
            or roll.target_id != target
            or roll.is_opportunity_attack
            or (roll.is_crit and not roll.is_hit)
            or len(damaged) != int(roll.is_hit)
            or (damaged and damaged[0].damage_type != activity.damage.parts[0].types[0])
        ):
            _invalid("Damage differs from the authorized spell attack")
        creates_effect = hit_effect and roll.is_hit
        if len(creations) != int(creates_effect) or len(applied) != int(creates_effect):
            _invalid("Spell attack requires one effect creation and application only on a hit")
        if creates_effect:
            (creation,) = creations
            if (
                creation.combat_id != combat_before.combat_id
                or creation.identity[0] != target
                or creation.lifecycle.state.application.source_id != caster
                or creation.lifecycle.state.applied_turn_serial != combat_before.turn_serial
                or creation.lifecycle.state.applied_round != combat_before.round_number
                or creation.expected_absent is not True
                or creation.lineage.statuses
            ):
                _invalid("Spell attack effect differs from its fixed cast or target")
            require_effect_applied(applied[0], creation.effect)
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
    budget_paid = False
    newly_dead = (
        set()
        if healing
        else validate_damage_result(
            request,
            result,
            target_before,
            source_id=f"spell:{spell_id}:{activity.id}",
            is_crit=attack and rolled[0].is_crit,
        )
    )
    dead_ids = combat_before.dead_ids | newly_dead
    turn_update = next(
        (op for op in result.state_delta.operations if type(op) is delta.TurnUpdate), None
    )
    turn_advanced = turn_update is not None
    turn = turn_update.value.current_turn_index if turn_update else combat_before.current_turn_index
    eligible = (
        next_turn_index(
            combat_before.initiative_ids, dead_ids, combat_before.current_turn_index + 1
        )
        if turn_advanced
        else None
    )
    if not 0 <= turn < len(snapshot.combat_state.initiative_ids):
        _invalid("Spell turn is outside encounter")
    next_actor = snapshot.combat_state.initiative_ids[turn]
    solo_wrap = (
        turn_update is not None
        and next_actor == caster
        and turn_update.value.round_number == snapshot.combat_state.round_number + 1
        and all(
            actor_id == caster or actor_id in dead_ids
            for actor_id in snapshot.combat_state.initiative_ids
        )
    )
    caster_before = before_actors[caster]
    actor_before = combatant(caster_before)
    if bonus:
        if not actor_before.bonus_action_available or actor_before.bonus_action_taken_this_turn:
            _invalid("Caster lacked the bonus action before the spell")
        payment = bonus_action_payment()
    else:
        payment = ordinary_action_payment(actor_before)
        if not payment or actor_before.action_taken_this_turn:
            _invalid("Caster lacked the ordinary action before the spell")

    resulting_effects = snapshot.effect_states + (
        (creation.effect,) if creation is not None else ()
    )

    def effective_speed(before, mode):
        """Use RE's fixed-actor speed projection for a turn ledger mode."""
        if hit_effect:
            return speed_with_effects(before, resulting_effects, mode)
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
        before = before_actors[actor_id]
        reset = reset_turn_budget(
            combatant(before), effective_speed=effective_speed(before, "walk"), attacks=1
        ).model_dump(mode="json")
        reset["sneak_attack_spent_this_turn"] = False
        return reset

    if bonus and turn_advanced == can_retain_turn(
        actor_before.model_copy(update=payment),
        movement_speed=lambda _: effective_speed(caster_before, "walk"),
    ):
        _invalid("Bonus action spell turn differs from its remaining budget")

    next_reset = ordinary_reset(next_actor) if turn_advanced else None
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
    if turn_update is None and not bonus:
        _invalid("Living caster requires a next-turn transition")
    rollover = (
        turn_update is not None and turn_update.value.round_number != combat_before.round_number
    )
    if (
        sum(
            event.type == "turn_ended" and event.actor_id == caster
            for event in result.proposed_events
        )
        != int(turn_advanced)
        or sum(
            event.type == "turn_started" and event.actor_id == next_actor
            for event in result.proposed_events
        )
        != int(turn_advanced)
        or sum(event.type == "round_started" for event in result.proposed_events) != int(rollover)
        or any(
            event.type == "round_started" and event.round_number != turn_update.value.round_number
            for event in result.proposed_events
        )
    ):
        _invalid("Spell turn handoff events differ from the accepted transition")
    if not turn_advanced and [event.type for event in result.proposed_events] != healing_order:
        _invalid("Retained spell turn has unrelated events")
    if attack:
        event_order: list[str | tuple[str, str | None, int]] = [
            "intent_submitted",
            "spell_cast",
            "attack_rolled",
        ]
        if damaged:
            event_order.append("damage_applied")
        if deaths:
            event_order.append("death")
        if applied:
            event_order.append("effect_applied")
        next_round = turn_update.value.round_number
        event_order.extend((("turn_end", caster, combat_before.round_number), "turn_ended"))
        if rollover:
            event_order.extend(("round_started", ("round_start", None, next_round)))
        event_order.extend(("turn_started", ("turn_start", next_actor, next_round)))
        actual_order = [
            (event.phase, event.actor_id, event.round_number)
            if event.type == "turn_phase"
            else event.type
            for event in result.proposed_events
        ]
        if actual_order != event_order:
            _invalid("Spell attack events differ from the accepted turn order")
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
            level = str(op.expected.level)
            if actors[caster]["spell_slots"].get(level) != old["current"]:
                _invalid("Caster slot mirror differs from the paid pool")
            actors[caster]["spell_slots"][level] = new["current"]
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
            expected_result = hp_changes.pop(op.target_id, None)
            if expected_result is None or op.resulting_hp != expected_result:
                _invalid("Spell HP differs from the healing event")
            edit("actor", op.target_id, "hp_current", op.expected_hp, op.resulting_hp)
        elif not healing and type(op) in DAMAGE_OPERATIONS:
            adopt_damage_operation(request, target_before.entity_type.lower(), op, edit)
        elif type(op) is delta.CombatEffectCreate and op is creation:
            data["effect_states"].append(op.effect.model_dump(mode="json"))
            combat["effect_lifecycles"].append(op.lifecycle.model_dump(mode="json"))
            combat["conditions_by_effect"].append(op.lineage.model_dump(mode="json"))
            changes.append(("combat_effect", op.identity, "create", op))
        elif type(op) is delta.ActionBudgetUpdate:
            expected_budget = op.expected.model_dump(mode="json")
            value_budget = op.value.model_dump(mode="json")
            changed = {
                field for field in value_budget if expected_budget[field] != value_budget[field]
            }
            if op.actor_id == caster and solo_wrap:
                budget_paid = True
            elif op.actor_id == caster:
                permitted = set(payment)
                if attack:
                    permitted.add("attack_rolls_made_this_turn")
                if turn_advanced:
                    permitted |= {"movement_remaining", "sneak_attack_spent_this_turn"}
                if not changed <= permitted or not payment.keys() <= changed:
                    _invalid("Spell changed an unrelated caster budget")
                budget_paid = all(value_budget[field] == value for field, value in payment.items())
            elif (op.actor_id != next_actor or not turn_advanced) and (
                creation is None or op.actor_id != target or changed != {"movement_remaining"}
            ):
                _invalid("Spell changed an unrelated action budget")
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
    if next_reset is not None and any(
        actors[next_actor][field] != next_reset[field]
        for field in delta.AttackBudgetState.model_fields
    ):
        _invalid("Next actor omitted or changed its ordinary turn budget")
    if not solo_wrap:
        outgoing = caster_before.model_dump(mode="json")
        outgoing.update(payment)
        if attack:
            outgoing["attack_rolls_made_this_turn"] += 1
        if turn_advanced:
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
    if turn_advanced and combat["movement_ledgers"].get(next_actor) != fresh_ledger:
        _invalid("Next actor omitted or changed its ordinary movement ledger")
    if creation is not None and target not in (caster, next_actor):
        target_ledger = combat_before.movement_ledgers[target]
        if actors[target]["movement_remaining"] != target_ledger.remaining(
            effective_speed(target_before, target_ledger.active_mode)
        ):
            _invalid("Spell attack target movement differs from its applied effect")
    if (
        not (budget_paid or solo_wrap)
        or (resource is None) == slot_paid
        or (ledger is None) == slot_paid
    ):
        _invalid("Spell action and slot payment are incomplete")
    if resource is not None and (
        ledger.slot_kind != resource.value.kind or ledger.level != resource.value.level
    ):
        _invalid("Spell payment and ledger disagree")
    if hp_changes:
        _invalid("Spell omitted HP changes required by its healing events")
    data.update(
        character_states=[actors[a.entity_id] for a in snapshot.character_states],
        combat_state=combat,
    )
    data["resource_state"]["pools"] = pools
    try:
        candidate = CombatSnapshot.model_validate_json(canonical_json(data))
    except ValidationError as exc:
        raise StateMachineError(
            "invalid_rule_result", "Spell Delta violates snapshot invariants"
        ) from exc
    # A new clock needs public Pass to reach its expiry. Reuse that admission
    # until the host can advance this graph across a nonliving roster.
    if creation is not None and not supports_pass_roster(candidate.character_states):
        raise UnsupportedSpellConsequence
    return tuple(changes)
