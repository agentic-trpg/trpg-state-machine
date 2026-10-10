"""Result admission for the Engine's empty-delta generic check slice."""

from dnd5e_engine.events import CheckRolled

from .errors import StateMachineError


def check_changes(request, result):
    payload = request.payload
    if (
        payload.tool is not None
        or payload.redeem_granted_die is not None
        or payload.context != "generic"
        or payload.required_sense != "none"
        or set(payload.advantage + payload.disadvantage) - {"flag"}
    ):
        raise StateMachineError("invalid_rule_result", "Accepted check exceeds admitted slice")
    if result.state_delta.operations or len(result.proposed_events) != 1:
        raise StateMachineError("invalid_rule_result", "Check requires empty Delta and one event")
    event = result.proposed_events[0]
    if type(event) is not CheckRolled or any(
        getattr(event, field) != getattr(payload, field)
        for field in (
            "actor_id",
            "ability",
            "skill",
            "dc",
            "target_id",
            "context",
            "required_sense",
            "social_interaction",
        )
    ):
        raise StateMachineError(
            "invalid_rule_result", "Check event differs from adjudicated request"
        )
    if (
        type(event.succeeded) is not bool
        or type(event.roll_total) is not int
        or type(event.modifier) is not int
        or type(event.natural) is not int
        or not 1 <= event.natural <= 20
        or not result.rng_transition.state_changed
    ):
        raise StateMachineError("invalid_rule_result", "Incomplete generic check outcome/RNG")
    # The Engine owns modifier, advantage, total and success; never recompute them.
    return ()
