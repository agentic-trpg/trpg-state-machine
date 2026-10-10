"""Authorized, filtered read-only adapter over real Engine attack availability."""

import copy
from dataclasses import asdict

from dnd5e_engine.evaluation_availability import (
    ActionAvailabilityRequest,
    ActionAvailabilityResult,
    query_action_availability,
)
from dnd5e_engine.evaluation_contracts import CombatIntentPayload, RuleError
from dnd5e_engine.evaluation_ruleset import RulesetBinding, RulesetBindingError
from pydantic import ValidationError

from .combat import process_id
from .errors import StateMachineError


class AvailabilityRuntime:
    def query_action_availability(self, session_id, actor_id, payload, package, *, context):
        """Return conditional status/reason only; no command key, RNG, receipt or write."""
        if type(payload) is not CombatIntentPayload or type(actor_id) is not str or not actor_id:
            raise StateMachineError("invalid_query", "Expected a typed intent and Actor identity")
        with self._transaction():
            row = self._session(session_id, package, context)
            self._authorize_command(row, actor_id, payload, context)
            snapshot = self._combat_snapshot(row)
            owner = self._conn.execute(
                "SELECT process_id FROM combats WHERE session_id=?", (session_id,)
            ).fetchone()[0]
        self._verify_dnd_binding(package)
        try:
            request = ActionAvailabilityRequest(
                schema_version="engine-availability/1",
                session_id=session_id,
                operation_kind="combat.intent",
                actor_id=actor_id,
                payload=copy.deepcopy(payload),
                state_snapshot=snapshot,
                ruleset_binding=RulesetBinding(**asdict(package.ruleset_binding)),
            )
            capability = self._dnd_capability(payload, actor_id, snapshot)
            if owner != process_id() and not snapshot.combat_state.ended:
                capability = "combat_unrestorable"
            if capability is not None:
                result = ActionAvailabilityResult(
                    schema_version=request.schema_version,
                    session_id=session_id,
                    actor_id=actor_id,
                    operation_kind=request.operation_kind,
                    snapshot_world_version=snapshot.world_version,
                    ruleset_binding=request.ruleset_binding,
                    status="unknown",
                    reason=RuleError(code=capability, reason="Capability is not supported"),
                )
            else:
                result = query_action_availability(request, loader=self._dnd_loader)
        except RulesetBindingError as exc:
            raise StateMachineError(
                "ruleset_binding_mismatch", "Availability evaluator binding differs"
            ) from exc
        except ValidationError as exc:
            raise StateMachineError("rule_schema_error", "Invalid availability schema") from exc
        except Exception as exc:
            raise StateMachineError("rule_query_failed", "Availability query failed") from exc
        # Free-text Engine preflight errors may mention hidden mechanical inputs.
        # Expose a bounded code and a generic status description to the authorized actor.
        return result.model_copy(
            update={
                "reason": RuleError(
                    code=result.reason.code,
                    reason={
                        "available": "Available now; execution revalidates permission and state.",
                        "unavailable": "Unavailable on this snapshot.",
                        "unknown": "This capability cannot currently be determined.",
                    }[result.status],
                )
            }
        )
