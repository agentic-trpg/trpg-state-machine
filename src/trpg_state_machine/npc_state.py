"""Versioned local NPC domain data and authorized reads; no cognition write API."""

import json
from dataclasses import asdict, dataclass, replace
from typing import Literal

from .errors import StateMachineError
from .package import canonical_json

NPC_STATE_SCHEMA = "npc-state/1"


@dataclass(frozen=True, slots=True)
class NPCProfile:
    role: str
    personality: tuple[str, ...]
    speaking_style: str
    constraints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceReference:
    # Package backstory is explicitly different from a Session Observation.
    kind: Literal["package", "memory", "observation"]
    reference_id: str
    version: int | None


@dataclass(frozen=True, slots=True)
class PrivateRecord:
    record_id: str
    version: int
    is_current: bool
    text: str
    sources: tuple[SourceReference, ...]


@dataclass(frozen=True, slots=True)
class EpisodicMemory(PrivateRecord):
    epistemic_status: Literal["observed", "inferred", "told"]


@dataclass(frozen=True, slots=True)
class Belief(PrivateRecord):
    epistemic_status: Literal["observed", "inferred", "told"]
    confidence_basis_points: int | None
    status: Literal["active", "retracted"]


@dataclass(frozen=True, slots=True)
class Relationship(PrivateRecord):
    target_id: str
    stance: Literal["friendly", "neutral", "hostile"]


@dataclass(frozen=True, slots=True)
class Goal(PrivateRecord):
    status: Literal["active", "completed", "abandoned"]


@dataclass(frozen=True, slots=True)
class Plan(PrivateRecord):
    goal_id: str
    goal_version: int
    steps: tuple[str, ...]
    status: Literal["active", "completed", "abandoned"]


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    schedule_id: str
    scene_id: str
    start_tick: int
    end_tick: int
    activity: str


@dataclass(frozen=True, slots=True)
class NPCRuntimeState:
    schedule: tuple[ScheduleEntry, ...]
    current_activity: str | None


@dataclass(frozen=True, slots=True)
class NPCScheduleRun:
    schedule_id: str
    status: Literal["active", "completed", "interrupted", "missed"]
    started_tick: int | None
    finished_tick: int | None
    updated_tick: int


@dataclass(frozen=True, slots=True)
class WorldFactDefinition:
    fact_id: str
    text: str
    visibility: Literal["npc_authorized", "dm_only"]
    authorized_npc_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnownFact:
    fact_id: str
    text: str


@dataclass(frozen=True, slots=True)
class NPCInitialState:
    profile: NPCProfile
    initial_knowledge_ids: tuple[str, ...]
    memories: tuple[EpisodicMemory, ...]
    beliefs: tuple[Belief, ...]
    relationships: tuple[Relationship, ...]
    goals: tuple[Goal, ...]
    plans: tuple[Plan, ...]
    runtime: NPCRuntimeState


@dataclass(frozen=True, slots=True)
class NPCView:
    schema_version: str
    package_digest: str
    session_id: str
    npc_id: str
    name: str | None
    controller_kind: str | None
    configuration: Literal["configured", "unconfigured"]
    initialization_origin: Literal["package", "legacy"]
    npc_state_version: int
    current_scene_id: str
    profile: NPCProfile | None
    known_facts: tuple[KnownFact, ...]
    memories: tuple[EpisodicMemory, ...]
    belief_history: tuple[Belief, ...]
    relationships: tuple[Relationship, ...]
    goals: tuple[Goal, ...]
    plans: tuple[Plan, ...]
    runtime: NPCRuntimeState | None
    schedule_runs: tuple[NPCScheduleRun, ...] = ()


RECORD_TYPES = {
    "memory": EpisodicMemory,
    "belief": Belief,
    "relationship": Relationship,
    "goal": Goal,
    "plan": Plan,
}


def _profile(data):
    return NPCProfile(
        data["role"], tuple(data["personality"]), data["speaking_style"], tuple(data["constraints"])
    )


def _runtime(data):
    return NPCRuntimeState(
        tuple(ScheduleEntry(**entry) for entry in data["schedule"]), data["current_activity"]
    )


class NPCRuntime:
    def _initialize_npc(self, sid, actor):
        """Only invoked with the insertion of the unique Actor, in its transaction."""
        initial = actor.npc_state
        self._conn.execute(
            "INSERT INTO npc_states VALUES (?, ?, ?, ?, ?, 'package', ?, ?, ?, 1)",
            (
                sid,
                actor.actor_id,
                NPC_STATE_SCHEMA,
                actor.name,
                actor.controller_kind,
                "configured" if initial else "unconfigured",
                canonical_json(asdict(initial.profile)) if initial else None,
                canonical_json(asdict(initial.runtime)) if initial else None,
            ),
        )
        self._conn.execute("INSERT INTO npc_modes VALUES (?,?,'inactive')", (sid, actor.actor_id))
        self._conn.execute(
            "INSERT INTO npc_completion_cursors VALUES (?,?,0)",
            (sid, actor.actor_id),
        )
        self._conn.execute(
            "INSERT INTO npc_perception_cursors VALUES (?,?,0)", (sid, actor.actor_id)
        )
        if initial is None:
            return
        self._conn.executemany(
            "INSERT INTO npc_known_facts VALUES (?, ?, ?)",
            [(sid, actor.actor_id, key) for key in initial.initial_knowledge_ids],
        )
        records = (
            ("memory", initial.memories),
            ("belief", initial.beliefs),
            ("relationship", initial.relationships),
            ("goal", initial.goals),
            ("plan", initial.plans),
        )
        pc_id = self._conn.execute(
            "SELECT pc_actor_id FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()[0]
        for kind, items in records:
            for record in items:
                if kind == "relationship" and record.target_id == "selected_pc":
                    record = replace(record, target_id=pc_id)
                data = asdict(record)
                for key in ("sources", "record_id", "version", "is_current"):
                    del data[key]
                self._conn.execute(
                    "INSERT INTO npc_private_records VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        sid,
                        actor.actor_id,
                        kind,
                        record.record_id,
                        record.version,
                        int(record.is_current),
                        canonical_json(data),
                    ),
                )
                for ordinal, source in enumerate(record.sources):
                    self._conn.execute(
                        "INSERT INTO npc_record_sources "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'memory')",
                        (
                            sid,
                            actor.actor_id,
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
                        "INSERT INTO npc_plan_goals VALUES (?, ?, 'plan', ?, ?, 'goal', ?, ?)",
                        (
                            sid,
                            actor.actor_id,
                            record.record_id,
                            record.version,
                            record.goal_id,
                            record.goal_version,
                        ),
                    )

    def get_npc_view(self, session_id, npc_id, package, *, context):
        """Complete private Meta for exactly the persisted, authenticated observer.

        Observations remain on the existing Perception/Inbox path. No ACK, cursor
        change, semantic selection, mechanical control grant or model execution.
        """
        with self._transaction():
            return self._npc_view(session_id, npc_id, package, context=context)

    def _npc_view(self, session_id, npc_id, package, *, context):
        observer = self._npc_authorized(session_id, package, context)
        if npc_id != observer:
            raise StateMachineError("unauthorized", "NPC private state belongs to the observer")
        row = self._conn.execute(
            "SELECT n.*,a.current_scene_id FROM npc_states n JOIN actors a "
            "ON a.session_id=n.session_id AND a.actor_id=n.npc_id "
            "WHERE n.session_id=? AND n.npc_id=? AND a.kind='npc'",
            (session_id, npc_id),
        ).fetchone()
        if row is None:
            raise StateMachineError("npc_state_unavailable", "NPC persistent state is missing")
        records = {kind: [] for kind in RECORD_TYPES}
        for stored in self._conn.execute(
            "SELECT * FROM npc_private_records WHERE session_id=? AND npc_id=? "
            "ORDER BY record_kind,record_id,content_version",
            (session_id, npc_id),
        ).fetchall():
            sources = tuple(
                SourceReference(s["source_kind"], s["reference_id"], s["reference_version"])
                for s in self._conn.execute(
                    "SELECT * FROM npc_record_sources WHERE session_id=? AND npc_id=? "
                    "AND record_kind=? AND record_id=? AND content_version=? ORDER BY ordinal",
                    (
                        session_id,
                        npc_id,
                        stored["record_kind"],
                        stored["record_id"],
                        stored["content_version"],
                    ),
                ).fetchall()
            )
            data = json.loads(stored["payload_json"])
            data.update(
                record_id=stored["record_id"],
                version=stored["content_version"],
                is_current=bool(stored["is_current"]),
            )
            if stored["record_kind"] == "plan":
                data["steps"] = tuple(data["steps"])
            records[stored["record_kind"]].append(
                RECORD_TYPES[stored["record_kind"]](**data, sources=sources)
            )
        known = tuple(
            KnownFact(f["fact_id"], f["text"])
            for f in self._conn.execute(
                "SELECT f.fact_id,f.text FROM npc_known_facts k JOIN world_facts f "
                "USING(session_id,fact_id) WHERE k.session_id=? AND k.npc_id=? "
                "AND f.visibility='npc_authorized' ORDER BY f.fact_id",
                (session_id, npc_id),
            ).fetchall()
        )
        return NPCView(
            row["schema_version"],
            package.package_digest,
            session_id,
            npc_id,
            row["name"],
            row["controller_kind"],
            row["configuration"],
            row["initialization_origin"],
            row["npc_state_version"],
            row["current_scene_id"],
            _profile(json.loads(row["profile_json"])) if row["profile_json"] else None,
            known,
            tuple(records["memory"]),
            tuple(records["belief"]),
            tuple(records["relationship"]),
            tuple(records["goal"]),
            tuple(records["plan"]),
            _runtime(json.loads(row["runtime_json"])) if row["runtime_json"] else None,
            tuple(
                NPCScheduleRun(
                    r["schedule_id"],
                    r["status"],
                    r["started_tick"],
                    r["finished_tick"],
                    r["updated_tick"],
                )
                for r in self._conn.execute(
                    "SELECT * FROM npc_schedule_runs WHERE session_id=? AND npc_id=? "
                    "ORDER BY schedule_id",
                    (session_id, npc_id),
                ).fetchall()
            ),
        )
