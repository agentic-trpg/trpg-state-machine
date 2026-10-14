"""World object capability orchestration within the existing command transaction."""

from dataclasses import asdict

from .errors import StateMachineError
from .world_commands import (
    BarObject,
    LockObject,
    PutItem,
    RepositionObject,
    SetLightEnabled,
    SwitchLightMode,
    TakeItem,
    UnbarObject,
    UnlockObject,
)
from .world_containers import ContainerService
from .world_lights import LightService
from .world_spatial import ObjectSpatialService


class WorldObjectService:
    def __init__(self, handler):
        self.handler = handler
        self.facts = handler.facts
        self.containers = ContainerService(handler)
        self.spatial = ObjectSpatialService(handler)
        self.lights = LightService(handler)

    def validate(self, row, actor_id, operation, package):
        h, sid = self.handler, row["session_id"]
        if type(operation) in (SetLightEnabled, SwitchLightMode):
            self.lights.validate(row, actor_id, operation, package)
            return
        if type(operation) is RepositionObject:
            self.spatial.validate_placement(row, actor_id, operation, package)
            return
        if type(operation) in (PutItem, TakeItem):
            self.containers.validate(row, actor_id, operation, package)
            return
        if type(operation) in (LockObject, UnlockObject):
            h._validate_lock(row, actor_id, operation, package)
            return
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

    def query(self, sid, actor_id, operation, package, *, context):
        from .commands import OPERATIONS, TypedCommand, validate_command
        from .spatial import WorldActionAvailability

        kind = next(k for k, v in OPERATIONS.items() if type(operation) is v)
        validate_command(TypedCommand(sid, "query", actor_id, kind, operation, 0))
        h = self.handler
        with h._transaction():
            h._spatial_reader(sid, actor_id, package, context)
            row = h._conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
            try:
                if type(operation) is RepositionObject and not getattr(
                    context, "can_place_world_objects", False
                ):
                    raise StateMachineError("unauthorized", "Trusted GM placement grant required")
                self.validate(row, actor_id, operation, package)
            except StateMachineError as exc:
                return WorldActionAvailability("unavailable", exc.code, row["world_version"])
            return WorldActionAvailability("available", "available", row["world_version"])

    def operate(self, row, actor_id, operation, package):
        if type(operation) in (SetLightEnabled, SwitchLightMode):
            return self.lights.operate(row, actor_id, operation, package)
        if type(operation) is RepositionObject:
            return self.spatial.operate(row, actor_id, operation, package)
        if type(operation) in (PutItem, TakeItem):
            return self.containers.operate(row, actor_id, operation, package)
        if type(operation) in (LockObject, UnlockObject):
            return self.handler._operate_lock(row, actor_id, operation, package)
        self.validate(row, actor_id, operation, package)
        h, sid = self.handler, row["session_id"]
        barred = type(operation) is BarObject
        if self.facts.openable_state(sid, operation.object_id)["barred"] == barred:
            return []
        self.facts.set_object_bar(sid, operation.object_id, barred)
        return [
            h._capture_event(
                sid,
                package,
                "object.bar_changed",
                {**asdict(operation), "barred": barred, "actor_id": actor_id},
                phase="world.after_bar_update",
                source_actor_id=actor_id,
            )
        ]

    def _visible(self, sid, actor_id, oid, package):
        h = self.handler
        position = h._actor_position(sid, actor_id)
        h._public_scope(sid, actor_id, package)
        obj = self.facts.object(sid, oid)
        if obj is None:
            raise StateMachineError("invalid_target", "No authorized local object")
        if obj["container_id"] is not None:
            try:
                self.containers.reachable(sid, actor_id, oid, package)
            except StateMachineError as exc:
                raise StateMachineError("invalid_target", "No authorized local object") from exc
            return obj
        if obj["carrier_id"] == actor_id:
            return obj
        targets = [e.threshold_cell for _, e in h._door_ports(position.scene_id, oid, package)]
        if obj["scene_id"] == position.scene_id:
            targets.append(obj["cell"])
        if not any(
            cell is not None
            and h._base_topology(sid, position.scene_id, package).can_see(position.cell, cell)
            for cell in targets
        ):
            raise StateMachineError("invalid_target", "No authorized local object")
        return obj

    def view(self, sid, actor_id, oid, package):
        obj = self._visible(sid, actor_id, oid, package)
        definition = self.facts.object_definition(package, oid)
        config = (
            asdict(definition)
            if definition
            else {
                "object_id": oid,
                "definition_ref": oid,
                "display_name": oid,
                "description_ref": None,
                "material": None,
                "size_category": None,
                "weight_lb": None,
            }
        )
        config.pop("capabilities", None)
        declared = (
            tuple(c.kind for c in definition.capabilities)
            if definition
            else (("openable",) if obj["state"] is not None else ())
        )
        openable = self.facts.openable_state(sid, oid)
        hp = self.facts.object_hp(sid, oid)
        destroyed = hp == 0 if hp is not None else None
        from ._operations import ObjectState, SetObjectState

        position = self.handler._actor_position(sid, actor_id)
        row = self.handler._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        available = {}
        for name, op in (
            ("can_open", SetObjectState(position.scene_id, oid, ObjectState.OPEN)),
            ("can_close", SetObjectState(position.scene_id, oid, ObjectState.CLOSED)),
            ("can_bar", BarObject(position.scene_id, oid, position.cell)),
            ("can_unbar", UnbarObject(position.scene_id, oid, position.cell)),
        ):
            try:
                if isinstance(op, SetObjectState):
                    self.handler._world_destination(row, actor_id, op, package)
                else:
                    self.validate(row, actor_id, op, package)
                available[name] = True
            except StateMachineError:
                available[name] = False
        contents_visible = self.containers.config(package, oid) and (
            destroyed or (obj["state"] != "closed" and not self.facts.door_lock(sid, oid))
        )
        total_weight = self.containers.total_weight(oid, self.containers._rows(sid), package)
        return {
            "contents": tuple(c["object_id"] for c in self.facts.contents(sid, oid))
            if contents_visible
            else None,
            "contents_weight_lb": total_weight - definition.weight_lb
            if contents_visible
            and definition
            and definition.weight_lb is not None
            and total_weight is not None
            else None,
            "light": self.facts.light_state(sid, oid),
            "spatial": asdict(self.spatial.config(package, oid))
            if self.spatial.config(package, oid)
            else None,
            "occupied_cells": self.spatial.cells(package, oid, obj["cell"]),
            "vision_occlusion": "unsupported",
            "cover_candidates": "unsupported",
            **available,
            "locks_for_object": tuple(
                {"lock_id": lock[0], "locked": bool(lock[1])}
                for lock in self.facts.locks_for_object(sid, oid)
            ),
            "effective_locked": self.facts.door_lock(sid, oid),
            "openable": openable,
            "current_hp": hp,
            "destroyed": destroyed,
            "durability": asdict(definition.capability("durability"))
            if definition and definition.capability("durability")
            else None,
            **config,
            "package_revision": package.package_digest,
            "capabilities": declared,
            "effective_capabilities": () if destroyed else declared,
            "open_state": obj["state"],
            "location": self.facts.object_position(sid, oid),
        }

    def get_object(self, sid, actor_id, oid, package, *, context):
        h = self.handler
        with h._transaction():
            h._spatial_reader(sid, actor_id, package, context)
            return self.view(sid, actor_id, oid, package)

    def list_objects(self, sid, actor_id, package, *, context):
        h = self.handler
        with h._transaction():
            h._spatial_reader(sid, actor_id, package, context)
            result = []
            for obj in h._conn.execute(
                "SELECT object_id FROM objects WHERE session_id=? ORDER BY object_id", (sid,)
            ):
                try:
                    result.append(self.view(sid, actor_id, obj[0], package))
                except StateMachineError as exc:
                    if exc.code != "invalid_target":
                        raise
            return tuple(result)
