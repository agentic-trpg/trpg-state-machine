"""Guard the bound RE's Shield payment, effect graph and next-turn expiry."""

from dnd5e_engine import evaluation_delta as delta
from dnd5e_engine.evaluation_effects import effect_state
from dnd5e_engine.evaluation_paid_projection import slot_ledger
from dnd5e_engine.evaluation_payments import spell_payment
from dnd5e_engine.evaluation_ruleset import get_default_ruleset
from dnd5e_engine.evaluation_shield import shield_graph, shield_source_id
from dnd5e_engine.evaluation_shield_delta import ShieldStateTransition
from dnd5e_engine.reaction_effect_rules import retained_reaction_response, shield_effect

from .errors import StateMachineError

SHIELD_OPS = (
    ShieldStateTransition,
    delta.ResourceUpdate,
    delta.SlotSpendLedgerUpdate,
    delta.PendingReactionUpdate,
)


class UnsupportedShieldTerminal(Exception):
    """A dead owner could never reach the retained effect's expiry boundary."""


class UnsupportedShieldKnockout(Exception):
    """The retained effect would leave a dying owner outside supported Pass admission."""


def is_retained_shield(effect):
    """Recognize the frozen retained effect, whose source uses a dormant template flag."""
    if effect.id != "effect:imperceptible_barrier" or effect.duration.seconds is not None:
        return False
    spell = get_default_ruleset().get_spell("shield")
    return effect == effect_state(shield_effect(spell, effect.target_id))


def shield_combat_fields(snapshot, loader):
    """Derive the canonical retained response and exact next-start owner from Effects."""
    spell = loader.get_spell("shield")
    if spell is None:
        return {}
    combat = snapshot.combat_state
    responses, expiries = [], {}
    linked = {link.identity for link in combat.conditions_by_effect if not link.statuses}
    for effect in snapshot.effect_states:
        identity = (effect.target_id, effect.id, effect.origin)
        if identity in linked and is_retained_shield(effect):
            expiries[effect.target_id] = [identity]
            if effect.target_id not in combat.dead_ids:
                responses.append(retained_reaction_response(spell.activities[0], identity))
    return (
        {"active_reaction_responses": responses, "reaction_effects_pending_expiry": expiries}
        if expiries
        else {}
    )


def _invalid(message):
    """Report a malformed Shield proposal without recording a business refusal."""
    raise StateMachineError("invalid_rule_result", message)


def _owner_died(owner, operations, events):
    """Bind a lost retained response to one terminal life edit and death record."""
    deaths = [event for event in events if event.type == "death" and event.target_id == owner]
    life = [
        op
        for op in operations
        if type(op) is delta.DeathStateUpdate
        and op.target_id == owner
        and op.expected.is_alive
        and not op.value.is_alive
    ]
    records = [
        record
        for op in operations
        if type(op) is delta.DeathLedgerUpdate and owner in op.dead_ids - op.expected_dead_ids
        for record in op.records[len(op.expected_records) :]
        if record.target_id == owner
    ]
    if not (deaths or life or records):
        return False
    if (
        len(deaths) != 1
        or len(life) != 1
        or len(records) != 1
        or deaths[0].reason != records[0].reason
    ):
        _invalid("Shield owner death lacks matching life state and death evidence")
    return True


def _verify_owner_turn_start(request, operations, events, start, turn_handoff):
    """Require the exact legal initiative transition before consuming Shield's clock."""
    combat = request.state_snapshot.combat_state
    turns = [op for op in operations if type(op) is delta.TurnUpdate]
    if len(turns) != 1:
        _invalid("Shield expiry requires the owner's actual turn transition")
    ended = next((index for index, event in enumerate(events) if event.type == "turn_ended"), None)
    if ended is None:
        _invalid("Shield expiry requires the previous turn's end")
    dead_before_handoff = {
        event.target_id for event in events[:ended] if event.type == "death"
    } - combat.dead_ids
    newly_recorded = {
        target
        for op in operations
        if type(op) is delta.DeathLedgerUpdate
        for target in op.dead_ids - op.expected_dead_ids
    }
    if not dead_before_handoff <= newly_recorded:
        _invalid("Shield expiry uses an unrecorded death in initiative")
    handoff_input = combat.model_copy(update={"dead_ids": combat.dead_ids | dead_before_handoff})
    if turn_handoff(handoff_input, request.actor_id, turns[0], events) != start.actor_id:
        _invalid("Shield expiry must match the verified incoming turn")


def shield_changes(request, result, turn_handoff):
    """Bind one exact temporary declaration and the active graph to this result."""
    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    old = shield_graph(snapshot)
    declarations = combat.pending_reactions
    declaration = declarations[0] if declarations else None
    operations = result.state_delta.operations
    graphs = [op for op in operations if type(op) is ShieldStateTransition]
    resources = [op for op in operations if type(op) is delta.ResourceUpdate]
    ledgers = [op for op in operations if type(op) is delta.SlotSpendLedgerUpdate]
    consumed = [op for op in operations if type(op) is delta.PendingReactionUpdate]
    events = result.proposed_events
    reactions = [event for event in events if event.type == "reaction_triggered"]
    casts = [event for event in events if event.type == "spell_cast"]
    applied = [event for event in events if event.type == "effect_applied"]
    expired = [event for event in events if event.type == "effect_expired"]
    starts = {
        event.actor_id
        for event in events
        if event.type == "turn_phase" and event.phase == "turn_start"
    }
    adopted = []
    if declaration:
        owner = declaration.owner_id
        payment = spell_payment(snapshot.resource_state, owner, declaration.slot_level)
        expected_consumption = delta.PendingReactionUpdate(
            kind="combat.pending_reaction_update",
            combat_id=combat.combat_id,
            owner_id=owner,
            expected=tuple(declarations),
            value=(),
        )
        if (
            request.payload.intent_type != "attack"
            or request.payload.target_id != owner
            or resources != [payment]
            or ledgers != [slot_ledger(snapshot, payment)]
            or consumed != [expected_consumption]
            or len(reactions) != 1
            or len(casts) != 1
            or len(applied) != 1
        ):
            _invalid("Shield payment and effect do not match the approved response")
        reaction, cast, effect = reactions[0], casts[0], applied[0].effect
        rolls = [event for event in events if event.type == "attack_rolled"]
        budgets = [
            op for op in operations if type(op) is delta.ActionBudgetUpdate and op.actor_id == owner
        ]
        spell_id = declaration.spell_id
        if (
            reaction.reaction_name != spell_id
            or reaction.actor_id != owner
            or reaction.triggering_actor_id != request.actor_id
            or reaction.affected_target_id != owner
            or reaction.provisional_attack is None
            or cast.actor_id != owner
            or cast.spell_id != spell_id
            or cast.slot_level != payment.expected.level
            or effect.target_id != owner
            or len(rolls) != 1
            or rolls[0].attacker_id != request.actor_id
            or rolls[0].target_id != owner
            or rolls[0].natural != reaction.provisional_attack.natural
            or rolls[0].roll_total != reaction.provisional_attack.roll_total
            or not events.index(reaction)
            < events.index(cast)
            < events.index(applied[0])
            < events.index(rolls[0])
            or (owner not in starts and (len(budgets) != 1 or budgets[0].value.reaction_available))
        ):
            _invalid("Shield must resolve between the original hit and its final attack evidence")
        identity = (effect.target_id, effect.id, effect.origin)
        adopted.extend(
            (
                ("resource", owner, payment.pool_id, (payment.expected.current, 1)),
                ("slot_ledger", owner, "expended_resources", ledgers[0]),
            )
        )
        if owner not in starts and (len(graphs) != 1 or graphs[0].value is None):
            _invalid("Shield response lacks its persistent effect")
        if owner in starts and graphs:
            _invalid("Shield cannot survive its owner's immediate turn start")
    else:
        if resources or ledgers or consumed or reactions or casts or applied or old is None:
            _invalid("Active Shield attack or Pass cannot make a new response payment")
        owner, identity = old.expiry_owner_id, old.expiry_identity
        removed = len(graphs) == 1 and graphs[0].value is None
        if removed != (owner in starts):
            _invalid("Shield must expire at the owner's turn start")
    if len(graphs) > 1:
        _invalid("Shield result has more than one effect graph transition")
    if expired:
        if (
            len(expired) != 1
            or owner not in starts
            or (expired[0].target_id, expired[0].effect_id, expired[0].origin) != identity
            or expired[0].reason != "duration"
        ):
            _invalid("Shield expiry does not belong to the expected turn start")
        start = next(
            event
            for event in events
            if event.type == "turn_phase"
            and event.phase == "turn_start"
            and event.actor_id == owner
        )
        if events.index(expired[0]) != events.index(start) + 1:
            _invalid("Shield expiry must immediately follow the owner's turn start")
        _verify_owner_turn_start(request, operations, events, start, turn_handoff)
    elif owner in starts:
        _invalid("Shield removal lacks its TurnStart expiry evidence")
    died = _owner_died(owner, operations, events)
    if died and not graphs and owner not in starts:
        _invalid("Shield owner death must remove the retained response")
    if graphs:
        graph = graphs[0]
        if (
            graph.combat_id != combat.combat_id
            or graph.actor_ids != combat.initiative_ids
            or graph.owner_id != owner
            or graph.spell_source_id != shield_source_id(snapshot, owner)
            or graph.expected != old
            or (
                graph.value
                and (
                    graph.value.expiry_identity != identity
                    or graph.value.owner_dead != (owner in combat.dead_ids or died)
                )
            )
        ):
            _invalid("Shield graph differs from the fixed effect and owner")
        if graph.value is not None and graph.value.owner_dead:
            raise UnsupportedShieldTerminal
        adopted.append(("shield_graph", owner, "update", graph))
    return tuple(adopted)
