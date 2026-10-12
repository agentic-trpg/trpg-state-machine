"""Private execution origins, constructed only after authoritative admission.

These are not command fields, credentials or caller-provided source labels.
"""

from dataclasses import dataclass
from enum import StrEnum

from .errors import StateMachineError


class _WorldEffectKind(StrEnum):
    PACKAGE = "package"
    CHECK = "check_consequence"
    SYNTHETIC = "synthetic_delta"


@dataclass(frozen=True, slots=True)
class _WorldEffectOrigin:
    kind: _WorldEffectKind
    reference_id: str


def _execution_data(actor_id, origin):
    if actor_id is not None and origin is None:
        return {"kind": "actor", "actor_id": actor_id}
    if (
        actor_id is None
        and type(origin) is _WorldEffectOrigin
        and type(origin.kind) is _WorldEffectKind
        and type(origin.reference_id) is str
        and 0 < len(origin.reference_id) <= 256
    ):
        return {"kind": origin.kind.value, "reference_id": origin.reference_id}
    raise StateMachineError("invalid_execution_origin", "An explicit admitted origin is required")
