"""Persisted scheduling and bounded domain eligibility, independent of physical models."""

import hashlib
import json
from dataclasses import asdict, dataclass

from .errors import StateMachineError
from .npc_evaluation import NPCEvaluationTask, _text
from .package import canonical_json
from .perception import EventObservation, PerceivedSignal


@dataclass(frozen=True, slots=True)
class MemoryGateJudgment:
    work_id: str
    classification: str


@dataclass(frozen=True, slots=True)
class MemoryGateObservation:
    work_id: str
    observation: EventObservation
    accepted_game_tick: int
    expires_game_tick: int
    eligible_game_tick: int
    latest_start_game_tick: int


@dataclass(frozen=True, slots=True)
class MemoryGateInput:
    schema_version: str
    session_id: str
    npc_id: str
    request_id: str
    mode: str
    observations: tuple[MemoryGateObservation, ...]


@dataclass(frozen=True, slots=True)
class NPCWorkEligibility:
    work_id: str
    observation_id: str
    classification: str
    eligible_game_tick: int | None
    latest_start_game_tick: int | None
    retention_status: str
    reason: str
    occupied: bool


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
    def get_npc_memory_gate_input(
        self, session_id, package, *, context, request_id, observation_work_ids=None
    ):
        """Authorize classification over an exact persisted subset; no model invocation."""
        _text(request_id, 128)
        if observation_work_ids is not None and (
            type(observation_work_ids) is not tuple
            or not 1 <= len(observation_work_ids) <= 100
            or any(type(w) is not str for w in observation_work_ids)
            or len(set(observation_work_ids)) != len(observation_work_ids)
        ):
            raise StateMachineError("invalid_gate", "Expected a unique bounded Work scope")
        for wid in observation_work_ids or ():
            try:
                _text(wid, 128)
            except StateMachineError as exc:
                raise StateMachineError("invalid_gate", "Invalid Work identity") from exc
        with self._transaction(write=True):
            self._evaluation_actor(session_id, package, context)
            key = (session_id, context.observer_id, context.principal_id, request_id)
            saved = self._conn.execute(
                "SELECT * FROM npc_routing_requests WHERE session_id=? AND npc_id=? "
                "AND principal_id=? AND request_id=?",
                key,
            ).fetchone()
            if saved:
                if saved["classification"] != "memory_gate":
                    raise StateMachineError("idempotency_conflict", "Gate request key already used")
                data = json.loads(saved["response_json"])
                if data["requested_scope"] != (
                    list(observation_work_ids) if observation_work_ids is not None else None
                ):
                    raise StateMachineError("idempotency_conflict", "Gate scope changed")
                current = {
                    r["work_id"]: r for r in self._observation_rows(session_id, context.observer_id)
                }
                if any(
                    item["work_id"] not in current
                    or self._retention(current[item["work_id"]]).retention_status != "recent"
                    for item in data["input"]["observations"]
                ):
                    raise StateMachineError("stale_gate", "Expired input needs Work recovery")
                return self._memory_gate_input(data["input"])
            rows = self._observation_rows(session_id, context.observer_id)
            available = {
                r["work_id"]: r
                for r in rows
                if r["status"] == "pending"
                and self._retention(r).retention_status == "recent"
                and not self._work_occupied(r["work_id"])
            }
            scope = (
                observation_work_ids if observation_work_ids is not None else tuple(available)[:100]
            )
            if not scope or any(w not in available for w in scope):
                raise StateMachineError("invalid_gate", "Work is unavailable, expired or occupied")
            selected = [r for r in rows if r["work_id"] in scope]
            data = MemoryGateInput(
                "npc-memory-gate/2",
                session_id,
                context.observer_id,
                request_id,
                self._mode(session_id, context.observer_id),
                tuple(
                    MemoryGateObservation(
                        r["work_id"],
                        self._observation(r),
                        r["accepted_game_tick"],
                        r["expires_game_tick"],
                        r["eligible_game_tick"],
                        r["latest_start_game_tick"],
                    )
                    for r in selected
                ),
            )
            raw = canonical_json(
                {
                    "input": asdict(data),
                    "requested_scope": observation_work_ids,
                    "fingerprint": None,
                    "result": None,
                }
            )
            if len(raw) > 131072:
                raise StateMachineError("invalid_gate", "Gate batch exceeds local size limit")
            self._conn.execute(
                "INSERT INTO npc_routing_requests VALUES (?,?,?,?,?,?)", (*key, "memory_gate", raw)
            )
            return data

    def _memory_gate_input(self, data):
        items = []
        for item in data["observations"]:
            observation = item["observation"]
            observation["perceived_signals"] = tuple(
                PerceivedSignal(**s) for s in observation["perceived_signals"]
            )
            item["observation"] = EventObservation(**observation)
            items.append(MemoryGateObservation(**item))
        data["observations"] = tuple(items)
        return MemoryGateInput(**data)

    def _work_occupied(self, work_id):
        return (
            self._conn.execute(
                "SELECT 1 FROM npc_evaluation_observations o JOIN npc_evaluation_tasks t "
                "USING(evaluation_id) WHERE o.work_id=? AND t.status='running'",
                (work_id,),
            ).fetchone()
            is not None
        )

    def _eligible_npc_work(self, sid, npc):
        occupied = (
            self._conn.execute(
                "SELECT 1 FROM npc_evaluation_tasks WHERE session_id=? AND npc_id=? "
                "AND status='running'",
                (sid, npc),
            ).fetchone()
            is not None
        )
        result = []
        for row in self._observation_rows(sid, npc):
            if row["status"] != "pending":
                continue
            retention = self._retention(row).retention_status
            due = row["eligible_game_tick"]
            if due is not None and row["game_tick"] < due:
                continue
            classification = row["gate_classification"] or "unclassified"
            reason = (
                "recovery"
                if retention != "recent"
                else "urgent"
                if classification == "urgent"
                else "maximum_wait"
                if row["game_tick"] >= row["latest_start_game_tick"]
                else "periodic"
            )
            result.append(
                NPCWorkEligibility(
                    row["work_id"],
                    row["observation_id"],
                    classification,
                    due,
                    row["latest_start_game_tick"],
                    retention,
                    reason,
                    occupied,
                )
            )
        return tuple(
            sorted(
                result,
                key=lambda w: (
                    w.classification != "urgent",
                    w.latest_start_game_tick or -1,
                    w.eligible_game_tick or -1,
                    w.work_id,
                ),
            )
        )

    def get_npc_evaluation_eligibility(self, session_id, package, *, context):
        """Domain eligibility only; Host discovers tasks, SM has no polling worker."""
        with self._transaction():
            npc = self._npc_authorized(session_id, package, context)
            return self._eligible_npc_work(session_id, npc)

    def _adopt_memory_gate(self, sid, package, context, request_id, judgments):
        self._evaluation_actor(sid, package, context)
        key = (sid, context.observer_id, context.principal_id, request_id)
        saved = self._conn.execute(
            "SELECT * FROM npc_routing_requests WHERE session_id=? AND npc_id=? "
            "AND principal_id=? AND request_id=?",
            key,
        ).fetchone()
        if saved is None or saved["classification"] != "memory_gate":
            raise StateMachineError("invalid_gate", "No authorized Memory Gate scope")
        data = json.loads(saved["response_json"])
        scope = {r["work_id"] for r in data["input"]["observations"]}
        if type(judgments) is not tuple or len(judgments) != len(scope):
            raise StateMachineError("invalid_gate", "Every requested Work needs one judgment")
        answers = {}
        for judgment in judgments:
            if (
                type(judgment) is not MemoryGateJudgment
                or type(judgment.work_id) is not str
                or judgment.work_id not in scope
                or judgment.work_id in answers
                or type(judgment.classification) is not str
                or judgment.classification not in ("urgent", "deferred", "irrelevant", "uncertain")
            ):
                raise StateMachineError("invalid_gate", "Invalid, duplicate or unknown Gate item")
            answers[judgment.work_id] = judgment.classification
        fingerprint = hashlib.sha256(canonical_json(answers).encode()).hexdigest()
        if data["fingerprint"]:
            if data["fingerprint"] != fingerprint:
                raise StateMachineError("idempotency_conflict", "Gate judgments changed")
            return NPCObservationRoute(
                data["result"]["status"], None, tuple(data["result"]["observation_work_ids"])
            )
        rows = {r["work_id"]: r for r in self._observation_rows(sid, context.observer_id)}
        for wid in scope:
            row = rows.get(wid)
            if (
                row is None
                or row["status"] != "pending"
                or self._work_occupied(wid)
                or self._retention(row).retention_status != "recent"
            ):
                raise StateMachineError("stale_gate", "Work is no longer classifiable")
        for wid, classification in answers.items():
            row = rows[wid]
            self._conn.execute(
                "UPDATE npc_observation_work SET gate_classification=?,eligible_game_tick=? "
                "WHERE work_id=?",
                (
                    classification,
                    min(row["eligible_game_tick"], row["game_tick"])
                    if classification == "urgent"
                    else row["eligible_game_tick"],
                    wid,
                ),
            )
            if classification == "irrelevant":
                self._conn.execute(
                    "INSERT INTO npc_gate_decisions VALUES (?,?,'npc-gate/1','no_op')",
                    (wid, context.principal_id),
                )
                self._conn.execute(
                    "UPDATE npc_observation_work SET status='completed',"
                    "disposition='no_op' WHERE work_id=?",
                    (wid,),
                )
        self._refresh_npc_completion(sid)
        result = NPCObservationRoute(
            "classified", None, tuple(r["work_id"] for r in data["input"]["observations"])
        )
        data.update(fingerprint=fingerprint, judgments=answers, result=asdict(result))
        self._conn.execute(
            "UPDATE npc_routing_requests SET response_json=? WHERE session_id=? "
            "AND npc_id=? AND principal_id=? AND request_id=?",
            (canonical_json(data), *key),
        )
        return result

    def _mode(self, sid, npc):
        row = self._conn.execute(
            "SELECT mode FROM npc_modes WHERE session_id=? AND npc_id=?", (sid, npc)
        ).fetchone()
        return row[0] if row else "inactive"

    def _route_eligible_observations(self, sid, package, context, request_id):
        actor = self._evaluation_actor(sid, package, context)
        due = self._eligible_npc_work(sid, context.observer_id)
        if due and due[0].occupied:
            return NPCObservationRoute("queued", None, tuple(w.work_id for w in due))
        if not due:
            return NPCObservationRoute("no_eligible_work", None, ())
        selected = {w.work_id for w in due[:100]}
        # Priority chooses the bounded set; original committed order determines
        # Task's immutable Work ordering, completion and evidence provenance.
        works = [
            r for r in self._observation_rows(sid, context.observer_id) if r["work_id"] in selected
        ]
        interactive = self._mode(sid, context.observer_id) == "interactive"
        task = self._create_evaluation(
            sid,
            package,
            context=context,
            request_id=request_id,
            mode="interactive" if interactive else "background",
            reason="observation_eligibility",
            expected_npc_state_version=actor["npc_state_version"],
            source_observation_ids=tuple(r["observation_id"] for r in works),
            gate_authorized=not interactive,
        )
        return NPCObservationRoute("evaluation", task, task.observation_work_ids)

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
            "JOIN event_observations o USING(observation_id) JOIN npc_perception_cursors c "
            "ON c.session_id=w.session_id AND c.npc_id=w.npc_id "
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
        self,
        session_id,
        package,
        *,
        context,
        request_id,
        classification="uncertain",
        judgments=None,
    ):
        _text(request_id, 128)
        if judgments is not None:
            if classification != "uncertain":
                raise StateMachineError("invalid_gate", "Cannot mix legacy and Memory Gate inputs")
            with self._transaction(write=True):
                return self._adopt_memory_gate(session_id, package, context, request_id, judgments)
        if type(classification) is not str or classification not in (
            "safe_retention",
            "no_op",
            "background",
            "uncertain",
            "eligible",
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
            "eligible",
        ):
            raise StateMachineError("invalid_gate", "Unknown classification; evidence retained")
        actor = self._evaluation_actor(session_id, package, context)
        if classification == "eligible":
            return self._route_eligible_observations(session_id, package, context, request_id)
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
