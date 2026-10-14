"""Event-time sensory admission, not D&D checks or a model runtime.

The pinned Engine can establish inability or mechanical capability, but has no
automatic-noticing API. Capability therefore remains UNRESOLVED, never POSITIVE.
Positive observations require the existing separately validated Host judgment.
"""

from dataclasses import dataclass
from enum import StrEnum

from dnd5e_engine.activities.passive_stats import CombatantSenses
from dnd5e_engine.evaluation_state import MechanicalGrid, SensesState
from dnd5e_engine.rules.conditions import Condition, conditions_auto_fail_check
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from pydantic import ValidationError

from .package import (
    SYNTHETIC_EVALUATION_RULESET,
    SYNTHETIC_RULESET,
    Signal,
    SignalOrigin,
    SignalStimulus,
    canonical_json,
)


class PerceptionOutcome(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class PerceptionDecision:
    outcome: PerceptionOutcome
    reason: str
    judgment_allowed: bool = False


def _unknown(reason, *, judgment=False):
    return PerceptionDecision(PerceptionOutcome.UNRESOLVED, reason, judgment)


def _negative(reason):
    return PerceptionDecision(PerceptionOutcome.NEGATIVE, reason)


def discover_signals(signals, observer):
    """Scene candidate discovery only. No senses, visibility or noticing judgment."""
    return tuple(
        s
        for s in signals
        if s.scope.kind == "scene" and s.scope.scene_id == observer.get("current_scene_id")
    )


def signal_environment(signal, evidence):
    """Resolve only immutable Event evidence, never an asynchronous database read."""
    origin = signal.origin
    if origin is None or origin.scene_id not in evidence.get("scenes", {}):
        return None
    environment = dict(evidence["scenes"][origin.scene_id])
    environment.pop("lighting_sources", None)
    source = next(
        (
            a
            for a in [*evidence["actors"], *evidence.get("source_actors", [])]
            if a["actor_id"] == origin.entity_id
        ),
        None,
    )
    obj = evidence.get("objects", {}).get(origin.entity_id)
    valid = (
        origin.entity_id is None
        or (source is not None and source["current_scene_id"] == origin.scene_id)
        or isinstance(obj, dict)
        and obj.get("scene_id") == origin.scene_id
    )
    state = source.get("mechanical") if source else None
    cell = (
        source.get("cell", state.get("cell") if state else None)
        if source
        else obj.get("cell")
        if obj
        else None
    )
    if cell is not None and origin.cell is not None and cell != origin.cell:
        valid = False
    environment.update(
        origin_valid=valid,
        origin_cell=cell if cell is not None else origin.cell,
        source_conditions=state.get("conditions") if state else None if source else [],
        ruleset_id=evidence["ruleset_binding"]["ruleset_id"],
    )
    return environment


def _admission(signal, observer, environment):
    if type(signal.origin) is not SignalOrigin or type(signal.stimulus) is not SignalStimulus:
        return _unknown("missing_origin_or_stimulus")
    if signal.origin.scene_id != signal.scope.scene_id:
        return _unknown("origin_scope_mismatch")
    if signal.scope.kind != "scene" or signal.scope.visibility not in (
        "clear",
        "blocked",
        "undetermined",
    ):
        return _unknown("unsupported_scope")
    senses = observer.get("senses")
    if not isinstance(senses, dict) or type(senses.get(signal.channel)) is not bool:
        return _unknown("missing_sensory_capability")
    if not senses[signal.channel]:
        if signal.channel == "visual" and isinstance(observer.get("mechanical"), dict):
            special = observer["mechanical"].get("senses")
            if not isinstance(special, dict) or any(
                special.get(k) is not None for k in ("darkvision", "blindsight", "truesight")
            ):
                return _unknown("declared_vision_special_sense_conflict")
        return _negative("declared_sense_unavailable")
    # Package scope.blocked is a channel-specific assertion, never an acoustic
    # inference from a visual wall. Unknown visibility does not mean blocked.
    if signal.scope.visibility == "blocked":
        return _negative("declared_channel_blocked")
    if not isinstance(environment, dict) or not environment.get("origin_valid"):
        return _unknown("missing_origin_evidence")
    mechanical = observer.get("mechanical")
    if not isinstance(mechanical, dict):
        # Synthetic domain fixtures can use explicit Host adjudication without
        # D&D machinery. A D&D event missing required state cannot ask JEV to
        # invent its historical Conditions/senses or substitute a rules check.
        return _unknown(
            "missing_mechanical_state",
            judgment=environment.get("ruleset_id")
            in (
                SYNTHETIC_RULESET.ruleset_id,
                SYNTHETIC_EVALUATION_RULESET.ruleset_id,
            ),
        )
    if mechanical.get("is_alive") is False or mechanical.get("dead_recorded") is True:
        # Commit-time death state does not establish whether a preceding
        # stimulus was noticed before incapacitation within the same command.
        return _unknown("inactive_observer_requires_temporal_evidence")
    conditions = mechanical.get("conditions")
    if type(conditions) is not list or any(type(c) is not str for c in conditions):
        return _unknown("missing_conditions")
    if any(c not in {v.value for v in Condition} for c in conditions):
        return _unknown("unknown_condition")
    if type(mechanical.get("traits")) is not list:
        return _unknown("missing_sensory_traits")
    if mechanical["traits"]:
        return _unknown("unsupported_sensory_traits")
    if environment.get("unsupported_effects") is not False:
        return _unknown("unsupported_environment_effects")
    return None


def judge_visual(signal: Signal, observer: dict, environment: dict | None):
    if (
        signal.channel != "visual"
        or signal.stimulus is not None
        and signal.stimulus.kind not in ("ordinary_activity", "light_emission")
    ):
        return _unknown("unsupported_visual_stimulus")
    if failure := _admission(signal, observer, environment):
        return failure
    mechanical = observer["mechanical"]
    if environment.get("origin_cell") in environment.get(
        "light_uncertain_cells", ()
    ) or mechanical.get("cell") in environment.get("light_uncertain_cells", ()):
        return _unknown("unsupported_light_geometry")
    conditions = mechanical["conditions"]
    try:
        senses = SensesState.model_validate_json(canonical_json(mechanical["senses"]))
        special = CombatantSenses(**senses.model_dump())
        if conditions_auto_fail_check(conditions, "sight"):
            if special.blindsight is not None:
                return _unknown("blindsight_condition_interaction")
            return _negative("engine_sight_auto_fail")
        # Canonical Deafened only affects hearing; other interactions remain
        # outside this bounded adapter instead of reimplementing their rules.
        if (
            set(conditions) - {Condition.DEAFENED.value}
            or environment.get("source_conditions") != []
        ):
            return _unknown("unsupported_condition_interaction")
        grid = MechanicalGrid.model_validate_json(canonical_json(environment["grid"]))
        topology = GridTopology(GridScene(**grid.model_dump()))
        a, b = mechanical.get("cell"), environment.get("origin_cell")
        if type(b) is str and topology.distance_ft(b, b) is None:
            return _unknown("invalid_spatial_evidence")
        if type(a) is not str or type(b) is not str:
            # A general Scene judgment may fill noticing uncertainty in a plain
            # bright Scene. Missing geometry in a nontrivial mechanical setting
            # must not let a callback invent an exception to darkness/obscurement.
            plain = grid.default_lighting == "bright" and not any(
                (
                    grid.lighting,
                    grid.obscurement_cells,
                    grid.wall_segments,
                    grid.blocked_cells,
                    grid.cover_cells,
                )
            )
            return _unknown("missing_spatial_evidence", judgment=plain)
        if topology.distance_ft(a, a) is None or topology.distance_ft(b, b) is None:
            return _unknown("invalid_spatial_evidence")
        # can_see is documented for ordinary targets; a luminous phenomenon
        # cannot be modelled by inventing a light radius or altering the grid.
        if signal.stimulus.kind == "light_emission":
            if not topology.has_line_of_sight(a, b) or topology.cover_between(a, b) == "total":
                return _negative("engine_line_of_sight_blocked")
            return _unknown("light_emission_rules_unavailable")
        if not topology.can_see(a, b, special):
            return _negative("engine_visual_path_unavailable")
        return _unknown("mechanically_visible_notice_unknown", judgment=True)
    except (KeyError, TypeError, ValueError, ValidationError):
        return _unknown("invalid_mechanical_evidence")


def judge_auditory(signal: Signal, observer: dict, environment: dict | None):
    if (
        signal.channel != "auditory"
        or signal.stimulus is not None
        and signal.stimulus.kind != "sound"
    ):
        return _unknown("unsupported_auditory_stimulus")
    if failure := _admission(signal, observer, environment):
        return failure
    conditions = observer["mechanical"]["conditions"]
    if conditions_auto_fail_check(conditions, "hearing"):
        return _negative("engine_hearing_auto_fail")
    if conditions:
        return _unknown("unsupported_condition_interaction")
    # The installed Engine exposes no acoustics/automatic-noticing function.
    # Neither same Scene nor visual LoS proves that a general sound was heard.
    return _unknown("auditory_notice_rules_unavailable", judgment=True)


def judge_signal(signal, observer, environment):
    if signal.channel == "visual":
        return judge_visual(signal, observer, environment)
    if signal.channel == "auditory":
        return judge_auditory(signal, observer, environment)
    return _unknown("unsupported_channel")
