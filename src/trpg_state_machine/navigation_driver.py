"""Bounded public-command driver; each slot is an independent durable command."""

import hashlib
from dataclasses import dataclass

from .commands import CommandReceipt, TypedCommand
from .errors import StateMachineError
from .navigation_models import AdvanceNavigation, NavigationActivity
from .package import canonical_json


@dataclass(frozen=True, slots=True)
class NavigationPumpResult:
    activity: NavigationActivity
    receipts: tuple[CommandReceipt, ...]
    stop_reason: str


def pump_navigation(
    submit,
    read,
    get_receipt,
    session_id,
    actor_id,
    activity_id,
    package,
    *,
    context,
    request_id,
    budget=32,
):
    """Narrow public callbacks, no StateMachine dependency and no transaction around the loop.

    Reuse a request_id and budget to reconcile interrupted slots. A new request_id
    authorizes another bounded batch, never implicitly resumes a blocked activity.
    """
    if type(budget) is not int or not 1 <= budget <= 64:
        raise StateMachineError("invalid_navigation_budget", "Expected 1..64 advance slots")
    if type(request_id) is not str or not request_id or len(request_id) > 128:
        raise StateMachineError("invalid_command", "Bounded pump request identity required")
    prefix = hashlib.sha256(
        canonical_json([session_id, actor_id, activity_id, request_id]).encode()
    ).hexdigest()
    receipts = []
    stop = "budget"
    for ordinal in range(budget):
        key = f"command.navigation.{prefix}.{ordinal}"
        receipt = get_receipt(session_id, key, package, context=context)
        if receipt is None:
            activity = read(session_id, actor_id, activity_id, package, context=context)
            if activity.status != "active":
                stop = activity.status
                break
            command = TypedCommand(
                session_id,
                key,
                actor_id,
                "activity.advance_navigation",
                AdvanceNavigation(activity_id, activity.activity_version),
                activity.world_version,
            )
            try:
                receipt = submit(command, package, context=context)
            except (OSError, StateMachineError) as exc:
                # A committed reply may have been lost, or another driver may have
                # filled this same deterministic slot. Query before retrying anything.
                if isinstance(exc, StateMachineError) and exc.code != "idempotency_conflict":
                    raise
                receipt = get_receipt(session_id, key, package, context=context)
                if receipt is None:
                    raise
        receipts.append(receipt)
        if receipt.status != "committed":
            stop = receipt.status
            break
    activity = read(session_id, actor_id, activity_id, package, context=context)
    return NavigationPumpResult(
        activity, tuple(receipts), activity.status if activity.status != "active" else stop
    )
