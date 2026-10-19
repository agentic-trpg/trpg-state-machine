"""Event-time projections; no Agent activation, belief, goal, EVA or cognition ACK."""

import hashlib
import json
from dataclasses import asdict, dataclass

from .errors import StateMachineError
from .models import CharacterState
from .npc_state import KnownFact
from .package import AdventurePackage, Signal, canonical_json
from .perception_conditions import (
    PerceptionOutcome,
    discover_signals,
    judge_signal,
    signal_environment,
)


@dataclass(frozen=True, slots=True)
class GameViewContext:
    """Adapter-authenticated role; never construct from untrusted request fields.

    The current local adapter must bind the Session owner's principal/player to
    gm, player or narrator. This grants reads only, never command control.
    """

    principal_id: str
    session_id: str
    player_id: str
    role: str


@dataclass(frozen=True, slots=True)
class GameSceneView:
    schema_version: str
    role: str
    session_id: str
    scene_id: str
    view_world_version: int
    status: str
    visible_actor_ids: tuple[str, ...]
    visible_objects: tuple[tuple[str, str], ...]
    own_character: CharacterState | None
    gm_facts: tuple[KnownFact, ...]
    gm_scene_flags: tuple[tuple[str, bool], ...]


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
    view_world_version: int = 0


@dataclass(frozen=True, slots=True)
class JudgmentRequest:
    observer_id: str
    event_ref: str
    scene_id: str
    senses: tuple[tuple[str, bool], ...]
    signals: tuple[Signal, ...]
    evidence: dict | None = None


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


def _valid_scene_evidence(scene, schema):
    basic = {"grid", "unsupported_effects"}
    extended = basic | {"light_uncertain_cells", "lighting_sources"}
    if set(scene) == basic:
        return schema != "event-evidence/6"
    # Explicit compatibility for the W7 producer's extended /5 records.
    if schema not in ("event-evidence/5", "event-evidence/6") or set(scene) != extended:
        return False
    uncertain, sources = scene["light_uncertain_cells"], scene["lighting_sources"]
    if type(uncertain) is not list or any(type(c) is not str or not c for c in uncertain):
        return False
    if len(uncertain) != len(set(uncertain)) or type(sources) is not dict:
        return False
    return all(
        type(cell) is str
        and bool(cell)
        and type(ids) is list
        and all(type(oid) is str and oid.startswith("object.") for oid in ids)
        and len(ids) == len(set(ids))
        for cell, ids in sources.items()
    )


def _evidence_v4(evidence, event, binding):
    if not isinstance(evidence, dict) or set(evidence) != {
        "schema",
        "world_version",
        "game_tick",
        "ruleset_binding",
        "actors",
        "scenes",
        "objects",
        "source_actors",
        "phase",
        "discovery",
    }:
        return False
    if (
        evidence["schema"] not in ("event-evidence/4", "event-evidence/5", "event-evidence/6")
        or type(evidence["world_version"]) is not int
        or evidence["world_version"] != event.world_version
        or type(evidence["game_tick"]) is not int
        or evidence["game_tick"] < 0
        or evidence["ruleset_binding"] != asdict(binding)
        or type(evidence["actors"]) is not list
        or type(evidence["scenes"]) is not dict
        or type(evidence["objects"]) is not dict
        or type(evidence["source_actors"]) is not list
    ):
        return False
    phase, discovery = evidence["phase"], evidence["discovery"]
    if (
        type(phase) is not dict
        or set(phase) != {"status", "code"}
        or phase["status"] != "captured"
        or type(phase["code"]) is not str
        or type(discovery) is not dict
        or set(discovery) != {"complete", "by_signal", "reason"}
        or discovery["complete"] is not True
        or discovery["reason"] is not None
        or type(discovery["by_signal"]) is not dict
        or set(discovery["by_signal"]) != {s.signal_id for s in event.signals}
    ):
        return False
    ids = set()
    for actor in [*evidence["actors"], *evidence["source_actors"]]:
        if (
            type(actor) is not dict
            or set(actor)
            != {"actor_id", "kind", "current_scene_id", "senses", "mechanical"}
            | (
                {"cell"}
                if evidence["schema"] in ("event-evidence/5", "event-evidence/6")
                else set()
            )
            or any(type(actor[k]) is not str for k in ("actor_id", "kind", "current_scene_id"))
            or actor.get("cell") is not None
            and type(actor["cell"]) is not str
        ):
            return False
        if actor["actor_id"] in ids:
            return False
        ids.add(actor["actor_id"])
        senses, mechanical = actor["senses"], actor["mechanical"]
        if senses is not None and (
            type(senses) is not dict
            or set(senses) != {"visual", "auditory"}
            or any(type(v) is not bool for v in senses.values())
        ):
            return False
        if mechanical is not None and (
            type(mechanical) is not dict
            or set(mechanical)
            != {"conditions", "senses", "traits", "cell", "is_alive", "dead_recorded"}
            or type(mechanical["traits"]) is not list
            or type(mechanical["conditions"]) is not list
            or any(type(c) is not str for c in mechanical["conditions"])
            or type(mechanical["is_alive"]) is not bool
            or type(mechanical["dead_recorded"]) is not bool
            or mechanical["cell"] is not None
            and type(mechanical["cell"]) is not str
        ):
            return False
    if not all(
        type(s) is dict
        and _valid_scene_evidence(s, evidence["schema"])
        and type(s["unsupported_effects"]) is bool
        for s in evidence["scenes"].values()
    ):
        return False
    if any(a["kind"] != "npc" for a in evidence["actors"]):
        return False
    candidates = set()
    for signal in event.signals:
        discovered = discovery["by_signal"][signal.signal_id]
        expected = {
            a["actor_id"] for a in evidence["actors"] if signal in discover_signals((signal,), a)
        }
        if (
            type(discovered) is not list
            or any(type(i) is not str for i in discovered)
            or len(set(discovered)) != len(discovered)
            or set(discovered) != expected
        ):
            return False
        candidates.update(discovered)
    if candidates != {a["actor_id"] for a in evidence["actors"]} or set(evidence["scenes"]) != {
        s.scope.scene_id for s in event.signals
    }:
        return False
    origins = {s.origin.entity_id for s in event.signals if s.origin and s.origin.entity_id}
    if (
        not {a["actor_id"] for a in evidence["source_actors"]} <= origins
        or not set(evidence["objects"]) <= origins
    ):
        return False
    return all(
        type(o) is dict
        and set(o)
        == {"scene_id", "state"}
        | ({"cell"} if evidence["schema"] in ("event-evidence/5", "event-evidence/6") else set())
        and type(o["scene_id"]) is str
        and o["state"]
        in (
            (None, "closed", "open")
            if evidence["schema"] in ("event-evidence/5", "event-evidence/6")
            else ("closed", "open")
        )
        and (o.get("cell") is None or type(o["cell"]) is str)
        for o in evidence["objects"].values()
    )


class PerceptionRuntime:
    def grant_npc_access(self, session_id, observer_id, principal_id, package, *, context):
        if type(principal_id) is not str or not principal_id.strip() or len(principal_id) > 256:
            raise StateMachineError("invalid_context", "Expected authenticated NPC principal")
        with self._transaction(write=True):
            self._session(session_id, package, context)
            self._sessions.require_legacy_npc_time(session_id)
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
        return self._sessions._npc_authorized(session_id, package, context)

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
        """Owner-compatible aggregate projection; global completion is audit only."""
        return self._project_perception(session_id, event_seq, package, context, judgment, None)

    def project_npc_event(self, session_id, event_seq, package, *, context, judgment=None):
        """Project one authorized NPC; never judges or completes another NPC."""
        if type(context) is not NPCContext:
            raise StateMachineError("unauthorized", "Expected an authenticated NPC worker")
        return self._project_perception(
            session_id, event_seq, package, context, judgment, context.observer_id
        )

    def _projection_authorized(self, sid, package, context, npc):
        if npc is None:
            self._session(sid, package, context)
            self._sessions.require_legacy_npc_time(sid)
        else:
            self._npc_authorized(sid, package, context)

    def _project_perception(self, session_id, event_seq, package, context, judgment, npc):
        """Return resolved/unresolved/already_complete/legacy_unavailable/not_candidate.

        Judgment receives only one observer/event's undecidable Signals and evidence,
        outside the write transaction. Immutable event-time evidence remains authority.
        """
        from .session_access import _binding

        if type(event_seq) is not int or event_seq <= 0:
            raise StateMachineError("invalid_event", "Expected a positive event sequence")
        with self._transaction(write=True):
            self._projection_authorized(session_id, package, context, npc)
            binding = _binding(
                self._conn.execute(
                    "SELECT * FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
            )
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
            queued = {
                r["npc_id"]: r
                for r in self._conn.execute(
                    "SELECT * FROM npc_perception_queue WHERE session_id=? AND event_seq=?",
                    (session_id, event_seq),
                )
            }
            if npc in queued and queued[npc]["complete"]:
                return "already_complete"
            if npc is None and work[0]:
                return "already_complete"
            event = self._read_event(session_id, event_seq)
            saved = self._conn.execute(
                "SELECT evidence_json FROM event_evidence WHERE session_id=? AND event_seq=?",
                (session_id, event_seq),
            ).fetchone()
            try:
                evidence = json.loads(saved[0]) if saved else None
            except (TypeError, ValueError):
                evidence = None
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
        # Historical incomplete projections lack mechanical evidence. Never
        # consult current state or reuse unsafe legacy partial positive answers.
        # Already completed historical OB/receipts remain immutable audit data.
        if not _evidence_v4(evidence, event, binding):
            if (
                isinstance(evidence, dict)
                and evidence.get("schema")
                in ("event-evidence/1", "event-evidence/2", "event-evidence/3")
                and not event.signals
            ):
                with self._transaction(write=True):
                    self._projection_authorized(session_id, package, context, npc)
                    self._complete_work(session_id, event_seq, "perception")
                return "resolved"
            return "unresolved"
        expected = {a["actor_id"] for a in evidence["actors"]}
        if set(queued) != expected or any(
            json.loads(queued[observer]["signal_ids_json"])
            != [
                s.signal_id
                for s in event.signals
                if observer in evidence["discovery"]["by_signal"][s.signal_id]
            ]
            for observer in expected
        ):
            return "unresolved"
        if npc is not None and npc not in queued:
            return "not_candidate"
        computed = {}
        for observer in evidence["actors"]:
            if observer["kind"] != "npc":
                continue
            observer_id = observer["actor_id"]
            if (npc is not None and observer_id != npc) or queued[observer_id]["complete"]:
                continue
            saved = prior.get(observer_id, ({}, False))[0]
            decisions = (
                dict(saved.get("answers", {}))
                if saved.get("schema") == "perception-decisions/2"
                else {}
            )
            reasons = (
                dict(saved.get("reasons", {}))
                if saved.get("schema") == "perception-decisions/2"
                else {}
            )
            candidates = {
                signal_id
                for signal_id, ids in evidence["discovery"]["by_signal"].items()
                if observer_id in ids
            }
            undecidable = []
            environments = {}
            for signal in event.signals:
                if signal.signal_id in decisions:
                    continue
                if signal.signal_id not in candidates:
                    decisions[signal.signal_id] = False
                    reasons[signal.signal_id] = {
                        "state": "negative",
                        "reason": "outside_scene_scope",
                    }
                    continue
                environment = signal_environment(signal, evidence)
                decision = judge_signal(signal, observer, environment)
                reasons[signal.signal_id] = {
                    "state": decision.outcome.value,
                    "reason": decision.reason,
                }
                if decision.outcome != PerceptionOutcome.UNRESOLVED:
                    decisions[signal.signal_id] = decision.outcome == PerceptionOutcome.POSITIVE
                elif decision.judgment_allowed:
                    undecidable.append(signal)
                    environments[signal.signal_id] = environment
            if undecidable:
                answers = _judge(
                    JudgmentRequest(
                        observer_id,
                        event.event_id,
                        observer["current_scene_id"],
                        tuple(sorted(observer["senses"].items())),
                        tuple(undecidable),
                        json.loads(
                            canonical_json(
                                {
                                    "schema": "perception-judgment-evidence/1",
                                    "observer": observer,
                                    "environments": environments,
                                    "reasons": {
                                        s.signal_id: reasons[s.signal_id] for s in undecidable
                                    },
                                    "ruleset_binding": evidence["ruleset_binding"],
                                    "world_version": evidence["world_version"],
                                    "game_tick": evidence["game_tick"],
                                }
                            )
                        ),
                    ),
                    judgment,
                )
                if answers is not None:
                    decisions.update(answers)
                    for signal_id, answer in answers.items():
                        reasons[signal_id] = {
                            "state": "positive" if answer else "negative",
                            "reason": "validated_judgment",
                        }
            computed[observer_id] = (decisions, reasons)
        with self._transaction(write=True):
            self._projection_authorized(session_id, package, context, npc)
            for observer_id, (decisions, reasons) in computed.items():
                complete = len(decisions) == len(event.signals)
                existing = self._conn.execute(
                    "SELECT complete FROM perception_results "
                    "WHERE session_id=? AND event_seq=? AND observer_id=?",
                    (session_id, event_seq, observer_id),
                ).fetchone()
                if existing and existing[0]:
                    self._conn.execute(
                        "UPDATE npc_perception_queue SET complete=1 "
                        "WHERE session_id=? AND npc_id=? AND event_seq=?",
                        (session_id, observer_id, event_seq),
                    )
                    continue
                self._conn.execute(
                    "INSERT INTO perception_results VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id,event_seq,observer_id) DO UPDATE SET "
                    "decisions_json=excluded.decisions_json, complete=excluded.complete",
                    (
                        session_id,
                        event_seq,
                        observer_id,
                        canonical_json(
                            {
                                "schema": "perception-decisions/2",
                                "answers": decisions,
                                "reasons": reasons,
                            }
                        ),
                        int(complete),
                    ),
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
                if complete:
                    self._conn.execute(
                        "UPDATE npc_perception_queue SET complete=1 "
                        "WHERE session_id=? AND npc_id=? AND event_seq=?",
                        (session_id, observer_id, event_seq),
                    )
            pending = self._conn.execute(
                "SELECT 1 FROM npc_perception_queue "
                "WHERE session_id=? AND event_seq=? AND complete=0",
                (session_id, event_seq),
            ).fetchone()
            barrier = self._conn.execute(
                "SELECT 1 FROM perception_event_barriers WHERE session_id=? AND event_seq=?",
                (session_id, event_seq),
            ).fetchone()
            if not pending and not barrier:
                self._complete_work(session_id, event_seq, "perception")
            if npc is not None:
                return (
                    "resolved"
                    if self._conn.execute(
                        "SELECT complete FROM npc_perception_queue "
                        "WHERE session_id=? AND npc_id=? AND event_seq=?",
                        (session_id, npc, event_seq),
                    ).fetchone()[0]
                    else "unresolved"
                )
        return "unresolved" if pending or barrier else "resolved"

    def read_observations(self, session_id, package, *, context):
        """Reads the durable inbox; read does not acknowledge cognition or delete it."""
        with self._transaction():
            observer_id = self._npc_authorized(session_id, package, context)
            end = self._npc_perception_cursor(session_id, observer_id)
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
        if type(context) is GameViewContext:
            return self._game_scene_view(session_id, package, context)
        with self._transaction():
            observer_id = self._npc_authorized(session_id, package, context)
            actor = self._conn.execute(
                "SELECT current_scene_id FROM actors WHERE session_id=? AND actor_id=?",
                (session_id, observer_id),
            ).fetchone()
            scene_id = actor[0]
            definition = self._world_facts.instances.actor(session_id, observer_id)
            version = self._conn.execute(
                "SELECT world_version FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            clear = self._scene_perception_clear(session_id, scene_id)
            if definition.senses is None or not clear:
                return CurrentPerceptualView(observer_id, scene_id, "unresolved", (), (), version)
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
            return CurrentPerceptualView(
                observer_id, scene_id, "resolved", actors, objects, version
            )

    def _scene_perception_clear(self, sid, scene):
        """No mechanical visibility calculation: unsupported environments stay unknown."""
        stored = self._world_facts.mechanical_scene(sid, scene)
        if stored:
            grid = stored["grid"]
            if grid["default_lighting"] != "bright" or any(
                grid[key]
                for key in (
                    "lighting",
                    "obscurement_cells",
                    "wall_segments",
                    "blocked_cells",
                    "cover_cells",
                )
            ):
                return False
            states = self._conn.execute(
                "SELECT m.state_json FROM mechanical_actors m JOIN actors a "
                "USING(session_id,actor_id) WHERE m.session_id=? AND a.current_scene_id=?",
                (sid, scene),
            ).fetchall()
            if any(json.loads(r[0])["conditions"] for r in states):
                return False
        return True

    def _game_scene_view(self, sid, package, context):
        if (
            context.session_id != sid
            or context.role not in ("gm", "player", "narrator")
            or not isinstance(package, AdventurePackage)
        ):
            raise StateMachineError("unauthorized", "Invalid scoped game view context")
        with self._transaction():
            row = self._conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
            if row is None or (row["principal_id"], row["player_id"]) != (
                context.principal_id,
                context.player_id,
            ):
                raise StateMachineError("unauthorized", "No authorized Session view")
            self._check_package_pin(row, package)
            gm = context.role == "gm"
            scene = row["current_scene_id"]
            clear = gm or self._scene_perception_clear(sid, scene)
            actors = (
                tuple(
                    r[0]
                    for r in self._conn.execute(
                        "SELECT actor_id FROM actors WHERE session_id=? AND current_scene_id=? "
                        "ORDER BY actor_id",
                        (sid, scene),
                    )
                )
                if clear
                else ()
            )
            objects = (
                tuple(
                    tuple(r)
                    for r in self._conn.execute(
                        "SELECT object_id,state FROM objects WHERE session_id=? AND scene_id=? "
                        "ORDER BY object_id",
                        (sid, scene),
                    )
                )
                if clear
                else ()
            )
            own = None
            if context.role == "player":
                character = self._conn.execute(
                    "SELECT * FROM characters WHERE session_id=? AND actor_id=?",
                    (sid, row["pc_actor_id"]),
                ).fetchone()
                own = CharacterState(
                    row["pc_actor_id"],
                    character["current_hp"],
                    character["max_hp"],
                    json.loads(character["resources_json"]),
                    tuple(json.loads(character["inventory_json"])),
                    tuple(json.loads(character["effects_json"])),
                    character["xp"],
                )
            facts = (
                tuple(
                    KnownFact(r[0], r[1])
                    for r in self._conn.execute(
                        "SELECT fact_id,text FROM world_facts WHERE session_id=? ORDER BY fact_id",
                        (sid,),
                    )
                )
                if gm
                else ()
            )
            flags = (
                tuple(
                    sorted(
                        json.loads(
                            self._conn.execute(
                                "SELECT flags_json FROM scene_instances "
                                "WHERE session_id=? AND scene_id=?",
                                (sid, scene),
                            ).fetchone()[0]
                        ).items()
                    )
                )
                if gm
                else ()
            )
            return GameSceneView(
                "game-view/1",
                context.role,
                sid,
                scene,
                row["world_version"],
                "resolved" if clear else "unresolved",
                actors,
                objects,
                own,
                facts,
                flags,
            )
