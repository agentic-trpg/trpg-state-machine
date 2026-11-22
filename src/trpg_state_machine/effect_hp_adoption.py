"""Apply admitted HP-effect endings through Character and Effects owners."""

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


def _transition_after_end(world, sid, target_id, effect_id, before, actor, *, package, command_id):
    """Project the paired delta after the exact application has been removed."""
    old = effective_hp_bonus(before)
    new = effective_hp_bonus(_target_hp_effect(world, sid, target_id, effect_id))
    transition = paired_hp_transition(
        actor["hp_current"],
        actor["hp_max"],
        old,
        new,
        entity_type=actor["entity_type"],
    )
    if old != new:
        world.characters.set_hp_pair(sid, target_id, transition)
    if transition.zero_consequence == "none":
        return []
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
                "target_kind": actor["entity_type"],
                "reason": "effect_bonus_ended",
                "scene_id": world._actor_position(sid, target_id).scene_id,
            },
            phase="effects.after_hp_zero",
        )
    )
    return events


def apply_world_spell_end(world, sid, ending, *, package, command_id):
    """End an exact checked spell application, retaining effective HP fallback."""
    effect = ending.expected.effect
    if reviewed_hp_bonus(effect) is None:
        world.characters.effects.apply_spell_end(sid, ending)
        return []
    target = effect.target_id
    before = _target_hp_effect(world, sid, target, effect.id)
    actor = world.characters.rule_actors(sid, (target,))[target]
    world.characters.effects.apply_spell_end(sid, ending)
    try:
        return _transition_after_end(
            world,
            sid,
            target,
            effect.id,
            before,
            actor,
            package=package,
            command_id=command_id,
        )
    except ValueError as exc:
        raise StateMachineError("invalid_rule_result", "HP effect ending input differs") from exc


def apply_hp_effect_expiry(world, sid, expired, remove, *, package, command_id):
    """Use the same one-target transition for a natural elapsed deadline."""
    if reviewed_hp_bonus(expired) is None:
        remove()
        return []
    target = expired.target_id
    before = _target_hp_effect(world, sid, target, expired.id)
    actor = world.characters.rule_actors(sid, (target,))[target]
    remove()
    try:
        return _transition_after_end(
            world,
            sid,
            target,
            expired.id,
            before,
            actor,
            package=package,
            command_id=command_id,
        )
    except ValueError as exc:
        raise StateMachineError("invalid_rule_result", "HP effect expiry input differs") from exc
