"""Result admission for the Engine's empty-delta generic check slice."""

import json

from dnd5e_engine.events import CheckRolled

from ._operations import SetSceneFlag
from .errors import StateMachineError


def world_effects_ready(runtime, row, command):
    """Check supported conditional world effects and their fact prerequisites."""
    effects = command.check_world_effects
    if effects is None:
        return True
    if command.actor_id != row["pc_actor_id"]:
        raise StateMachineError(
            "unsupported_operation", "Conditional effects require the selected PC"
        )
    ready = True
    for index, operations in enumerate(
        (effects.preconditions, effects.on_success, effects.on_failure)
    ):
        for operation in operations:
            runtime._world_operation_capability(row, operation)
            if operation.scene_id != row["current_scene_id"]:
                raise StateMachineError("invalid_target", "World effects require the current scene")
            if type(operation) is SetSceneFlag:
                flags = json.loads(
                    runtime._conn.execute(
                        "SELECT flags_json FROM scene_instances WHERE session_id=? AND scene_id=?",
                        (row["session_id"], operation.scene_id),
                    ).fetchone()[0]
                )
                if operation.flag_id not in flags:
                    raise StateMachineError("invalid_target", "Unknown declared scene flag")
                actual, expected = flags[operation.flag_id], operation.value
            else:
                obj = runtime._conn.execute(
                    "SELECT state FROM objects WHERE session_id=? AND scene_id=? AND object_id=?",
                    (row["session_id"], operation.scene_id, operation.object_id),
                ).fetchone()
                if obj is None:
                    raise StateMachineError("invalid_target", "Unknown scene object")
                actual, expected = obj[0], operation.state.value
            if index == 0 and actual != expected:
                ready = False
    return ready


def check_changes(request, result):
    """Validate the check result and return supported domain state changes."""
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
