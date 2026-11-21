"""Adopt configured fixed-area conditions at verified combat boundaries."""

from dataclasses import asdict

from dnd5e_engine.evaluation_region_effect import supports_fixed_combat_regions
from dnd5e_engine.evaluation_ruleset import RulesetBinding

from .errors import StateMachineError
from .session_access import _binding


def adopt_combat_region_edges(
    world, row, snapshot, actor_id, scene, edges, *, effect_updates=False
):
    """Apply ordered source membership changes in the command's existing transaction.

    The configured Poisoned rule leaves movement and turn processing unchanged.
    Published conditions affect subsequent attacks. Other active effect profiles
    may require changes to the same evaluation and refuse this boundary atomically.
    """
    if not edges:
        return
    sid = row["session_id"]
    if (
        snapshot is None
        or snapshot.snapshot_schema_version != "engine-snapshot/20"
        or effect_updates
        or snapshot.combat_state.concentration_chain
        or snapshot.combat_state.pending_reactions
        or snapshot.combat_state.active_reaction_responses
        or snapshot.combat_state.reaction_effects_pending_expiry
        or snapshot.combat_state.sap_marks
        or (
            (snapshot.effect_states or any(a.conditions for a in snapshot.character_states))
            and not supports_fixed_combat_regions(snapshot, world.loader)
        )
        or world.characters.effects.world_passive_identities(
            sid, snapshot.combat_state.initiative_ids
        )
    ):
        raise StateMachineError(
            "combat.region_capability",
            "Fixed Poisoned regions require the reviewed combat effect graph",
        )
    immunities = world.characters.condition_immunities(sid, actor_id)
    if immunities is None:
        raise StateMachineError(
            "invalid_mechanical_state", "Combat region actor lacks mechanical facts"
        )
    binding = RulesetBinding(**asdict(_binding(row)))
    for entering, source, rule in edges:
        world.characters.effects.transition_region_effect(
            sid,
            actor_id,
            scene,
            source,
            rule=rule,
            entering=entering,
            condition_immunities=immunities,
            ruleset_binding=binding,
            loader=world.loader,
        )


def adopt_combat_region_turn_start(world, row, snapshot, actor_id, *, effect_updates=False):
    """Apply missing covered effects when initiative hands control to this actor."""
    sid = row["session_id"]
    position = world.characters.position(sid, actor_id)
    sources = world.environment.region_condition_sources(
        sid, position.scene_id, position.cell, combat=True
    )
    adopt_combat_region_edges(
        world,
        row,
        snapshot,
        actor_id,
        position.scene_id,
        tuple((True, source, rule) for source, rule in sources),
        effect_updates=effect_updates,
    )
