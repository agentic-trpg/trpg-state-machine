"""Persisted scheduling and bounded domain eligibility, independent of physical models."""

import hashlib
import json
from dataclasses import asdict, dataclass

from .errors import StateMachineError
from .npc_evaluation import NPCEvaluationTask, _text
from .package import canonical_json


@dataclass(frozen=True, slots=True)
class NPCModeChange:
    requested_mode: str
    task: NPCEvaluationTask | None


@dataclass(frozen=True, slots=True)
class NPCObservationRoute:
    status: str
    task: NPCEvaluationTask | None
    observation_work_ids: tuple[str, ...]


class NPCModeRuntime:
    def _mode(self, sid, npc):
        row = self._conn.execute(
            "SELECT mode FROM npc_modes WHERE session_id=? AND npc_id=?", (sid, npc)
        ).fetchone()
        return row[0] if row else "inactive"

    def _set_mode(self, sid, npc, mode):
        self._conn.execute(
            "INSERT INTO npc_modes VALUES (?,?,?) ON CONFLICT(session_id,npc_id) "
            "DO UPDATE SET mode=excluded.mode",
            (sid, npc, mode),
        )

    def _evaluation_mode_check(self, sid, npc, mode, gate_authorized):
        if mode == "background":
            if not gate_authorized or self._mode(sid, npc) != "inactive":
                raise StateMachineError(
                    "npc_mode_conflict", "Background requires Inactive Gate authorization"
                )
        else:
            self._set_mode(sid, npc, "interactive")

    def get_npc_mode(self, session_id, package, *, context):
        with self._transaction():
            npc = self._npc_authorized(session_id, package, context)
            return self._mode(session_id, npc)

    def _pending_npc_observations(self, sid, npc):
        return self._conn.execute(
            "SELECT w.work_id,w.observation_id FROM npc_observation_work w "
            "JOIN event_observations o USING(observation_id) JOIN projection_cursors c "
            "ON c.session_id=w.session_id AND c.consumer='perception' "
            "WHERE w.session_id=? AND w.npc_id=? AND w.status='pending' "
            "AND o.event_seq<=c.event_seq "
            "AND NOT EXISTS (SELECT 1 FROM npc_evaluation_observations e "
            "JOIN npc_evaluation_tasks t USING(evaluation_id) "
            "WHERE e.work_id=w.work_id AND t.status='completed') "
            "ORDER BY o.event_seq,w.observation_id LIMIT 100",
            (sid, npc),
        ).fetchall()

    def _switch_mode(self, sid, package, context, request_id, mode, player_input, background):
        actor = self._evaluation_actor(sid, package, context)
        _text(request_id, 128)
        if type(background) is not bool:
            raise StateMachineError("invalid_evaluation", "Background routing flag must be Boolean")
        if player_input is not None:
            _text(player_input)
        fingerprint = hashlib.sha256(
            canonical_json([mode, player_input, background]).encode()
        ).hexdigest()
        old = self._conn.execute(
            "SELECT * FROM npc_mode_requests WHERE session_id=? AND npc_id=? "
            "AND principal_id=? AND request_id=?",
            (sid, context.observer_id, context.principal_id, request_id),
        ).fetchone()
        if old:
            if old["fingerprint"] != fingerprint:
                raise StateMachineError("idempotency_conflict", "Mode request content changed")
            task = self._conn.execute(
                "SELECT * FROM npc_evaluation_tasks WHERE evaluation_id=?", (old["evaluation_id"],)
            ).fetchone()
            return NPCModeChange(old["requested_mode"], self._task(task) if task else None)
        if self._conn.execute(
            "SELECT 1 FROM npc_evaluation_tasks WHERE session_id=? AND npc_id=? "
            "AND principal_id=? AND request_id=?",
            (sid, context.observer_id, context.principal_id, request_id),
        ).fetchone():
            raise StateMachineError(
                "idempotency_conflict", "Mode request key already used by a Task"
            )
        self._conn.execute(
            "UPDATE npc_evaluation_tasks SET status='cancelled',last_error='mode_switch' "
            "WHERE session_id=? AND npc_id=? AND status='running'",
            (sid, context.observer_id),
        )
        self._set_mode(sid, context.observer_id, mode)
        works = self._pending_npc_observations(sid, context.observer_id)
        task = None
        if mode == "interactive" or background and works:
            task = self._create_evaluation(
                sid,
                package,
                context=context,
                request_id=request_id,
                mode="interactive" if mode == "interactive" else "background",
                reason="mode_switch",
                expected_npc_state_version=actor["npc_state_version"],
                source_observation_ids=tuple(w["observation_id"] for w in works),
                player_input=player_input,
                gate_authorized=background,
            )
        self._conn.execute(
            "INSERT INTO npc_mode_requests VALUES (?,?,?,?,?,?,?)",
            (
                sid,
                context.observer_id,
                context.principal_id,
                request_id,
                fingerprint,
                mode,
                task.evaluation_id if task else None,
            ),
        )
        return NPCModeChange(mode, task)

    def request_npc_interaction(self, session_id, player_input, package, *, context, request_id):
        _text(player_input)
        with self._transaction(write=True):
            return self._switch_mode(
                session_id, package, context, request_id, "interactive", player_input, False
            )

    def set_npc_inactive(self, session_id, package, *, context, request_id, evaluate_pending=False):
        with self._transaction(write=True):
            return self._switch_mode(
                session_id, package, context, request_id, "inactive", None, evaluate_pending
            )

    def route_npc_observations(
        self, session_id, package, *, context, request_id, classification="uncertain"
    ):
        _text(request_id, 128)
        if type(classification) is not str or classification not in (
            "safe_retention",
            "no_op",
            "background",
            "uncertain",
        ):
            raise StateMachineError("invalid_gate", "Unknown classification; evidence retained")
        with self._transaction(write=True):
            self._evaluation_actor(session_id, package, context)
            key = (session_id, context.observer_id, context.principal_id, request_id)
            saved = self._conn.execute(
                "SELECT * FROM npc_routing_requests WHERE session_id=? AND npc_id=? "
                "AND principal_id=? AND request_id=?",
                key,
            ).fetchone()
            if saved:
                if saved["classification"] != classification:
                    raise StateMachineError("idempotency_conflict", "Gate request changed")
                data = json.loads(saved["response_json"])
                if data["task"]:
                    task = data["task"]
                    for field in ("source_observation_ids", "observation_work_ids"):
                        task[field] = tuple(task[field])
                    data["task"] = NPCEvaluationTask(**task)
                data["observation_work_ids"] = tuple(data["observation_work_ids"])
                return NPCObservationRoute(**data)
            result = self._route_npc_observations(
                session_id,
                package,
                context=context,
                request_id=request_id,
                classification=classification,
            )
            self._conn.execute(
                "INSERT INTO npc_routing_requests VALUES (?,?,?,?,?,?)",
                (*key, classification, canonical_json(asdict(result))),
            )
            return result

    def _route_npc_observations(
        self, session_id, package, *, context, request_id, classification="uncertain"
    ):
        """Adopt a bounded Host classification; uncertainty keeps pending evidence.

        Interactive routing never invokes Gate. The input snapshot of an existing
        running task is immutable; subsequent observations stay queued in the Inbox.
        """
        _text(request_id, 128)
        if type(classification) is not str or classification not in (
            "safe_retention",
            "no_op",
            "background",
            "uncertain",
        ):
            raise StateMachineError("invalid_gate", "Unknown classification; evidence retained")
        actor = self._evaluation_actor(session_id, package, context)
        npc = context.observer_id
        # Request replay is independent of newly arrived Work and NPC version.
        prior = self._conn.execute(
            "SELECT * FROM npc_evaluation_tasks WHERE session_id=? AND npc_id=? "
            "AND principal_id=? AND request_id=?",
            (session_id, npc, context.principal_id, request_id),
        ).fetchone()
        if prior:
            expected_mode = (
                "interactive" if self._mode(session_id, npc) == "interactive" else "background"
            )
            if (
                prior["mode"] != expected_mode
                or prior["reason"] != "observation"
                or (expected_mode == "background" and classification != "background")
            ):
                raise StateMachineError(
                    "idempotency_conflict", "Observation routing request changed"
                )
            return NPCObservationRoute(
                "evaluation", self._task(prior), self._task(prior).observation_work_ids
            )
        works = self._pending_npc_observations(session_id, npc)
        work_ids = tuple(w["work_id"] for w in works)
        if self._conn.execute(
            "SELECT 1 FROM npc_evaluation_tasks WHERE session_id=? AND npc_id=? "
            "AND status='running'",
            (session_id, npc),
        ).fetchone():
            return NPCObservationRoute("queued", None, work_ids)
        if not works:
            return NPCObservationRoute("no_visible_work", None, ())
        interactive = self._mode(session_id, npc) == "interactive"
        if interactive or classification == "background":
            task = self._create_evaluation(
                session_id,
                package,
                context=context,
                request_id=request_id,
                mode="interactive" if interactive else "background",
                reason="observation",
                expected_npc_state_version=actor["npc_state_version"],
                source_observation_ids=tuple(w["observation_id"] for w in works),
                gate_authorized=not interactive,
            )
            return NPCObservationRoute("evaluation", task, work_ids)
        if classification == "uncertain":
            return NPCObservationRoute("deferred", None, work_ids)
        for wid in work_ids:
            self._conn.execute(
                "INSERT INTO npc_gate_decisions VALUES (?,?,'npc-gate/1',?)",
                (wid, context.principal_id, classification),
            )
            self._conn.execute(
                "UPDATE npc_observation_work SET status='completed',disposition=? WHERE work_id=?",
                (classification, wid),
            )
        self._refresh_npc_completion(session_id)
        return NPCObservationRoute(classification, None, work_ids)

    def report_npc_evaluation_failure(
        self, evaluation_id, reason, package, *, context, expected_attempts
    ):
        """OCC makes retransmitting one failure report consume at most one attempt."""
        if type(reason) is not str or reason not in ("timeout", "api_error", "context_budget"):
            raise StateMachineError("invalid_evaluation", "Unknown invocation failure")
        if type(expected_attempts) is not int or expected_attempts < 0:
            raise StateMachineError("invalid_evaluation", "Expected attempt counter required")
        with self._transaction(write=True):
            row = self._evaluation_row(evaluation_id, package, context)
            if row["status"] == "cancelled" or row["status"] == "completed":
                raise StateMachineError("evaluation_inactive", "Task eligibility cannot be revived")
            if row["attempts"] == expected_attempts + 1 and row["last_error"] == reason:
                return self._task(row)
            if row["status"] != "running" or row["attempts"] != expected_attempts:
                raise StateMachineError("evaluation_inactive", "Task attempt is no longer current")
            self._record_evaluation_failure(row, reason)
            return self._task(
                self._conn.execute(
                    "SELECT * FROM npc_evaluation_tasks WHERE evaluation_id=?", (evaluation_id,)
                ).fetchone()
            )
