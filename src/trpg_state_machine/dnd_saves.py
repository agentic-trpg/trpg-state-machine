"""Admit the Engine's empty-delta standalone saving throw result."""

from dnd5e_engine.events import SaveRolled

from .errors import StateMachineError


def save_changes(request, result):
    """Require one bound saving throw and its rolled or automatic RNG transition."""
    payload = request.payload
    if result.state_delta.operations or len(result.proposed_events) != 1:
        raise StateMachineError("invalid_rule_result", "Save requires empty Delta and one event")
    event = result.proposed_events[0]
    if type(event) is not SaveRolled or any(
        getattr(event, field) != getattr(payload, field) for field in ("target_id", "ability", "dc")
    ):
        raise StateMachineError(
            "invalid_rule_result", "Save event differs from adjudicated request"
        )
    detail = event.roll_detail
    invalid = (
        type(event.succeeded) is not bool
        or type(event.roll_total) is not int
        or type(event.modifier) is not int
        or detail is None
    )
    if not invalid and detail.status == "automatic_failure":
        invalid = bool(
            event.succeeded
            or event.roll_total != 0
            or event.modifier != 0
            or event.natural is not None
            or event.sources
            or detail.d20 is not None
            or detail.modifiers
            or result.rng_transition.state_changed
        )
    elif not invalid:
        d20 = detail.d20
        invalid = bool(
            detail.status != "rolled"
            or d20 is None
            or type(event.natural) is not int
            or not 1 <= event.natural <= 20
            or d20.origin != "rng"
            or len(d20.faces) != (1 if event.advantage == "normal" else 2)
            or any(not 1 <= face <= 20 for face in d20.faces)
            or not 0 <= d20.kept_index < len(d20.faces)
            or d20.faces[d20.kept_index] != event.natural
            or not result.rng_transition.state_changed
        )
    if invalid:
        raise StateMachineError("invalid_rule_result", "Incomplete save outcome/RNG")
    return ()
