"""Current dynamic movement projection, using the existing grid and actual facts."""

from dataclasses import asdict

from .errors import StateMachineError


class ObjectSpatialService:
    def __init__(self, handler):
        self.h = handler

    def config(self, package, oid):
        d = self.h.facts.object_definition(package, oid)
        return d.capability("spatial") if d else None

    def blocks(self, sid, oid, package):
        c = self.config(package, oid)
        if c is None:
            return None
        obj = self.h.facts.object(sid, oid)
        d = self.h.facts.object_definition(package, oid)
        durability = d.capability("durability")
        if (
            self.h.facts.object_hp(sid, oid) == 0
            and durability.destruction_policy_ref == "disable_spatial_policy"
        ):
            return c.destroyed_blocks_movement
        return c.open_blocks_movement if obj["state"] == "open" else c.closed_blocks_movement

    def cells(self, package, oid, anchor):
        c = self.config(package, oid)
        if c is None or anchor is None:
            return ()
        try:
            x, y = map(int, anchor.split(","))
        except (ValueError, AttributeError) as exc:
            raise StateMachineError("invalid_spatial_state", "Invalid object anchor") from exc
        return tuple(f"{x + dx},{y + dy}" for dx, dy in c.footprint)

    def movement_cells(self, sid, scene_id, package, *, exclude_object=None):
        cells = set()
        doors = {p.door_object_id for p in package.passages if p.door_object_id}
        for obj in self.h._conn.execute(
            "SELECT object_id,cell FROM objects WHERE session_id=? AND scene_id=?", (sid, scene_id)
        ):
            oid = obj["object_id"]
            if oid != exclude_object and oid not in doors and self.blocks(sid, oid, package):
                cells.update(self.cells(package, oid, obj["cell"]))
        return cells

    def validate_placement(self, row, actor, op, package):
        h, sid = self.h, row["session_id"]
        h._world_operation_capability(row, op)
        h._world_actor_ready(sid, actor)
        position = h._public_scope(sid, actor, package)
        obj = h.facts.object(sid, op.object_id)
        c = self.config(package, op.object_id)
        if c is None or c.mobility_policy != "gm_placement":
            raise StateMachineError(
                "unsupported_object_capability", "Object has no GM placement policy"
            )
        if obj is None or obj["scene_id"] != op.scene_id or position.scene_id != op.scene_id:
            raise StateMachineError(
                "invalid_target", "Only actual local ground objects can be placed"
            )
        if obj["cell"] != op.expected_cell:
            raise StateMachineError("position_conflict", "Object anchor changed")
        if any(p.door_object_id == op.object_id for p in package.passages):
            raise StateMachineError(
                "unsupported_object_capability", "Shared passage objects are fixed"
            )
        topology = h._movement_topology(sid, op.scene_id, package, exclude_object=op.object_id)
        cells = self.cells(package, op.object_id, op.to_cell)
        thresholds = {
            cell
            for p in package.passages
            for e in p.endpoints
            if e.scene_id == op.scene_id
            for cell in (e.threshold_cell, e.standing_cell)
        }
        if any(not topology.is_valid_cell(cell) or cell in thresholds for cell in cells):
            raise StateMachineError(
                "object_placement_blocked", "Footprint is outside bounds or occupied"
            )

    def operate(self, row, actor, op, package):
        self.validate_placement(row, actor, op, package)
        h, sid = self.h, row["session_id"]
        if op.expected_cell == op.to_cell:
            return []
        h.facts.place(sid, op.object_id, op.to_cell)
        return [
            h._capture_event(
                sid,
                package,
                "object.placed",
                {**asdict(op), "actor_id": actor},
                phase="world.after_object_placement",
                source_actor_id=actor,
            )
        ]

    def audit(self, sid, package):
        h = self.h
        doors = {p.door_object_id for p in package.passages if p.door_object_id}
        for scene in package.scenes:
            if scene.grid_json is None or h._spatial_scene(sid, scene.scene_id) is None:
                continue
            base = h._base_topology(sid, scene.scene_id)
            actors, objects = h._scene_occupants(sid, scene.scene_id, package)
            occupied = {cell for _, cell in actors}
            blocking = set()
            for oid, anchor in objects:
                if self.config(package, oid) is None or oid in doors:
                    continue
                cells = self.cells(package, oid, anchor)
                if any(not base.is_valid_cell(cell) for cell in cells):
                    raise StateMachineError(
                        "invalid_spatial_state", "Object footprint conflicts with grid"
                    )
                if self.blocks(sid, oid, package):
                    if set(cells) & (occupied | blocking):
                        raise StateMachineError(
                            "invalid_spatial_state", "Blocking footprints overlap"
                        )
                    blocking.update(cells)
