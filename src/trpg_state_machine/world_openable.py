"""Openable operations over the single WorldFacts state, in the caller's transaction."""

from dataclasses import asdict

from ._execution import _execution_data, _WorldEffectKind
from ._operations import LockDoor, ObjectState, SetObjectState, UnlockDoor
from .errors import StateMachineError
from .world_commands import BarObject, ClearJam, LockObject, PickLock, UnlockObject


class OpenableService:
    def __init__(self, handler):
        self.h = handler
        self.facts = handler.facts

    def manual_target(self, sid, actor_id, operation, package, *, navigation_scope=None):
        position = self.h._actor_position(sid, actor_id)
        if operation.scene_id != position.scene_id:
            raise StateMachineError("invalid_target", "Interaction must be in the actor's scene")
        definition = self.facts.object_definition(package, operation.object_id)
        capability = definition.capability("openable") if definition else None
        if (
            capability
            and capability.operation_sides
            and position.scene_id not in capability.operation_sides
        ):
            raise StateMachineError(
                "operation_side_forbidden", "Operation unavailable from this side"
            )
        return self.h._manual_object_target(
            sid,
            actor_id,
            operation,
            package,
            navigation_scope=navigation_scope,
        )

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
        obj, passage = self.manual_target(sid, actor_id, operation, package)
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

    def jam_policy(self, package, oid):
        d = self.facts.object_definition(package, oid)
        c = d.capability("openable") if d else None
        return c.jam_policy if c else "prevent_transitions"

    def validate_transition(self, sid, operation, package):
        obj = self.facts.object(sid, operation.object_id)
        if obj is None:
            raise StateMachineError("invalid_target", "No object at target")
        if obj["state"] is None:
            raise StateMachineError("unsupported_object_capability", "Object has no switch")
        self.h._validate_door_occupancy(sid, operation, package)
        self.lock_status(sid, operation.object_id, package)
        self.facts.validate_door_switch(
            sid,
            operation.object_id,
            operation.state.value,
            jam_policy=self.jam_policy(package, operation.object_id),
        )

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
            obj, passage = self.manual_target(
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
        self.facts.switch(
            sid,
            operation.object_id,
            operation.state.value,
            jam_policy=self.jam_policy(package, operation.object_id),
        )
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
        self.manual_target(sid, actor_id, operation, package)
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

    def prepare_check(self, row, actor_id, op, package):
        from dnd5e_engine.evaluation_contracts import CheckPayload

        h, sid = self.h, row["session_id"]
        h._world_operation_capability(row, op)
        h._world_actor_ready(sid, actor_id)
        position = h._public_scope(sid, actor_id, package)
        self.manual_target(sid, actor_id, op, package)
        if position.cell != op.expected_cell:
            raise StateMachineError("position_conflict", "Interaction position changed")
        if self.facts.object_hp(sid, op.object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed object is unusable")
        d = self.facts.object_definition(package, op.object_id)
        c = d.capability("openable") if d else None
        if c is None:
            raise StateMachineError("unsupported_object_capability", "No openable definition")
        method = None
        if type(op) is PickLock:
            locks = d.capability("locks")
            lock = (
                next((lock for lock in locks.locks if lock.lock_id == op.lock_id), None)
                if locks
                else None
            )
            if lock is None:
                raise StateMachineError("invalid_target", "No admitted lock on target")
            if lock.operable_sides and op.scene_id not in lock.operable_sides:
                raise StateMachineError(
                    "operation_side_forbidden", "Lock inaccessible from this side"
                )
            # Identity and access are checked above, before exposing lock state.
            if not bool(
                next(
                    value[1]
                    for value in self.facts.locks_for_object(sid, op.object_id)
                    if value[0] == op.lock_id
                )
            ):
                return None, "pick_lock", "thieves-tools", False, None, True
            if lock.pick_policy == "unconfigured":
                raise StateMachineError(
                    "unsupported_lock_policy", "Picking rules are not configured"
                )
            ability, skill, dc = (
                "dex",
                "sleight_of_hand",
                15 if lock.pick_policy == "srd_lock" else lock.pick_dc,
            )
            tool, leverage, kind = "thieves-tools", False, "pick_lock"
            noop = False
        else:
            method = next((m for m in c.force_methods if m.method_id == op.method_id), None)
            if method is None or (type(op) is ClearJam and not method.clear_jam):
                raise StateMachineError(
                    "unsupported_force_method", "No configured method for this result"
                )
            ability, skill, dc = method.ability, method.skill, method.dc
            tool, leverage = method.required_tool, method.leverage_applicable
            kind = "clear_jam" if type(op) is ClearJam else "force_open"
            state = self.facts.openable_state(sid, op.object_id)
            locks = {
                lock[0]: bool(lock[1]) for lock in self.facts.locks_for_object(sid, op.object_id)
            }
            remaining_obstacle = (
                any(
                    locked
                    for identity, locked in locks.items()
                    if identity != method.unlock_lock_id
                )
                or state["barred"]
                and not method.clear_bar
                or state["jammed"]
                and not method.clear_jam
                and c.jam_policy != "prevent_closing"
            )
            if (
                method.open_after
                and self.facts.object(sid, op.object_id)["state"] != "open"
                and remaining_obstacle
            ):
                raise StateMachineError(
                    "object_obstructed", "Configured opening does not clear remaining obstacles"
                )
            noop = not (
                method.clear_jam
                and state["jammed"]
                or method.clear_bar
                and state["barred"]
                or method.unlock_lock_id
                and locks[method.unlock_lock_id]
                or method.open_after
                and self.facts.object(sid, op.object_id)["state"] != "open"
            )
        if (tool is None) != (op.tool_instance_id is None):
            raise StateMachineError(
                "tool_unavailable", "Method tool and explicit instance must agree"
            )
        if tool is not None:
            obj = self.facts.object(sid, op.tool_instance_id)
            if (
                obj is None
                or self.facts.placement(sid, op.tool_instance_id)["carrier_id"] != actor_id
                or not self.facts.item_accessible(sid, op.tool_instance_id)
            ):
                raise StateMachineError("tool_unavailable", "No accessible owned tool")
        check = CheckPayload(
            actor_id=actor_id,
            ability=ability,
            skill=skill,
            tool=None,
            dc=dc,
            target_id=None,
            context="generic",
            required_sense="none",
            social_interaction=False,
            advantage=(),
            disadvantage=(),
            redeem_granted_die=None,
        )
        return check, kind, tool, leverage, method, bool(noop)

    def apply_check(self, row, actor_id, op, package, plan, *, succeeded):
        self.h.store.require_write()
        if not succeeded:
            return []
        sid, events = row["session_id"], []
        method = plan[4]
        lock_id = op.lock_id if type(op) is PickLock else method.unlock_lock_id
        if lock_id is not None:
            locks = {
                lock[0]: bool(lock[1]) for lock in self.facts.locks_for_object(sid, op.object_id)
            }
            if locks[lock_id]:
                self.facts.set_door_lock(sid, op.object_id, False, lock_id)
                events.append(
                    self.h._capture_event(
                        sid,
                        package,
                        "door.lock_changed",
                        {
                            "scene_id": op.scene_id,
                            "object_id": op.object_id,
                            "lock_id": lock_id,
                            "locked": False,
                            "actor_id": actor_id,
                            "execution": _execution_data(actor_id, None),
                        },
                        phase="world.after_door_lock_update",
                        source_actor_id=actor_id,
                    )
                )
        if method is not None:
            if method.clear_bar and self.facts.openable_state(sid, op.object_id)["barred"]:
                self.facts.set_object_bar(sid, op.object_id, False)
                events.append(
                    self.h._capture_event(
                        sid,
                        package,
                        "object.bar_changed",
                        {
                            "scene_id": op.scene_id,
                            "object_id": op.object_id,
                            "barred": False,
                            "actor_id": actor_id,
                        },
                        phase="world.after_bar_update",
                        source_actor_id=actor_id,
                    )
                )
            if method.clear_jam and self.facts.openable_state(sid, op.object_id)["jammed"]:
                self.facts.clear_object_jam(sid, op.object_id)
                events.append(
                    self.h._capture_event(
                        sid,
                        package,
                        "object.jam_cleared",
                        {"scene_id": op.scene_id, "object_id": op.object_id, "actor_id": actor_id},
                        phase="world.after_jam_update",
                        source_actor_id=actor_id,
                    )
                )
            if method.open_after:
                # The same transition validator still sees every remaining lock/bar/jam.
                events.extend(
                    self.operate_open(
                        row,
                        actor_id,
                        SetObjectState(op.scene_id, op.object_id, ObjectState.OPEN),
                        package,
                    )
                )
        self.h._assert_spatial_consistency(sid, package)
        return events

    def check_availability(self, row, actor_id, op, package):
        from dnd5e_engine.evaluation_openable import OpenableCheckRequest, openable_support_failure

        from .commands import TypedCommand

        plan = self.prepare_check(row, actor_id, op, package)
        if type(op) is PickLock and plan[5]:
            return plan
        rules = getattr(self, "rules", None)
        if rules is None:
            raise StateMachineError("rules_unavailable", "No composed Rule Engine adapter")
        snapshot = rules._noncombat_snapshot(row, package)
        command = TypedCommand(
            row["session_id"], "query", actor_id, "rules.check", plan[0], row["world_version"]
        )
        req = rules.request(command, package, snapshot, rules._rng_context(row))
        spec = OpenableCheckRequest(
            schema_version="engine-openable/1",
            request=req,
            operation=plan[1],
            tool_instance_id=op.tool_instance_id,
            required_tool=plan[2],
            leverage_applicable=plan[3],
        )
        rules._verify_dnd_binding(package)
        failure = openable_support_failure(spec, loader=rules.loader)
        if failure is not None:
            raise StateMachineError(failure["error"].code, "Operation outside supported rule facts")
        return plan
