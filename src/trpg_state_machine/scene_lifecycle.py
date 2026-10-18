"""Scene coordination inside the caller's admitted write transaction.

This component neither authorizes movement nor commits. WorldHandler validates
the move first; CommandRuntime owns permission, replay, version and rollback.
Facts retain ownership of all persisted state. No scene is reset on departure.
"""

from dataclasses import asdict, replace

from ._execution import _execution_data
from .errors import StateMachineError
from .events import _ActorMotion


class SceneLifecycleService:
    def __init__(self, store, sessions, characters, facts, capture, npc_bootstrap):
        self.store = store
        self.sessions = sessions
        self.characters = characters
        self.facts = facts
        self.capture = capture
        self.npc_bootstrap = npc_bootstrap

    def ensure_scene(self, sid, scene_id, package):
        """Install first-instance defaults once, without recording a PC visit."""
        self.store.require_write()
        if not self.facts.initialize_scene(sid, scene_id, package):
            return False
        self.facts.initialize_object_capabilities(sid, scene_id, package)
        for actor in package.actors:
            if actor.initial_scene_id == scene_id and self.characters.initialize_npc_actor(
                sid, actor
            ):
                self.npc_bootstrap.initialize(sid, actor)
        self.facts.initialize_mechanical_scene(sid, scene_id, package)
        return True

    def initialize_session(self, sid, pc_actor_id, pc, package):
        """Fresh-session participant; creation replay is handled by the caller."""
        self.ensure_scene(sid, package.entry_scene_id, package)
        # Existing spatial initialization policy: navigation may inspect both sides.
        for scene in package.scenes:
            if scene.grid_json is not None and scene.scene_id != package.entry_scene_id:
                self.ensure_scene(sid, scene.scene_id, package)
        self.characters.initialize_pc(sid, pc_actor_id, package.entry_scene_id, pc)
        self.sessions.move_pc_scene(sid, package.entry_scene_id)
        self.facts.visit(sid, package.entry_scene_id)

    def move_admitted(self, row, package, actor_id, destination):
        """Apply a destination already validated by the movement owner.

        Same-scene steps retain their existing event semantics. Only a PC crossing
        a scene boundary records a visit. Exit evidence precedes initialization
        and movement; arrival evidence sees the resulting authoritative facts.
        """
        self.store.require_write()
        sid = row["session_id"]
        before = self.characters.position(sid, actor_id)
        if before == destination:
            return []
        is_pc = actor_id == row["pc_actor_id"]
        crossing = before.scene_id != destination.scene_id
        events = []
        if crossing:
            events.append(
                self.capture._capture_event(
                    sid,
                    package,
                    "scene.exited" if is_pc else "actor.departed",
                    {
                        "scene_id": before.scene_id,
                        **({} if is_pc else {"actor_id": actor_id, "from_cell": before.cell}),
                    },
                    phase="scene.before_departure" if is_pc else "world.before_actor_departure",
                    source_actor_id=actor_id,
                )
            )
        self.ensure_scene(sid, destination.scene_id, package)
        self.characters.move(sid, actor_id, destination)
        if is_pc:
            self.sessions.move_pc_scene(sid, destination.scene_id)
            if crossing:
                self.facts.visit(sid, destination.scene_id)
        if is_pc and crossing:
            events.append(
                self.capture._capture_event(
                    sid,
                    package,
                    "scene.entered",
                    {"scene_id": destination.scene_id},
                    phase="scene.after_arrival",
                    source_actor_id=actor_id,
                )
            )
        else:
            events.append(
                self.capture._capture_event(
                    sid,
                    package,
                    "actor.moved" if crossing else "actor.stepped",
                    {
                        "actor_id": actor_id,
                        "to_scene_id": destination.scene_id,
                        **(
                            {"from_cell": before.cell, "to_cell": destination.cell}
                            if destination.cell is not None
                            else {}
                        ),
                    },
                    phase="world.after_actor_move",
                    source_actor_id=actor_id,
                )
            )
        events[-1] = replace(
            events[-1],
            mechanical_motion=_ActorMotion(
                actor_id, before.scene_id, before.cell, destination.scene_id, destination.cell
            ),
        )
        return events

    def set_flag(self, row, package, operation, *, actor_id, effect_origin=None):
        """Declared scene-local booleans only; no domain-state reinterpretation."""
        self.store.require_write()
        if operation.scene_id != row["current_scene_id"]:
            raise StateMachineError("invalid_target", "Only the current scene can be modified")
        if type(operation.value) is not bool:
            raise StateMachineError("invalid_operation", "Flag value must be boolean")
        sid = row["session_id"]
        flags = self.facts.scene_flags(sid, operation.scene_id)
        if flags is None or operation.flag_id not in flags:
            raise StateMachineError("invalid_target", "Flag must be declared in this scene")
        if flags[operation.flag_id] == operation.value:
            return []
        flags[operation.flag_id] = operation.value
        self.facts.set_scene_flags(sid, operation.scene_id, flags)
        return [
            self.capture._capture_event(
                sid,
                package,
                "scene.flag_changed",
                {**asdict(operation), "execution": _execution_data(actor_id, effect_origin)},
                phase="world.after_flag_update",
            )
        ]
