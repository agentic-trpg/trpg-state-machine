"""Validate the pinned Engine's bounded closure, without computing its outcome."""

from dnd5e_engine.evaluation_delta import CombatClose
from dnd5e_engine.events import CombatEnded

from .errors import StateMachineError


def closure_changes(request, result):
    def require(condition, message):
        if not condition:
            raise StateMachineError("invalid_rule_result", message)

    snapshot = request.state_snapshot
    combat = snapshot.combat_state
    actors = {a.entity_id: a for a in snapshot.character_states}
    operations = result.state_delta.operations
    closes = [op for op in operations if type(op) is CombatClose]
    require(
        len(closes) == 1 and len(operations) == 1,
        "Closure requires only one CombatClose; other actor mutations are not admitted",
    )
    close = closes[0]
    require(not combat.ended and close.combat_id == combat.combat_id, "Closure fence differs")
    require(
        len(result.proposed_events) == 1
        and type(result.proposed_events[0]) is CombatEnded
        and result.proposed_events[0].reason == close.reason,
        "Closure requires its corresponding CombatEnded event",
    )
    require(not result.rng_transition.state_changed, "Bounded closure cannot consume RNG")
    require(
        [r.model_dump(mode="json") for r in close.historical.deaths]
        == [r.model_dump(mode="json") for r in combat.deaths_recorded],
        "Closure cannot rewrite death history",
    )
    require(
        close.historical.residual_hp == {i: actors[i].hp_current for i in combat.party_ids}
        and close.historical.residual_temp_hp
        == {i: actors[i].temp_hp for i in combat.party_ids if actors[i].temp_hp > 0}
        and close.historical.expended_resources == combat.expended_resources,
        "Closure final balances differ from committed facts",
    )
    require(
        set(close.xp_increments) <= combat.party_ids - combat.dead_ids,
        "XP ownership is outside the surviving party",
    )
    require(
        all(i in actors and not actors[i].is_alive for i in combat.dead_ids),
        "Closure requires already committed terminal death facts",
    )
    changes = []
    changes.append(("combat", combat.combat_id, "ended", True))
    changes.extend(("xp", i, "xp", amount) for i, amount in close.xp_increments.items())
    return tuple(changes)
