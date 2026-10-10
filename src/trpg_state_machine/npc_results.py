"""Seven-field local NPC result and atomic private history commit; no action execution."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from uuid import uuid4

from .errors import StateMachineError
from .npc_state import RECORD_TYPES, SourceReference
from .package import canonical_json


@dataclass(frozen=True, slots=True)
class NPCActionIntent:
    operation: str
    destination_scene_id: str
    expected_world_version: int


@dataclass(frozen=True, slots=True)
class NPCDialogue:
    text: str
    audience_ids: tuple[str, ...]
    visibility: str
    source_observation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NPCEvaluationResult:
    dialogue: NPCDialogue | None = None
    memory_updates: tuple = ()
    belief_updates: tuple = ()
    relationship_updates: tuple = ()
    goal_updates: tuple = ()
    plan_updates: tuple = ()
    action_intents: tuple = ()


@dataclass(frozen=True, slots=True)
class NPCOutputHandoff:
    ordinal: int
    kind: str
    command_id: str | None
    status: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class EvaluationReceipt:
    evaluation_id: str
    status: str
    npc_state_version: int
    outputs: tuple[NPCOutputHandoff, ...]


def _receipt(raw):
    data = json.loads(raw)
    data["outputs"] = tuple(NPCOutputHandoff(**item) for item in data["outputs"])
    return EvaluationReceipt(**data)


def _invalid(message):
    raise StateMachineError("invalid_npc_result", message)


def _bounded_text(value):
    if type(value) is not str or not value.strip() or len(value) > 4096:
        _invalid("Expected nonempty text of at most 4096 characters")


def _identifier(value, kind):
    if (
        type(value) is not str
        or len(value) > 128
        or not re.fullmatch(re.escape(kind) + r"\.[a-zA-Z0-9_.-]+", value)
    ):
        _invalid("Invalid private record identity")


def _positive(value):
    if type(value) is not int or not 1 <= value <= 1_000_000:
        _invalid("Positive bounded integer version required")


def _result_json(result):
    if type(result) is not NPCEvaluationResult:
        _invalid("Expected the seven-field local NPCEvaluationResult")
    for field in ("memory", "belief", "relationship", "goal", "plan", "action_intents"):
        items = getattr(result, field + "_updates" if field != "action_intents" else field)
        if type(items) is not tuple or len(items) > 100:
            _invalid("Each update/output list must be a tuple of at most 100 items")
    try:
        raw = canonical_json(asdict(result))
    except (TypeError, ValueError, RecursionError) as exc:
        raise StateMachineError("invalid_npc_result", "Result is not bounded typed data") from exc
    if len(raw.encode("utf-8")) > 262144:
        _invalid("Result exceeds 256 KiB")
    return raw


class NPCResultRuntime:
    def _validate_meta(self, row, result):
        sid, npc = row["session_id"], row["npc_id"]
        observations = set(self._task(row).source_observation_ids)
        approved = []
        goals = {
            (r[0], r[1])
            for r in self._conn.execute(
                "SELECT record_id,content_version FROM npc_private_records "
                "WHERE session_id=? AND npc_id=? AND record_kind='goal'",
                (sid, npc),
            )
        }
        for kind, record_type in RECORD_TYPES.items():
            seen = set()
            for record in getattr(result, kind + "_updates"):
                if type(record) is not record_type:
                    _invalid("Update must use its exact closed record type")
                _identifier(record.record_id, kind)
                _positive(record.version)
                _bounded_text(record.text)
                if record.is_current is not True or record.record_id in seen:
                    _invalid("One new current version per record per Result")
                seen.add(record.record_id)
                previous = self._conn.execute(
                    "SELECT MAX(content_version) FROM npc_private_records WHERE session_id=? "
                    "AND npc_id=? AND record_kind=? AND record_id=?",
                    (sid, npc, kind, record.record_id),
                ).fetchone()[0]
                if record.version != (previous or 0) + 1:
                    _invalid("History version must append without gaps or replacement")
                if type(record.sources) is not tuple or not 1 <= len(record.sources) <= 4:
                    _invalid("One to four explicit sources required")
                source_keys = set()
                for source in record.sources:
                    if type(source) is not SourceReference:
                        _invalid("Expected a typed source reference")
                    if type(source.reference_id) is not str or len(source.reference_id) > 128:
                        _invalid("Invalid source identity")
                    key = (source.kind, source.reference_id, source.version)
                    if type(source.kind) is not str or type(source.version) not in (
                        int,
                        type(None),
                    ):
                        _invalid("Invalid source type/version")
                    if key in source_keys:
                        _invalid("Duplicate source")
                    source_keys.add(key)
                    if source.kind == "package":
                        valid = source.reference_id == npc and source.version is None
                    elif source.kind == "observation":
                        valid = source.version is None and source.reference_id in observations
                    elif source.kind == "memory":
                        _positive(source.version)
                        valid = self._conn.execute(
                            "SELECT 1 FROM npc_private_records WHERE session_id=? AND npc_id=? "
                            "AND record_kind='memory' AND record_id=? AND content_version=?",
                            (sid, npc, source.reference_id, source.version),
                        ).fetchone()
                    else:
                        valid = False
                    if not valid:
                        _invalid("Source must be own Package, task Observation or committed Memory")
                if kind in ("memory", "belief") and record.epistemic_status not in (
                    "observed",
                    "inferred",
                    "told",
                ):
                    _invalid("Invalid epistemic status")
                if kind == "belief":
                    confidence = record.confidence_basis_points
                    if confidence is not None and (
                        type(confidence) is not int or not 0 <= confidence <= 10000
                    ):
                        _invalid("Invalid confidence")
                    if record.status not in ("active", "retracted"):
                        _invalid("Invalid Belief status")
                if kind == "relationship":
                    if (
                        type(record.target_id) is not str
                        or record.target_id == npc
                        or not self._conn.execute(
                            "SELECT 1 FROM actors WHERE session_id=? AND actor_id=?",
                            (sid, record.target_id),
                        ).fetchone()
                    ):
                        _invalid("Relationship requires another existing Session Actor")
                    if record.stance not in ("friendly", "neutral", "hostile"):
                        _invalid("Invalid relationship stance")
                if kind in ("goal", "plan") and record.status not in (
                    "active",
                    "completed",
                    "abandoned",
                ):
                    _invalid("Invalid Goal/Plan status")
                if kind == "goal":
                    goals.add((record.record_id, record.version))
                if kind == "plan":
                    _identifier(record.goal_id, "goal")
                    _positive(record.goal_version)
                    if (record.goal_id, record.goal_version) not in goals:
                        _invalid("Plan must reference own committed or same-batch Goal")
                    if type(record.steps) is not tuple or len(record.steps) > 16:
                        _invalid("Bounded Plan steps required")
                    for step in record.steps:
                        _bounded_text(step)
                approved.append((kind, record))
        return approved

    def _output_candidates(self, row, result, package):
        outputs = []
        if result.dialogue is not None:
            dialogue = result.dialogue
            error = None
            if type(dialogue) is not NPCDialogue:
                error = "invalid_dialogue"
            else:
                try:
                    _bounded_text(dialogue.text)
                    if dialogue.visibility not in ("private", "local"):
                        _invalid("Unknown dialogue visibility")
                    if type(dialogue.audience_ids) is not tuple or len(dialogue.audience_ids) > 16:
                        _invalid("Bounded dialogue audience required")
                    if any(type(a) is not str for a in dialogue.audience_ids) or len(
                        set(dialogue.audience_ids)
                    ) != len(dialogue.audience_ids):
                        _invalid("Duplicate or invalid audience")
                    if (
                        type(dialogue.source_observation_ids) is not tuple
                        or len(dialogue.source_observation_ids) > 100
                    ):
                        _invalid("Bounded dialogue sources required")
                    if any(type(s) is not str for s in dialogue.source_observation_ids):
                        _invalid("Invalid dialogue source")
                    if not set(dialogue.source_observation_ids) <= set(
                        self._task(row).source_observation_ids
                    ):
                        _invalid("Dialogue source is outside Task evidence")
                except StateMachineError:
                    error = "invalid_dialogue"
            outputs.append(("dialogue", dialogue, error))
        for intent in result.action_intents:
            error = None
            if type(intent) is not NPCActionIntent or intent.operation != "move_actor":
                error = "unsupported_npc_action"
            elif (
                type(intent.expected_world_version) is not int
                or intent.expected_world_version < 0
                or type(intent.destination_scene_id) is not str
                or intent.destination_scene_id not in {s.scene_id for s in package.scenes}
            ):
                error = "invalid_npc_action"
            outputs.append(("action", intent, error))
        return outputs

    def _record_evaluation_failure(self, row, code):
        self._conn.execute(
            "UPDATE npc_evaluation_tasks SET attempts=attempts+1,last_error=?,"
            "status=CASE WHEN attempts+1>=attempt_limit THEN 'failed' ELSE 'running' END "
            "WHERE evaluation_id=? AND status='running'",
            (code, row["evaluation_id"]),
        )

    def submit_npc_evaluation_result(self, evaluation_id, result, package, *, context):
        """Task and trusted context supply all identity; invalid Meta chooses no Result."""
        error = None
        with self._transaction(write=True):
            row = self._evaluation_row(evaluation_id, package, context)
            saved = self._conn.execute(
                "SELECT * FROM npc_evaluation_receipts WHERE evaluation_id=?", (evaluation_id,)
            ).fetchone()
            if saved:
                raw = _result_json(result)
                if hashlib.sha256(raw.encode()).hexdigest() != saved["fingerprint"]:
                    raise StateMachineError(
                        "idempotency_conflict", "Evaluation Result already selected"
                    )
                return _receipt(saved["receipt_json"])
            if row["status"] != "running":
                raise StateMachineError("evaluation_inactive", "Task cannot select a new Result")
            actor = self._evaluation_actor(row["session_id"], package, context)
            if actor["npc_state_version"] != row["expected_npc_state_version"]:
                raise StateMachineError(
                    "npc_version_conflict", "NPC state changed; authorize a new Task"
                )
            try:
                raw = _result_json(result)
                records = self._validate_meta(row, result)
            except StateMachineError as exc:
                self._record_evaluation_failure(row, exc.code)
                error = exc
            if error is None:
                version = actor["npc_state_version"] + bool(records)
                self._append_npc_records(row, records, version)
                self._conn.execute(
                    "UPDATE npc_states SET npc_state_version=? WHERE session_id=? AND npc_id=?",
                    (version, row["session_id"], row["npc_id"]),
                )
                outputs = []
                for ordinal, (kind, payload, problem) in enumerate(
                    self._output_candidates(row, result, package)
                ):
                    command_id = (
                        "command.npc." + uuid4().hex if kind == "action" and not problem else None
                    )
                    status = "rejected" if problem else "pending"
                    outputs.append(NPCOutputHandoff(ordinal, kind, command_id, status, problem))
                    encoded = (
                        canonical_json(asdict(payload))
                        if hasattr(payload, "__dataclass_fields__")
                        else canonical_json(payload)
                    )
                    self._conn.execute(
                        "INSERT INTO npc_output_handoffs VALUES (?,?,?,?,?,?,?,NULL)",
                        (evaluation_id, ordinal, kind, command_id, encoded, status, problem),
                    )
                receipt = EvaluationReceipt(evaluation_id, "completed", version, tuple(outputs))
                self._conn.execute(
                    "INSERT INTO npc_evaluation_receipts VALUES (?,?,?,?)",
                    (
                        evaluation_id,
                        hashlib.sha256(raw.encode()).hexdigest(),
                        raw,
                        canonical_json(asdict(receipt)),
                    ),
                )
                self._conn.execute(
                    "UPDATE npc_evaluation_tasks SET status='completed' WHERE evaluation_id=?",
                    (evaluation_id,),
                )
        if error:
            raise error
        return receipt

    def _append_npc_records(self, row, records, version):
        sid, npc, eid = row["session_id"], row["npc_id"], row["evaluation_id"]
        for kind, record in records:
            self._conn.execute(
                "UPDATE npc_private_records SET is_current=0 WHERE session_id=? AND npc_id=? "
                "AND record_kind=? AND record_id=? AND is_current=1",
                (sid, npc, kind, record.record_id),
            )
            data = asdict(record)
            for field in ("record_id", "version", "is_current", "sources"):
                del data[field]
            self._conn.execute(
                "INSERT INTO npc_private_records VALUES (?,?,?,?,?,1,?,?)",
                (sid, npc, kind, record.record_id, record.version, canonical_json(data), version),
            )
            for ordinal, source in enumerate(record.sources):
                self._conn.execute(
                    "INSERT INTO npc_record_sources VALUES (?,?,?,?,?,?,?,?,?,'memory')",
                    (
                        sid,
                        npc,
                        kind,
                        record.record_id,
                        record.version,
                        ordinal,
                        source.kind,
                        source.reference_id,
                        source.version,
                    ),
                )
            if kind == "plan":
                self._conn.execute(
                    "INSERT INTO npc_plan_goals VALUES (?,?,'plan',?,?,'goal',?,?)",
                    (
                        sid,
                        npc,
                        record.record_id,
                        record.version,
                        record.goal_id,
                        record.goal_version,
                    ),
                )
            self._conn.execute(
                "INSERT INTO npc_record_evaluations VALUES (?,?,?,?,?,?)",
                (sid, npc, kind, record.record_id, record.version, eid),
            )

    def get_npc_evaluation_receipt(self, evaluation_id, package, *, context):
        with self._transaction():
            self._evaluation_row(evaluation_id, package, context)
            row = self._conn.execute(
                "SELECT receipt_json FROM npc_evaluation_receipts WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            return _receipt(row[0]) if row else None
