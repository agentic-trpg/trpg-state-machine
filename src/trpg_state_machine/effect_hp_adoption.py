"""Apply admitted HP-effect endings through Character and Effects owners."""

from collections import defaultdict

from dnd5e_engine.evaluation_aid import (
    effective_hp_bonus,
    paired_hp_transition,
    reviewed_hp_bonus,
)
from dnd5e_engine.evaluation_effects import EffectState
from dnd5e_engine.evaluation_state import ConditionRecord

from .errors import StateMachineError
from .package import canonical_json


def _target_hp_effect(world, sid, target_id, effect_id):
    """Read one target's admitted same-name applications in persisted order."""
    return tuple(
        EffectState.model_validate_json(canonical_json(data))
        for data in world.characters.effects.for_target_effect(sid, target_id, effect_id)
    )


def _adopt_hp_bonus_change(world, sid, target_id, old, new, *, package, command_id):
    """Adopt one resulting bonus and any life change through its existing owners."""
    if old == new:
        return []
    current, maximum, entity_type = world.characters.effect_hp_facts(sid, target_id)
    transition = paired_hp_transition(current, maximum, old, new, entity_type=entity_type)
    world.characters.set_hp_pair(sid, target_id, transition)
    if transition.zero_consequence == "none":
        return []
    # RUO-45 tracks atomic adoption with the combat round and turn lifecycle.
    if world._active_combat(sid):
        raise StateMachineError(
            "unsupported_effect_consequence", "Combat zero-HP lifecycle needs encounter adoption"
        )
    events = []
    casting = world.spell_casts.active(sid, target_id)
    if casting is not None:
        events.append(world.spell_casts.interrupt(casting, package, command_id))
    else:
        events.extend(
            world.runtime.time.consumers["effects"].end_world_concentration(
                sid, package, target_id, "concentration_ended"
            )
        )
    if transition.zero_consequence == "unconscious":
        if "unconscious" not in world.characters.condition_names(sid, target_id) and (
            "unconscious" not in world.characters.condition_immunities(sid, target_id)
        ):
            world.characters.add_condition(
                sid,
                target_id,
                ConditionRecord(
                    condition="unconscious",
                    source_entity_id="implied:event",
                    scope="combat",
                    duration_rounds=None,
                    save_dc=None,
                    applied_round=0,
                    exhaustion_level=1,
                    source_effect_id=None,
                ),
            )
    else:
        world.characters.update_life_state(sid, target_id, "is_alive", False)
    events.append(
        world._capture_event(
            sid,
            package,
            "actor.hp_zero",
            {
                "actor_id": target_id,
                "target_kind": entity_type,
                "reason": "effect_bonus_ended",
                "scene_id": world._actor_position(sid, target_id).scene_id,
            },
            phase="effects.after_hp_zero",
        )
    )
    return events


def apply_world_spell_end(world, sid, ending, *, package, command_id):
    """End an exact checked spell application, retaining effective HP fallback."""
    return apply_world_spell_ends(world, sid, (ending,), package=package, command_id=command_id)


def _hp_ending_bonuses(world, sid, target, effect_id, removed_origins):
    """Read and rank one same-name group once for all removals in the operation."""
    before = _target_hp_effect(world, sid, target, effect_id)
    return (
        effective_hp_bonus(before),
        effective_hp_bonus(effect for effect in before if effect.origin not in removed_origins),
    )


def apply_world_spell_ends(world, sid, endings, *, package, command_id):
    """Check each exact removal and adopt each affected HP group once."""
    try:
        removed = defaultdict(set)
        for ending in endings:
            effect = ending.expected.effect
            if reviewed_hp_bonus(effect) is not None:
                removed[effect.target_id, effect.id].add(effect.origin)
        bonuses = {
            target_effect: _hp_ending_bonuses(world, sid, *target_effect, origins)
            for target_effect, origins in removed.items()
        }
        for ending in endings:
            world.characters.effects.apply_spell_end(sid, ending)
        events = []
        for (target, _), (old, new) in bonuses.items():
            events.extend(
                _adopt_hp_bonus_change(
                    world, sid, target, old, new, package=package, command_id=command_id
                )
            )
        return events
    except ValueError as exc:
        raise StateMachineError("invalid_rule_result", "HP effect ending input differs") from exc


def apply_hp_effect_expiry(world, sid, expired, remove, *, package, command_id):
    """Use the same one-target transition for a natural elapsed deadline."""
    if reviewed_hp_bonus(expired) is None:
        remove()
        return []
    target = expired.target_id
    try:
        old, new = _hp_ending_bonuses(world, sid, target, expired.id, {expired.origin})
        remove()
        return _adopt_hp_bonus_change(
            world, sid, target, old, new, package=package, command_id=command_id
        )
    except ValueError as exc:
        raise StateMachineError("invalid_rule_result", "HP effect expiry input differs") from exc
