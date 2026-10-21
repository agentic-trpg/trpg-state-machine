"""World object capability orchestration within the existing command transaction."""

from dataclasses import asdict

from .errors import StateMachineError
from .scoped_reads import scoped_read
from .world_commands import (
    OPENABLE_CHECK_OPERATIONS,
    ActivateMechanism,
    BarObject,
    CheckMechanism,
    LockObject,
    PickLock,
    PutItem,
    RepositionObject,
    ServiceMechanism,
    SetLightEnabled,
    SwitchLightMode,
    TakeItem,
    UnbarObject,
    UnlockObject,
)
from .world_containers import ContainerService
from .world_lights import LightService
from .world_mechanisms import MechanismService
from .world_openable import OpenableService
from .world_spatial import ObjectSpatialService


class WorldObjectService:
    def __init__(self, handler):
        self.handler = handler
        self.facts = handler.facts
        self.containers = ContainerService(handler)
        self.spatial = ObjectSpatialService(handler)
        self.lights = LightService(handler)
        self.mechanisms = MechanismService(handler)
        self.openable = OpenableService(handler)

    def validate(self, row, actor_id, operation, package):
        if type(operation) in OPENABLE_CHECK_OPERATIONS:
            return self.openable.check_availability(row, actor_id, operation, package)
        if type(operation) is CheckMechanism:
            return self.mechanisms.check_availability(row, actor_id, operation, package)
        if type(operation) is ServiceMechanism:
            return self.mechanisms.method_plan(row, actor_id, operation, package)
        if type(operation) is ActivateMechanism:
            self.mechanisms.validate(row, actor_id, operation, package)
            return
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
            return self.openable.validate_lock(row, actor_id, operation, package)
        return self.openable.validate_bar(row, actor_id, operation, package)

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
                plan = self.validate(row, actor_id, operation, package)
            except StateMachineError as exc:
                return WorldActionAvailability("unavailable", exc.code, row["world_version"])
            reason = "already_unlocked" if type(operation) is PickLock and plan[5] else "available"
            return WorldActionAvailability("available", reason, row["world_version"])

    def operate(self, row, actor_id, operation, package, *, source_command_id=None):
        if type(operation) in OPENABLE_CHECK_OPERATIONS:
            raise StateMachineError(
                "invalid_execution_origin", "Object checks require shared rule execution"
            )
        if type(operation) is ServiceMechanism:
            return self.mechanisms.service(row, actor_id, operation, package)
        if type(operation) is ActivateMechanism:
            return self.mechanisms.operate(row, actor_id, operation, package, source_command_id)
        if type(operation) in (SetLightEnabled, SwitchLightMode):
            return self.lights.operate(row, actor_id, operation, package)
        if type(operation) is RepositionObject:
            return self.spatial.operate(row, actor_id, operation, package)
        if type(operation) in (PutItem, TakeItem):
            return self.containers.operate(row, actor_id, operation, package)
        if type(operation) in (LockObject, UnlockObject):
            return self.openable.operate_lock(row, actor_id, operation, package)
        return self.openable.operate_bar(row, actor_id, operation, package)

    def _visible(self, sid, actor_id, oid, package):
        h = self.handler
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
        if not self.spatial.visible_cells(sid, actor_id, oid, package):
            raise StateMachineError("invalid_target", "No authorized local object")
        return obj

    @scoped_read
    def _view_inputs(self, sid, actor_id):
        return self.handler._actor_position(sid, actor_id), self.handler._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()

    def view(self, sid, actor_id, oid, package, *, containment=None):
        obj = self._visible(sid, actor_id, oid, package)
        definition = self.facts.object_definition(sid, oid)
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
        projection = self.spatial.project(sid, oid, package)
        from ._operations import ObjectState, SetObjectState

        position, row = self._view_inputs(sid, actor_id)
        available = {}
        for name, op in (
            ("can_activate", ActivateMechanism(position.scene_id, oid, position.cell)),
            ("can_open", SetObjectState(position.scene_id, oid, ObjectState.OPEN)),
            ("can_close", SetObjectState(position.scene_id, oid, ObjectState.CLOSED)),
            ("can_bar", BarObject(position.scene_id, oid, position.cell)),
            ("can_unbar", UnbarObject(position.scene_id, oid, position.cell)),
        ):
            try:
                if isinstance(op, SetObjectState):
                    self.openable.validate_open(row, actor_id, op, package)
                else:
                    self.validate(row, actor_id, op, package)
                available[name] = True
            except StateMachineError:
                available[name] = False
        contents_visible = self.containers.config(sid, oid) and self.openable.contents_accessible(
            sid, oid
        )
        contents_weight = self.containers.contents_weight(sid, oid) if contents_visible else None
        return {
            "contents": tuple(c["object_id"] for c in self.facts.contents(sid, oid))
            if contents_visible
            else None,
            "contents_weight_lb": contents_weight
            if definition and definition.weight_lb is not None
            else None,
            "mechanism_status": self.facts.mechanism_status(sid, oid),
            "mechanism_state": asdict(self.facts.mechanism_state(sid, oid))
            if self.facts.mechanism_state(sid, oid)
            else None,
            "linked_result_preview": "hidden" if self.mechanisms.config(sid, oid) else None,
            "light": self.facts.light_state(sid, oid),
            "spatial": asdict(self.spatial.config(sid, oid))
            if self.spatial.config(sid, oid)
            else None,
            "orientation": obj["orientation"],
            "occupied_cells": projection.occupied_cells,
            "movement_blocking_cells": projection.movement_blocking_cells,
            "difficult_terrain_cells": projection.difficult_terrain_cells,
            "vision_occlusion": projection.vision_occlusion_cells,
            "cover_candidates": projection.cover_cells,
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
        if type(oid) is not str or not 0 < len(oid) <= 256:
            raise StateMachineError("invalid_query", "Object identity must be a bounded string")
        h = self.handler
        with h._transaction():
            h._spatial_reader(sid, actor_id, package, context)
            return self.view(sid, actor_id, oid, package)

    def list_objects(self, sid, actor_id, package, *, context):
        h = self.handler
        with h._transaction():
            h._spatial_reader(sid, actor_id, package, context)
            result = []
            position = h._public_scope(sid, actor_id, package)
            doors = tuple(
                p.door_object_id
                for p in self.facts.instances.passages(sid, scene_id=position.scene_id)
                if p.door_object_id and any(e.scene_id == position.scene_id for e in p.endpoints)
            )
            candidates = self.facts.scene_object_rows(
                sid, position.scene_id, carrier_id=actor_id, extra=doors
            )
            for obj in candidates:
                try:
                    result.append(self.view(sid, actor_id, obj["object_id"], package))
                except StateMachineError as exc:
                    if exc.code != "invalid_target":
                        raise
            return tuple(result)
