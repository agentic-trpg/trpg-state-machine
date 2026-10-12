"""Committed facts, bounded package effects and one durable delivery outbox."""

import json
from dataclasses import asdict

from .errors import StateMachineError
from .events import CommittedWorldEvent, _StagedEvent
from .package import (
    AdventurePackage,
    Signal,
    SignalOrigin,
    SignalScope,
    SignalStimulus,
    canonical_json,
)
from .perception import _evidence_v4


def _enqueue_perception_proof(connection, read_event, sid, seq, binding):
    """Only at event commit or locked migration; never discover from current state."""
    boundary = connection.execute(
        "SELECT through_event_seq FROM legacy_perception_boundaries WHERE session_id=?",
        (sid,),
    ).fetchone()
    if boundary and seq <= boundary[0]:
        return
    try:
        event = read_event(sid, seq)
        stored = connection.execute(
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
        and evidence.get("schema") in ("event-evidence/1", "event-evidence/2", "event-evidence/3")
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
        connection.execute(
            "INSERT INTO perception_event_barriers VALUES (?,?,?)", (sid, seq, reason)
        )
    if known_empty or valid and not event.signals:
        return
    # A legacy per-NPC result is completion evidence, unlike global Outbox Done.
    proofs = {
        r[0]
        for r in connection.execute(
            "SELECT p.observer_id FROM perception_results p JOIN actors a "
            "ON a.session_id=p.session_id AND a.actor_id=p.observer_id "
            "WHERE p.session_id=? AND p.event_seq=? AND p.complete=1 AND a.kind='npc'",
            (sid, seq),
        )
    }
    for npc in sorted(set(scopes) | proofs):
        ids = scopes.get(npc, [s.signal_id for s in event.signals] if event else [])
        connection.execute(
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


class EventPersistence:
    def __init__(self, store):
        self.store = store

    @property
    def _conn(self):
        return self.store.connection

    def _append_events(
        self,
        session_id: str,
        version: int,
        events: list[_StagedEvent],
        *,
        source_command_id: str,
        package: AdventurePackage,
    ) -> None:
        self.store.require_write()
        # Evidence has already been frozen at the producer stage. This method
        # assigns durable identities and writes; it never samples world state.
        seq = self._conn.execute(
            "SELECT event_seq FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        for event in events:
            if type(event) is not _StagedEvent:
                raise StateMachineError(
                    "invalid_event_evidence", "Producer must supply staged evidence"
                )
            seq += 1
            evidence = json.loads(event.evidence_json)
            evidence["world_version"] = version
            self._conn.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (session_id, seq, event.event_type, version, canonical_json(event.payload)),
            )
            self._conn.execute(
                "INSERT INTO event_evidence VALUES (?, ?, ?)",
                (session_id, seq, canonical_json(evidence)),
            )
            self._conn.execute(
                "INSERT INTO event_records VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    seq,
                    f"event.{session_id}.{seq}",
                    source_command_id,
                    canonical_json([asdict(signal) for signal in event.signals]),
                ),
            )
            self._conn.executemany(
                "INSERT INTO event_outbox(session_id,event_seq,consumer) VALUES (?, ?, ?)",
                [(session_id, seq, "delivery"), (session_id, seq, "perception")],
            )
            self._enqueue_perception(session_id, seq, package.ruleset_binding)
        self._conn.execute(
            "UPDATE sessions SET event_seq = ? WHERE session_id = ?", (seq, session_id)
        )

    def _read_event(self, session_id, seq):
        row = self._conn.execute(
            "SELECT e.*, r.event_id, r.source_command_id, r.signals_json FROM events e "
            "JOIN event_records r USING(session_id,event_seq) WHERE session_id=? AND event_seq=?",
            (session_id, seq),
        ).fetchone()
        if row is None:
            raise StateMachineError("event_not_found", "Unknown committed event")
        try:
            data = json.loads(row["signals_json"])
            if type(data) is not list or any(
                type(s) is not dict
                or set(s) - {"signal_id", "channel", "content", "scope", "origin", "stimulus"}
                for s in data
            ):
                raise ValueError("Unknown Signal fields")
            signals = tuple(
                Signal(
                    s["signal_id"],
                    s["channel"],
                    s["content"],
                    SignalScope(**s["scope"]),
                    SignalOrigin(**s["origin"]) if s.get("origin") is not None else None,
                    SignalStimulus(**s["stimulus"]) if s.get("stimulus") is not None else None,
                )
                for s in data
            )
            if any(
                type(s.signal_id) is not str
                or type(s.content) is not str
                or s.scope.kind != "scene"
                or type(s.scope.scene_id) is not str
                or s.scope.visibility not in ("clear", "blocked", "undetermined")
                or s.channel not in ("visual", "auditory")
                or s.origin is not None
                and (
                    s.origin.scene_id != s.scope.scene_id
                    or any(
                        v is not None and type(v) is not str
                        for v in (s.origin.entity_id, s.origin.cell)
                    )
                )
                or s.stimulus is not None
                and s.stimulus.kind
                not in (
                    ("sound",)
                    if s.channel == "auditory"
                    else ("ordinary_activity", "light_emission")
                )
                for s in signals
            ) or len({s.signal_id for s in signals}) != len(signals):
                raise ValueError("Unsupported stored scope/channel")
        except (KeyError, TypeError, ValueError) as exc:
            raise StateMachineError("invalid_event", "Unsupported stored Signal schema") from exc
        return CommittedWorldEvent(
            row["event_id"],
            seq,
            row["source_command_id"],
            row["event_type"],
            row["world_version"],
            json.loads(row["payload_json"]),
            signals,
        )

    def _enqueue_perception(self, sid, seq, binding):
        self.store.require_write()
        return _enqueue_perception_proof(self._conn, self._read_event, sid, seq, binding)
