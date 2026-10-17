"""Openable operations over the single WorldFacts state, in the caller's transaction."""

from dataclasses import asdict

from ._execution import _execution_data, _WorldEffectKind
from ._operations import LockDoor, ObjectState, SetObjectState, UnlockDoor
from .errors import StateMachineError
from .world_commands import BarObject, LockObject, UnlockObject


class OpenableService:
    def __init__(self, handler):
        self.h = handler
        self.facts = handler.facts

    def lock_status(self, sid, object_id, package):
        locked = self.h.facts.door_lock(sid, object_id)
        definition = self.h.facts.object_definition(package, object_id)
        configured = any(d.object_id == object_id for d in package.lockable_doors) or bool(
            definition and definition.capability("locks")
        )
        if (locked is not None) != configured:
            raise StateMachineError("invalid_lock_state", "Lock capability contradicts package")
        return locked

    def validate_lock(self, row, actor_id, operation, package):
        sid = row["session_id"]
        self.h._world_operation_capability(row, operation)
        self.h._world_actor_ready(sid, actor_id)
        position = self.h._public_scope(sid, actor_id, package)
        obj, passage = self.h._manual_object_target(sid, actor_id, operation, package)
        if passage is None and type(operation) in (LockDoor, UnlockDoor):
            raise StateMachineError("unsupported_object_capability", "No approved shared lock")
        if position.cell != operation.expected_cell:
            raise StateMachineError("position_conflict", "Door interaction position differs")
        locked = self.lock_status(sid, operation.object_id, package)
        if locked is None:
            raise StateMachineError("unsupported_object_capability", "No approved shared lock")
        locks = self.h.facts.locks_for_object(sid, operation.object_id)
        if type(operation) in (LockObject, UnlockObject):
            lock_id = operation.lock_id
        elif len(locks) == 1:
            lock_id = locks[0][0]
        else:
            raise StateMachineError("ambiguous_lock", "Explicit lock identity required")
        selected = next((lock for lock in locks if lock[0] == lock_id), None)
        if selected is None:
            raise StateMachineError("invalid_target", "Lock does not belong to object")
        locked = bool(selected[1])
        definition = self.h.facts.object_definition(package, operation.object_id)
        config = definition.capability("locks") if definition else None
        policy = (
            next((lock for lock in config.locks if lock.lock_id == lock_id), None)
            if config
            else None
        )
        if policy and policy.operable_sides and position.scene_id not in policy.operable_sides:
            raise StateMachineError("operation_side_forbidden", "Lock inaccessible from this side")
        if self.h.facts.object_hp(sid, operation.object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        # No distinction between absent, foreign, ground and mismatched credentials.
        item = self.h._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND object_id=?",
            (sid, operation.credential_instance_id),
        ).fetchone()
        if (
            item is None
            or self.h.facts.placement(sid, operation.credential_instance_id)["carrier_id"]
            != actor_id
            or item["item_json"] is None
            or operation.credential_instance_id
            not in {i.instance_id for i in package.portable_items}
            or not self.h.facts.item_accessible(sid, operation.credential_instance_id)
            or not self.h.facts.accepts_credential(
                sid, operation.object_id, operation.credential_instance_id, lock_id
            )
        ):
            raise StateMachineError("credential_unavailable", "No usable owned credential")
        known = set(self.h.characters.rule_actors(sid))
        if actor_id not in known:
            raise StateMachineError("unsupported_actor_state", "Explicit actor facts required")
        components = self.h.characters.rule_components(sid, known)
        if any(e["target_id"] == actor_id for e in components["effect_states"]):
            raise StateMachineError("unsupported_actor_state", "Active effects need adjudication")
        entry = next(
            (
                e
                for e in components["inventory_state"]["entries"]
                if e["instance_id"] == operation.credential_instance_id
            ),
            None,
        )
        if (
            entry is None
            or entry["owner_id"] != actor_id
            or entry["quantity"] != 1
            or not entry["accessible"]
        ):
            raise StateMachineError("credential_unavailable", "No usable owned credential")
        if type(operation) in (LockDoor, LockObject) and obj[1] != "closed":
            raise StateMachineError("door_must_be_closed", "Close the door before locking")
        self.h._validate_door_occupancy(
            sid,
            SetObjectState(operation.scene_id, operation.object_id, ObjectState.CLOSED),
            package,
        )
        return position, passage, locked

    def operate_lock(self, row, actor_id, operation, package):
        position, passage, before = self.validate_lock(row, actor_id, operation, package)
        locked = type(operation) in (LockDoor, LockObject)
        if before == locked:
            return []
        sid = row["session_id"]
        self.h.facts.set_door_lock(
            sid, operation.object_id, locked, getattr(operation, "lock_id", None)
        )
        return [
            self.h._capture_event(
                sid,
                package,
                "door.lock_changed",
                {
                    "scene_id": position.scene_id,
                    "actor_id": actor_id,
                    "actor_cell": position.cell,
                    "object_id": operation.object_id,
                    "passage_id": passage.passage_id if passage else None,
                    "lock_id": getattr(operation, "lock_id", None),
                    "credential_instance_id": operation.credential_instance_id,
                    "locked": locked,
                    "execution": _execution_data(actor_id, None),
                },
                phase="world.after_door_lock_update",
                source_actor_id=actor_id,
            )
        ]

    def validate_transition(self, sid, operation, package):
        obj = self.facts.object(sid, operation.object_id)
        if obj is None:
            raise StateMachineError("invalid_target", "No object at target")
        if obj["state"] is None:
            raise StateMachineError("unsupported_object_capability", "Object has no switch")
        self.h._validate_door_occupancy(sid, operation, package)
        self.lock_status(sid, operation.object_id, package)
        self.facts.validate_door_switch(sid, operation.object_id, operation.state.value)

    def validate_open(
        self, row, actor_id, operation, package, *, origin=None, navigation_scope=None
    ):
        h, sid = self.h, row["session_id"]
        if type(operation.state) is not ObjectState:
            raise StateMachineError("invalid_operation", "Expected a typed ObjectState")
        _execution_data(actor_id, origin)
        passage = None
        if origin is None:
            h._world_operation_capability(row, operation)
            obj, passage = h._manual_object_target(
                sid, actor_id, operation, package, navigation_scope=navigation_scope
            )
        else:
            obj = self.facts.object(sid, operation.object_id)
            if obj is None or obj["scene_id"] != operation.scene_id:
                raise StateMachineError("invalid_target", "No object in the effect scene")
            if origin.kind is _WorldEffectKind.MECHANISM:
                h.objects.mechanisms.qualify_link(
                    sid,
                    origin,
                    operation.object_id,
                    "set_open_state",
                    operation.state.value,
                    package,
                )
            elif operation.scene_id != row["current_scene_id"]:
                raise StateMachineError("invalid_target", "Only the current scene can be modified")
        self.validate_transition(sid, operation, package)
        return passage

    def operate_open(
        self, row, actor_id, operation, package, *, origin=None, navigation_scope=None
    ):
        self.h.store.require_write()
        passage = self.validate_open(
            row, actor_id, operation, package, origin=origin, navigation_scope=navigation_scope
        )
        sid = row["session_id"]
        if self.facts.object(sid, operation.object_id)["state"] == operation.state.value:
            return []
        self.facts.switch(sid, operation.object_id, operation.state.value)
        self.h.objects.spatial.audit(sid, package)
        return [
            self.h._capture_event(
                sid,
                package,
                "object.state_changed",
                {
                    **asdict(operation),
                    "execution": _execution_data(actor_id, origin),
                    **({"passage_id": passage.passage_id} if passage else {}),
                },
                phase="world.after_object_update",
                source_actor_id=actor_id if passage else None,
            )
        ]

    def validate_bar(self, row, actor_id, operation, package):
        h, sid = self.h, row["session_id"]
        h._world_operation_capability(row, operation)
        h._world_actor_ready(sid, actor_id)
        h._manual_object_target(sid, actor_id, operation, package)
        if h._actor_position(sid, actor_id).cell != operation.expected_cell:
            raise StateMachineError("position_conflict", "Actor position changed")
        definition = self.facts.object_definition(package, operation.object_id)
        c = definition.capability("openable") if definition else None
        if c is None or c.initial_barred is None:
            raise StateMachineError("unsupported_object_capability", "Object has no bar")
        if c.bar_operable_sides and operation.scene_id not in c.bar_operable_sides:
            raise StateMachineError("operation_side_forbidden", "Bar inaccessible from this side")
        if self.facts.object_hp(sid, operation.object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        if (
            type(operation) is BarObject
            and self.facts.object(sid, operation.object_id)["state"] != "closed"
        ):
            raise StateMachineError("object_must_be_closed", "Close object before barring")

    def operate_bar(self, row, actor_id, operation, package):
        self.h.store.require_write()
        self.validate_bar(row, actor_id, operation, package)
        sid = row["session_id"]
        barred = type(operation) is BarObject
        if self.facts.openable_state(sid, operation.object_id)["barred"] == barred:
            return []
        self.facts.set_object_bar(sid, operation.object_id, barred)
        self.h.objects.spatial.audit(sid, package)
        return [
            self.h._capture_event(
                sid,
                package,
                "object.bar_changed",
                {**asdict(operation), "barred": barred, "actor_id": actor_id},
                phase="world.after_bar_update",
                source_actor_id=actor_id,
            )
        ]
