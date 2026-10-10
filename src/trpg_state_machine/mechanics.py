"""Local noncombat evaluation protocol and an explicitly synthetic evaluator.

No D&D rules are copied here. Combat and continuation protocols are unsupported.
"""

import hashlib
from dataclasses import asdict, dataclass

from ._operations import SetSceneFlag
from .models import ActorState, CharacterState, RNGState, SceneInstance
from .package import SYNTHETIC_EVALUATION_RULESET, RulesetBinding, canonical_json


@dataclass(frozen=True, slots=True)
class RuleOperation:
    operation_kind: str
    target_actor_id: str
    resource_id: str
    scene_id: str
    flag_id: str
    difficulty: int | None
    domain: str = "noncombat"


@dataclass(frozen=True, slots=True)
class NonCombatSnapshot:
    session_id: str
    world_version: int
    current_scene_id: str
    world_flags: dict[str, bool]
    scenes: dict[str, SceneInstance]
    actors: dict[str, ActorState]
    characters: tuple[CharacterState, ...]


def snapshot_of(session):
    return NonCombatSnapshot(
        session.session_id,
        session.world_version,
        session.current_scene_id,
        session.world.flags,
        session.scenes,
        session.actors,
        (session.character,),
    )


def snapshot_digest(snapshot):
    return hashlib.sha256(canonical_json(asdict(snapshot)).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RuleEvaluationRequest:
    request_id: str
    session_id: str
    command_id: str
    actor_id: str
    input_world_version: int
    ruleset_binding: RulesetBinding
    package_digest: str
    operation: RuleOperation
    snapshot: NonCombatSnapshot
    rng: RNGState
    read_set_digest: str
    schema_version: str = "synthetic-rule-request/1"


@dataclass(frozen=True, slots=True)
class HPChange:
    actor_id: str
    before: int
    after: int


@dataclass(frozen=True, slots=True)
class ResourceSpend:
    actor_id: str
    resource_id: str
    before: int
    amount: int


@dataclass(frozen=True, slots=True)
class StateDelta:
    operations: tuple[HPChange | ResourceSpend | SetSceneFlag, ...]


@dataclass(frozen=True, slots=True)
class RNGTransition:
    before: RNGState
    after: RNGState
    draws: int


@dataclass(frozen=True, slots=True)
class ProposedEvent:
    event_type: str
    payload: dict


@dataclass(frozen=True, slots=True)
class RuleError:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RuleEvaluationResult:
    request_id: str
    session_id: str
    command_id: str
    input_world_version: int
    ruleset_binding: RulesetBinding
    read_set_digest: str
    status: str
    delta: StateDelta | None
    proposed_events: tuple[ProposedEvent, ...]
    rng_transition: RNGTransition | None
    choice: dict | None
    error: RuleError | None
    schema_version: str = "synthetic-rule-result/1"


class SyntheticEvaluator:
    """Costs one declared token on success OR mechanical failure; never real rules."""

    ruleset_binding = SYNTHETIC_EVALUATION_RULESET

    def evaluate(self, request: RuleEvaluationRequest) -> RuleEvaluationResult:
        op = request.operation
        status, error, choice = "accepted", None, None
        delta, transition, events = None, None, ()
        pc = request.snapshot.characters[0]
        if (
            request.ruleset_binding != self.ruleset_binding
            or op.domain != "noncombat"
            or op.operation_kind != "synthetic_attempt"
        ):
            status, error = "unsupported", "unsupported_rule_operation"
        elif (
            op.target_actor_id != pc.actor_id
            or op.scene_id != request.snapshot.current_scene_id
            or op.flag_id not in request.snapshot.scenes[op.scene_id].local_flags
            or pc.resources.get(op.resource_id, 0) < 1
        ):
            status, error = "rejected", "mechanical_precondition"
        elif op.difficulty is None:
            status, choice = "needs_choice", {"difficulty_options": [1, 2, 3, 4, 5, 6]}
        else:
            # State + stream identity determine every draw; there is no global RNG.
            rng = request.rng
            value = (
                int.from_bytes(
                    hashlib.sha256(f"{rng.state_id}:{rng.position}".encode()).digest()[:8], "big"
                )
                % 6
                + 1
            )
            success = value >= op.difficulty
            next_rng = RNGState(
                rng.state_id,
                rng.algorithm,
                rng.encoding,
                rng.position + 1,
                rng.rng_version + 1,
                canonical_json({"counter": rng.position + 1}),
            )
            transition = RNGTransition(rng, next_rng, 1)
            delta = StateDelta(
                (
                    ResourceSpend(pc.actor_id, op.resource_id, pc.resources[op.resource_id], 1),
                    HPChange(
                        pc.actor_id, pc.current_hp, min(pc.max_hp, pc.current_hp + int(success))
                    ),
                    SetSceneFlag(op.scene_id, op.flag_id, success),
                )
            )
            events = (
                ProposedEvent(
                    "rule.synthetic_attempt",
                    {"success": success, "roll": value, "scene_id": op.scene_id},
                ),
            )
        return RuleEvaluationResult(
            request.request_id,
            request.session_id,
            request.command_id,
            request.input_world_version,
            request.ruleset_binding,
            request.read_set_digest,
            status,
            delta,
            events,
            transition,
            choice,
            RuleError(error, "Synthetic evaluation declined") if error else None,
        )
