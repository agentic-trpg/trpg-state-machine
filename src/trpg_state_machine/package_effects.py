"""Committed facts, bounded package effects and one durable delivery outbox."""

import json

from ._execution import _WorldEffectKind, _WorldEffectOrigin
from .errors import StateMachineError


class PackageEffects:
    """Expand configured effects through shared world, capture and time interfaces."""

    def __init__(self, store, world, capture):
        """Bind Package effects, stage capture and the unified time interface."""
        self.store, self.world, self.capture = store, world, capture
        from .time_coordination import SessionTime

        if world.runtime.time is None:
            world.runtime.time = SessionTime(world.runtime, world, self)
        else:
            world.runtime.time.effects = self

    @property
    def _conn(self):
        """Return the shared Store connection."""
        return self.store.connection

    def _expand_events(self, row, package, events, command_id):
        """Expand bounded candidate consequences with stable source event identities.

        An admitted direct action's activation drains synchronous consequences
        before World admits the next candidate for the same source event.
        """
        self.store.require_write()
        by_trigger, _ = self.capture.event_index(package)
        # Immutable instance configuration cannot change through the closed
        # event consequence vocabulary. New scenes/configurations use a new batch.
        mechanism_index = {}
        queue = list(events)
        settled = set()

        def settle_region(activation_events):
            """Drain one ordered activation and its emitted consequences."""
            index = len(queue)
            queue.extend(activation_events)
            while index < len(queue):
                expand(index)
                settled.add(index)
                index += 1

        def expand(index):
            """Apply one event's reconciliations, mechanism hooks and Package effects."""
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
                self.world.magic.reconcile_world_event(row["session_id"], package, queue[index - 1])
            )
            queue.extend(
                self.world.objects.mechanisms.automatic(
                    row,
                    package,
                    queue[index - 1],
                    command_id,
                    row["event_seq"] + index,
                    prepared=mechanism_index,
                    settle_region=settle_region,
                )
            )
            scene_id = payload.get("scene_id", payload.get("to_scene_id"))
            for definition in by_trigger.get((event_type, scene_id), ()):
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

        index = 0
        while index < len(queue):
            if index not in settled:
                expand(index)
            index += 1
        return queue
