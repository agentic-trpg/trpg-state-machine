"""Bounded containment; parent links own location and inventory is a projection."""

from collections.abc import Mapping
from dataclasses import asdict
from types import MappingProxyType

from .errors import StateMachineError
from .runtime_instances import instance_read
from .world_commands import PutItem, TakeItem


class ContainmentState(Mapping):
    """One detached relationship candidate and its local adjacency/weight memo.

    Rows cannot change behind the index. relocate updates the candidate index and
    expires local weights. Only unchanged subtrees in the originating scope/stage
    can reuse instance weights; modified ancestors read this isolated candidate.
    """

    def __init__(self, rows, *, source_stage=None, source_scope=None):
        self._rows, self.children = {}, {}
        for r in rows.values():
            oid = r["object_id"]
            self._rows[oid] = MappingProxyType(dict(r))
            self.children.setdefault(r["container_id"], []).append(oid)
        self.weights = {}
        self.definitions = {}
        self.source_stage = source_stage
        self.source_scope = source_scope
        self.weight_dirty = set()

    def __getitem__(self, key):
        return self._rows[key]

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)

    def contents(self, oid):
        return tuple(self._rows[i] for i in self.children.get(oid, ()))

    def relocate(self, oid, **changes):
        def ancestors(current):
            result = set()
            while current in self._rows and current not in result:
                result.add(current)
                current = self._rows[current]["container_id"]
            return result

        before = self._rows[oid]
        self.weight_dirty.update(ancestors(before["container_id"]))
        after = {**before, **changes}
        old, new = before["container_id"], after["container_id"]
        if old != new:
            self.children[old].remove(oid)
            self.children.setdefault(new, []).append(oid)
        self._rows[oid] = MappingProxyType(after)
        self.weight_dirty.update(ancestors(after["container_id"]))
        if any(k not in ("container_id", "carrier_id", "scene_id", "cell") for k in changes):
            self.weight_dirty.add(oid)
        self.weights.clear()

    def definition(self, facts, oid):
        if oid not in self.definitions:
            self.definitions[oid] = facts.object_definition(self[oid]["session_id"], oid)
        return self.definitions[oid]


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

    def config(self, sid, oid):
        d = self.facts.object_definition(sid, oid)
        return d.capability("container") if d else None

    def require_contents_access(self, sid, oid, package):
        c = self.config(sid, oid)
        if c is None:
            raise StateMachineError(
                "unsupported_object_capability", "Object has no container capability"
            )
        if not self.h.objects.openable.contents_accessible(sid, oid):
            raise StateMachineError("container_inaccessible", "Container contents are inaccessible")
        return c

    def _rows(self, sid, roots=None):
        return ContainmentState(
            {
                r["object_id"]: dict(r)
                for r in (
                    self.h._conn.execute("SELECT * FROM objects WHERE session_id=?", (sid,))
                    if roots is None
                    else self.facts.related_objects(sid, roots)
                )
            },
            source_stage=self.h._conn.total_changes,
            source_scope=self.h.store._scope,
        )

    @staticmethod
    def prepare(rows):
        return rows if isinstance(rows, ContainmentState) else ContainmentState(rows)

    def total_weight(self, oid, rows, package, *, path=frozenset()):
        if self.h.store._scope is None:
            with self.h.store.transaction():
                return self.total_weight(oid, rows, package, path=path)
        state = self.prepare(rows)
        return self._weight(oid, state, path)

    @instance_read("object", "container", "own_weight")
    def own_weight(self, sid, oid):
        encoded = self.facts.instances.get(sid, "object", oid)["definition"]
        definition = self.h.store.memory.json(encoded)
        row = self.h._conn.execute(
            "SELECT item_json FROM objects WHERE session_id=? AND object_id=?", (sid, oid)
        ).fetchone()
        if row is None:
            raise StateMachineError("invalid_target", "Object does not exist")
        data = self.h.store.memory.json(row["item_json"]) if row["item_json"] else {}
        weight = definition["fields"]["weight_lb"] if definition else None
        return None if weight is None else weight * data.get("quantity", 1)

    @instance_read("object", "container", "total_weight", capacity=4)
    def current_weight(self, sid, oid, *, path=frozenset()):
        if oid in path:
            raise StateMachineError("container_cycle", "Invalid parent relation")
        children = self.facts.contents(sid, oid)
        encoded = self.facts.instances.get(sid, "object", oid)["definition"]
        definition = self.h.store.memory.json(encoded)
        if children and (
            definition is None
            or not any(
                c["type"] == "Container" for c in definition["fields"]["capabilities"]["tuple"]
            )
        ):
            raise StateMachineError("unsupported_object_capability", "Parent is not a container")
        total = self.own_weight(sid, oid)
        for child in children:
            weight = self.current_weight(sid, child["object_id"], path=path | {oid})
            total = None if total is None or weight is None else total + weight
        return total

    @instance_read("object", "container", "contents_weight")
    def contents_weight(self, sid, oid):
        total = 0
        for child in self.facts.contents(sid, oid):
            weight = self.current_weight(sid, child["object_id"], path=frozenset((oid,)))
            total = None if total is None or weight is None else total + weight
        return total

    def _weight(self, oid, state, path):
        if oid in path or oid not in state:
            raise StateMachineError("container_cycle", "Invalid parent relation")
        if oid in state.weights:
            return state.weights[oid]
        if (
            state.source_stage is not None
            and state.source_scope is self.h.store._scope
            and state.source_scope is not None
            and state.source_stage == self.h._conn.total_changes
            and oid not in state.weight_dirty
        ):
            total = self.current_weight(state[oid]["session_id"], oid)
            state.weights[oid] = total
            return total
        d = state.definition(self.facts, oid)
        children = state.contents(oid)
        if children and (d is None or d.capability("container") is None):
            raise StateMachineError("unsupported_object_capability", "Parent is not a container")
        data = self.h.store.memory.json(state[oid]["item_json"]) if state[oid]["item_json"] else {}
        total = None if d is None or d.weight_lb is None else d.weight_lb * data.get("quantity", 1)
        for child in children:
            weight = self._weight(child["object_id"], state, path | {oid})
            total = None if total is None or weight is None else total + weight
        state.weights[oid] = total
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
        rows = self.prepare(rows)
        affected, pending = set(), [oid]
        while pending:
            current = pending.pop()
            if current in affected or current not in rows:
                raise StateMachineError("container_cycle", "Invalid parent relation")
            affected.add(current)
            pending.extend(r["object_id"] for r in rows.contents(current))
        return affected

    def audit(self, rows, package, *, affected=None):
        """Global diagnostic by default; command admission passes its dependency scope."""
        rows = self.prepare(rows)
        selected = rows.keys() if affected is None else sorted(affected)
        for oid in selected:
            parents = self.ancestry(oid, rows)
            configs = []
            for parent in parents:
                d = rows.definition(self.facts, parent)
                config = d.capability("container") if d else None
                if config is None:
                    raise StateMachineError(
                        "unsupported_object_capability", "Parent is not a container"
                    )
                configs.append(config)
            d = rows.definition(self.facts, oid)
            if own := d.capability("container") if d else None:
                configs.append(own)
            # Count containers, including a moved container itself, never ordinary items.
            if len(configs) > 2:
                raise StateMachineError(
                    "container_nesting_limit", "Containment exceeds two container levels"
                )
        for oid in selected:
            d = rows.definition(self.facts, oid)
            c = d.capability("container") if d else None
            contents = rows.contents(oid)
            if c is None:
                if contents:
                    raise StateMachineError(
                        "unsupported_object_capability", "Parent is not a container"
                    )
                continue
            weights, volumes = [], []
            for item in contents:
                d = rows.definition(self.facts, item["object_id"])
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
                quantity = self.h.store.memory.json(item["item_json"])["quantity"]
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
        actors = h.characters.rule_actors(sid, {actor})
        if actor not in actors:
            raise StateMachineError("unsupported_actor_state", "Explicit mechanical facts required")
        components = h.characters.rule_components(sid, {actor})
        if any(e["target_id"] == actor and not e["disabled"] for e in components["effect_states"]):
            raise StateMachineError("unsupported_actor_state", "Active effect transfer unsupported")
        rows = self._rows(sid, (iid,)) if rows is None else rows
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
        state = actors[actor]
        for oid in affected:
            obj = self.facts.object(sid, oid)
            if not obj["item_json"]:
                continue
            slug = self.facts.require_item_unit(obj)["item_slug"]
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
        if not self.facts.instances.is_portable(sid, op.instance_id):
            raise StateMachineError(
                "unsupported_object_capability", "Only approved portable instances"
            )
        if type(op) is TakeItem and item["container_id"] != op.container_id:
            raise StateMachineError("position_conflict", "Item is not in the specified container")
        candidate = self._rows(sid, (op.instance_id, op.container_id))
        moved = self.subtree(op.instance_id, candidate)
        affected = moved | set(self.ancestry(op.instance_id, candidate))
        self.require_unbound(sid, actor, op.instance_id, rows=candidate, affected=moved)
        candidate.relocate(
            op.instance_id,
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
        inventory_change = h.characters.prepare_inventory_change(sid, (op.instance_id,))
        after = self.facts.transfer_item(
            sid,
            item,
            carrier_id=actor if type(op) is TakeItem else None,
            container_id=op.container_id if type(op) is PutItem else None,
        )
        h.characters.project_item_transfer(sid, item, after, prepared=inventory_change)
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
