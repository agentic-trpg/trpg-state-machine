"""Evaluate detached inputs, then validate and commit to the sole SQLite authority."""

import json
from copy import deepcopy
from dataclasses import asdict

from ._operations import SetSceneFlag
from .commands import CommandReceipt
from .errors import StateMachineError
from .mechanics import (
    HPChange,
    ProposedEvent,
    ResourceSpend,
    RNGTransition,
    RuleError,
    RuleEvaluationRequest,
    RuleEvaluationResult,
    StateDelta,
    snapshot_digest,
    snapshot_of,
)
from .models import RNGState
from .package import SYNTHETIC_EVALUATION_RULESET, canonical_json


def validate_result(request, result):
    def invalid():
        raise StateMachineError("invalid_rule_result", "Evaluator returned an invalid envelope")

    if type(result) is not RuleEvaluationResult:
        invalid()
    if result.ruleset_binding != request.ruleset_binding:
        raise StateMachineError(
            "ruleset_binding_mismatch", "Evaluator result does not match the Session binding"
        )
    if type(result.input_world_version) is not int:
        invalid()
    if result.schema_version != "synthetic-rule-result/1" or (
        result.request_id,
        result.session_id,
        result.command_id,
        result.input_world_version,
        result.ruleset_binding,
        result.read_set_digest,
    ) != (
        request.request_id,
        request.session_id,
        request.command_id,
        request.input_world_version,
        request.ruleset_binding,
        request.read_set_digest,
    ):
        invalid()
    if result.status == "accepted":
        if (
            type(result.delta) is not StateDelta
            or type(result.delta.operations) is not tuple
            or type(result.rng_transition) is not RNGTransition
            or result.choice is not None
            or result.error is not None
            or type(result.proposed_events) is not tuple
        ):
            invalid()
        transition = result.rng_transition
        before, after = transition.before, transition.after
        if (
            type(before) is not RNGState
            or type(after) is not RNGState
            or before != request.rng
            or type(transition.draws) is not int
            or transition.draws not in (0, 1)
            or after.state_id != before.state_id
            or after.algorithm != before.algorithm
            or after.encoding != before.encoding
            or type(after.position) is not int
            or type(after.rng_version) is not int
            or after.position != before.position + transition.draws
            or after.rng_version != before.rng_version + int(transition.draws > 0)
            or after.state_json != canonical_json({"counter": after.position})
        ):
            invalid()
        if len(result.delta.operations) > 8 or len(result.proposed_events) > 8:
            invalid()
        for event in result.proposed_events:
            if (
                type(event) is not ProposedEvent
                or event.event_type != "rule.synthetic_attempt"
                or type(event.payload) is not dict
                or set(event.payload) != {"success", "roll", "scene_id"}
                or type(event.payload["success"]) is not bool
                or type(event.payload["roll"]) is not int
                or not 1 <= event.payload["roll"] <= 6
                or event.payload["scene_id"] != request.operation.scene_id
            ):
                invalid()
    elif result.status in ("rejected", "unsupported", "needs_choice"):
        if (
            result.delta is not None
            or result.rng_transition is not None
            or result.proposed_events != ()
        ):
            invalid()
        if result.status == "needs_choice":
            if (
                result.choice != {"difficulty_options": [1, 2, 3, 4, 5, 6]}
                or result.error is not None
            ):
                invalid()
        elif (
            result.choice is not None
            or type(result.error) is not RuleError
            or type(result.error.code) is not str
            or not 1 <= len(result.error.code) <= 128
            or type(result.error.message) is not str
            or not 1 <= len(result.error.message) <= 4096
        ):
            invalid()
    else:
        invalid()


class MechanicalRuntime:
    def _submit_mechanical(self, command, package, context, fingerprint):
        # Read/replay before evaluation prevents reroll after a committed lost response.
        with self._transaction():
            row = self._session(command.session_id, package, context)
            self._authorize_command(row, command.actor_id, command.payload, context)
            replay = self._command_replay(command, fingerprint)
            if replay is not None:
                return replay
            snapshot = self._snapshot(row, package)
        detached = snapshot_of(snapshot)
        request = RuleEvaluationRequest(
            f"evaluation.{command.session_id}.{command.command_id}",
            command.session_id,
            command.command_id,
            command.actor_id,
            snapshot.world_version,
            package.ruleset_binding,
            package.package_digest,
            command.payload,
            detached,
            snapshot.rng,
            snapshot_digest(detached),
        )
        result = None
        unsupported_choice = (
            command.payload.domain == "noncombat"
            and command.payload.operation_kind == "synthetic_attempt"
            and command.payload.difficulty is None
        )
        if (
            snapshot.world_version == command.expected_world_version
            and package.ruleset_binding == SYNTHETIC_EVALUATION_RULESET
        ):
            if getattr(self._evaluator, "ruleset_binding", None) != package.ruleset_binding:
                raise StateMachineError(
                    "ruleset_binding_mismatch", "Configured Evaluator does not match the Session"
                )
            if not unsupported_choice:
                try:
                    result = deepcopy(self._evaluator.evaluate(deepcopy(request)))
                except Exception as exc:
                    raise StateMachineError(
                        "rule_evaluation_failed", "Evaluation may be retried"
                    ) from exc
                validate_result(request, result)
                if result.status == "needs_choice":
                    raise StateMachineError(
                        "invalid_rule_result", "Unexpected choice after capability admission"
                    )
        with self._transaction(write=True):
            row = self._session(command.session_id, package, context)
            self._authorize_command(row, command.actor_id, command.payload, context)
            replay = self._command_replay(command, fingerprint)
            if replay is not None:
                return replay
            current = self._snapshot(row, package)
            status, error = "committed", None
            version, seq = row["world_version"], row["event_seq"]
            if (
                version != command.expected_world_version
                or version != snapshot.world_version
                or snapshot_digest(snapshot_of(current)) != request.read_set_digest
                or current.rng != request.rng
            ):
                status, error = "conflict", "version_conflict"
            elif unsupported_choice and package.ruleset_binding == SYNTHETIC_EVALUATION_RULESET:
                status, error = "rejected", "pending_choice_unsupported"
            elif result is None:
                status, error = "rejected", "unsupported_ruleset"
            elif result.status != "accepted":
                status = "rejected"
                error = result.error.code
            else:
                # Revalidate the exact result and closed Delta after reacquiring authority.
                validate_result(request, result)
                self._validate_delta(request, result.delta)
                self._conn.execute("SAVEPOINT mechanical_effects")
                try:
                    events, changed = self._apply_delta(row, package, result.delta)
                    events.extend(
                        self._capture_event(
                            command.session_id,
                            package,
                            event.event_type,
                            event.payload,
                            phase="synthetic.intermediate_phase_unavailable",
                            unresolved=True,
                        )
                        for event in result.proposed_events
                    )
                    events = self._expand_events(row, package, events, command.command_id)
                    if not changed:
                        # Event-only mechanical version policy remains OPEN.
                        raise StateMachineError(
                            "unsupported_delta", "An actual mechanical change is required"
                        )
                    version += 1
                    self._conn.execute(
                        "UPDATE sessions SET world_version=? WHERE session_id=?",
                        (version, command.session_id),
                    )
                    rng = result.rng_transition.after
                    self._conn.execute(
                        "UPDATE rng_states SET position=?,rng_version=?,state_json=? "
                        "WHERE session_id=?",
                        (rng.position, rng.rng_version, rng.state_json, command.session_id),
                    )
                    self._append_events(
                        command.session_id,
                        version,
                        events,
                        source_command_id=command.command_id,
                        package=package,
                    )
                except StateMachineError as exc:
                    # Only this deterministic domain limit is a durable rejection.
                    # Invalid results/configuration/storage failures remain retryable errors.
                    if exc.code != "event_cascade_limit":
                        raise
                    self._conn.execute("ROLLBACK TO mechanical_effects")
                    version = row["world_version"]
                    status, error = "rejected", exc.code
                finally:
                    self._conn.execute("RELEASE mechanical_effects")
            end = self._conn.execute(
                "SELECT event_seq FROM sessions WHERE session_id=?", (command.session_id,)
            ).fetchone()[0]
            receipt = CommandReceipt(
                command.session_id,
                command.command_id,
                command.actor_id,
                status,
                version,
                tuple(range(seq + 1, end + 1)),
                error,
            )
            self._conn.execute(
                "INSERT INTO command_receipts VALUES (?, ?, ?, ?, ?)",
                (
                    command.session_id,
                    command.command_id,
                    context.principal_id,
                    fingerprint,
                    canonical_json(asdict(receipt)),
                ),
            )
        return receipt

    def _validate_delta(self, request, delta):
        pc = request.snapshot.characters[0]
        op = request.operation
        seen = set()
        for change in delta.operations:
            valid = False
            if type(change) is HPChange:
                key = ("hp", change.actor_id)
                valid = (
                    change.actor_id == request.actor_id == op.target_actor_id
                    and type(change.before) is int
                    and change.before == pc.current_hp
                    and type(change.after) is int
                    and 0 <= change.after <= pc.max_hp
                )
            elif type(change) is ResourceSpend:
                key = ("resource", change.actor_id, change.resource_id)
                valid = (
                    change.actor_id == request.actor_id == op.target_actor_id
                    and change.resource_id == op.resource_id
                    and type(change.before) is int
                    and change.before == pc.resources.get(change.resource_id)
                    and type(change.amount) is int
                    and 0 < change.amount <= change.before
                )
            elif type(change) is SetSceneFlag:
                key = ("flag", change.scene_id, change.flag_id)
                valid = (
                    change.scene_id == request.snapshot.current_scene_id == op.scene_id
                    and change.flag_id == op.flag_id
                    and type(change.value) is bool
                    and change.flag_id in request.snapshot.scenes[change.scene_id].local_flags
                )
            else:
                raise StateMachineError("unsupported_delta", "Delta operation is not allowlisted")
            if not valid or key in seen:
                raise StateMachineError(
                    "invalid_delta", "Delta scope/precondition/invariant violation"
                )
            seen.add(key)

    def _apply_delta(self, row, package, delta):
        events, changed = [], False
        for change in delta.operations:
            if type(change) is HPChange:
                self._conn.execute(
                    "UPDATE characters SET current_hp=? WHERE session_id=? AND actor_id=?",
                    (change.after, row["session_id"], change.actor_id),
                )
                changed |= change.before != change.after
            elif type(change) is ResourceSpend:
                resources = json.loads(
                    self._conn.execute(
                        "SELECT resources_json FROM characters WHERE session_id=? AND actor_id=?",
                        (row["session_id"], change.actor_id),
                    ).fetchone()[0]
                )
                resources[change.resource_id] -= change.amount
                self._conn.execute(
                    "UPDATE characters SET resources_json=? WHERE session_id=? AND actor_id=?",
                    (canonical_json(resources), row["session_id"], change.actor_id),
                )
                changed = True
            else:
                consequences = self._operate(row, package, change)
                changed |= bool(consequences)
                events.extend(consequences)
        return events, changed
