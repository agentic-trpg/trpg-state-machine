"""Process-local, uncommitted exploration action choices.

Candidates are never persisted or published as Session facts. A cold runtime
cannot resume one; its caller starts a new action from committed state.
"""

from dataclasses import dataclass

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
