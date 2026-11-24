"""Process-local, uncommitted exploration action choices.

Candidates are never persisted or published as Session facts. A cold runtime
cannot resume one; its caller starts a new action from committed state.
"""

from dataclasses import dataclass

from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest, RuleEvaluationResult
from dnd5e_engine.evaluation_rng import RNGContext, RNGTransition
from dnd5e_engine.evaluation_spell_saves import SpellSaveProgress
from dnd5e_engine.events import SaveRolled

from .commands import CommandReceipt, TypedCommand
from .errors import StateMachineError
from .ongoing_effects import BardicInspirationGrant


@dataclass(frozen=True, slots=True)
class ActionChoiceReply:
    """Remember a provisional reply until the whole action commits."""

    command: TypedCommand
    fingerprint: str
    receipt: CommandReceipt


@dataclass(slots=True)
class PendingActionChoice:
    """Hold one frozen action and its already evaluated save prefix in live RAM."""

    command: TypedCommand
    fingerprint: str
    requests: tuple
    prepared: dict
    waiting_key: tuple[str, str]
    grant: BardicInspirationGrant
    receipt: CommandReceipt
    step_replies: tuple[ActionChoiceReply, ...] = ()


@dataclass(slots=True)
class PendingSpellSaveChoice:
    """Hold RE-authored ordered saves and the original fixed cast request."""

    command: TypedCommand
    fingerprint: str
    request: RuleEvaluationRequest
    progress: SpellSaveProgress
    grant: BardicInspirationGrant
    receipt: CommandReceipt


@dataclass(slots=True, kw_only=True)
class PendingPickSaveChoice(PendingActionChoice):
    """Keep the original admitted lock check alongside its triggered save."""

    check: tuple


def resolve_action_save_choice(
    request: RuleEvaluationRequest,
    result: RuleEvaluationResult,
    grant: BardicInspirationGrant,
    *,
    use: bool,
) -> tuple[RuleEvaluationRequest, RuleEvaluationResult, BardicInspirationGrant | None]:
    """Continue the fixed failed roll and combine both RNG transitions for adoption."""
    from dnd5e_engine.evaluation_saves import resolve_bardic_save_choice

    transition = result.rng_transition
    original = result.proposed_events[0] if result.proposed_events else None
    if transition is None or not isinstance(original, SaveRolled):
        raise StateMachineError(
            "invalid_rule_result", "Pending action is missing its original save"
        )
    after_original = RNGContext(
        stream_id=request.rng_context.stream_id,
        version=request.rng_context.version + int(transition.state_changed),
        state=transition.next_state,
    )
    final, bonus = resolve_bardic_save_choice(original, grant.die_sides, after_original, use=use)
    combined = RNGTransition(
        stream_id=request.rng_context.stream_id,
        input_version=request.rng_context.version,
        input_state=request.rng_context.state,
        next_state=bonus.next_state,
        state_changed=request.rng_context.state != bonus.next_state,
    )
    return (
        request,
        result.model_copy(update={"proposed_events": (final,), "rng_transition": combined}),
        grant if use else None,
    )
