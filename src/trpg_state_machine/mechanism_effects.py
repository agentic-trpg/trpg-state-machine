"""SM-owned mechanism-effect/1 port. This is NOT the pinned RE /23 ABI.

No rules, random draws, effect clocks or synthetic actor identities live here.
The production port refuses all evaluation until RE implements world sources.
"""

import copy
import hashlib
from typing import Annotated, Literal, Protocol

from dnd5e_engine.evaluation_contracts import ReadVersion, RuleError
from dnd5e_engine.evaluation_delta import DeathStateUpdate, HPDelta, TempHPSet
from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
from dnd5e_engine.evaluation_ruleset import RulesetBinding
from dnd5e_engine.evaluation_state import NonCombatSnapshot
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.dataclasses import dataclass

from .errors import StateMachineError
from .package import canonical_json
from .world_spatial import ObjectSpatialProjection

Identity = Annotated[str, Field(min_length=1, max_length=256)]
Bound = Annotated[int, Field(ge=0, le=2147483647)]
Ability = Literal["str", "dex", "con", "int", "wis", "cha"]
CONFIG = ConfigDict(extra="forbid", strict=True)


@dataclass(frozen=True, config=CONFIG)
class AttackRequirement:
    bonus: Annotated[int, Field(ge=-100, le=100)]
    mode: Literal["melee", "ranged"]
    range_feet: Annotated[int, Field(gt=0, le=10000)]


@dataclass(frozen=True, config=CONFIG)
class SaveRequirement:
    ability: Ability
    dc: Annotated[int, Field(ge=0, le=100)]
    on_success: Literal["none", "half_damage"]


@dataclass(frozen=True, config=CONFIG)
class DamageRequirement:
    dice: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,2}d(4|6|8|10|12|20|100)$")]
    bonus: Annotated[int, Field(ge=-10000, le=10000)]
    damage_type: Literal[
        "acid",
        "bludgeoning",
        "cold",
        "fire",
        "force",
        "lightning",
        "necrotic",
        "piercing",
        "poison",
        "psychic",
        "radiant",
        "slashing",
        "thunder",
    ]


@dataclass(frozen=True, config=CONFIG)
class StatusRequirement:
    condition: Literal[
        "blinded",
        "charmed",
        "deafened",
        "frightened",
        "grappled",
        "incapacitated",
        "invisible",
        "paralyzed",
        "petrified",
        "poisoned",
        "prone",
        "restrained",
        "stunned",
        "unconscious",
    ]
    duration_seconds: Annotated[int, Field(gt=0, le=2147483647)] | None
    repeat_save: SaveRequirement | None
    end_condition: Literal["duration", "effects_explicit", "source_destroyed"]


@dataclass(frozen=True, config=CONFIG)
class RuleEffectDefinition:
    effect_ref: Identity
    schema_version: Literal["mechanism-effect-definition/1"]
    attack: AttackRequirement | None
    save: SaveRequirement | None
    damage: DamageRequirement | None
    status: StatusRequirement | None
    magical: bool

    def __post_init__(self):
        if self.damage is None and self.status is None:
            raise ValueError("An effect must declare damage or status")
        if self.attack is not None and self.save is not None:
            raise ValueError("Combined attack/save requires a future profile")
        if self.status is not None and (
            (self.status.end_condition == "duration") != (self.status.duration_seconds is not None)
        ):
            raise ValueError("Duration must match the explicit end condition")
        if self.save is not None and self.save.on_success == "half_damage" and self.damage is None:
            raise ValueError("Half damage requires damage")
        if (
            self.status is not None
            and self.status.repeat_save is not None
            and self.status.repeat_save.on_success != "none"
        ):
            raise ValueError("Repeated status saves cannot specify damage scaling")


@dataclass(frozen=True, config=CONFIG)
class TargetSelector:
    scope: Literal["trigger_actor", "area"]
    cells: Annotated[tuple[str, ...], Field(max_length=256)]
    actor_kind: Literal["all", "pc", "npc"]
    living_only: bool
    exclude_trigger_actor: bool
    selection_time: Literal["before_consequence"]

    def __post_init__(self):
        if bool(self.cells) != (self.scope == "area") or len(set(self.cells)) != len(self.cells):
            raise ValueError("Only area selectors require unique cells")
        if self.scope == "trigger_actor" and self.exclude_trigger_actor:
            raise ValueError("Single target cannot exclude itself")


@dataclass(frozen=True, config=CONFIG)
class RuleEffectAction:
    kind: Literal["rule_effect"]
    effect_ref: Identity
    targets: TargetSelector


class PortModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )


class MechanismSource(PortModel):
    kind: Literal["mechanism"]
    object_id: Identity
    definition_ref: Identity
    scene_id: Identity
    cell: Identity
    activation_id: Identity
    consequence_id: Identity
    source_event_seq: Annotated[int, Field(ge=1)]
    package_digest: Identity


class ActorPose(PortModel):
    actor_id: Identity
    scene_id: Identity
    cell: Identity


class ObjectContribution(PortModel):
    object_id: Identity
    spatial: ObjectSpatialProjection


class WorldDependencies(PortModel):
    """Explicit candidate geometry and locations missing from RE NonCombatSnapshot.

    These are owner projections, not a merged Terrain or Environment simulator.
    Environment-dependent effects are outside definition/1's closed profile.
    """

    actor_poses: tuple[ActorPose, ...]
    object_contributions: tuple[ObjectContribution, ...]


class MechanismEffectRequest(PortModel):
    schema_version: Literal["mechanism-effect/1"]
    operation_kind: Literal["rules.effect"]
    session_id: Identity
    command_id: Identity
    source: MechanismSource
    effect: RuleEffectDefinition
    target_ids: Annotated[tuple[Identity, ...], Field(min_length=1, max_length=128)]
    selector: TargetSelector
    state_snapshot: NonCombatSnapshot
    world_dependencies: WorldDependencies
    ruleset_binding: RulesetBinding
    rng_context: RNGContext

    @model_validator(mode="after")
    def validate_scope(self):
        snapshot = self.state_snapshot
        ids = {a.entity_id for a in snapshot.character_states}
        if (
            self.session_id != snapshot.session_id
            or self.source.scene_id != snapshot.scene_state.scene_id
            or not set(self.target_ids) <= ids
            or tuple(sorted(set(self.target_ids))) != self.target_ids
            or {p.actor_id for p in self.world_dependencies.actor_poses} != ids
            or len(self.world_dependencies.actor_poses) != len(ids)
        ):
            raise ValueError("Request scope/targets do not match complete snapshot")
        return self

    def digest(self):
        return (
            "sha256:"
            + hashlib.sha256(
                canonical_json(self.model_dump(mode="json")).encode("utf-8")
            ).hexdigest()
        )


MechanicalDelta = Annotated[HPDelta | TempHPSet | DeathStateUpdate, Field(discriminator="kind")]


class MechanismAudit(PortModel):
    kind: Literal["attack", "save", "damage", "status"]
    target_id: Identity
    succeeded: bool | None
    roll: Annotated[int, Field(ge=1, le=100)] | None
    amount: Bound | None

    @model_validator(mode="after")
    def audit_shape(self):
        if self.kind in ("attack", "save"):
            valid = self.succeeded is not None and self.roll is not None and self.amount is None
        elif self.kind == "damage":
            valid = self.succeeded is None and self.roll is None and self.amount is not None
        else:
            valid = self.succeeded is not None and self.roll is None and self.amount is None
        if not valid:
            raise ValueError("Audit fields do not match their mechanical kind")
        return self


class MechanismEffectResult(PortModel):
    schema_version: Literal["mechanism-effect/1"]
    session_id: Identity
    command_id: Identity
    source: MechanismSource
    request_digest: Identity
    input_world_version: Bound
    input_ruleset_binding: RulesetBinding
    target_ids: tuple[Identity, ...]
    status: Literal["accepted", "rejected", "unsupported", "needs_choice"]
    read_set: tuple[ReadVersion, ...]
    state_delta: tuple[MechanicalDelta, ...] | None
    proposed_events: tuple[MechanismAudit, ...]
    rng_transition: RNGTransition | None
    error: RuleError | None
    # v1 only permits a typed preflight choice identity, never a partial continuation.
    choice: Identity | None

    @model_validator(mode="after")
    def validate_status(self):
        if self.status == "accepted":
            valid = self.state_delta is not None and self.rng_transition is not None
            valid &= self.error is None and self.choice is None
        else:
            valid = self.state_delta is None and self.rng_transition is None
            valid &= not self.proposed_events
            valid &= (
                (self.choice is not None and self.error is None)
                if self.status == "needs_choice"
                else (self.error is not None and self.choice is None)
            )
        if not valid:
            raise ValueError("Nonaccepted results cannot carry changes or RNG")
        return self


def result_envelope(request):
    """Common correlation fields, also usable by independently authored fixtures."""
    return dict(
        schema_version="mechanism-effect/1",
        session_id=request.session_id,
        command_id=request.command_id,
        source=request.source,
        request_digest=request.digest(),
        input_world_version=request.state_snapshot.world_version,
        input_ruleset_binding=request.ruleset_binding,
        target_ids=request.target_ids,
        read_set=(
            ReadVersion(
                kind="world", ref=request.session_id, version=request.state_snapshot.world_version
            ),
        ),
    )


class MechanismEffectPort(Protocol):
    """Bounded pure synchronous evaluator: no IO/waits or authoritative writes.

    Async/remote evaluation needs prepare/evaluate/OCC adoption outside the write
    transaction. The production implementation below never calls an evaluator.
    """

    def evaluate(self, request: MechanismEffectRequest) -> MechanismEffectResult: ...


class UnsupportedMechanismEffects:
    def available(self):
        return False

    def evaluate(self, request):
        return MechanismEffectResult(
            **result_envelope(request),
            status="unsupported",
            state_delta=None,
            proposed_events=(),
            rng_transition=None,
            choice=None,
            error=RuleError(
                code="unsupported_mechanism_effect",
                reason="Pinned RE /23 has no world-source effect operation",
            ),
        )


def validate_mechanism_result(request, value):
    """Reuse RE DTO invariants, complete read fence and closed owner adoption.

    The attack validator cannot be reused unchanged: its authorization and
    attribution require a character attacker. This path never fabricates one.
    """
    from .rules_adapter import _invalid

    try:
        result = MechanismEffectResult.model_validate(copy.deepcopy(value))
        expected = result_envelope(request)
        if any(getattr(result, k) != v for k, v in expected.items()):
            _invalid("Mechanism result correlation/source/version/read fence differs")
        if result.status != "accepted":
            return result, ()
        result.rng_transition.verify_input(request.rng_context)
        if any(e.target_id not in request.target_ids for e in result.proposed_events):
            _invalid("Mechanism audit target exceeds approved scope")
        if any(getattr(request.effect, e.kind) is None for e in result.proposed_events):
            _invalid("Mechanism audit kind exceeds declared effect")
        # No condition clocks or actor-attributed effect graph can be safely
        # adopted with the pinned lifecycle contract. Refuse before any write.
        if request.effect.status is not None:
            raise StateMachineError(
                "unsupported_mechanism_effect_lifecycle",
                "Effects requires world-source lifecycle support",
            )
        actors = {
            a.entity_id: a.model_dump(mode="json") for a in request.state_snapshot.character_states
        }
        if request.state_snapshot.effect_states or any(
            actors[t]["conditions"] or not actors[t]["is_alive"] for t in request.target_ids
        ):
            raise StateMachineError(
                "unsupported_mechanism_effect_delta",
                "Bounded adopter cannot settle effect/death dependencies",
            )
        changes, seen = [], set()

        def edit(target, field, expected, value):
            key = (target, field)
            if key in seen or actors[target][field] != expected:
                _invalid("Duplicate mechanism delta or incorrect pre-value")
            seen.add(key)
            actors[target][field] = value
            changes.append(("actor", target, field, value))

        for op in result.state_delta:
            if op.target_id not in request.target_ids or request.effect.damage is None:
                _invalid("Mechanism delta exceeds its approved targets/effect")
            if type(op) is HPDelta:
                if op.amount > 0:
                    _invalid("Damage cannot heal")
                if op.resulting_hp == 0:
                    raise StateMachineError(
                        "unsupported_mechanism_effect_delta",
                        "Zero-HP settlement requires a complete RE/Effects packet",
                    )
                edit(op.target_id, "hp_current", op.expected_hp, op.resulting_hp)
            elif type(op) is TempHPSet:
                if op.temp_hp > op.expected_temp_hp:
                    _invalid("Damage cannot grant temporary HP")
                edit(op.target_id, "temp_hp", op.expected_temp_hp, op.temp_hp)
            elif type(op) is DeathStateUpdate:
                raise StateMachineError(
                    "unsupported_mechanism_effect_delta",
                    "Death adoption needs the corresponding conditions/Effects packet",
                )
        data = request.state_snapshot.model_dump(mode="json")
        data["character_states"] = [
            actors[a.entity_id] for a in request.state_snapshot.character_states
        ]
        NonCombatSnapshot.model_validate_json(canonical_json(data))
        return result, tuple(changes)
    except (ValidationError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise StateMachineError(
            "invalid_rule_result", "Malformed mechanism result/delta/RNG"
        ) from exc


class MechanismEffectRunner:
    def __init__(self, handler):
        self.h = handler
        self.port: MechanismEffectPort = UnsupportedMechanismEffects()

    def available(self):
        # No test evaluator is selected by any public command or Package field.
        return not isinstance(self.port, UnsupportedMechanismEffects)

    def request(
        self, row, package, oid, actor, activation, consequence, source_seq, command_id, action
    ):
        rules = self.h.objects.openable.rules
        rules._verify_dnd_binding(package, row)
        snapshot = rules._noncombat_snapshot(row, package)
        config = self.h.objects.mechanisms.config(package, oid)
        definition = next(e for e in config.rule_effects if e.effect_ref == action.effect_ref)
        location = self.h.facts.object_position(row["session_id"], oid)
        if location["scene_id"] != snapshot.scene_state.scene_id or location["cell"] is None:
            raise StateMachineError(
                "unsupported_mechanism_source", "Effect source needs a complete local pose"
            )
        positions = {
            a.entity_id: rules.characters.position(row["session_id"], a.entity_id)
            for a in snapshot.character_states
        }
        kinds = dict(
            self.h._conn.execute(
                "SELECT actor_id,kind FROM actors WHERE session_id=?", (row["session_id"],)
            )
        )
        selector = action.targets
        grid = snapshot.scene_state.grid
        if any(
            not (0 <= int(c.split(",")[0]) < grid.width and 0 <= int(c.split(",")[1]) < grid.height)
            for c in selector.cells
        ):
            raise StateMachineError(
                "invalid_mechanism_targets", "Portable mechanism area outside current scene"
            )
        if any(p.cell is None for p in positions.values()):
            raise StateMachineError(
                "invalid_mechanical_state", "Effect dependencies need actual actor cells"
            )
        targets = tuple(
            sorted(
                a.entity_id
                for a in snapshot.character_states
                if positions[a.entity_id].scene_id == location["scene_id"]
                and (
                    a.entity_id == actor
                    if selector.scope == "trigger_actor"
                    else positions[a.entity_id].cell in selector.cells
                )
                and (selector.actor_kind == "all" or kinds[a.entity_id] == selector.actor_kind)
                and (not selector.living_only or a.is_alive)
                and (not selector.exclude_trigger_actor or a.entity_id != actor)
            )
        )
        if not targets:
            raise StateMachineError(
                "invalid_mechanism_targets", "Selector produced no eligible targets"
            )
        return MechanismEffectRequest(
            schema_version="mechanism-effect/1",
            operation_kind="rules.effect",
            session_id=row["session_id"],
            command_id=command_id,
            source=MechanismSource(
                kind="mechanism",
                object_id=oid,
                definition_ref=self.h.facts.object_definition(package, oid).definition_ref,
                scene_id=location["scene_id"],
                cell=location["cell"],
                activation_id=activation,
                consequence_id=consequence,
                source_event_seq=source_seq,
                package_digest=package.package_digest,
            ),
            effect=definition,
            selector=selector,
            target_ids=targets,
            state_snapshot=snapshot,
            world_dependencies=WorldDependencies(
                actor_poses=tuple(
                    ActorPose(actor_id=aid, scene_id=p.scene_id, cell=p.cell)
                    for aid, p in sorted(positions.items())
                ),
                object_contributions=tuple(
                    ObjectContribution(object_id=identity, spatial=projection)
                    for identity, projection in self.h.objects.spatial.scene_contributions(
                        row["session_id"], location["scene_id"], package
                    ).items()
                ),
            ),
            ruleset_binding=rules.session_binding(row),
            rng_context=rules._rng_context(row),
        )

    def apply(
        self, row, package, oid, actor, activation, consequence, source_seq, command_id, action
    ):
        request = self.request(
            row, package, oid, actor, activation, consequence, source_seq, command_id, action
        )
        try:
            value = self.port.evaluate(copy.deepcopy(request))
        except Exception as exc:
            raise StateMachineError(
                "rule_evaluation_failed", "Mechanism evaluator fault; retry original command"
            ) from exc
        result, changes = validate_mechanism_result(request, value)
        if result.status != "accepted":
            code = {
                "unsupported": "unsupported_mechanism_effect",
                "rejected": "mechanism_rule_rejected",
                "needs_choice": "mechanism_needs_choice",
            }[result.status]
            raise StateMachineError(code, "Mechanism consequence was not accepted")
        rules = self.h.objects.openable.rules
        current_row = self.h._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (row["session_id"],)
        ).fetchone()
        current = self.request(
            current_row,
            package,
            oid,
            actor,
            activation,
            consequence,
            source_seq,
            command_id,
            action,
        )
        if current != request:
            raise StateMachineError("version_conflict", "Mechanism effect dependencies changed")
        rules._apply_dnd_changes(row, changes)
        self.h.runtime.advance_rule_rng(
            row["session_id"], result.rng_transition, request.rng_context
        )
        # Audit is nested under the activation, never an extra sensory/LLM route.
        return result.model_dump(mode="json")
