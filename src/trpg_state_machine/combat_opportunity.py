"""Typed, reactor-owned decision for one NPC mover's opportunity window."""

from dataclasses import dataclass

from .errors import StateMachineError


@dataclass(frozen=True, slots=True)
class CombatOpportunity:
    """Let the Session PC accept or decline an OA against the current NPC mover."""

    mover_id: str
    accept: bool


def validate_opportunity(payload):
    """Require a bounded target identity and an explicit boolean decision."""
    if (
        type(payload.mover_id) is not str
        or not payload.mover_id.strip()
        or len(payload.mover_id) > 256
        or type(payload.accept) is not bool
    ):
        raise StateMachineError("invalid_operation", "Expected one NPC mover and an OA decision")
