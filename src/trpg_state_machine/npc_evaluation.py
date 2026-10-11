"""Local npc-evaluation/1 durable authorization; no model runtime or ACK."""

import hashlib
from dataclasses import dataclass
from uuid import uuid4

from .errors import StateMachineError
from .npc_state import NPCView
from .observation_lifecycle import RecentObservation
from .package import canonical_json
from .perception import EventObservation, NPCContext


@dataclass(frozen=True, slots=True)
class NPCEvaluationTask:
    schema_version: str
    evaluation_id: str
    session_id: str
    npc_id: str
    mode: str
    reason: str
    expected_npc_state_version: int
    source_observation_ids: tuple[str, ...]
    observation_work_ids: tuple[str, ...]
    player_input: str | None
    status: str
    attempts: int
    attempt_limit: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class NPCEvaluationInput:
    task: NPCEvaluationTask
    npc_meta: NPCView
    observations: tuple[EventObservation, ...]
    recent_observations: tuple[RecentObservation, ...] = ()


def _text(value, maximum=4096):
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise StateMachineError("invalid_evaluation", "Expected bounded nonempty text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise StateMachineError("invalid_evaluation", "Expected UTF-8 encodable text") from exc
    return value


class NPCEvaluationRuntime:
    def _evaluation_actor(self, sid, package, context):
        npc = self._npc_authorized(sid, package, context)
        row = self._conn.execute(
            "SELECT * FROM npc_states WHERE session_id=? AND npc_id=?", (sid, npc)
        ).fetchone()
        if row is None or row["configuration"] != "configured":
            raise StateMachineError("npc_unconfigured", "Evaluation requires a configured Profile")
        return row

    def _evaluation_row(self, evaluation_id, package, context):
        _text(evaluation_id, 128)
        if type(context) is not NPCContext:
            raise StateMachineError("unauthorized", "Expected trusted NPC Context")
        # Authenticate the context before revealing even task existence.
        self._npc_authorized(context.session_id, package, context)
        row = self._conn.execute(
            "SELECT * FROM npc_evaluation_tasks WHERE evaluation_id=?", (evaluation_id,)
        ).fetchone()
        if row is None or (row["session_id"], row["npc_id"], row["principal_id"]) != (
            context.session_id,
            context.observer_id,
            context.principal_id,
        ):
            raise StateMachineError("unauthorized", "Task is not bound to this controller")
        return row

    def _task(self, row):
        works = self._conn.execute(
            "SELECT w.work_id,w.observation_id FROM npc_evaluation_observations t "
            "JOIN npc_observation_work w USING(work_id) WHERE evaluation_id=? ORDER BY ordinal",
            (row["evaluation_id"],),
        ).fetchall()
        return NPCEvaluationTask(
            row["schema_version"],
            row["evaluation_id"],
            row["session_id"],
            row["npc_id"],
            row["mode"],
            row["reason"],
            row["expected_npc_state_version"],
            tuple(w["observation_id"] for w in works),
            tuple(w["work_id"] for w in works),
            row["player_input"],
            row["status"],
            row["attempts"],
            row["attempt_limit"],
            row["last_error"],
        )

    def create_npc_evaluation_task(
        self,
        session_id,
        package,
        *,
        context,
        request_id,
        mode,
        reason,
        expected_npc_state_version,
        source_observation_ids=(),
        player_input=None,
    ):
        """Controller request id dedupes authorization, never substitutes for evaluation_id."""
        with self._transaction(write=True):
            return self._create_evaluation(
                session_id,
                package,
                context=context,
                request_id=request_id,
                mode=mode,
                reason=reason,
                expected_npc_state_version=expected_npc_state_version,
                source_observation_ids=source_observation_ids,
                player_input=player_input,
            )

    def _create_evaluation(
        self,
        sid,
        package,
        *,
        context,
        request_id,
        mode,
        reason,
        expected_npc_state_version,
        source_observation_ids=(),
        player_input=None,
        gate_authorized=False,
    ):
        actor = self._evaluation_actor(sid, package, context)
        _text(request_id, 128)
        _text(reason)
        if player_input is not None:
            _text(player_input)
        if mode not in ("interactive", "background") or type(mode) is not str:
            raise StateMachineError("invalid_evaluation", "Unknown Evaluation Mode")
        if type(expected_npc_state_version) is not int or expected_npc_state_version < 1:
            raise StateMachineError("invalid_evaluation", "Positive NPCStateVersion required")
        if type(source_observation_ids) is not tuple or len(source_observation_ids) > 100:
            raise StateMachineError(
                "invalid_evaluation", "Bounded ordered Observation tuple required"
            )
        for oid in source_observation_ids:
            _text(oid, 128)
        if len(set(source_observation_ids)) != len(source_observation_ids):
            raise StateMachineError("invalid_evaluation", "Duplicate Observation")
        fingerprint = hashlib.sha256(
            canonical_json(
                [
                    mode,
                    reason,
                    expected_npc_state_version,
                    source_observation_ids,
                    player_input,
                ]
            ).encode()
        ).hexdigest()
        existing = self._conn.execute(
            "SELECT * FROM npc_evaluation_tasks WHERE session_id=? AND npc_id=? "
            "AND principal_id=? AND request_id=?",
            (sid, context.observer_id, context.principal_id, request_id),
        ).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise StateMachineError("idempotency_conflict", "Authorization request changed")
            return self._task(existing)
        if actor["npc_state_version"] != expected_npc_state_version:
            raise StateMachineError("npc_version_conflict", "NPC State changed")
        self._evaluation_mode_check(sid, context.observer_id, mode, gate_authorized)
        if self._conn.execute(
            "SELECT 1 FROM npc_evaluation_tasks WHERE session_id=? AND npc_id=? AND "
            "status='running'",
            (sid, context.observer_id),
        ).fetchone():
            raise StateMachineError("evaluation_active", "NPC already has a valid Evaluation")
        works, seqs = [], []
        for oid in source_observation_ids:
            work = self._conn.execute(
                "SELECT w.*,o.event_seq FROM npc_observation_work w "
                "JOIN event_observations o USING(observation_id) "
                "JOIN projection_cursors c ON c.session_id=w.session_id AND "
                "c.consumer='perception' "
                "WHERE w.session_id=? AND w.npc_id=? AND w.observation_id=? "
                "AND o.event_seq<=c.event_seq AND w.status='pending' "
                "AND NOT EXISTS (SELECT 1 FROM npc_evaluation_observations e "
                "JOIN npc_evaluation_tasks t USING(evaluation_id) "
                "WHERE e.work_id=w.work_id AND t.status='completed')",
                (sid, context.observer_id, oid),
            ).fetchone()
            if work is None:
                raise StateMachineError(
                    "invalid_observation", "Observation is unavailable or completed"
                )
            works.append(work["work_id"])
            seqs.append(work["event_seq"])
        if seqs != sorted(seqs):
            raise StateMachineError(
                "invalid_observation", "Observations must follow committed order"
            )
        eid = "evaluation." + uuid4().hex
        self._conn.execute(
            "INSERT INTO npc_evaluation_tasks(evaluation_id,schema_version,session_id,npc_id,"
            "principal_id,request_id,fingerprint,mode,reason,"
            "expected_npc_state_version,player_input) "
            "VALUES (?,'npc-evaluation/1',?,?,?,?,?,?,?,?,?)",
            (
                eid,
                sid,
                context.observer_id,
                context.principal_id,
                request_id,
                fingerprint,
                mode,
                reason,
                expected_npc_state_version,
                player_input,
            ),
        )
        self._conn.executemany(
            "INSERT INTO npc_evaluation_observations VALUES (?,?,?)",
            [(eid, i, wid) for i, wid in enumerate(works)],
        )
        self._conn.executemany(
            "INSERT INTO npc_evaluation_recent_scope VALUES (?,?,?,?)",
            [
                (eid, sid, context.observer_id, item.observation.observation_id)
                for item in self._recent_observations(sid, context.observer_id)
            ],
        )
        return self._task(
            self._conn.execute(
                "SELECT * FROM npc_evaluation_tasks WHERE evaluation_id=?", (eid,)
            ).fetchone()
        )

    def get_npc_evaluation_task(self, evaluation_id, package, *, context):
        with self._transaction():
            row = self._evaluation_row(evaluation_id, package, context)
            task = self._task(row)
            meta = self._npc_view(row["session_id"], row["npc_id"], package, context=context)
            recent = self._task_recent_observations(row)
            # Existing field continues to describe Task Work, now only valid OB.
            # Expired/unknown Work content is available through explicit recovery.
            current = {
                r["observation_id"]: r
                for r in self._observation_rows(row["session_id"], row["npc_id"])
            }
            observations = tuple(
                self._observation(current[oid])
                for oid in task.source_observation_ids
                if oid in current and self._retention(current[oid]).retention_status == "recent"
            )
            return NPCEvaluationInput(task, meta, observations, recent)

    def _task_recent_observations(self, row):
        authorized = {
            r[0]
            for r in self._conn.execute(
                "SELECT observation_id FROM npc_evaluation_recent_scope WHERE evaluation_id=?",
                (row["evaluation_id"],),
            )
        }
        return tuple(
            item
            for item in self._recent_observations(row["session_id"], row["npc_id"])
            if item.observation.observation_id in authorized
        )

    def _meta_observation_scope(self, row):
        # Original Work grants survive TTL for safe in-flight/recovery processing.
        # Completed background OB requires both immutable Task grant and validity now.
        return set(self._task(row).source_observation_ids) | {
            item.observation.observation_id for item in self._task_recent_observations(row)
        }

    def get_npc_evaluation_recovery_input(self, evaluation_id, package, *, context):
        """Explicit, own active Task's expired/unknown Pending evidence; never Recent."""
        with self._transaction():
            row = self._evaluation_row(evaluation_id, package, context)
            if row["status"] != "running":
                raise StateMachineError("evaluation_inactive", "Recovery requires a valid Task")
            actor = self._evaluation_actor(row["session_id"], package, context)
            if actor["npc_state_version"] != row["expected_npc_state_version"]:
                raise StateMachineError("npc_version_conflict", "Task version is no longer current")
            scope = set(self._task(row).source_observation_ids)
            return tuple(
                RecentObservation(self._observation(item), self._retention(item))
                for item in self._observation_rows(row["session_id"], row["npc_id"])
                if item["observation_id"] in scope
                and item["status"] == "pending"
                and self._retention(item).retention_status != "recent"
            )

    def list_npc_evaluation_tasks(self, session_id, package, *, context):
        with self._transaction():
            npc = self._npc_authorized(session_id, package, context)
            return tuple(
                self._task(row)
                for row in self._conn.execute(
                    "SELECT * FROM npc_evaluation_tasks WHERE session_id=? AND npc_id=? "
                    "AND principal_id=? ORDER BY rowid",
                    (session_id, npc, context.principal_id),
                ).fetchall()
            )
