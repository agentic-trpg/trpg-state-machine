"""Durable NPC work references and ordering; the global event log stays unique."""

import json
from dataclasses import dataclass

from .errors import StateMachineError
from .package import RulesetBinding, canonical_json
from .perception import _evidence_v4


@dataclass(frozen=True, slots=True)
class NPCPerceptionEvent:
    event_seq: int
    signal_ids: tuple[str, ...]
    complete: bool
    provenance: str


class PerceptionQueueRuntime:
    def _enqueue_perception(self, sid, seq, binding):
        """Only at event commit or locked migration; never discover from current state."""
        boundary = self._conn.execute(
            "SELECT through_event_seq FROM legacy_perception_boundaries WHERE session_id=?",
            (sid,),
        ).fetchone()
        if boundary and seq <= boundary[0]:
            return
        try:
            event = self._read_event(sid, seq)
            stored = self._conn.execute(
                "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
                (sid, seq),
            ).fetchone()
            evidence = json.loads(stored[0]) if stored else None
        except (StateMachineError, ValueError, TypeError):
            event, evidence = None, None
        valid = event is not None and _evidence_v4(evidence, event, binding)
        known_empty = (
            event is not None
            and not event.signals
            and isinstance(evidence, dict)
            and evidence.get("schema")
            in ("event-evidence/1", "event-evidence/2", "event-evidence/3")
        )
        scopes = {}
        if valid:
            for signal in event.signals:
                for npc in evidence["discovery"]["by_signal"][signal.signal_id]:
                    scopes.setdefault(npc, []).append(signal.signal_id)
        elif not known_empty:
            reason = "unproven_event_evidence"
            if isinstance(evidence, dict):
                phase = evidence.get("phase")
                discovery = evidence.get("discovery")
                if isinstance(phase, dict) and phase.get("status") == "unresolved":
                    reason = phase.get("code")
                elif isinstance(discovery, dict) and discovery.get("reason"):
                    reason = discovery["reason"]
                elif evidence.get("schema") in (
                    "event-evidence/1",
                    "event-evidence/2",
                    "event-evidence/3",
                ):
                    reason = "legacy_candidates_or_stage_unknown"
            if type(reason) is not str or not 1 <= len(reason) <= 256:
                reason = "unproven_event_evidence"
            self._conn.execute(
                "INSERT INTO perception_event_barriers VALUES (?,?,?)", (sid, seq, reason)
            )
        if known_empty or valid and not event.signals:
            return
        # A legacy per-NPC result is completion evidence, unlike global Outbox Done.
        proofs = {
            r[0]
            for r in self._conn.execute(
                "SELECT p.observer_id FROM perception_results p JOIN actors a "
                "ON a.session_id=p.session_id AND a.actor_id=p.observer_id "
                "WHERE p.session_id=? AND p.event_seq=? AND p.complete=1 AND a.kind='npc'",
                (sid, seq),
            )
        }
        for npc in sorted(set(scopes) | proofs):
            ids = scopes.get(npc, [s.signal_id for s in event.signals] if event else [])
            self._conn.execute(
                "INSERT INTO npc_perception_queue VALUES (?,?,?,?,?,?)",
                (
                    sid,
                    npc,
                    seq,
                    canonical_json(ids),
                    "discovery" if npc in scopes else "legacy_projection",
                    int(npc in proofs),
                ),
            )

    def _migrate_perception_queues(self):
        for session in self._conn.execute("SELECT * FROM sessions").fetchall():
            binding = RulesetBinding(
                session["ruleset_id"], session["data_revision"], session["evaluator_version"]
            )
            for (seq,) in self._conn.execute(
                "SELECT event_seq FROM event_records WHERE session_id=? ORDER BY event_seq",
                (session["session_id"],),
            ).fetchall():
                self._enqueue_perception(session["session_id"], seq, binding)
            self._refresh_npc_perception(session["session_id"])

    def _refresh_npc_perception(self, sid):
        end = self._conn.execute(
            "SELECT event_seq FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()[0]
        for (npc,) in self._conn.execute(
            "SELECT actor_id FROM actors WHERE session_id=? AND kind='npc'", (sid,)
        ).fetchall():
            gap = self._conn.execute(
                "SELECT min(event_seq) FROM ("
                "SELECT event_seq FROM npc_perception_queue "
                "WHERE session_id=? AND npc_id=? AND complete=0 "
                "UNION ALL SELECT b.event_seq FROM perception_event_barriers b "
                "WHERE b.session_id=? "
                "AND NOT EXISTS (SELECT 1 FROM npc_perception_queue q "
                "WHERE q.session_id=b.session_id "
                "AND q.event_seq=b.event_seq AND q.npc_id=? AND q.complete=1))",
                (sid, npc, sid, npc),
            ).fetchone()[0]
            cursor = gap - 1 if gap is not None else end
            self._conn.execute(
                "INSERT INTO npc_perception_cursors VALUES (?,?,?) ON CONFLICT(session_id,npc_id) "
                "DO UPDATE SET event_seq=excluded.event_seq",
                (sid, npc, cursor),
            )

    def npc_perception_cursor(self, session_id, package, *, context):
        """Own NPC's projection prefix, distinct from cognition completion and TTL."""
        with self._transaction():
            npc = self._npc_authorized(session_id, package, context)
            row = self._conn.execute(
                "SELECT event_seq FROM npc_perception_cursors WHERE session_id=? AND npc_id=?",
                (session_id, npc),
            ).fetchone()
            return row[0] if row else 0

    def get_npc_perception_queue(self, session_id, package, *, context):
        """Authorized worker metadata only; never exposes Event payload or Evidence."""
        with self._transaction():
            npc = self._npc_authorized(session_id, package, context)
            return tuple(
                NPCPerceptionEvent(
                    r["event_seq"],
                    tuple(json.loads(r["signal_ids_json"])),
                    bool(r["complete"]),
                    r["provenance"],
                )
                for r in self._conn.execute(
                    "SELECT * FROM npc_perception_queue "
                    "WHERE session_id=? AND npc_id=? ORDER BY event_seq",
                    (session_id, npc),
                )
            )
