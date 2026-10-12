"""Committed facts, bounded package effects and one durable delivery outbox."""

import json
from dataclasses import asdict, replace

from .events import _StagedEvent
from .package import Signal, SignalOrigin, SignalScope, SignalStimulus, canonical_json
from .perception_conditions import discover_signals


class EventCapture:
    def __init__(self, store, characters, world):
        self.store, self.characters, self.world = store, characters, world

    @property
    def _conn(self):
        return self.store.connection

    def _event_signals(self, package, event_type, payload, source_actor_id):
        scene_id = payload.get("scene_id", payload.get("to_scene_id"))
        descriptions = {
            "scene.entered": "Someone enters the scene.",
            "scene.exited": "Someone leaves the scene.",
            "actor.moved": "Someone moves into view.",
            "actor.stepped": "Someone takes a step.",
            "object.placed": "Someone adjusts an object.",
            "object.state_changed": "An object changes position.",
        }
        if (
            event_type == "combat.engine_event"
            and payload["engine_event"]["type"] == "intent_submitted"
            and payload["engine_event"]["intent_type"] == "attack"
        ):
            descriptions[event_type] = "Someone makes an attack."
        signals = []
        if event_type == "npc.dialogue_published":
            signals = [
                Signal(
                    "signal.dialogue", "auditory", payload["text"], SignalScope(scene_id, "clear")
                )
            ]
        elif event_type in descriptions and scene_id:
            signals = [
                Signal(
                    "signal.visible",
                    "visual",
                    descriptions[event_type],
                    SignalScope(scene_id, "clear"),
                )
            ]
        elif event_type == "package.event_fired":
            signals = next(
                e.signals
                for e in package.events
                if e.event_definition_id == payload["event_definition_id"]
            )
        return tuple(
            replace(
                signal,
                origin=signal.origin
                or SignalOrigin(
                    signal.scope.scene_id,
                    source_actor_id
                    or payload.get("actor_id", payload.get("object_id"))
                    or payload.get("engine_event", {}).get("actor_id"),
                ),
                stimulus=signal.stimulus
                or SignalStimulus("sound" if signal.channel == "auditory" else "ordinary_activity"),
            )
            for signal in signals
        )

    def _capture_event(
        self, sid, package, event_type, payload, *, phase, source_actor_id=None, unresolved=False
    ):
        """Called at a proven producer stage, BEFORE subsequent mutations.

        Signals precede Discovery. Unknown Engine stages collect no purported
        historical state. The frozen JSON travels to the same atomic commit.
        """
        self.store.require_write()
        signals = self._event_signals(package, event_type, payload, source_actor_id)
        evidence = {
            "schema": "event-evidence/5"
            if any(s.grid_json or any(o.state is None for o in s.objects) for s in package.scenes)
            else "event-evidence/4",
            "world_version": None,
            "game_tick": self._conn.execute(
                "SELECT game_tick FROM world_states WHERE session_id=?", (sid,)
            ).fetchone()[0],
            "ruleset_binding": asdict(package.ruleset_binding),
            "phase": {
                "status": "unresolved" if unresolved and signals else "captured",
                "code": phase,
            },
            "discovery": {"complete": False, "by_signal": {}, "reason": None},
            "actors": [],
            "source_actors": [],
            "scenes": {},
            "objects": {},
        }
        if not signals:
            evidence["discovery"]["complete"] = True
        elif unresolved:
            evidence["discovery"]["reason"] = phase
        else:
            self._event_perception_evidence(sid, package, signals, evidence)
        return _StagedEvent(event_type, payload, signals, canonical_json(evidence))

    def _event_perception_evidence(self, sid, package, signals, evidence):
        """Signal-scoped capture at the calling producer's actual stage."""
        scene_ids = {s.scope.scene_id for s in signals}
        if any(s.scope.kind != "scene" for s in signals):
            evidence["discovery"]["reason"] = "unsupported_scope"
            return
        for scene_id in sorted(scene_ids):
            if not self._conn.execute(
                "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, scene_id)
            ).fetchone():
                evidence["discovery"]["reason"] = "missing_scope_scene"
                return
        # Enumerate each supported Scene completely, including inactive and
        # sensory-incapable NPCs. No judgment or budget truncation here.
        actors = []
        for scene_id in sorted(scene_ids):
            actors.extend(
                dict(r)
                for r in self._conn.execute(
                    "SELECT actor_id,kind,current_scene_id FROM actors "
                    "WHERE session_id=? AND kind='npc' AND current_scene_id=? ORDER BY actor_id",
                    (sid, scene_id),
                )
            )
        evidence["discovery"] = {
            "complete": True,
            "by_signal": {
                s.signal_id: [a["actor_id"] for a in actors if s in discover_signals((s,), a)]
                for s in signals
            },
            "reason": None,
        }
        evidence["actors"] = actors
        observer_ids = {a["actor_id"] for a in actors}
        for entity_id in sorted(
            {s.origin.entity_id for s in signals if s.origin and s.origin.entity_id} - observer_ids
        ):
            source = self._conn.execute(
                "SELECT actor_id,kind,current_scene_id FROM actors "
                "WHERE session_id=? AND actor_id=?",
                (sid, entity_id),
            ).fetchone()
            if source:
                evidence["source_actors"].append(dict(source))
            else:
                obj = self._conn.execute(
                    "SELECT scene_id,state FROM objects WHERE session_id=? AND object_id=?",
                    (sid, entity_id),
                ).fetchone()
                if obj:
                    evidence["objects"][entity_id] = dict(obj)
        combat = self._conn.execute(
            "SELECT scene_id,state_json FROM combats WHERE session_id=?", (sid,)
        ).fetchone()
        state = json.loads(combat["state_json"]) if combat else None
        active = state is not None and not state["ended"]
        for actor in [*actors, *evidence["source_actors"]]:
            position = self.characters.position(sid, actor["actor_id"])
            fine = self.world.spatial_scene(sid, actor["current_scene_id"]) is not None
            if evidence["schema"] == "event-evidence/5":
                actor["cell"] = position.cell
            definition = next((a for a in package.actors if a.actor_id == actor["actor_id"]), None)
            actor["senses"] = dict(definition.senses) if definition and definition.senses else None
            stored = self._conn.execute(
                "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
                (sid, actor["actor_id"]),
            ).fetchone()
            data = json.loads(stored[0]) if stored else None
            actor["mechanical"] = (
                None
                if data is None
                else {
                    "conditions": [c["condition"] for c in data["conditions"]],
                    "senses": data["senses"],
                    "traits": data["trait_mechanics"],
                    "is_alive": data["is_alive"],
                    "dead_recorded": bool(
                        state is not None and actor["actor_id"] in state["dead_ids"]
                    ),
                    "cell": position.cell
                    if fine
                    else state.get("actor_zone", {}).get(actor["actor_id"])
                    if active and actor["current_scene_id"] == combat["scene_id"]
                    else None,
                }
            )
            if data is not None and fine:
                actor["mechanical"]["cell"] = position.cell
        if evidence["schema"] == "event-evidence/5":
            for oid, obj in evidence["objects"].items():
                obj["cell"] = self._conn.execute(
                    "SELECT cell FROM objects WHERE session_id=? AND object_id=?", (sid, oid)
                ).fetchone()[0]
        for scene_id in sorted(scene_ids):
            stored = self._conn.execute(
                "SELECT state_json FROM mechanical_scenes WHERE session_id=? AND scene_id=?",
                (sid, scene_id),
            ).fetchone()
            evidence["scenes"][scene_id] = {
                "grid": json.loads(stored[0])["grid"] if stored else None,
                "unsupported_effects": bool(
                    active
                    and combat["scene_id"] == scene_id
                    and (
                        state["persistent_areas"]["areas"]
                        or state["conditions_by_effect"]
                        or state["effect_lifecycles"]
                    )
                ),
            }
