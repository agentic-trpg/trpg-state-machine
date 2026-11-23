"""Public command payloads and Host-facing details for pending choices."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ChoiceRespondOperationPayload:
    """Select an option for a pending operation as the actor in the command envelope."""

    pending_operation_id: str
    selected_option_id: Literal["accept", "decline"]


@dataclass(frozen=True, slots=True)
class ChoiceFinalizeSpellOperationPayload:
    """Confirm the original spell against a subset of its willing targets."""

    pending_operation_id: str
    target_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PendingChoice:
    """Describe a saved prompt in its original command receipt."""

    pending_operation_id: str
    authorized_responder: str
    choice_kind: Literal[
        "spell.target_willingness",
        "spell.targets_willingness",
        "combat.shield_reaction",
        "rules.bardic_inspiration",
    ]
    allowed_options: tuple[str, ...]
    expires_at_tick: int | None
