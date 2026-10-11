"""Local ob-retention/1 projections over the existing Inbox/Work, without deletion.

TTL is 100 abstract game ticks from first durable Work adoption. This local
candidate does not freeze the architecture's OPEN TTL policy or invent event time.
"""

import json
from dataclasses import dataclass

from .errors import StateMachineError
from .perception import EventObservation, PerceivedSignal


@dataclass(frozen=True, slots=True)
class ObservationRetention:
    observation_id: str
    work_id: str
    processing_status: str
    accepted_game_tick: int | None
    expires_game_tick: int | None
    retention_status: str
    archived: bool


@dataclass(frozen=True, slots=True)
class RecentObservation:
    observation: EventObservation
    retention: ObservationRetention


class ObservationLifecycleRuntime:
    def _observation_rows(self, sid, npc):
        # Only this NPC's projection prefix fences reads. Processing and TTL
        # remain independent, and another NPC's work cannot hold this one.
        return self._conn.execute(
            "SELECT w.*,o.content_json,o.event_seq,s.game_tick,EXISTS ("
            "SELECT 1 FROM npc_record_sources r WHERE r.session_id=w.session_id "
            "AND r.npc_id=w.npc_id AND r.source_kind='observation' "
            "AND r.reference_id=w.observation_id) AS referenced "
            "FROM npc_observation_work w JOIN npc_inbox i ON "
            "i.session_id=w.session_id AND i.observer_id=w.npc_id "
            "AND i.observation_id=w.observation_id "
            "JOIN event_observations o USING(observation_id) "
            "JOIN world_states s ON s.session_id=w.session_id "
            "JOIN npc_perception_cursors c ON c.session_id=w.session_id "
            "AND c.npc_id=w.npc_id WHERE w.session_id=? AND w.npc_id=? "
            "AND o.event_seq<=c.event_seq ORDER BY o.event_seq,w.observation_id",
            (sid, npc),
        ).fetchall()

    def _retention(self, row):
        expiry = row["expires_game_tick"]
        status = (
            "legacy_unknown"
            if expiry is None
            else "recent"
            if row["game_tick"] < expiry
            else "expired"
        )
        return ObservationRetention(
            row["observation_id"],
            row["work_id"],
            row["status"],
            row["accepted_game_tick"],
            expiry,
            status,
            status != "recent" and bool(row["referenced"]),
        )

    def _observation(self, row):
        return EventObservation(
            row["observation_id"],
            row["npc_id"],
            tuple(PerceivedSignal(**s) for s in json.loads(row["content_json"])),
        )

    def _recent_observations(self, sid, npc):
        return tuple(
            RecentObservation(self._observation(row), self._retention(row))
            for row in self._observation_rows(sid, npc)
            if self._retention(row).retention_status == "recent"
        )

    def get_recent_npc_observations(self, session_id, package, *, context):
        """All valid Pending and Completed OB; no cognition ACK or Work creation."""
        with self._transaction():
            npc = self._npc_authorized(session_id, package, context)
            return self._recent_observations(session_id, npc)

    def read_observations(self, session_id, package, *, context):
        """Existing ordinary read now projects Recent only; return type is preserved."""
        return tuple(
            item.observation
            for item in self.get_recent_npc_observations(session_id, package, context=context)
        )

    def get_npc_observation_retention(self, session_id, package, *, context):
        """Own-NPC metadata, including expiry/unknown/archival, never archived text."""
        with self._transaction():
            npc = self._npc_authorized(session_id, package, context)
            return tuple(self._retention(row) for row in self._observation_rows(session_id, npc))

    def get_pending_npc_observation_work(self, session_id, package, *, context):
        """Independent Pending scope; expired evidence needs task-scoped recovery."""
        return tuple(
            item
            for item in self.get_npc_observation_retention(session_id, package, context=context)
            if item.processing_status == "pending"
        )

    def get_npc_observation_expiry(self, session_id, package, *, context, observation_ids=None):
        """Idempotent metadata query for Host's next invocation; no Context/ACK writes."""
        if observation_ids is not None and (
            type(observation_ids) is not tuple
            or len(observation_ids) > 1000
            or any(type(oid) is not str for oid in observation_ids)
            or len(set(observation_ids)) != len(observation_ids)
        ):
            raise StateMachineError("invalid_observation", "Expected a bounded unique OB scope")
        with self._transaction():
            npc = self._npc_authorized(session_id, package, context)
            retained = tuple(self._retention(r) for r in self._observation_rows(session_id, npc))
            if observation_ids is not None and not set(observation_ids) <= {
                r.observation_id for r in retained
            }:
                raise StateMachineError(
                    "invalid_observation", "OB is outside authorized visible scope"
                )
            return tuple(
                r
                for r in retained
                if r.retention_status != "recent"
                and (observation_ids is None or r.observation_id in observation_ids)
            )
