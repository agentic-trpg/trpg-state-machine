"""Derived object geometry, candidate validation and bounded full-cell contributions.

WorldFacts owns anchors/orientations/state. Nothing derived here is persisted.
North is the definition's footprint; x grows east, y grows south. Clockwise
quarter turns about the anchor map (dx,dy) to (-dy,dx), (-dx,-dy), (dy,-dx).
"""

from dataclasses import asdict, dataclass, replace

from dnd5e_engine.spatial import canonical_cell_id

from .errors import StateMachineError

ORIENTATIONS = ("north", "east", "south", "west")


@dataclass(frozen=True, slots=True)
class ObjectSpatialState:
    scene_id: str | None
    anchor: str | None
    orientation: str
    open_state: str | None
    destroyed: bool


@dataclass(frozen=True, slots=True)
class ObjectSpatialProjection:
    """Object-side inputs for future terrain consumers, not a mechanical Grid."""

    occupied_cells: tuple[str, ...]
    movement_blocking_cells: tuple[str, ...] = ()
    difficult_terrain_cells: tuple[str, ...] = ()
    vision_occlusion_cells: tuple[str, ...] = ()
    cover_cells: tuple[tuple[str, str], ...] = ()


class ObjectSpatialService:
    def __init__(self, handler):
        self.h = handler

    def config(self, package, oid):
        d = self.h.facts.object_definition(package, oid)
        return d.capability("spatial") if d else None

    @staticmethod
    def _legacy(package, oid):
        return next(o for s in package.scenes for o in s.objects if o.object_id == oid)

    def state(self, sid, oid, package):
        obj = self.h.facts.object(sid, oid)
        if obj is not None:
            return ObjectSpatialState(
                obj["scene_id"],
                obj["cell"],
                obj["orientation"],
                obj["state"],
                self.h.facts.object_hp(sid, oid) == 0,
            )
        # Unvisited scenes have configured initial facts, never fallback over a live row.
        initial = self._legacy(package, oid)
        d = self.h.facts.object_definition(package, oid)
        durability = d.capability("durability") if d else None
        scene = next(s.scene_id for s in package.scenes if initial in s.objects)
        return ObjectSpatialState(
            scene,
            initial.initial_cell,
            initial.initial_orientation,
            initial.state,
            bool(durability and durability.initial_hp == 0),
        )

    def blocks(self, sid, oid, package, *, candidate=None):
        c = self.config(package, oid)
        if c is None:
            return None
        state = candidate or self.state(sid, oid, package)
        return getattr(c, self._property_state(package, oid, state) + "_blocks_movement")

    def _property_state(self, package, oid, state):
        d = self.h.facts.object_definition(package, oid)
        durability = d.capability("durability") if d else None
        if (
            state.destroyed
            and durability
            and (durability.destruction_policy_ref == "disable_spatial_policy")
        ):
            return "destroyed"
        return "open" if state.open_state == "open" else "closed"

    def cells(self, package, oid, anchor, orientation="north"):
        c = self.config(package, oid)
        if orientation not in (c.allowed_orientations if c else ("north",)):
            raise StateMachineError("invalid_spatial_state", "Object orientation is not allowed")
        if anchor is None:
            return ()
        try:
            if type(anchor) is not str or canonical_cell_id(anchor) != anchor:
                raise ValueError("Noncanonical anchor")
            x, y = map(int, anchor.split(","))
        except (ValueError, AttributeError) as exc:
            raise StateMachineError("invalid_spatial_state", "Invalid object anchor") from exc
        offsets = c.footprint if c else ((0, 0),)
        turns = ORIENTATIONS.index(orientation)
        result = []
        for dx, dy in offsets:
            for _ in range(turns):
                dx, dy = -dy, dx
            result.append(f"{x + dx},{y + dy}")
        return tuple(result)

    def project(self, sid, oid, package, *, candidate=None):
        state = candidate or self.state(sid, oid, package)
        cells = self.cells(package, oid, state.anchor, state.orientation)
        c = self.config(package, oid)
        if c is None:
            return ObjectSpatialProjection(
                cells, cells if self._legacy(package, oid).blocks_passage else ()
            )
        props = getattr(c, self._property_state(package, oid, state) + "_properties")
        return ObjectSpatialProjection(
            cells,
            cells if self.blocks(sid, oid, package, candidate=state) else (),
            cells if props.difficult_terrain else (),
            cells if props.vision_occlusion else (),
            tuple((cell, props.cover) for cell in cells) if props.cover != "none" else (),
        )

    def target_cells(self, sid, scene_id, oid, package):
        ports = self.h._door_ports(scene_id, oid, package)
        if ports:
            return tuple(dict.fromkeys(e.threshold_cell for _, e in ports))
        state = self.state(sid, oid, package)
        return self.project(sid, oid, package).occupied_cells if state.scene_id == scene_id else ()

    def visible_cells(self, sid, actor, oid, package):
        position = self.h._public_scope(sid, actor, package)
        base = self.h._base_topology(sid, position.scene_id, package)
        return tuple(
            cell
            for cell in self.target_cells(sid, position.scene_id, oid, package)
            if base.can_see(position.cell, cell)
        )

    def contact_cell(self, sid, actor, oid, package):
        """Nearest visible legal adjacent contact, excluding only the target itself.

        Business authorization (door standing ports, container access, tools, etc.)
        belongs to callers. This helper does not authorize an operation.
        """
        position = self.h._public_scope(sid, actor, package)
        visible = self.visible_cells(sid, actor, oid, package)
        # Existing manual access permits contact at the actor's own cell even
        # without visual light there (e.g. an owned local emitter switch).
        if position.cell in self.target_cells(sid, position.scene_id, oid, package):
            visible = tuple(dict.fromkeys((*visible, position.cell)))
        if not visible:
            raise StateMachineError("invalid_target", "No authorized local object")
        movement = self.h._movement_topology(
            sid, position.scene_id, package, exclude_actor=actor, exclude_object=oid
        )
        reachable = [
            cell
            for cell in visible
            if cell == position.cell or movement.edge_distance(position.cell, cell) is not None
        ]
        if not reachable:
            raise StateMachineError("object_out_of_reach", "Object requires clear adjacent reach")
        return min(reachable, key=lambda cell: (movement.distance_ft(position.cell, cell), cell))

    def movement_cells(self, sid, scene_id, package, *, exclude_object=None):
        cells = set()
        doors = {p.door_object_id for p in package.passages if p.door_object_id}
        _, objects = self.h._scene_occupants(sid, scene_id, package)
        for oid, _ in objects:
            if oid != exclude_object and oid not in doors and self.config(package, oid):
                cells.update(self.project(sid, oid, package).movement_blocking_cells)
        return cells

    def scene_contributions(self, sid, scene_id, package):
        """Trusted composition seam: per-object full-cell inputs for one local map.

        This is not an actor-authorized catalogue or a merged terrain system.
        Shared doors contribute at this scene's endpoints, not their stored anchor.
        """
        _, objects = self.h._scene_occupants(sid, scene_id, package)
        local_doors = {
            p.door_object_id
            for p in package.passages
            if p.door_object_id and any(e.scene_id == scene_id for e in p.endpoints)
        }
        result = {}
        for oid in sorted({oid for oid, _ in objects} | local_doors):
            projection = self.project(sid, oid, package)
            if oid in local_doors:
                cells = self.target_cells(sid, scene_id, oid, package)
                projection = ObjectSpatialProjection(
                    cells,
                    cells
                    if self.blocks(sid, oid, package)
                    or (
                        self.config(package, oid) is None
                        and self.state(sid, oid, package).open_state == "closed"
                    )
                    else (),
                    cells if projection.difficult_terrain_cells else (),
                    cells if projection.vision_occlusion_cells else (),
                    tuple((cell, projection.cover_cells[0][1]) for cell in cells)
                    if projection.cover_cells
                    else (),
                )
            result[oid] = projection
        return result

    def _check_scene(
        self, sid, scene_id, package, *, replacement=None, error_code="invalid_spatial_state"
    ):
        """The same rules for candidate admission and final audit.

        Occupancy is distinct from blocking. Only movement-blocking objects
        conflict with actors/other blockers; nonblocking overlap stays supported.
        """
        h = self.h
        base = h._base_topology(sid, scene_id)
        walls = h._scene_grid(sid, scene_id, package)["wall_segments"]
        actors, objects = h._scene_occupants(sid, scene_id, package)
        actor_cells = {cell for _, cell in actors if cell is not None}
        blocking = set()
        doors = {p.door_object_id for p in package.passages if p.door_object_id}
        reserved = {
            cell
            for p in package.passages
            for e in p.endpoints
            if e.scene_id == scene_id
            for cell in (e.threshold_cell, e.standing_cell)
        }
        for oid, _ in objects:
            state = replacement[1] if replacement and replacement[0] == oid else None
            projection = self.project(sid, oid, package, candidate=state)
            cells = set(projection.occupied_cells)
            if oid in doors:
                continue  # Shared doors use endpoint/threshold checks in Openable/WorldHandler.
            if self.config(package, oid):
                if not cells:
                    raise StateMachineError(error_code, "Ground footprint needs an anchor")
                if any(
                    not all(float(w[k]).is_integer() for k in ("x1", "y1", "x2", "y2"))
                    or (w["x1"] != w["x2"] and w["y1"] != w["y2"])
                    for w in walls
                ):
                    raise StateMachineError(
                        "unsupported_object_geometry", "Object shapes require grid-edge walls"
                    )
                if cells & reserved or any(not base.is_valid_cell(cell) for cell in cells):
                    raise StateMachineError(error_code, "Object footprint conflicts with grid")
                # Each internal edge of a rigid full-cell shape must be clear.
                # Per-cell validity alone misses walls between otherwise valid cells.
                for cell in cells:
                    x, y = map(int, cell.split(","))
                    for other in (f"{x + 1},{y}", f"{x},{y + 1}"):
                        if other in cells and base.edge_distance(cell, other) is None:
                            raise StateMachineError(error_code, "Rigid footprint crosses a wall")
            current = set(projection.movement_blocking_cells)
            if current & (actor_cells | blocking):
                raise StateMachineError(error_code, "Blocking footprints overlap")
            blocking.update(current)

    def check_candidate(
        self, sid, oid, package, candidate, *, error_code="object_placement_blocked"
    ):
        if type(candidate) is not ObjectSpatialState or candidate.scene_id is None:
            raise StateMachineError("invalid_spatial_state", "Ground candidate required")
        if candidate.scene_id != self.state(sid, oid, package).scene_id:
            raise StateMachineError(
                "unsupported_object_geometry", "Cross-scene placement unsupported"
            )
        self._check_scene(
            sid, candidate.scene_id, package, replacement=(oid, candidate), error_code=error_code
        )
        return self.project(sid, oid, package, candidate=candidate)

    def validate_placement(self, row, actor, op, package):
        h, sid = self.h, row["session_id"]
        h._world_operation_capability(row, op)
        h._world_actor_ready(sid, actor)
        position = h._public_scope(sid, actor, package)
        obj = h.facts.object(sid, op.object_id)
        c = self.config(package, op.object_id)
        if c is None or c.mobility_policy != "gm_placement":
            raise StateMachineError("unsupported_object_capability", "No GM placement policy")
        if obj is None or obj["scene_id"] != op.scene_id or position.scene_id != op.scene_id:
            raise StateMachineError(
                "invalid_target", "Only actual local ground objects can be placed"
            )
        before = self.state(sid, op.object_id, package)
        if before.anchor != op.expected_cell:
            raise StateMachineError("position_conflict", "Object anchor changed")
        if op.expected_orientation is not None and before.orientation != op.expected_orientation:
            raise StateMachineError("orientation_conflict", "Object orientation changed")
        if op.to_orientation is not None and op.expected_orientation is None:
            raise StateMachineError(
                "invalid_operation", "Rotation needs an orientation precondition"
            )
        orientation = op.to_orientation or before.orientation
        if orientation not in c.allowed_orientations:
            raise StateMachineError("unsupported_object_orientation", "Orientation is not allowed")
        if any(p.door_object_id == op.object_id for p in package.passages):
            raise StateMachineError(
                "unsupported_object_capability", "Shared passage objects are fixed"
            )
        candidate = replace(before, anchor=op.to_cell, orientation=orientation)
        self.check_candidate(sid, op.object_id, package, candidate)
        return candidate

    def operate(self, row, actor, op, package):
        candidate = self.validate_placement(row, actor, op, package)
        h, sid = self.h, row["session_id"]
        before = self.state(sid, op.object_id, package)
        if before == candidate:
            return []
        h.facts.place(sid, op.object_id, candidate.anchor, orientation=candidate.orientation)
        self.audit(sid, package)
        return [
            h._capture_event(
                sid,
                package,
                "object.placed",
                {
                    **asdict(op),
                    "actor_id": actor,
                    "orientation": candidate.orientation,
                    "previous_orientation": before.orientation,
                },
                phase="world.after_object_placement",
                source_actor_id=actor,
            )
        ]

    def audit(self, sid, package):
        for scene in package.scenes:
            if (
                scene.grid_json is not None
                and self.h._spatial_scene(sid, scene.scene_id) is not None
            ):
                self._check_scene(sid, scene.scene_id, package)
