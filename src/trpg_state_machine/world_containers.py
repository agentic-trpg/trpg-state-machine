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
            root = location["ancestry"][-1] if location["ancestry"] else oid
            h.objects.spatial.contact_cell(sid, actor, root, package)
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
        if not self.h.objects.openable.contents_accessible(sid, oid):
            raise StateMachineError("container_inaccessible", "Container contents are inaccessible")
        return c

    def _rows(self, sid):
        return {
            r["object_id"]: dict(r)
            for r in self.h._conn.execute("SELECT * FROM objects WHERE session_id=?", (sid,))
        }

    def total_weight(self, oid, rows, package, *, path=frozenset()):
        if oid in path or oid not in rows:
            raise StateMachineError("container_cycle", "Invalid parent relation")
        d = self.facts.object_definition(package, oid)
        if d is None or d.weight_lb is None:
            return None
        data = json.loads(rows[oid]["item_json"]) if rows[oid]["item_json"] else {}
        total = d.weight_lb * data.get("quantity", 1)
        for child in rows.values():
            if child["container_id"] == oid:
                weight = self.total_weight(child["object_id"], rows, package, path=path | {oid})
                if weight is None:
                    return None
                total += weight
        return total

    def ancestry(self, oid, rows):
        seen, parents = {oid}, []
        while rows[oid]["container_id"] is not None:
            oid = rows[oid]["container_id"]
            if oid in seen or oid not in rows:
                raise StateMachineError("container_cycle", "Invalid parent relation")
            seen.add(oid)
            parents.append(oid)
        return parents

    def subtree(self, oid, rows):
        affected, pending = set(), [oid]
        while pending:
            current = pending.pop()
            if current in affected:
                raise StateMachineError("container_cycle", "Invalid parent relation")
            affected.add(current)
            pending.extend(r["object_id"] for r in rows.values() if r["container_id"] == current)
        return affected

    def audit(self, rows, package, *, affected=None):
        """Global diagnostic by default; command admission passes its dependency scope."""
        selected = rows.keys() if affected is None else sorted(affected)
        for oid in selected:
            parents = self.ancestry(oid, rows)
            configs = []
            for parent in parents:
                config = self.config(package, parent)
                if config is None:
                    raise StateMachineError(
                        "unsupported_object_capability", "Parent is not a container"
                    )
                configs.append(config)
            if own := self.config(package, oid):
                configs.append(own)
            # Count containers, including a moved container itself, never ordinary items.
            if len(configs) > 2:
                raise StateMachineError(
                    "container_nesting_limit", "Containment exceeds two container levels"
                )
        for oid in selected:
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

    def require_unbound(self, sid, actor, iid, *, rows=None, affected=None):
        h = self.h
        known = set(h.characters.rule_actors(sid))
        if actor not in known:
            raise StateMachineError("unsupported_actor_state", "Explicit mechanical facts required")
        components = h.characters.rule_components(sid, known)
        if any(e["target_id"] == actor and not e["disabled"] for e in components["effect_states"]):
            raise StateMachineError("unsupported_actor_state", "Active effect transfer unsupported")
        rows = self._rows(sid) if rows is None else rows
        affected = self.subtree(iid, rows) if affected is None else affected
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
            if asset and (asset.requires_attunement or asset.passive_effects):
                raise StateMachineError(
                    "unsupported_item_binding", "Item effect/attunement lifecycle is unsupported"
                )
            # Equipment fields are slug-based. Only already-owned instances can be
            # bound to the actor's equipment; ground instances with the same slug cannot.
            if self.facts.placement(sid, oid)["carrier_id"] != actor:
                continue
            if (
                state["weapon_in_hands"] == slug
                or state["other_hand_occupied"]
                or (state["shield_equipped"] and slug == "shield")
                or (
                    state["worn_armor"] is not None
                    and asset is not None
                    and asset.item_kind == "armor"
                    and asset.armor_category == state["worn_armor"]
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
        candidate = self._rows(sid)
        moved = self.subtree(op.instance_id, candidate)
        affected = moved | set(self.ancestry(op.instance_id, candidate))
        self.require_unbound(sid, actor, op.instance_id, rows=candidate, affected=moved)
        candidate[op.instance_id].update(
            carrier_id=actor if type(op) is TakeItem else None,
            container_id=op.container_id if type(op) is PutItem else None,
            scene_id=None,
            cell=None,
        )
        affected.update(self.ancestry(op.instance_id, candidate))
        self.audit(candidate, package, affected=affected)
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
