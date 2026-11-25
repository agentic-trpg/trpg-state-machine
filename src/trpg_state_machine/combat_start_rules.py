"""Translate a confirmed encounter roster into a pinned D&D opening candidate."""

from dnd5e_engine.evaluation_actor import combatant
from dnd5e_engine.evaluation_initial_state import initial_combat_state
from dnd5e_engine.evaluation_movement import timed_walk_bonuses, walk_speed
from dnd5e_engine.evaluation_preflight import template_support_failure
from dnd5e_engine.evaluation_state import (
    CombatSetupState,
    CombatSnapshot,
    NPCStatBlockBinding,
    ObjectState,
    PersistentAreasState,
    TimedActivitiesState,
)
from dnd5e_engine.turn_rules import fresh_turn_budget

from .errors import StateMachineError
from .package import canonical_json


def prepare_start(snapshot, payload, pc_id, combat_id, cells, binding, loader):
    """Prepare fresh encounter state and RE budget proposals from fixed live inputs.

    This adapter accepts a confirmed order. It does not set or roll initiative,
    hydrate actors from stat blocks, or issue a resource grant or reward.
    """
    actors = {actor.entity_id: actor for actor in snapshot.character_states}
    if any(not actor.is_alive or actor.hp_current <= 0 for actor in actors.values()) or (
        actors[pc_id].entity_type != "Character"
    ):
        raise StateMachineError("invalid_combat_roster", "Opening requires living participants")
    enemies = tuple(actor_id for actor_id in payload.initiative_order if actor_id != pc_id)
    enemy_ids = set(enemies)
    if any(actors[actor_id].entity_type == "Character" for actor_id in enemies) or any(
        profile.actor_id not in enemy_ids for profile in payload.opponent_profiles
    ):
        raise StateMachineError("invalid_combat_roster", "Expected one PC and selected opponents")
    setup = CombatSetupState(
        combat_id=combat_id,
        party_ids=(pc_id,),
        encounter_ids=enemies,
        roll_order=(pc_id, *enemies),
        actor_zone=cells,
        fixed_initiative={actor_id: actor.initiative for actor_id, actor in actors.items()},
        initiative_modifiers=dict.fromkeys(actors),
        surprised_ids=set(),
        xp_value_by_entity=dict.fromkeys(enemies, 0),
        opportunity_attack_weapons={
            actor_id: actor.weapon_in_hands
            for actor_id, actor in actors.items()
            if actor.weapon_in_hands is not None
        },
        npc_stat_blocks=tuple(
            NPCStatBlockBinding(
                actor_id=profile.actor_id,
                monster_slug=profile.monster_slug,
                data_revision=binding.data_revision,
            )
            for profile in payload.opponent_profiles
        ),
        timed_activities=TimedActivitiesState(pending=[], next_sequence=0),
        persistent_areas=PersistentAreasState(
            areas=[], next_sequence=0, next_turn_start_effects=()
        ),
        combat_objects=ObjectState(objects=[], used_ids=set()),
    )
    candidate = CombatSnapshot.model_validate_json(
        canonical_json(
            {
                **snapshot.model_dump(mode="json", exclude={"combat_setup", "snapshot_kind"}),
                "snapshot_kind": "combat",
                "combat_state": initial_combat_state(setup, payload.initiative_order).model_dump(
                    mode="json"
                ),
            }
        )
    )
    if reason := template_support_failure(candidate, loader):
        raise StateMachineError("unsupported_combat_foundation", reason)
    try:
        walk_bonuses = timed_walk_bonuses(candidate)
    except ValueError as exc:
        raise StateMachineError("unsupported_combat_foundation", str(exc)) from exc
    budgets = {
        actor_id: fresh_turn_budget(
            combatant(actor), effective_speed=walk_speed(actor, walk_bonuses.get(actor_id, 0))
        )
        for actor_id, actor in actors.items()
    }
    return candidate, budgets
