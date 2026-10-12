"""Committed facts, bounded package effects and one durable delivery outbox."""

from dataclasses import dataclass

from .errors import StateMachineError
from .package import Signal


@dataclass(frozen=True, slots=True)
class CommittedWorldEvent:
    event_id: str
    event_seq: int
    source_command_id: str
    event_type: str
    world_version: int
    payload: dict
    signals: tuple[Signal, ...]


@dataclass(frozen=True, slots=True)
class _StagedEvent:
    """Transaction-local candidate, never an additional authoritative state."""

    event_type: str
    payload: dict
    signals: tuple[Signal, ...]
    evidence_json: str


class EventRuntime:
    def _capture_event(
        self, sid, package, event_type, payload, *, phase, source_actor_id=None, unresolved=False
    ):
        return self._event_capture._capture_event(
            sid,
            package,
            event_type,
            payload,
            phase=phase,
            source_actor_id=source_actor_id,
            unresolved=unresolved,
        )

    def _expand_events(self, row, package, events, command_id):
        return self._package_effects._expand_events(row, package, events, command_id)

    def _read_event(self, session_id, seq):
        return self._event_persistence._read_event(session_id, seq)

    def read_events(self, session_id, package, *, context, after_seq=0):
        if type(after_seq) is not int or after_seq < 0:
            raise StateMachineError("invalid_cursor", "Expected nonnegative event cursor")
        with self._transaction():
            self._session(session_id, package, context)
            seqs = self._conn.execute(
                "SELECT event_seq FROM events WHERE session_id=? "
                "AND event_seq>? ORDER BY event_seq",
                (session_id, after_seq),
            )
            return tuple(self._read_event(session_id, row[0]) for row in seqs.fetchall())

    def _complete_work(self, session_id, seq, consumer):
        self._conn.execute(
            "UPDATE event_outbox SET done=1, last_error=NULL "
            "WHERE session_id=? AND event_seq=? AND consumer=?",
            (session_id, seq, consumer),
        )
        row = self._conn.execute(
            "SELECT MIN(event_seq) FROM event_outbox WHERE session_id=? AND consumer=? AND done=0 "
            "AND (consumer!='perception' OR event_seq>coalesce("
            "(SELECT through_event_seq FROM legacy_perception_boundaries WHERE session_id=?),0))",
            (session_id, consumer, session_id),
        ).fetchone()
        end = self._conn.execute(
            "SELECT event_seq FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        cursor = row[0] - 1 if row[0] is not None else end
        self._conn.execute(
            "INSERT INTO projection_cursors VALUES (?, ?, ?) "
            "ON CONFLICT(session_id,consumer) DO UPDATE SET event_seq=excluded.event_seq",
            (session_id, consumer, cursor),
        )

    def delivery_cursor(self, session_id, package, *, context):
        with self._transaction():
            self._session(session_id, package, context)
            row = self._conn.execute(
                "SELECT event_seq FROM projection_cursors "
                "WHERE session_id=? AND consumer='delivery'",
                (session_id,),
            ).fetchone()
            return row[0] if row else 0

    def deliver_event(self, session_id, event_seq, package, *, context, sink):
        """At-least-once callback outside SQLite transaction; sink dedupes event_id.

        Completion records durable delivery only, never completed NPC cognition.
        Out-of-order delivery is permitted but the cursor cannot cross an older gap.
        """
        if type(event_seq) is not int or event_seq <= 0:
            raise StateMachineError("invalid_event", "Expected a positive event sequence")
        with self._transaction(write=True):
            self._session(session_id, package, context)
            work = self._conn.execute(
                "SELECT done FROM event_outbox WHERE session_id=? "
                "AND event_seq=? AND consumer='delivery'",
                (session_id, event_seq),
            ).fetchone()
            if work is None:
                raise StateMachineError("event_not_found", "Unknown delivery work")
            if work[0]:
                return False
            event = self._read_event(session_id, event_seq)
            self._conn.execute(
                "UPDATE event_outbox SET attempts=attempts+1 WHERE session_id=? "
                "AND event_seq=? AND consumer='delivery'",
                (session_id, event_seq),
            )
        try:
            sink(event)
        except Exception:
            with self._transaction(write=True):
                self._conn.execute(
                    "UPDATE event_outbox SET last_error='delivery_failed' "
                    "WHERE session_id=? AND event_seq=? AND consumer='delivery'",
                    (session_id, event_seq),
                )
            raise
        with self._transaction(write=True):
            self._session(session_id, package, context)
            self._complete_work(session_id, event_seq, "delivery")
        return True
