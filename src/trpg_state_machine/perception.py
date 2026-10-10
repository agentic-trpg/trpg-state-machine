"""Event-time projections; no Agent activation, belief, goal, EVA or cognition ACK."""

import hashlib
import json
from dataclasses import asdict, dataclass

from .errors import StateMachineError
from .package import Signal, canonical_json


@dataclass(frozen=True, slots=True)
class NPCContext:
    """Authenticated principal from adapter; access also requires persisted owner grant."""

    principal_id: str
    session_id: str
    observer_id: str


@dataclass(frozen=True, slots=True)
class PerceivedSignal:
    channel: str
    content: str


@dataclass(frozen=True, slots=True)
class EventObservation:
    observation_id: str
    observer_id: str
    perceived_signals: tuple[PerceivedSignal, ...]


@dataclass(frozen=True, slots=True)
class CurrentPerceptualView:
    observer_id: str
    scene_id: str
    status: str
    visible_actor_ids: tuple[str, ...]
    visible_objects: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class JudgmentRequest:
    observer_id: str
    event_ref: str
    scene_id: str
    senses: tuple[tuple[str, bool], ...]
    signals: tuple[Signal, ...]


@dataclass(frozen=True, slots=True)
class SignalJudgment:
    signal_id: str
    perceived: bool


@dataclass(frozen=True, slots=True)
class JudgmentResult:
    judgments: tuple[SignalJudgment, ...]


def _judge(request, judgment):
    """All requested IDs exactly once; malformed/timeout never becomes false."""
    if judgment is None:
        return None
    try:
        result = judgment(request)
    except Exception:
        return None
    if type(result) is not JudgmentResult or type(result.judgments) is not tuple:
        return None
    ids = [s.signal_id for s in request.signals]
    answers = {}
    for item in result.judgments:
        if (
            type(item) is not SignalJudgment
            or type(item.signal_id) is not str
            or item.signal_id not in ids
            or item.signal_id in answers
            or type(item.perceived) is not bool
        ):
            return None
        answers[item.signal_id] = item.perceived
    return answers if set(answers) == set(ids) else None


class PerceptionRuntime:
    def grant_npc_access(self, session_id, observer_id, principal_id, package, *, context):
        if type(principal_id) is not str or not principal_id.strip() or len(principal_id) > 256:
            raise StateMachineError("invalid_context", "Expected authenticated NPC principal")
        with self._transaction(write=True):
            self._session(session_id, package, context)
            actor = self._conn.execute(
                "SELECT kind FROM actors WHERE session_id=? AND actor_id=?",
                (session_id, observer_id),
            ).fetchone()
            if actor is None or actor[0] != "npc":
                raise StateMachineError(
                    "invalid_actor", "Access can only be granted to an existing NPC"
                )
            self._conn.execute(
                "INSERT INTO npc_access VALUES (?, ?, ?) "
                "ON CONFLICT(session_id,observer_id) DO UPDATE SET "
                "principal_id=excluded.principal_id",
                (session_id, observer_id, principal_id),
            )

    def _npc_authorized(self, session_id, package, context):
        if (
            type(context) is not NPCContext
            or context.session_id != session_id
            or any(
                type(v) is not str or not v
                for v in (context.principal_id, context.session_id, context.observer_id)
            )
        ):
            raise StateMachineError("unauthorized", "Expected a session-bound NPC context")
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        grant = self._conn.execute(
            "SELECT principal_id FROM npc_access WHERE session_id=? AND observer_id=?",
            (session_id, context.observer_id),
        ).fetchone()
        if row is None or grant is None or grant[0] != context.principal_id:
            raise StateMachineError("unauthorized", "No NPC observation access grant")
        # Use the owner check only for package pins, never expose its snapshot to NPC.
        from .models import TrustedContext

        self._session(session_id, package, TrustedContext(row["principal_id"], row["player_id"]))
        return context.observer_id

    def perception_cursor(self, session_id, package, *, context):
        with self._transaction():
            self._session(session_id, package, context)
            row = self._conn.execute(
                "SELECT event_seq FROM projection_cursors "
                "WHERE session_id=? AND consumer='perception'",
                (session_id,),
            ).fetchone()
            return row[0] if row else 0

    def project_event(self, session_id, event_seq, package, *, context, judgment=None):
        """Return resolved/unresolved/already_complete/legacy_unavailable.

        Judgment receives only one observer/event's undecidable Signals and evidence,
        outside the write transaction. Immutable event-time evidence remains authority.
        """
        if type(event_seq) is not int or event_seq <= 0:
            raise StateMachineError("invalid_event", "Expected a positive event sequence")
        with self._transaction(write=True):
            self._session(session_id, package, context)
            work = self._conn.execute(
                "SELECT done FROM event_outbox WHERE session_id=? "
                "AND event_seq=? AND consumer='perception'",
                (session_id, event_seq),
            ).fetchone()
            if work is None:
                raise StateMachineError("event_not_found", "Unknown perception work")
            boundary = self._conn.execute(
                "SELECT through_event_seq FROM legacy_perception_boundaries WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if boundary is not None and event_seq <= boundary[0]:
                return "legacy_unavailable"
            if work[0]:
                return "already_complete"
            event = self._read_event(session_id, event_seq)
            saved = self._conn.execute(
                "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
                (session_id, event_seq),
            ).fetchone()
            evidence = json.loads(saved[0]) if saved else None
            self._conn.execute(
                "UPDATE event_outbox SET attempts=attempts+1 WHERE session_id=? "
                "AND event_seq=? AND consumer='perception'",
                (session_id, event_seq),
            )
            prior = {
                r["observer_id"]: (json.loads(r["decisions_json"]), r["complete"])
                for r in self._conn.execute(
                    "SELECT * FROM perception_results WHERE session_id=? AND event_seq=?",
                    (session_id, event_seq),
                )
            }
        if evidence is None or (
            event.source_command_id == "legacy.unavailable"
            and evidence.get("schema") != "event-evidence/2"
        ):
            return "unresolved"
        computed = {}
        for observer in evidence["actors"]:
            if observer["kind"] != "npc":
                continue
            observer_id = observer["actor_id"]
            if observer_id in prior and prior[observer_id][1]:
                continue
            decisions = prior.get(observer_id, ({}, False))[0]
            senses = observer.get("senses")
            for signal in event.signals:
                if signal.signal_id in decisions:
                    continue
                if (
                    observer["current_scene_id"] != signal.scope.scene_id
                    or senses is not None
                    and not senses[signal.channel]
                    or signal.scope.visibility == "blocked"
                ):
                    decisions[signal.signal_id] = False
                elif senses is not None and signal.scope.visibility == "clear":
                    decisions[signal.signal_id] = True
            undecidable = tuple(s for s in event.signals if s.signal_id not in decisions)
            # Unknown senses are missing evidence, not a judgment question or false.
            if undecidable and senses is not None:
                answers = _judge(
                    JudgmentRequest(
                        observer_id,
                        event.event_id,
                        observer["current_scene_id"],
                        tuple(sorted(senses.items())),
                        undecidable,
                    ),
                    judgment,
                )
                if answers is not None:
                    decisions.update(answers)
            computed[observer_id] = decisions
        with self._transaction(write=True):
            self._session(session_id, package, context)
            for observer_id, decisions in computed.items():
                complete = len(decisions) == len(event.signals)
                existing = self._conn.execute(
                    "SELECT complete FROM perception_results "
                    "WHERE session_id=? AND event_seq=? AND observer_id=?",
                    (session_id, event_seq, observer_id),
                ).fetchone()
                if existing and existing[0]:
                    continue
                self._conn.execute(
                    "INSERT INTO perception_results VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id,event_seq,observer_id) DO UPDATE SET "
                    "decisions_json=excluded.decisions_json, complete=excluded.complete",
                    (session_id, event_seq, observer_id, canonical_json(decisions), int(complete)),
                )
                perceived = tuple(
                    PerceivedSignal(s.channel, s.content)
                    for s in event.signals
                    if decisions.get(s.signal_id) is True
                )
                if complete and perceived:
                    oid = (
                        "observation."
                        + hashlib.sha256(
                            f"{session_id}|{event.event_id}|{observer_id}|1".encode()
                        ).hexdigest()
                    )
                    self._conn.execute(
                        "INSERT OR IGNORE INTO event_observations VALUES (?, ?, ?, 1, ?, ?)",
                        (
                            session_id,
                            event_seq,
                            observer_id,
                            oid,
                            canonical_json([asdict(p) for p in perceived]),
                        ),
                    )
                    self._conn.execute(
                        "INSERT OR IGNORE INTO npc_inbox VALUES (?, ?, ?)",
                        (session_id, observer_id, oid),
                    )
                    self._conn.execute(
                        "INSERT OR IGNORE INTO npc_observation_work"
                        "(work_id,session_id,npc_id,observation_id) VALUES (?,?,?,?)",
                        ("work." + oid, session_id, observer_id, oid),
                    )
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO observation_signal_sources VALUES (?,?,?)",
                        [
                            (oid, event.event_id, s.signal_id)
                            for s in event.signals
                            if decisions.get(s.signal_id) is True
                        ],
                    )
            pending = self._conn.execute(
                "SELECT 1 FROM perception_results "
                "WHERE session_id=? AND event_seq=? AND complete=0",
                (session_id, event_seq),
            ).fetchone()
            if pending:
                return "unresolved"
            self._complete_work(session_id, event_seq, "perception")
        return "resolved"

    def read_observations(self, session_id, package, *, context):
        """Reads the durable inbox; read does not acknowledge cognition or delete it."""
        with self._transaction():
            observer_id = self._npc_authorized(session_id, package, context)
            cursor = self._conn.execute(
                "SELECT event_seq FROM projection_cursors WHERE session_id=? "
                "AND consumer='perception'",
                (session_id,),
            ).fetchone()
            end = cursor[0] if cursor else 0
            rows = self._conn.execute(
                "SELECT o.observation_id,o.content_json FROM event_observations o "
                "JOIN npc_inbox i USING(session_id,observer_id,observation_id) "
                "WHERE o.session_id=? AND o.observer_id=? AND o.event_seq<=? "
                "ORDER BY o.event_seq",
                (session_id, observer_id, end),
            ).fetchall()
            return tuple(
                EventObservation(
                    r[0], observer_id, tuple(PerceivedSignal(**s) for s in json.loads(r[1]))
                )
                for r in rows
            )

    def get_scene_view(self, session_id, package, *, context):
        from .models import TrustedContext

        if type(context) is TrustedContext:
            snapshot = self.read_session(session_id, package, context=context)
            return snapshot.scenes[snapshot.current_scene_id]
        with self._transaction():
            observer_id = self._npc_authorized(session_id, package, context)
            actor = self._conn.execute(
                "SELECT current_scene_id FROM actors WHERE session_id=? AND actor_id=?",
                (session_id, observer_id),
            ).fetchone()
            scene_id = actor[0]
            definition = next(a for a in package.actors if a.actor_id == observer_id)
            if definition.senses is None:
                return CurrentPerceptualView(observer_id, scene_id, "unresolved", (), ())
            can_see = dict(definition.senses)["visual"]
            actors = (
                tuple(
                    r[0]
                    for r in self._conn.execute(
                        "SELECT actor_id FROM actors "
                        "WHERE session_id=? AND current_scene_id=? ORDER BY actor_id",
                        (session_id, scene_id),
                    )
                )
                if can_see
                else ()
            )
            objects = (
                tuple(
                    (r[0], r[1])
                    for r in self._conn.execute(
                        "SELECT object_id,state FROM objects "
                        "WHERE session_id=? AND scene_id=? ORDER BY object_id",
                        (session_id, scene_id),
                    )
                )
                if can_see
                else ()
            )
            return CurrentPerceptualView(observer_id, scene_id, "resolved", actors, objects)
