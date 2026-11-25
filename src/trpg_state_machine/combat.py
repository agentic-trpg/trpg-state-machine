"""Combat command inputs and validation of authored mechanical foundations."""

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
    """Return the process identity including this interpreter's private token."""
    return f"{os.getpid()}:{_PROCESS_TOKEN}"


@dataclass(frozen=True, slots=True)
class InitializeCombat:
    """Initialize the one reviewed encounter in the pinned Adventure Package."""


@dataclass(frozen=True, slots=True)
class CombatOpponent:
    """Associate a selected opponent with a stat block in the bound ruleset."""

    actor_id: str
    monster_slug: str


@dataclass(frozen=True, slots=True)
class StartCombat:
    """Start combat with a fixed defense area or a pursuit distance in feet.

    Participants and their confirmed initiative order use actual Session actor
    identities. Optional opponent profiles select actions from bound stat blocks.
    The optional end condition names one policy installed by the trusted host;
    callers cannot submit its predicate or a claimed satisfaction result.
    """

    participant_ids: tuple[str, ...]
    initiative_order: tuple[str, ...]
    opening_cell: str | None = None
    area_cells: tuple[str, ...] | None = None
    opponent_profiles: tuple[CombatOpponent, ...] = ()
    withdrawal_distance_ft: int | None = None
    end_condition_id: str | None = None


@dataclass(frozen=True, slots=True)
class EncounterRegion:
    """Immutable set of playable cells owned by one encounter in one Scene."""

    combat_id: str
    scene_id: str
    cells: frozenset[str]


@dataclass(frozen=True, slots=True)
class DistanceWithdrawal:
    """Immutable distance threshold, measured in feet, for one pursuit encounter."""

    combat_id: str
    scene_id: str
    distance_ft: int


def validate_start_combat(payload: StartCombat) -> None:
    """Validate immutable command shape and exact agreement of the two rosters."""

    def valid_id(value):
        """Check an identity before set operations and canonical encoding."""
        return type(value) is str and bool(value.strip()) and len(value) <= 256

    roster, order = payload.participant_ids, payload.initiative_order
    if (
        type(roster) is not tuple
        or type(order) is not tuple
        or len(roster) < 2
        or len(roster) != len(order)
        or not all(valid_id(value) for values in (roster, order) for value in values)
        or set(roster) != set(order)
        or len(set(order)) != len(order)
    ):
        raise StateMachineError("invalid_operation", "Provide a unique roster and its exact order")
    opening, area, distance = (
        payload.opening_cell,
        payload.area_cells,
        payload.withdrawal_distance_ft,
    )
    if (
        sum(choice is not None for choice in (opening, area, distance)) != 1
        or (opening is not None and not valid_id(opening))
        or (distance is not None and (type(distance) is not int or not 0 < distance < 2**63))
        or (
            area is not None
            and (
                type(area) is not tuple
                or not area
                or not all(valid_id(cell) for cell in area)
                or len(set(area)) != len(area)
            )
        )
    ):
        raise StateMachineError(
            "invalid_operation",
            "Choose an opening cell, a complete distinct area, or a positive integer distance",
        )
    profiles = payload.opponent_profiles
    if payload.end_condition_id is not None and not valid_id(payload.end_condition_id):
        raise StateMachineError("invalid_operation", "Expected a registered end condition identity")
    if (
        type(profiles) is not tuple
        or any(
            type(profile) is not CombatOpponent
            or not valid_id(profile.actor_id)
            or not valid_id(profile.monster_slug)
            for profile in profiles
        )
        or len({profile.actor_id for profile in profiles}) != len(profiles)
    ):
        raise StateMachineError("invalid_operation", "Provide distinct, typed opponent profiles")


def validate_foundation(value):
    """Validate all required Engine fields, including explicit empty components."""
    if type(value) is not dict or set(value) != {"snapshot", "rng_state"}:
        raise StateMachineError("invalid_combat_foundation", "Expected snapshot and rng_state")
    try:
        snapshot = CombatSnapshot.model_validate_json(canonical_json(value["snapshot"]))
        RNGState.model_validate_json(canonical_json(value["rng_state"]))
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateMachineError("invalid_combat_foundation", "Incomplete Engine state") from exc
    if snapshot.world_version != 0:
        raise StateMachineError("invalid_combat_foundation", "Initial world version must be zero")
    if snapshot.combat_state.ended:
        raise StateMachineError("invalid_combat_foundation", "An initial encounter must be active")
    if snapshot.snapshot_schema_version in ("engine-snapshot/19", "engine-snapshot/20"):
        # This RE admission includes the plain guards and checks complete
        # authored class/equipment dependencies for current snapshots.
        from dnd5e_engine.evaluation_class_dependencies import operation_dependencies
        from dnd5e_engine.evaluation_ruleset import get_default_ruleset

        reason = operation_dependencies(
            snapshot, get_default_ruleset(), equipped=True, reviewed_class_lifecycle=True
        )
    else:
        reason = snapshot_support_failure(snapshot)
    if reason:
        raise StateMachineError("unsupported_combat_foundation", reason)
    return snapshot


def validate_noncombat_foundation(value):
    """Validate noncombat foundation, scene, actor and isolated RNG inputs."""
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
            snapshot.world_version != 0
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
    if snapshot.combat_setup is not None:
        raise StateMachineError(
            "unsupported_noncombat_foundation", "Combat setup persistence is not admitted"
        )
    if snapshot.effect_states:
        raise StateMachineError(
            "unsupported_noncombat_foundation", "Effect persistence is not admitted"
        )
    return snapshot


def _remap(value, old, new):
    """Recursively replace selected identity keys and values in structured data."""
    if isinstance(value, dict):
        return {new if k == old else k: _remap(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, old, new) for v in value]
    return new if value == old else value
