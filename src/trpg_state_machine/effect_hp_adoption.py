"""Apply admitted HP-effect endings through Character and Effects owners."""

from dnd5e_engine.evaluation_aid import (
    effective_hp_bonus,
    paired_hp_transition,
    reviewed_hp_bonus,
)
from dnd5e_engine.evaluation_effects import EffectState

from .errors import StateMachineError
from .package import canonical_json


def _target_hp_effect(world, sid, target_id, effect_id):
    """Read one target's admitted same-name applications in persisted order."""
    return tuple(
        EffectState.model_validate_json(canonical_json(data))
        for data in world.characters.effects.for_target_effect(sid, target_id, effect_id)
    )


def _transition_after_end(world, sid, target_id, effect_id, before, current, maximum):
    """Project the paired delta after the exact application has been removed."""
    old = effective_hp_bonus(before)
    new = effective_hp_bonus(_target_hp_effect(world, sid, target_id, effect_id))
    transition = paired_hp_transition(current, maximum, old, new)
    if old != new:
        world.characters.set_hp_pair(sid, target_id, transition)


def apply_world_spell_end(world, sid, ending):
    """End an exact checked spell application, retaining effective HP fallback."""
    effect = ending.expected.effect
    if reviewed_hp_bonus(effect) is None:
        world.characters.effects.apply_spell_end(sid, ending)
        return
    target = effect.target_id
    before = _target_hp_effect(world, sid, target, effect.id)
    actor = world.characters.rule_actors(sid, (target,))[target]
    current, maximum = actor["hp_current"], actor["hp_max"]
    world.characters.effects.apply_spell_end(sid, ending)
    try:
        _transition_after_end(world, sid, target, effect.id, before, current, maximum)
    except ValueError as exc:
        raise StateMachineError("invalid_rule_result", "HP effect ending input differs") from exc


def apply_hp_effect_expiry(world, sid, expired, remove):
    """Use the same one-target transition for a natural elapsed deadline."""
    if reviewed_hp_bonus(expired) is None:
        remove()
        return
    target = expired.target_id
    before = _target_hp_effect(world, sid, target, expired.id)
    actor = world.characters.rule_actors(sid, (target,))[target]
    current, maximum = actor["hp_current"], actor["hp_max"]
    remove()
    try:
        _transition_after_end(world, sid, target, expired.id, before, current, maximum)
    except ValueError as exc:
        raise StateMachineError("invalid_rule_result", "HP effect expiry input differs") from exc
