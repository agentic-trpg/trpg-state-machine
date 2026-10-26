"""Versioned local NPC domain data and authorized reads; no cognition write API."""

from dataclasses import dataclass
from typing import Literal

from .errors import StateMachineError
from .scoped_reads import scoped_read

NPC_STATE_SCHEMA = "npc-state/1"


@dataclass(frozen=True, slots=True)
class NPCProfile:
    """Describe NPC role, personality, speaking style and behavior constraints."""

    role: str
    personality: tuple[str, ...]
    speaking_style: str
    constraints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceReference:
    # Package backstory is explicitly different from a Session Observation.
    """Distinguish Package backstory, memory and versioned observation provenance."""

    kind: Literal["package", "memory", "observation"]
    reference_id: str
    version: int | None


@dataclass(frozen=True, slots=True)
class PrivateRecord:
    """Carry versioned NPC cognition text and its source references."""

    record_id: str
    version: int
    is_current: bool
    text: str
    sources: tuple[SourceReference, ...]


@dataclass(frozen=True, slots=True)
class EpisodicMemory(PrivateRecord):
    """Record a private episode with observed/inferred/told epistemic status."""

    epistemic_status: Literal["observed", "inferred", "told"]


@dataclass(frozen=True, slots=True)
class Belief(PrivateRecord):
    """Record a private belief, confidence and active/retracted state."""

    epistemic_status: Literal["observed", "inferred", "told"]
    confidence_basis_points: int | None
    status: Literal["active", "retracted"]


@dataclass(frozen=True, slots=True)
class Relationship(PrivateRecord):
    """Record a private stance toward an identified target."""

    target_id: str
    stance: Literal["friendly", "neutral", "hostile"]


@dataclass(frozen=True, slots=True)
class Goal(PrivateRecord):
    """Record a versioned NPC goal and its lifecycle state."""

    status: Literal["active", "completed", "abandoned"]


@dataclass(frozen=True, slots=True)
class Plan(PrivateRecord):
    """Associate ordered private steps with a specific goal version."""

    goal_id: str
    goal_version: int
    steps: tuple[str, ...]
    status: Literal["active", "completed", "abandoned"]


@dataclass(frozen=True, slots=True)
class ScheduleEntry:
    """Assign an NPC scene/activity to a game-tick interval."""

    schedule_id: str
    scene_id: str
    start_tick: int
    end_tick: int
    activity: str


@dataclass(frozen=True, slots=True)
class NPCRuntimeState:
    """Group the NPC schedule and current activity claim."""

    schedule: tuple[ScheduleEntry, ...]
    current_activity: str | None


@dataclass(frozen=True, slots=True)
class NPCScheduleRun:
    """Track schedule execution status and actual timing."""

    schedule_id: str
    status: Literal["active", "completed", "interrupted", "missed"]
    started_tick: int | None
    finished_tick: int | None
    updated_tick: int


@dataclass(frozen=True, slots=True)
class WorldFactDefinition:
    """Define objective fact text and its NPC/DM disclosure policy."""

    fact_id: str
    text: str
    visibility: Literal["npc_authorized", "dm_only"]
    authorized_npc_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnownFact:
    """Expose an objective fact that the NPC is authorized to know."""

    fact_id: str
    text: str


@dataclass(frozen=True, slots=True)
class NPCInitialState:
    """Group configured initial profile, knowledge, cognition and runtime data."""

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
    """Expose authorized NPC state/history with configuration and version metadata."""

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
    """Restore an NPC profile from its persisted dictionary."""
    return NPCProfile(
        data["role"], tuple(data["personality"]), data["speaking_style"], tuple(data["constraints"])
    )


def _runtime(data):
    """Restore NPC runtime and schedule entries."""
    return NPCRuntimeState(
        tuple(ScheduleEntry(**entry) for entry in data["schedule"]), data["current_activity"]
    )


class NPCRuntime:
    """Initialize and read authorized NPC state without a public cognition-write API."""

    def _initialize_npc(self, sid, actor):
        """Delegate NPC state creation to the initialization coordinator."""
        return self._npc_bootstrap.initialize(sid, actor)

    def get_npc_view(self, session_id, npc_id, package, *, context):
        """Complete private Meta for exactly the persisted, authenticated observer.

        Observations remain on the existing Perception/Inbox path. No ACK, cursor
        change, semantic selection, mechanical control grant or model execution.
        """
        with self._transaction():
            return self._npc_view(session_id, npc_id, package, context=context)

    def _npc_view(self, session_id, npc_id, package, *, context):
        """Verify authenticated observer identity before reading the NPC view."""
        observer = self._npc_authorized(session_id, package, context)
        if npc_id != observer:
            raise StateMachineError("unauthorized", "NPC private state belongs to the observer")
        return self._npc_current_view(session_id, npc_id, package)

    @scoped_read
    def _npc_current_view(self, session_id, npc_id, package):
        """Build the current NPC view from profile, cognition records, sources and runtime."""
        row = self._conn.execute(
            "SELECT * FROM npc_states WHERE session_id=? AND npc_id=?",
            (session_id, npc_id),
        ).fetchone()
        actor = self._conn.execute(
            "SELECT current_scene_id,kind FROM actors WHERE session_id=? AND actor_id=?",
            (session_id, npc_id),
        ).fetchone()
        if row is None or actor is None or actor["kind"] != "npc":
            raise StateMachineError("npc_state_unavailable", "NPC persistent state is missing")
        row = {**dict(row), "current_scene_id": actor["current_scene_id"]}
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
            data = self._store.memory.json(stored["payload_json"])
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
        known = []
        for (fid,) in self._conn.execute(
            "SELECT fact_id FROM npc_known_facts WHERE session_id=? AND npc_id=? ORDER BY fact_id",
            (session_id, npc_id),
        ):
            fact = self._conn.execute(
                "SELECT text,visibility FROM world_facts WHERE session_id=? AND fact_id=?",
                (session_id, fid),
            ).fetchone()
            if fact is not None and fact["visibility"] == "npc_authorized":
                known.append(KnownFact(fid, fact["text"]))
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
            _profile(self._store.memory.json(row["profile_json"])) if row["profile_json"] else None,
            tuple(known),
            tuple(records["memory"]),
            tuple(records["belief"]),
            tuple(records["relationship"]),
            tuple(records["goal"]),
            tuple(records["plan"]),
            _runtime(self._store.memory.json(row["runtime_json"])) if row["runtime_json"] else None,
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
