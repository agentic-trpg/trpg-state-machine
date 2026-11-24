"""Process-local, uncommitted exploration action choices.

Candidates are never persisted or published as Session facts. A cold runtime
cannot resume one; its caller starts a new action from committed state.
"""

from dataclasses import dataclass

from dnd5e_engine.evaluation_contracts import RuleEvaluationRequest
from dnd5e_engine.evaluation_spell_saves import SpellSaveProgress

from .commands import CommandReceipt, TypedCommand
from .ongoing_effects import BardicInspirationGrant


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


@dataclass(slots=True)
class PendingSpellSaveChoice:
    """Hold RE-authored ordered saves and the original fixed cast request."""

    command: TypedCommand
    fingerprint: str
    request: RuleEvaluationRequest
    progress: SpellSaveProgress
    grant: BardicInspirationGrant
    receipt: CommandReceipt
