"""Pinned Engine schema adapter. SQLite components are the only durable authority.

This is a restricted, authored initial encounter, not the proposed combat.start API.
No initiative calculation, registry handle, snapshot replacement, or rule formula.
"""

import os
from dataclasses import dataclass
from uuid import uuid4

from dnd5e_engine.evaluation_preflight import snapshot_support_failure
from dnd5e_engine.evaluation_rng import RNGState
from dnd5e_engine.evaluation_state import CombatSnapshot, NonCombatSnapshot, SceneState
from pydantic import ValidationError

from .errors import StateMachineError
from .package import canonical_json

DND_RULESET = "dnd-2024-srd-5.2.1"
_PROCESS_TOKEN = uuid4().hex


def process_id():
    return f"{os.getpid()}:{_PROCESS_TOKEN}"


@dataclass(frozen=True, slots=True)
class InitializeCombat:
    """Initialize the one reviewed encounter in the pinned Adventure Package."""


def validate_foundation(value):
    """Validate all required Engine fields, including explicit empty components."""
    if type(value) is not dict or set(value) != {"snapshot", "rng_state"}:
        raise StateMachineError("invalid_combat_foundation", "Expected snapshot and rng_state")
    try:
        snapshot = CombatSnapshot.model_validate_json(canonical_json(value["snapshot"]))
        RNGState.model_validate_json(canonical_json(value["rng_state"]))
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateMachineError("invalid_combat_foundation", "Incomplete Engine state") from exc
    if snapshot.combat_state.event_log or snapshot.world_version != 0:
        raise StateMachineError(
            "invalid_combat_foundation", "Initial history/version must be empty"
        )
    if snapshot.combat_state.ended:
        raise StateMachineError("invalid_combat_foundation", "An initial encounter must be active")
    if reason := snapshot_support_failure(snapshot):
        raise StateMachineError("unsupported_combat_foundation", reason)
    return snapshot


def validate_noncombat_foundation(value):
    if type(value) is not dict or set(value) != {
        "snapshot",
        "rng_state",
        "xp",
        "additional_scenes",
    }:
        raise StateMachineError("invalid_noncombat_foundation", "Expected complete initial facts")
    try:
        snapshot = NonCombatSnapshot.model_validate_json(canonical_json(value["snapshot"]))
        RNGState.model_validate_json(canonical_json(value["rng_state"]))
        scenes = [
            SceneState.model_validate_json(canonical_json(scene))
            for scene in value["additional_scenes"]
        ]
        ids = [snapshot.scene_state.scene_id, *(s.scene_id for s in scenes)]
        if (
            snapshot.snapshot_schema_version != "engine-snapshot/2"
            or snapshot.world_version != 0
            or not snapshot.character_states
            or len(ids) != len(set(ids))
            or type(value["xp"]) is not dict
            or set(value["xp"]) != {a.entity_id for a in snapshot.character_states}
            or any(
                type(xp) is not int or not 0 <= xp <= 1_000_000_000 for xp in value["xp"].values()
            )
        ):
            raise ValueError("Invalid schema/version, scenes or XP owners")
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateMachineError(
            "invalid_noncombat_foundation", "Incomplete mechanical facts"
        ) from exc
    if snapshot.effect_states:
        raise StateMachineError(
            "unsupported_noncombat_foundation", "Effect persistence is not admitted"
        )
    return snapshot


def _remap(value, old, new):
    if isinstance(value, dict):
        return {new if k == old else k: _remap(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, old, new) for v in value]
    return new if value == old else value
