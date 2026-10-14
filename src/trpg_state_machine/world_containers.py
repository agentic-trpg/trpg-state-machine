"""Bounded containment; parent links own location and inventory is a projection."""

import json
from dataclasses import asdict

from .errors import StateMachineError
from .world_commands import PutItem, TakeItem


class ContainerService:
    def __init__(self, handler):
        self.h = handler
        self.facts = handler.facts

    def reachable(self, sid, actor, oid, package):
        h = self.h
        position = h._public_scope(sid, actor, package)
        obj = self.facts.object(sid, oid)
        if obj is None:
            raise StateMachineError("invalid_target", "No authorized local object")
        location = self.facts.placement(sid, oid)
        if location["scene_id"] != position.scene_id or (
            location["carrier_id"] is not None and location["carrier_id"] != actor
        ):
            raise StateMachineError("invalid_target", "No authorized local object")
        if not self.facts.item_accessible(sid, oid, include_self=False):
            raise StateMachineError(
                "container_inaccessible", "An enclosing container is inaccessible"
            )
        if location["carrier_id"] is None:
            cell = location["cell"]
            base = h._base_topology(sid, position.scene_id, package)
            topology = h._movement_topology(sid, position.scene_id, package, exclude_actor=actor)
            if cell is None or (cell != position.cell and not base.can_see(position.cell, cell)):
                raise StateMachineError("invalid_target", "No authorized local object")
            if cell != position.cell and topology.edge_distance(position.cell, cell) is None:
                raise StateMachineError("object_out_of_reach", "Object requires adjacent access")
        return obj

    def config(self, package, oid):
        d = self.facts.object_definition(package, oid)
        return d.capability("container") if d else None

    def require_contents_access(self, sid, oid, package):
        c = self.config(package, oid)
        if c is None:
            raise StateMachineError(
                "unsupported_object_capability", "Object has no container capability"
            )
        obj = self.facts.object(sid, oid)
        if self.facts.object_hp(sid, oid) != 0 and (
            obj["state"] == "closed" or self.facts.door_lock(sid, oid)
        ):
            raise StateMachineError("container_inaccessible", "Container is closed or locked")
        return c

    def _rows(self, sid):
        return {
            r["object_id"]: dict(r)
            for r in self.h._conn.execute("SELECT * FROM objects WHERE session_id=?", (sid,))
        }

    def total_weight(self, oid, rows, package):
        d = self.facts.object_definition(package, oid)
        if d is None or d.weight_lb is None:
            return None
        data = json.loads(rows[oid]["item_json"]) if rows[oid]["item_json"] else {}
        total = d.weight_lb * data.get("quantity", 1)
        for child in rows.values():
            if child["container_id"] == oid:
                weight = self.total_weight(child["object_id"], rows, package)
                if weight is None:
                    return None
                total += weight
        return total

    def audit(self, rows, package):
        for oid, obj in rows.items():
            seen, current, depth = {oid}, obj, 0
            while current["container_id"] is not None:
                parent = current["container_id"]
                if parent in seen or parent not in rows:
                    raise StateMachineError("container_cycle", "Invalid parent relation")
                seen.add(parent)
                depth += 1
                config = self.config(package, parent)
                if config is None:
                    raise StateMachineError(
                        "unsupported_object_capability", "Parent is not a container"
                    )
                if depth > config.nesting_limit or depth > 8:
                    raise StateMachineError(
                        "container_nesting_limit", "Containment depth exceeds policy"
                    )
                current = rows[parent]
        for oid in rows:
            c = self.config(package, oid)
            if c is None:
                continue
            contents = [r for r in rows.values() if r["container_id"] == oid]
            weights, volumes = [], []
            for item in contents:
                d = self.facts.object_definition(package, item["object_id"])
                if d is None or d.fit_profile is None or d.content_kind is None:
                    raise StateMachineError(
                        "unknown_item_fit", "Explicit fit/content metadata required"
                    )
                if (
                    d.fit_profile not in c.opening_profiles
                    or d.content_kind not in c.allowed_content_kinds
                ):
                    raise StateMachineError(
                        "container_rejects_item", "Content or opening policy rejects item"
                    )
                weights.append(self.total_weight(item["object_id"], rows, package))
                quantity = json.loads(item["item_json"])["quantity"]
                volumes.append(None if d.volume_cu_ft is None else d.volume_cu_ft * quantity)
            for limit, values in (
                (c.capacity_weight_lb, weights),
                (c.capacity_volume_cu_ft, volumes),
            ):
                if limit == "unknown":
                    raise StateMachineError(
                        "unknown_container_capacity", "Container capacity is unknown"
                    )
                if limit is not None:
                    if any(v is None for v in values):
                        raise StateMachineError(
                            "unknown_item_capacity", "Required item measurements are unknown"
                        )
                    if sum(values) > limit:
                        raise StateMachineError(
                            "container_capacity_exceeded", "Container capacity exceeded"
                        )

    def require_unbound(self, sid, actor, iid):
        h = self.h
        known = set(h.characters.rule_actors(sid))
        if actor not in known:
            raise StateMachineError("unsupported_actor_state", "Explicit mechanical facts required")
        components = h.characters.rule_components(sid, known)
        if any(e["target_id"] == actor for e in components["effect_states"]):
            raise StateMachineError("unsupported_actor_state", "Active effect transfer unsupported")
        affected = {iid}
        for obj in self._rows(sid).values():
            if iid in self.facts.placement(sid, obj["object_id"])["ancestry"]:
                affected.add(obj["object_id"])
        casting = components["casting_state"]
        if casting and any(
            m["inventory_instance_id"] in affected
            for owner in casting["actors"]
            for m in owner["materials"]
        ):
            raise StateMachineError(
                "unsupported_actor_activity", "Casting-bound item transfer unsupported"
            )
        state = h.characters.rule_actors(sid)[actor]
        for oid in affected:
            obj = self.facts.object(sid, oid)
            if not obj["item_json"]:
                continue
            slug = json.loads(obj["item_json"])["item_slug"]
            asset = h.loader.get_item(slug)
            if (
                state["weapon_in_hands"] == slug
                or state["other_hand_occupied"]
                or (state["shield_equipped"] and slug == "shield")
                or (
                    state["worn_armor"] is not None
                    and asset is not None
                    and asset.item_kind == "armor"
                )
            ):
                raise StateMachineError(
                    "equipped_item_unsupported", "Equipment binding cannot be safely moved"
                )

    def validate(self, row, actor, op, package):
        h, sid = self.h, row["session_id"]
        h._world_operation_capability(row, op)
        h._world_actor_ready(sid, actor)
        position = h._public_scope(sid, actor, package)
        if (position.scene_id, position.cell) != (op.scene_id, op.expected_cell):
            raise StateMachineError("position_conflict", "Actor position changed")
        item = self.reachable(sid, actor, op.instance_id, package)
        self.reachable(sid, actor, op.container_id, package)
        self.require_contents_access(sid, op.container_id, package)
        if item["item_json"] is None or op.instance_id not in {
            i.instance_id for i in package.portable_items
        }:
            raise StateMachineError(
                "unsupported_object_capability", "Only approved portable instances"
            )
        if type(op) is TakeItem and item["container_id"] != op.container_id:
            raise StateMachineError("position_conflict", "Item is not in the specified container")
        self.require_unbound(sid, actor, op.instance_id)
        candidate = self._rows(sid)
        candidate[op.instance_id].update(
            carrier_id=actor if type(op) is TakeItem else None,
            container_id=op.container_id if type(op) is PutItem else None,
            scene_id=None,
            cell=None,
        )
        self.audit(candidate, package)
        return item

    def operate(self, row, actor, op, package):
        item = self.validate(row, actor, op, package)
        h, sid = self.h, row["session_id"]
        if type(op) is PutItem and item["container_id"] == op.container_id:
            return []
        events = [
            h._capture_event(
                sid,
                package,
                "item.container_transferred",
                {**asdict(op), "actor_id": actor, "phase": "before"},
                phase="world.before_container_transfer",
                source_actor_id=op.instance_id,
            )
        ]
        after = self.facts.transfer_item(
            sid,
            item,
            carrier_id=actor if type(op) is TakeItem else None,
            container_id=op.container_id if type(op) is PutItem else None,
        )
        h.characters.project_item_transfer(sid, item, after)
        events.append(
            h._capture_event(
                sid,
                package,
                "item.container_transferred",
                {**asdict(op), "actor_id": actor, "phase": "after"},
                phase="world.after_container_transfer",
                source_actor_id=op.instance_id,
            )
        )
        return events
