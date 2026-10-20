"""Committed facts, bounded package effects and one durable delivery outbox."""

import json

from ._execution import _WorldEffectKind, _WorldEffectOrigin
from .errors import StateMachineError


class PackageEffects:
    def __init__(self, store, world, capture):
        self.store, self.world, self.capture = store, world, capture
        self._event_package = None
        self._event_index = {}
        from .time_coordination import SessionTime

        if world.runtime.time is None:
            world.runtime.time = SessionTime(world.runtime, world, self)
        else:
            world.runtime.time.effects = self

    @property
    def _conn(self):
        return self.store.connection

    def _expand_events(self, row, package, events, command_id):
        """Only world effects computable now. No async mechanical commands or scripts."""
        self.store.require_write()
        if self._event_package is not package:
            index = {}
            for definition in package.events:
                index.setdefault((definition.trigger_type, definition.scene_id), []).append(
                    definition
                )
            self._event_index = {k: tuple(v) for k, v in index.items()}
            self._event_package = package
        # Immutable instance configuration cannot change through the closed
        # event consequence vocabulary. New scenes/configurations use a new batch.
        mechanism_index = {}
        queue = list(events)
        index = 0
        while index < len(queue):
            if (
                len(queue)
                + sum(
                    len(e.payload.get("rule_results", ()))
                    for e in queue
                    if e.event_type == "mechanism.activated"
                )
                > 32
            ):
                raise StateMachineError(
                    "event_cascade_limit", "At most 32 consequences per command"
                )
            event_type, payload = queue[index].event_type, queue[index].payload
            index += 1
            queue.extend(
                self.world.objects.mechanisms.automatic(
                    row,
                    package,
                    queue[index - 1],
                    command_id,
                    row["event_seq"] + index,
                    prepared=mechanism_index,
                )
            )
            scene_id = payload.get("scene_id", payload.get("to_scene_id"))
            for definition in self._event_index.get((event_type, scene_id), ()):
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
                queue.extend(
                    self.world._operate(
                        current,
                        package,
                        definition.effect,
                        effect_origin=_WorldEffectOrigin(
                            _WorldEffectKind.PACKAGE, definition.event_definition_id
                        ),
                    )
                )
                queue.append(
                    self.capture._capture_event(
                        row["session_id"],
                        package,
                        "package.event_fired",
                        {
                            "event_definition_id": definition.event_definition_id,
                            "scene_id": definition.scene_id,
                        },
                        phase="package.after_effect",
                    )
                )
        return queue
