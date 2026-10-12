"""Existing NPC initialization, bounded to a newly inserted actor."""

from dataclasses import asdict, replace

from .npc_state import NPC_STATE_SCHEMA
from .package import canonical_json


class NPCBootstrap:
    def __init__(self, store):
        self.store = store

    @property
    def _conn(self):
        return self.store.connection

    def initialize(self, sid, actor):
        """Only invoked with the insertion of the unique Actor, in its transaction."""
        self.store.require_write()
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
