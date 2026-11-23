"""Authorized current scene views and event-time perception projections."""

import hashlib
import json
from dataclasses import asdict, dataclass
from itertools import chain
from typing import NamedTuple

from dnd5e_engine.spatial import parse_cell

from .errors import StateMachineError
from .grid import cell_in_bounds
from .instance_data import passage_index
from .models import CharacterState
from .npc_state import KnownFact
from .package import AdventurePackage, Signal, canonical_json
from .perception_conditions import (
    PerceptionOutcome,
    discover_signals,
    judge_signal,
    signal_environment,
)
from .runtime_instances import resolve_field
from .visual_observer_state import sight_conditions_unknown
from .visual_target_state import target_is_invisible
from .world_lights import _corridor_cells


def _sight_conditions_unknown(characters, sid, actor):
    """Check the named actor for conditions outside the supported sight rules."""
    return sight_conditions_unknown(characters, sid, actor)


def _object_sight_unresolved(spatial, sid, target, package):
    """Check the object's live vision and total cover channels."""
    if spatial.config(sid, target) is None:
        return False
    projection = spatial.project(sid, target, package)
    return bool(projection.vision_occlusion_cells) or any(
        degree == "total" for _, degree in projection.cover_cells
    )


def visible_actor_in_range(world, sid, actor, target_id, target_cell, package, *, range_ft):
    """Prove disclosure from one authorized observer to a currently placed creature.

    The caller supplies the target cell from the same Store scope. Match scene
    view sight rules for the selected ray and its nearby fixtures. Unknown sight
    mechanics, out-of-range cells and unproven visibility have the same result.
    """
    try:
        position = world._public_scope(sid, actor, package)
        topology = world.grid.base_topology(sid, position.scene_id)
    except StateMachineError as exc:
        if exc.code in (
            "incomplete_session",
            "spatial_unconfigured",
            "unsupported_interaction_scope",
            "unsupported_target_visibility",
        ):
            return False
        raise
    if not (
        cell_in_bounds(topology, position.cell)
        and cell_in_bounds(topology, target_cell)
        and topology.within_range(position.cell, target_cell, range_ft)
    ):
        return False
    sight = world._prepare_visibility(sid, actor, position, package, cells=(target_cell,))
    if not sight.can_see_creature(
        target_cell, invisible=target_is_invisible(world, sid, target_id)
    ) or (actor != target_id and _sight_conditions_unknown(world.characters, sid, target_id)):
        return False

    corridor = frozenset(_corridor_cells(((position.cell, (parse_cell(target_cell),)),)))
    passages = passage_index(world.store, sid)
    doors = {
        door
        for cell in corridor
        for door in passages.doors_by_threshold.get((position.scene_id, cell), ())
    }
    spatial = world.objects.spatial
    if any(spatial.door_shell_closed(sid, door, package) for door in doors):
        return False
    _, occupants = world.grid.reader.region_occupants(sid, position.scene_id, corridor)
    return not any(
        _object_sight_unresolved(spatial, sid, fixture, package)
        for fixture in {oid for oid, _, _ in occupants} | doors
    )


class _SceneDisclosure(NamedTuple):
    """Retain only immutable current scene members admitted for one observer."""

    status: str
    actors: tuple[str, ...] = ()
    objects: tuple[tuple[str, str | None], ...] = ()

    def __deepcopy__(self, memo):
        """Share the recursively immutable disclosure across fixed reads."""
        return self


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
    """Expose an authorized game scene view with explicit environment support status."""

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
    detection_clues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NPCContext:
    """Authenticated principal from adapter; access also requires persisted owner grant."""

    principal_id: str
    session_id: str
    observer_id: str


@dataclass(frozen=True, slots=True)
class PerceivedSignal:
    """Carry a signal admitted into an observer's perception result."""

    channel: str
    content: str


@dataclass(frozen=True, slots=True)
class EventObservation:
    """Bind an NPC observation to committed event evidence and perceived signals."""

    observation_id: str
    observer_id: str
    perceived_signals: tuple[PerceivedSignal, ...]


@dataclass(frozen=True, slots=True)
class CurrentPerceptualView:
    """Expose the observer's currently authorized scene perception projection."""

    observer_id: str
    scene_id: str
    status: str
    visible_actor_ids: tuple[str, ...]
    visible_objects: tuple[tuple[str, str], ...]
    view_world_version: int = 0


@dataclass(frozen=True, slots=True)
class JudgmentRequest:
    """Bundle fixed evidence for a perception judgment request."""

    observer_id: str
    event_ref: str
    scene_id: str
    senses: tuple[tuple[str, bool], ...]
    signals: tuple[Signal, ...]
    evidence: dict | None = None


@dataclass(frozen=True, slots=True)
class SignalJudgment:
    """Record a judgment for one observer/signal pair."""

    signal_id: str
    perceived: bool


@dataclass(frozen=True, slots=True)
class JudgmentResult:
    """Group perception decisions for the fixed judgment request."""

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
    """Validate the version and shape of fixed scene perception evidence."""
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
    """Return whether event-stage evidence has a supported complete, consistent shape.

    Check schema-specific actor/object/source references, candidate identities and fixed
    scene evidence. Malformed or incomplete evidence returns False rather than deriving
    perception from current world state or filling missing historical facts.
    """
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
    """Authorize scene/perception views and manage observation-related access."""

    def grant_npc_access(self, session_id, observer_id, principal_id, package, *, context):
        """Authorize setting the NPC access-subject mapping."""
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
        """Delegate NPC access identity validation to SessionAccess."""
        return self._sessions._npc_authorized(session_id, package, context)

    def perception_cursor(self, session_id, package, *, context):
        """Authorize reading the perception consumer cursor."""
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
        """Check Session, time and observer permissions required by the projection."""
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
        """Read current scene disclosure for the authenticated role or NPC observer."""
        from .models import TrustedContext

        if type(context) is TrustedContext:
            snapshot = self.read_session(session_id, package, context=context)
            return snapshot.scenes[snapshot.current_scene_id]
        if type(context) is GameViewContext:
            return self._game_scene_view(session_id, package, context)
        with self._transaction():
            observer_id = self._npc_authorized(session_id, package, context)
            scene_id = self._world._actor_position(session_id, observer_id).scene_id
            version = self._conn.execute(
                "SELECT world_version FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            disclosure = self._observer_scene_disclosure(
                session_id, scene_id, observer_id, package, npc=True
            )
            return CurrentPerceptualView(observer_id, scene_id, *disclosure, version)

    def _observer_scene_disclosure(self, sid, scene, actor, package, *, npc=False):
        """Use fine exploration sight where enabled, and preserve legacy scene reads."""
        mode = self._conn.execute(
            "SELECT spatial_enabled FROM mechanical_scenes WHERE session_id=? AND scene_id=?",
            (sid, scene),
        ).fetchone()
        if mode and mode[0] and not self._active_combat(sid):
            if npc:
                definition = self._world_facts.instances.actor(sid, actor)
                if (
                    definition.senses is not None
                    and not dict(definition.senses)["visual"]
                    and self._characters.effective_senses(sid, actor).blindsight is None
                ):
                    return _SceneDisclosure("resolved")
            try:
                position = self._world._public_scope(sid, actor, package)
            except StateMachineError as exc:
                if exc.code not in (
                    "incomplete_session",
                    "unsupported_interaction_scope",
                    "unsupported_target_visibility",
                ):
                    raise
                return _SceneDisclosure("unresolved")
            if position.scene_id != scene or position.cell is None:
                return _SceneDisclosure("unresolved")
            return resolve_field(
                self._store,
                "actor",
                sid,
                actor,
                None,
                "scene_visibility",
                (position, package.package_digest),
                lambda: self._fine_scene_disclosure(sid, position, actor, package),
            )
        if not self._scene_perception_clear(sid, scene):
            return _SceneDisclosure("unresolved")
        if npc:
            definition = self._world_facts.instances.actor(sid, actor)
            if definition.senses is None:
                return _SceneDisclosure("unresolved")
            if not dict(definition.senses)["visual"]:
                return _SceneDisclosure("resolved")
        return self._unfiltered_scene_members(sid, scene)

    def _unfiltered_scene_members(self, sid, scene):
        """Read the scene membership used by the GM and clear legacy views."""
        actors = tuple(
            row[0]
            for row in self._conn.execute(
                "SELECT actor_id FROM actors WHERE session_id=? AND current_scene_id=? "
                "ORDER BY actor_id",
                (sid, scene),
            )
        )
        objects = tuple(
            tuple(row)
            for row in self._conn.execute(
                "SELECT object_id,state FROM objects WHERE session_id=? AND scene_id=? "
                "ORDER BY object_id",
                (sid, scene),
            )
        )
        return _SceneDisclosure("resolved", actors, objects)

    def _fine_scene_disclosure(self, sid, position, actor, package):
        """Collect bounded scene sight dependencies in the current Store candidate.

        Scene membership and target cells come from maintained ranges. The
        observer's current mechanical senses determine which members can be seen.
        The actor field retains the immutable filtered result independently of
        the public envelope, authorization and world version.

        Explicit object occlusion and total cover remain unresolved until dynamic
        sight composition is supported.
        """
        if _sight_conditions_unknown(self._characters, sid, actor):
            return _SceneDisclosure("unresolved")
        scene = position.scene_id
        actors = dict(
            tuple(row)
            for row in self._conn.execute(
                "SELECT actor_id,cell FROM actors WHERE session_id=? AND current_scene_id=? "
                "ORDER BY actor_id",
                (sid, scene),
            )
        )

        spatial = self._world.objects.spatial
        objects = {}
        for target, cell in self._conn.execute(
            "SELECT object_id,cell FROM object_spatial_cells WHERE session_id=? AND scene_id=? "
            "ORDER BY object_id,cell",
            (sid, scene),
        ):
            objects.setdefault(target, []).append(cell)
        doors = {
            passage.door_object_id
            for passage in self._world_facts.instances.passages(sid, scene_id=scene)
            if passage.door_object_id is not None
        }
        for target in chain(objects, sorted(doors - objects.keys())):
            if target not in objects and self._world_facts.object(sid, target) is None:
                return _SceneDisclosure("unresolved")
            if _object_sight_unresolved(spatial, sid, target, package):
                return _SceneDisclosure("unresolved")
        for target in objects.keys() & doors:
            objects[target] = spatial.target_cells(sid, scene, target, package)
        cells = tuple(
            dict.fromkeys(
                chain(
                    (cell for cell in actors.values() if cell is not None),
                    chain.from_iterable(objects.values()),
                )
            )
        )
        sight = self._world._prepare_visibility(sid, actor, position, package, cells=cells)
        if not cell_in_bounds(sight.topology, position.cell):
            return _SceneDisclosure("unresolved")
        visible = frozenset(cell for cell in cells if sight.can_see(cell))
        status = "resolved"
        visible_actors = []
        for target, cell in actors.items():
            if cell is None:
                status = "unresolved"
            elif cell in visible:
                if target != actor and _sight_conditions_unknown(self._characters, sid, target):
                    status = "unresolved"
                elif target != actor and not sight.can_see_creature(
                    cell, invisible=target_is_invisible(self._world, sid, target)
                ):
                    continue
                else:
                    visible_actors.append(target)
        visible_objects = tuple(
            (
                target,
                self._conn.execute(
                    "SELECT state FROM objects WHERE session_id=? AND object_id=?", (sid, target)
                ).fetchone()[0],
            )
            for target, footprint in objects.items()
            if not visible.isdisjoint(footprint)
        )
        return _SceneDisclosure(status, tuple(visible_actors), visible_objects)

    def _scene_perception_clear(self, sid, scene):
        """No mechanical visibility calculation: unsupported environments stay unknown."""
        if self._world.magic.may_have_obscurement(sid, scene):
            return False
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
                "SELECT m.actor_id FROM mechanical_actors m JOIN actors a "
                "USING(session_id,actor_id) WHERE m.session_id=? AND a.current_scene_id=?",
                (sid, scene),
            ).fetchall()
            if any(
                self._characters.has_conditions(sid, r["actor_id"])
                or self._characters.effects.has_linked_conditions(
                    sid, r["actor_id"], capability="visibility"
                )
                for r in states
            ):
                return False
        return True

    def _game_scene_view(self, sid, package, context):
        """Build the authorized game view, preserving unresolved environment status.

        The player sees enabled own-character effects and their private passive clues.
        """
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
            disclosure = (
                self._unfiltered_scene_members(sid, scene)
                if gm
                else self._observer_scene_disclosure(sid, scene, row["pc_actor_id"], package)
            )
            own = None
            if context.role == "player":
                actor_id = row["pc_actor_id"]
                character = self._conn.execute(
                    "SELECT * FROM characters WHERE session_id=? AND actor_id=?",
                    (sid, actor_id),
                ).fetchone()
                own = CharacterState(
                    actor_id,
                    character["current_hp"],
                    character["max_hp"],
                    self._characters.resource_balances(sid, actor_id),
                    tuple(self._characters.carried_slugs(sid, actor_id)),
                    tuple(
                        {"id": effect["id"], "name": effect["name"]}
                        for effect in self._characters.effects.visible_for_target(sid, actor_id)
                    ),
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
                *disclosure,
                own,
                facts,
                flags,
                self._world.detection.clues(sid, scene, row["pc_actor_id"], package)
                if context.role == "player"
                else (),
            )
