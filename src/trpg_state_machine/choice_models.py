"""Public response payload and Host-facing details for a pending exploration spell."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ChoiceRespondOperationPayload:
    """Select an option for a pending operation as the actor in the command envelope."""

    pending_operation_id: str
    selected_option_id: Literal["accept", "decline"]


@dataclass(frozen=True, slots=True)
class PendingChoice:
    """Describe a saved prompt in its original command receipt."""

    pending_operation_id: str
    authorized_responder: str
    choice_kind: Literal["spell.target_willingness"]
    allowed_options: tuple[str, ...]
    expires_at_tick: int
