"""One-shot manual mechanisms over reviewed capabilities in the shared scope."""

from dataclasses import asdict

from ._execution import _execution_data, _WorldEffectKind, _WorldEffectOrigin
from ._operations import ObjectState, SetObjectState
from .errors import StateMachineError
from .world_commands import SetLightEnabled


class MechanismService:
    def __init__(self, handler):
        self.h = handler

    def config(self, package, oid):
        d = self.h.facts.object_definition(package, oid)
        return d.capability("mechanism") if d else None

    def qualify_link(self, sid, origin, target, kind, value, package):
        if type(origin) is not _WorldEffectOrigin or origin.kind is not _WorldEffectKind.MECHANISM:
            raise StateMachineError("invalid_execution_origin", "No admitted mechanism invocation")
        c = self.config(package, origin.reference_id)
        if (
            c is None
            or self.h.facts.mechanism_status(sid, origin.reference_id) != "armed"
            or not any(
                a.target_object_id == target and a.kind == kind and a.value == value
                for a in c.actions
            )
        ):
            raise StateMachineError(
                "invalid_execution_origin", "Result is outside admitted mechanism graph"
            )

    def _preflight(self, sid, action, package):
        h = self.h
        obj = h.facts.object(sid, action.target_object_id)
        d = h.facts.object_definition(package, action.target_object_id)
        if obj is None or d is None or h.facts.object_hp(sid, action.target_object_id) == 0:
            raise StateMachineError(
                "mechanism_target_unavailable", "A mechanism target is unavailable"
            )
        if action.kind == "set_open_state":
            if d.capability("openable") is None:
                raise StateMachineError("unsupported_object_capability", "Target is not openable")
            h.facts.validate_door_switch(sid, action.target_object_id, action.value)
            h._validate_door_occupancy(
                sid,
                SetObjectState(
                    h.facts.placement(sid, action.target_object_id)["scene_id"],
                    action.target_object_id,
                    ObjectState(action.value),
                ),
                package,
            )
        elif action.kind == "set_light_enabled":
            if d.capability("light") is None:
                raise StateMachineError("unsupported_object_capability", "Target is not an emitter")
        else:
            raise StateMachineError("unsupported_operation", "Unknown linked result")

    def validate(self, row, actor, op, package):
        h, sid = self.h, row["session_id"]
        h._world_operation_capability(row, op)
        h._world_actor_ready(sid, actor)
        position = h._public_scope(sid, actor, package)
        if (position.scene_id, position.cell) != (op.scene_id, op.expected_cell):
            raise StateMachineError("position_conflict", "Actor position changed")
        h.objects.containers.reachable(sid, actor, op.object_id, package)
        c = self.config(package, op.object_id)
        if c is None:
            raise StateMachineError("unsupported_object_capability", "Object is not a mechanism")
        if h.facts.object_hp(sid, op.object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed mechanism cannot be activated")
        if h.facts.mechanism_status(sid, op.object_id) != "armed":
            raise StateMachineError("mechanism_spent", "One-shot mechanism is spent")
        for action in c.actions:
            self._preflight(sid, action, package)
        return c

    def operate(self, row, actor, op, package, source_command_id):
        c = self.validate(row, actor, op, package)
        h, sid = self.h, row["session_id"]
        if type(source_command_id) is not str or not source_command_id:
            raise StateMachineError(
                "invalid_execution_origin", "An admitted source command is required"
            )
        key = "mechanism." + op.object_id
        if h._conn.execute(
            "SELECT 1 FROM trigger_history WHERE session_id=? AND event_definition_id=?", (sid, key)
        ).fetchone():
            raise StateMachineError("mechanism_spent", "Activation already adopted")
        origin = _WorldEffectOrigin(_WorldEffectKind.MECHANISM, op.object_id)
        events = [
            h._capture_event(
                sid,
                package,
                "mechanism.activated",
                {**asdict(op), "actor_id": actor, "activation_id": key},
                phase="world.before_mechanism_results",
                source_actor_id=actor,
            )
        ]
        for action in c.actions:
            self.qualify_link(
                sid, origin, action.target_object_id, action.kind, action.value, package
            )
            self._preflight(sid, action, package)  # Recheck sequential candidate state.
            location = h.facts.placement(sid, action.target_object_id)
            if action.kind == "set_light_enabled":
                events.extend(
                    h.objects.lights.operate(
                        row,
                        None,
                        SetLightEnabled(
                            location["scene_id"],
                            action.target_object_id,
                            location["cell"],
                            action.value,
                        ),
                        package,
                        origin=origin,
                    )
                )
            else:
                before = h.facts.object(sid, action.target_object_id)["state"]
                if before != action.value:
                    h.facts.switch(sid, action.target_object_id, action.value)
                    h.objects.spatial.audit(sid, package)
                    events.append(
                        h._capture_event(
                            sid,
                            package,
                            "object.state_changed",
                            {
                                "scene_id": location["scene_id"],
                                "object_id": action.target_object_id,
                                "state": action.value,
                                "execution": _execution_data(None, origin),
                            },
                            phase="world.after_mechanism_open_update",
                            source_actor_id=action.target_object_id,
                        )
                    )
        h.facts.consume_mechanism(sid, op.object_id)
        h._conn.execute(
            "INSERT INTO trigger_history VALUES (?,?,?,?,'linked')",
            (sid, key, source_command_id, row["event_seq"] + 1),
        )
        events.append(
            h._capture_event(
                sid,
                package,
                "mechanism.spent",
                {
                    "scene_id": op.scene_id,
                    "object_id": op.object_id,
                    "activation_id": key,
                },
                phase="world.after_mechanism_results",
                source_actor_id=actor,
            )
        )
        return events
