"""Committed facts, bounded package effects and one durable delivery outbox."""

import json
from dataclasses import asdict, dataclass

from .errors import StateMachineError
from .package import Signal, SignalOrigin, SignalScope, SignalStimulus


@dataclass(frozen=True, slots=True)
class CommittedWorldEvent:
    event_id: str
    event_seq: int
    source_command_id: str
    event_type: str
    world_version: int
    payload: dict
    signals: tuple[Signal, ...]


class EventRuntime:
    def _event_perception_evidence(self, sid, package, version):
        """Copy only committed sensory inputs in the existing Event transaction."""
        actors = [
            dict(r)
            for r in self._conn.execute(
                "SELECT actor_id,kind,current_scene_id FROM actors WHERE session_id=?", (sid,)
            )
        ]
        combat = self._conn.execute(
            "SELECT scene_id,state_json FROM combats WHERE session_id=?", (sid,)
        ).fetchone()
        state = json.loads(combat["state_json"]) if combat else None
        active = state is not None and not state["ended"]
        for actor in actors:
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
                    "cell": state["actor_zone"].get(actor["actor_id"])
                    if active and actor["current_scene_id"] == combat["scene_id"]
                    else None,
                }
            )
        scenes = {}
        for row in self._conn.execute(
            "SELECT scene_id FROM scene_instances WHERE session_id=?", (sid,)
        ):
            stored = self._conn.execute(
                "SELECT state_json FROM mechanical_scenes WHERE session_id=? AND scene_id=?",
                (sid, row[0]),
            ).fetchone()
            scenes[row[0]] = {
                "grid": json.loads(stored[0])["grid"] if stored else None,
                "unsupported_effects": bool(
                    active
                    and combat["scene_id"] == row[0]
                    and (
                        state["persistent_areas"]["areas"]
                        or state["conditions_by_effect"]
                        or state["effect_lifecycles"]
                    )
                ),
            }
        return {
            "schema": "event-evidence/3",
            "world_version": version,
            "game_tick": self._conn.execute(
                "SELECT game_tick FROM world_states WHERE session_id=?", (sid,)
            ).fetchone()[0],
            "ruleset_binding": asdict(package.ruleset_binding),
            "actors": actors,
            "scenes": scenes,
            "objects": {
                r[0]: r[1]
                for r in self._conn.execute(
                    "SELECT object_id,scene_id FROM objects WHERE session_id=?", (sid,)
                )
            },
        }

    def _expand_events(self, row, package, events, command_id):
        """Only world effects computable now. No async mechanical commands or scripts."""
        queue = list(events)
        index = 0
        while index < len(queue):
            if len(queue) > 32:
                raise StateMachineError(
                    "event_cascade_limit", "At most 32 consequences per command"
                )
            event_type, payload = queue[index]
            index += 1
            scene_id = payload.get("scene_id", payload.get("to_scene_id"))
            for definition in package.events:
                if definition.trigger_type != event_type or definition.scene_id != scene_id:
                    continue
                if self._conn.execute(
                    "SELECT 1 FROM trigger_history WHERE session_id=? AND event_definition_id=?",
                    (row["session_id"], definition.event_definition_id),
                ).fetchone():
                    continue
                current = self._conn.execute(
                    "SELECT * FROM sessions WHERE session_id=?", (row["session_id"],)
                ).fetchone()
                if current["current_scene_id"] != definition.scene_id:
                    continue
                flags = json.loads(
                    self._conn.execute(
                        "SELECT flags_json FROM scene_instances WHERE session_id=? AND scene_id=?",
                        (row["session_id"], definition.scene_id),
                    ).fetchone()[0]
                )
                if flags[definition.condition_flag] != definition.condition_value:
                    continue
                self._conn.execute(
                    "INSERT INTO trigger_history VALUES (?, ?, ?, ?, 'linked')",
                    (
                        row["session_id"],
                        definition.event_definition_id,
                        command_id,
                        row["event_seq"] + index,
                    ),
                )
                queue.extend(self._operate(current, package, definition.effect))
                queue.append(
                    (
                        "package.event_fired",
                        {
                            "event_definition_id": definition.event_definition_id,
                            "scene_id": definition.scene_id,
                        },
                    )
                )
        return queue

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
