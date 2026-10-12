"""Candidate SM spatial-world/1. SQLite facts, ephemeral upstream topology views.

No geometry algorithm or rule cost is implemented here. A movement-only projection
must never be passed to the Engine or perception as a mechanical Grid.
"""

import json
from dataclasses import dataclass

from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene

from ._operations import (
    EnterScene,
    MoveActor,
    PlaceObject,
    SetObjectState,
    StepActor,
    TraversePassage,
)
from .errors import StateMachineError
from .models import TrustedContext


@dataclass(frozen=True, slots=True)
class SpatialPosition:
    scene_id: str
    cell: str | None


@dataclass(frozen=True, slots=True)
class WorldActionAvailability:
    status: str
    reason: str
    world_version: int


@dataclass(frozen=True, slots=True)
class SpatialDoor:
    passage_id: str
    object_id: str
    state: str
    standing_cell: str
    threshold_cell: str


@dataclass(frozen=True, slots=True)
class SpatialView:
    actor_position: SpatialPosition
    world_version: int
    objects: tuple[tuple[str, str, bool, bool], ...]
    exits: tuple[tuple[str, str, str, str], ...]
    doors: tuple[SpatialDoor, ...] = ()


def cell_in_bounds(topology, cell):
    """Upstream bounds/distance, deliberately distinct from walkability."""
    try:
        return (
            type(cell) is str
            and canonical_cell_id(cell) == cell
            and topology.distance_ft(cell, cell) is not None
        )
    except ValueError:
        return False


class SpatialRuntime:
    def _world_actor_ready(self, sid, actor_id):
        core = self._conn.execute(
            "SELECT current_hp,effects_json FROM characters WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        mechanical = self._conn.execute(
            "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        state = json.loads(mechanical[0]) if mechanical else None
        if (
            (core and (core[0] <= 0 or json.loads(core[1])))
            or state
            and (not state["is_alive"] or state["conditions"] or state["concentration_effect_id"])
        ):
            raise StateMachineError(
                "unsupported_actor_state", "Actor state needs unsupported rules"
            )
        npc = self._conn.execute(
            "SELECT runtime_json FROM npc_states WHERE session_id=? AND npc_id=?", (sid, actor_id)
        ).fetchone()
        if (
            self._actor_position(sid, actor_id).cell is not None
            and npc
            and npc[0]
            and json.loads(npc[0])["current_activity"] is not None
        ):
            raise StateMachineError(
                "unsupported_actor_activity", "Active NPC activity needs adjudication"
            )

    def _public_scope(self, sid, actor_id, package):
        position = self._actor_position(sid, actor_id)
        if not package.scene(position.scene_id).public_interactions:
            raise StateMachineError(
                "unsupported_interaction_scope", "Public spatial scope is required"
            )
        definition = next((a for a in package.actors if a.actor_id == actor_id), None)
        if definition and (definition.senses is None or not dict(definition.senses)["visual"]):
            raise StateMachineError(
                "unsupported_target_visibility", "Target authorization is unproven"
            )
        mechanical = self._conn.execute(
            "SELECT state_json FROM mechanical_actors WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        if mechanical and json.loads(mechanical[0])["conditions"]:
            raise StateMachineError(
                "unsupported_target_visibility",
                "Condition-aware target authorization is unavailable",
            )
        return position

    def _scene_grid(self, sid, scene_id, package):
        stored = self._spatial_scene(sid, scene_id)
        raw = (
            stored["grid"]
            if stored
            else (
                json.loads(package.scene(scene_id).grid_json)
                if package.scene(scene_id).grid_json
                else None
            )
        )
        if raw is None:
            raise StateMachineError("spatial_unconfigured", "Fine spatial data is unavailable")
        return raw

    def _scene_occupants(self, sid, scene_id, package):
        """Rows if initialized; otherwise still-uninstantiated configured facts only."""
        initialized = self._conn.execute(
            "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, scene_id)
        ).fetchone()
        actors = [
            tuple(r)
            for r in self._conn.execute(
                "SELECT actor_id,cell FROM actors WHERE session_id=? AND current_scene_id=?",
                (sid, scene_id),
            )
        ]
        objects = [
            tuple(r)
            for r in self._conn.execute(
                "SELECT object_id,cell FROM objects WHERE session_id=? AND scene_id=?",
                (sid, scene_id),
            )
        ]
        if not initialized:
            existing = {
                r[0]
                for r in self._conn.execute(
                    "SELECT actor_id FROM actors WHERE session_id=?", (sid,)
                )
            }
            actors.extend(
                (a.actor_id, a.initial_cell)
                for a in package.actors
                if a.initial_scene_id == scene_id and a.actor_id not in existing
            )
            objects.extend((o.object_id, o.initial_cell) for o in package.scene(scene_id).objects)
        return actors, objects

    def _movement_topology(
        self,
        sid,
        scene_id,
        package,
        *,
        exclude_actor=None,
        exclude_object=None,
        handled_thresholds=(),
    ):
        raw = self._scene_grid(sid, scene_id, package)
        actors, objects = self._scene_occupants(sid, scene_id, package)
        definitions = {o.object_id: o for s in package.scenes for o in s.objects}
        cells = set(raw["blocked_cells"])
        cells.update(cell for aid, cell in actors if aid != exclude_actor and cell is not None)
        cells.update(
            cell
            for oid, cell in objects
            if oid != exclude_object and definitions[oid].blocks_passage and cell is not None
        )
        for passage in package.passages:
            if passage.door_object_id and self._door_closed(sid, passage, package):
                cells.update(
                    e.threshold_cell
                    for e in passage.endpoints
                    if e.scene_id == scene_id
                    and (passage.passage_id, e.threshold_cell) not in handled_thresholds
                )
        # This projection is movement-only and exists for this single calculation.
        return GridTopology(GridScene(**{**raw, "blocked_cells": sorted(cells)}))

    def _visible_target(self, sid, actor_id, cell, package):
        position = self._public_scope(sid, actor_id, package)
        topology = self._base_topology(sid, position.scene_id)
        if not cell_in_bounds(topology, cell) or not cell_in_bounds(topology, position.cell):
            raise StateMachineError(
                "invalid_spatial_state", "Position is missing or outside its map"
            )
        if not topology.can_see(position.cell, cell):
            raise StateMachineError(
                "unsupported_target_visibility", "Target authorization is unproven"
            )
        return position

    def _validate_step(self, sid, actor_id, operation, package):
        position = self._public_scope(sid, actor_id, package)
        if position.scene_id != operation.scene_id or position.cell != operation.expected_cell:
            raise StateMachineError("position_conflict", "Position precondition differs")
        base = self._base_topology(sid, position.scene_id)
        if not cell_in_bounds(base, operation.to_cell):
            raise StateMachineError("invalid_cell", "Target is outside this local map")
        if position.cell == operation.to_cell:
            return position
        topology = self._movement_topology(sid, position.scene_id, package, exclude_actor=actor_id)
        if topology.edge_distance(position.cell, operation.to_cell) is None:
            raise StateMachineError("movement_unavailable", "No legal adjacent step")
        # No cost, time or battle-budget calculation is performed for exploration.
        return SpatialPosition(position.scene_id, operation.to_cell)

    def _validate_placement(self, sid, actor_id, operation, package):
        position = self._public_scope(sid, actor_id, package)
        obj = self._conn.execute(
            "SELECT scene_id,cell FROM objects WHERE session_id=? AND object_id=?",
            (sid, operation.object_id),
        ).fetchone()
        if obj is None or obj[0] != position.scene_id or operation.scene_id != position.scene_id:
            raise StateMachineError("invalid_target", "No authorized local object")
        try:
            self._visible_target(sid, actor_id, obj[1], package)
        except StateMachineError as exc:
            raise StateMachineError("invalid_target", "No authorized local object") from exc
        definition = next(
            o for s in package.scenes for o in s.objects if o.object_id == operation.object_id
        )
        if not definition.movable:
            raise StateMachineError("unsupported_object_capability", "Object cannot be adjusted")
        if obj[1] != operation.expected_cell:
            raise StateMachineError("position_conflict", "Object position precondition differs")
        self._visible_target(sid, actor_id, operation.to_cell, package)
        handled_thresholds = {
            (p.passage_id, e.threshold_cell)
            for p in package.passages
            for e in p.endpoints
            if e.scene_id == position.scene_id
            and e.standing_cell == position.cell
            and e.threshold_cell in (obj[1], operation.to_cell)
        }
        topology = self._movement_topology(
            sid,
            position.scene_id,
            package,
            exclude_actor=actor_id,
            exclude_object=operation.object_id,
            handled_thresholds=handled_thresholds,
        )
        # Reach to both old and new positions, and a legal adjacent object shift.
        # Excluding the handled object allows reaching its occupied square.
        for cell in (obj[1], operation.to_cell):
            if cell != position.cell and topology.edge_distance(position.cell, cell) is None:
                raise StateMachineError(
                    "object_out_of_reach", "Object adjustment requires clear adjacent reach"
                )
        if (
            obj[1] != operation.to_cell
            and topology.edge_distance(obj[1], operation.to_cell) is None
        ):
            raise StateMachineError(
                "movement_unavailable", "Object must move by a legal adjacent step"
            )
        actors, _ = self._scene_occupants(sid, position.scene_id, package)
        if any(cell == operation.to_cell for _, cell in actors):
            raise StateMachineError("movement_unavailable", "Object destination is occupied")
        return position

    def _door_ports(self, scene_id, object_id, package):
        return tuple(
            (p, e)
            for p in package.passages
            if p.door_object_id == object_id
            for e in p.endpoints
            if e.scene_id == scene_id
        )

    def _manual_object_target(self, sid, actor_id, operation, package):
        """A shared door is addressed at this actor's port, never at a foreign Cell."""
        position = self._actor_position(sid, actor_id)
        if operation.scene_id != position.scene_id:
            raise StateMachineError("invalid_target", "Interaction must be in the actor's scene")
        fine = package.scene(position.scene_id).grid_json is not None
        if fine:
            self._world_actor_ready(sid, actor_id)
            self._public_scope(sid, actor_id, package)
        obj = self._conn.execute(
            "SELECT scene_id,state,cell FROM objects WHERE session_id=? AND object_id=?",
            (sid, operation.object_id),
        ).fetchone()
        if obj is None:
            raise StateMachineError("invalid_target", "No authorized local object")
        ports = self._door_ports(position.scene_id, operation.object_id, package)
        if ports:
            base = self._base_topology(sid, position.scene_id)
            visible = [(p, e) for p, e in ports if base.can_see(position.cell, e.threshold_cell)]
            if not visible:
                raise StateMachineError("invalid_target", "No authorized local door")
            matches = [(p, e) for p, e in visible if e.standing_cell == position.cell]
            if not matches:
                raise StateMachineError("object_out_of_reach", "Door needs its standing point")
            for passage, endpoint in matches:
                handled = {
                    (p.passage_id, e.threshold_cell)
                    for p, e in ports
                    if e.threshold_cell == endpoint.threshold_cell
                }
                topology = self._movement_topology(
                    sid,
                    position.scene_id,
                    package,
                    exclude_actor=actor_id,
                    handled_thresholds=handled,
                )
                if topology.edge_distance(position.cell, endpoint.threshold_cell) is not None:
                    return obj, passage
            raise StateMachineError("object_out_of_reach", "Door requires clear adjacent reach")
        actor = self._conn.execute(
            "SELECT kind FROM actors WHERE session_id=? AND actor_id=?", (sid, actor_id)
        ).fetchone()
        if actor[0] != "pc" or obj[0] != position.scene_id:
            raise StateMachineError("invalid_target", "No authorized local switch")
        if fine:
            self._validate_switch_range(sid, actor_id, operation, package)
        return obj, None

    def _validate_switch_range(self, sid, actor_id, operation, package):
        self._world_actor_ready(sid, actor_id)
        obj = self._conn.execute(
            "SELECT cell FROM objects WHERE session_id=? AND object_id=?",
            (sid, operation.object_id),
        ).fetchone()
        try:
            position = self._visible_target(sid, actor_id, obj[0], package)
        except StateMachineError as exc:
            raise StateMachineError("invalid_target", "No authorized local object") from exc
        topology = self._movement_topology(
            sid,
            position.scene_id,
            package,
            exclude_actor=actor_id,
        )
        if obj[0] != position.cell and topology.edge_distance(position.cell, obj[0]) is None:
            raise StateMachineError("object_out_of_reach", "Switch requires clear adjacent reach")

    def _validate_door_occupancy(self, sid, operation, package):
        if operation.state.value == "closed":
            for passage in package.passages:
                if passage.door_object_id == operation.object_id:
                    for endpoint in passage.endpoints:
                        actors, _ = self._scene_occupants(sid, endpoint.scene_id, package)
                        if any(cell == endpoint.threshold_cell for _, cell in actors):
                            raise StateMachineError(
                                "passage_unavailable", "Door threshold is occupied"
                            )

    def _passage_route(self, sid, actor_id, to_scene_id, package, passage_id=None):
        position = self._actor_position(sid, actor_id)
        source = package.scene(position.scene_id)
        # Retain the abstract API's unknown-scene rejection.
        if source.grid_json is None:
            package.scene(to_scene_id)
        if to_scene_id not in source.exits:
            raise StateMachineError(
                "invalid_target" if source.grid_json is not None else "scene_unreachable",
                "No authorized directed exit",
            )
        target = package.scene(to_scene_id)
        if source.grid_json is None and target.grid_json is None:
            return SpatialPosition(target.scene_id, None)
        if source.grid_json is None or target.grid_json is None:
            raise StateMachineError(
                "unsupported_spatial_transition", "Abstract/fine conversion is unsupported"
            )
        self._public_scope(sid, actor_id, package)
        pid = dict(source.exit_passages).get(to_scene_id)
        if not pid or passage_id is not None and passage_id != pid:
            raise StateMachineError("scene_unreachable", "No authorized spatial passage")
        passage = next(p for p in package.passages if p.passage_id == pid)
        a = next(e for e in passage.endpoints if e.scene_id == source.scene_id)
        b = next(e for e in passage.endpoints if e.scene_id == target.scene_id)
        try:
            self._visible_target(sid, actor_id, a.standing_cell, package)
        except StateMachineError as exc:
            raise StateMachineError("invalid_target", "No authorized spatial passage") from exc
        if not target.public_interactions:
            raise StateMachineError(
                "unsupported_interaction_scope", "Target scene has no public spatial interactions"
            )
        if position.cell != a.standing_cell:
            raise StateMachineError(
                "passage_out_of_reach", "Actor must be at the declared standing point"
            )
        for endpoint in (a, b):
            topology = self._movement_topology(
                sid, endpoint.scene_id, package, exclude_actor=actor_id
            )
            if (
                not topology.is_valid_cell(endpoint.standing_cell)
                or topology.edge_distance(endpoint.standing_cell, endpoint.threshold_cell) is None
            ):
                raise StateMachineError("passage_unavailable", "Passage is currently unavailable")
        return SpatialPosition(target.scene_id, b.standing_cell)

    def _world_destination(self, row, actor_id, operation, package):
        sid = row["session_id"]
        self._world_operation_capability(row, operation)
        self._world_actor_ready(sid, actor_id)
        position = self._actor_position(sid, actor_id)
        # Establish caller capability before looking up any target reference.
        if package.scene(position.scene_id).grid_json is not None:
            self._public_scope(sid, actor_id, package)
        if type(operation) is StepActor:
            return self._validate_step(sid, actor_id, operation, package)
        if type(operation) is PlaceObject:
            return self._validate_placement(sid, actor_id, operation, package)
        if type(operation) is SetObjectState:
            from ._operations import ObjectState

            if type(operation.state) is not ObjectState:
                raise StateMachineError("invalid_operation", "Expected a typed ObjectState")
            obj, _ = self._manual_object_target(sid, actor_id, operation, package)
            if obj[1] is None:
                raise StateMachineError("unsupported_object_capability", "Object has no switch")
            self._validate_door_occupancy(sid, operation, package)
            return position
        if type(operation) is TraversePassage:
            passage = next(
                (p for p in package.passages if p.passage_id == operation.passage_id), None
            )
            if passage is None or position.scene_id not in {e.scene_id for e in passage.endpoints}:
                raise StateMachineError("invalid_target", "No authorized passage")
            endpoint = next(e for e in passage.endpoints if e.scene_id == position.scene_id)
            try:
                self._visible_target(sid, actor_id, endpoint.standing_cell, package)
            except StateMachineError as exc:
                raise StateMachineError("invalid_target", "No authorized spatial passage") from exc
            if (
                position.scene_id != operation.from_scene_id
                or position.cell != operation.expected_cell
            ):
                raise StateMachineError("position_conflict", "Passage source precondition differs")
            target = next(e.scene_id for e in passage.endpoints if e.scene_id != position.scene_id)
            return self._passage_route(sid, actor_id, target, package, passage.passage_id)
        target = operation.scene_id if type(operation) is EnterScene else operation.to_scene_id
        if position.scene_id == target:
            return position
        if (
            type(operation) is MoveActor
            and package.scene(position.scene_id).grid_json is None
            and not self._conn.execute(
                "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?", (sid, target)
            ).fetchone()
        ):
            raise StateMachineError("invalid_target", "Destination scene is not initialized")
        return self._passage_route(sid, actor_id, target, package)

    def query_world_action(self, session_id, actor_id, operation, package, *, context):
        """Same decision as execution, generic reasons and no hidden entity/path data."""
        from .commands import OPERATIONS, TypedCommand, validate_command

        kind = next((k for k, v in OPERATIONS.items() if type(operation) is v), None)
        if type(operation) not in (
            StepActor,
            PlaceObject,
            TraversePassage,
            EnterScene,
            MoveActor,
            SetObjectState,
        ):
            raise StateMachineError("unsupported_operation", "Unsupported spatial query")
        validate_command(TypedCommand(session_id, "query", actor_id, kind, operation, 0))
        with self._transaction():
            self._spatial_reader(session_id, actor_id, package, context)
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if (
                type(operation) is EnterScene
                and actor_id != row["pc_actor_id"]
                or type(operation) is MoveActor
                and operation.actor_id != actor_id
            ):
                raise StateMachineError("invalid_actor", "Operation actor differs")
            if (
                type(operation) is MoveActor
                and self._conn.execute(
                    "SELECT kind FROM actors WHERE session_id=? AND actor_id=?",
                    (session_id, actor_id),
                ).fetchone()[0]
                != "npc"
            ):
                raise StateMachineError("invalid_target", "Expected an existing NPC")
            try:
                self._world_destination(row, actor_id, operation, package)
            except StateMachineError as exc:
                return WorldActionAvailability("unavailable", exc.code, row["world_version"])
            return WorldActionAvailability("available", "available", row["world_version"])

    def get_spatial_view(self, session_id, actor_id, package, *, context):
        with self._transaction():
            self._spatial_reader(session_id, actor_id, package, context)
            if self._active_combat(session_id):
                raise StateMachineError(
                    "unsupported_spatial_view", "Active combat needs its perception adapter"
                )
            position = self._public_scope(session_id, actor_id, package)
            topology = self._base_topology(session_id, position.scene_id)
            objects = []
            for obj in self._conn.execute(
                "SELECT object_id,cell FROM objects WHERE session_id=? AND scene_id=? "
                "ORDER BY object_id",
                (session_id, position.scene_id),
            ):
                if topology.can_see(position.cell, obj[1]):
                    definition = next(
                        o for s in package.scenes for o in s.objects if o.object_id == obj[0]
                    )
                    objects.append((obj[0], obj[1], definition.movable, definition.blocks_passage))
            exits = tuple(
                (target, pid, e.standing_cell, e.threshold_cell)
                for target, pid in package.scene(position.scene_id).exit_passages
                for p in package.passages
                if p.passage_id == pid
                for e in p.endpoints
                if e.scene_id == position.scene_id
                and topology.can_see(position.cell, e.standing_cell)
            )
            doors = tuple(
                SpatialDoor(
                    p.passage_id,
                    p.door_object_id,
                    self._conn.execute(
                        "SELECT state FROM objects WHERE session_id=? AND object_id=?",
                        (session_id, p.door_object_id),
                    ).fetchone()[0],
                    e.standing_cell,
                    e.threshold_cell,
                )
                for p in package.passages
                if p.door_object_id
                for e in p.endpoints
                if e.scene_id == position.scene_id
                and topology.can_see(position.cell, e.threshold_cell)
            )
            version = self._conn.execute(
                "SELECT world_version FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            return SpatialView(position, version, tuple(objects), exits, doors)

    def _operate_spatial(self, row, package, operation, actor_id):
        sid = row["session_id"]
        before = self._actor_position(sid, actor_id)
        destination = self._world_destination(row, actor_id, operation, package)
        if type(operation) is PlaceObject:
            if operation.expected_cell == operation.to_cell:
                return []
            self._conn.execute(
                "UPDATE objects SET cell=? WHERE session_id=? AND object_id=?",
                (operation.to_cell, sid, operation.object_id),
            )
            return [
                self._capture_event(
                    sid,
                    package,
                    "object.placed",
                    {
                        "scene_id": before.scene_id,
                        "object_id": operation.object_id,
                        "actor_id": actor_id,
                        "from_cell": operation.expected_cell,
                        "to_cell": operation.to_cell,
                    },
                    phase="world.after_object_placement",
                    source_actor_id=operation.object_id,
                )
            ]
        if before == destination:
            return []
        is_pc = actor_id == row["pc_actor_id"]
        events = []
        if is_pc and before.scene_id != destination.scene_id:
            events.append(
                self._capture_event(
                    sid,
                    package,
                    "scene.exited",
                    {"scene_id": before.scene_id},
                    phase="scene.before_departure",
                    source_actor_id=actor_id,
                )
            )
        existed = self._conn.execute(
            "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?",
            (sid, destination.scene_id),
        ).fetchone()
        self._initialize_scene(sid, destination.scene_id, package)
        self._conn.execute(
            "UPDATE actors SET current_scene_id=?,cell=? WHERE session_id=? AND actor_id=?",
            (destination.scene_id, destination.cell, sid, actor_id),
        )
        if is_pc:
            self._conn.execute(
                "UPDATE sessions SET current_scene_id=? WHERE session_id=?",
                (destination.scene_id, sid),
            )
            if before.scene_id != destination.scene_id and existed:
                self._conn.execute(
                    "UPDATE scene_instances SET visited_count=visited_count+1 "
                    "WHERE session_id=? AND scene_id=?",
                    (sid, destination.scene_id),
                )
        if is_pc and before.scene_id != destination.scene_id:
            events.append(
                self._capture_event(
                    sid,
                    package,
                    "scene.entered",
                    {"scene_id": destination.scene_id},
                    phase="scene.after_arrival",
                    source_actor_id=actor_id,
                )
            )
        else:
            events.append(
                self._capture_event(
                    sid,
                    package,
                    "actor.stepped" if before.scene_id == destination.scene_id else "actor.moved",
                    {
                        "actor_id": actor_id,
                        "to_scene_id": destination.scene_id,
                        **(
                            {"from_cell": before.cell, "to_cell": destination.cell}
                            if destination.cell is not None
                            else {}
                        ),
                    },
                    phase="world.after_actor_move",
                    source_actor_id=actor_id,
                )
            )
        return events

    def _actor_position(self, sid, actor_id):
        actor = self._conn.execute(
            "SELECT current_scene_id,cell FROM actors WHERE session_id=? AND actor_id=?",
            (sid, actor_id),
        ).fetchone()
        if actor is None:
            raise StateMachineError("invalid_actor", "No authorized actor")
        return SpatialPosition(actor[0], actor[1])

    def _spatial_scene(self, sid, scene_id):
        scene = self._conn.execute(
            "SELECT state_json FROM mechanical_scenes "
            "WHERE session_id=? AND scene_id=? AND spatial_enabled=1",
            (sid, scene_id),
        ).fetchone()
        return json.loads(scene[0]) if scene else None

    def _base_topology(self, sid, scene_id):
        scene = self._spatial_scene(sid, scene_id)
        if scene is None:
            raise StateMachineError("spatial_unconfigured", "Scene has no fine spatial capability")
        return GridTopology(GridScene(**scene["grid"]))

    def _current_actor_cells(self, sid, scene_id, roster):
        topology = self._base_topology(sid, scene_id)
        positions = {}
        for actor_id in roster:
            position = self._actor_position(sid, actor_id)
            if position.scene_id != scene_id or not cell_in_bounds(topology, position.cell):
                raise StateMachineError("invalid_spatial_state", "Encounter position is incomplete")
            if not topology.is_valid_cell(position.cell):
                raise StateMachineError("invalid_spatial_state", "Actor cannot stand on this cell")
            positions[actor_id] = position.cell
        if len(set(positions.values())) != len(positions):
            raise StateMachineError("invalid_spatial_state", "Encounter occupancy overlaps")
        return positions

    def _spatial_combat_guard(self, sid, scene_id, package):
        if self._spatial_scene(sid, scene_id) is None:
            return
        definitions = {o.object_id: o for s in package.scenes for o in s.objects}
        objects = self._conn.execute(
            "SELECT object_id FROM objects WHERE session_id=? AND scene_id=?", (sid, scene_id)
        ).fetchall()
        if any(definitions[o[0]].blocks_passage for o in objects):
            raise StateMachineError(
                "unsupported_combat_dynamic_obstacles",
                "Movement-only objects cannot be represented by the pinned combat evaluator",
            )
        for passage in package.passages:
            if scene_id not in {e.scene_id for e in passage.endpoints}:
                continue
            if passage.door_object_id and self._door_closed(sid, passage, package):
                raise StateMachineError(
                    "unsupported_combat_dynamic_obstacles",
                    "Closed passage door needs combat integration",
                )

    def _door_closed(self, sid, passage, package):
        door = self._conn.execute(
            "SELECT state FROM objects WHERE session_id=? AND object_id=?",
            (sid, passage.door_object_id),
        ).fetchone()
        if door:
            return door[0] == "closed"
        # An unvisited Scene has not instantiated its objects yet. This is an
        # initial fact only, never a replacement for an existing runtime row.
        return (
            next(
                o.state
                for s in package.scenes
                for o in s.objects
                if o.object_id == passage.door_object_id
            )
            == "closed"
        )

    def _spatial_reader(self, sid, actor_id, package, context):
        if type(actor_id) is not str or not actor_id:
            raise StateMachineError("invalid_query", "Actor identity is required")
        if type(context) is TrustedContext:
            row = self._session(sid, package, context)
            if actor_id != row["pc_actor_id"] and actor_id not in context.controlled_npc_ids:
                raise StateMachineError("unauthorized", "Actor control is required")
        else:
            npc = self._npc_authorized(sid, package, context)
            if actor_id != npc:
                raise StateMachineError(
                    "unauthorized", "Only this NPC's spatial view is authorized"
                )
        return self._actor_position(sid, actor_id)

    def read_actor_position(self, session_id, actor_id, package, *, context):
        """Own/control-granted position only; never a global entity catalogue."""
        with self._transaction():
            return self._spatial_reader(session_id, actor_id, package, context)

    def _assert_spatial_consistency(self, sid, package):
        definitions = {o.object_id: o for s in package.scenes for o in s.objects}
        for scene in package.scenes:
            if scene.grid_json is None:
                continue
            if self._spatial_scene(sid, scene.scene_id) is None:
                continue  # Lazy initialization is still allowed.
            topology = self._base_topology(sid, scene.scene_id)
            cells = []
            for actor in self._conn.execute(
                "SELECT cell FROM actors WHERE session_id=? AND current_scene_id=?",
                (sid, scene.scene_id),
            ):
                if not cell_in_bounds(topology, actor[0]) or not topology.is_valid_cell(actor[0]):
                    raise StateMachineError("invalid_spatial_state", "Invalid actor occupancy")
                cells.append(actor[0])
            if len(cells) != len(set(cells)):
                raise StateMachineError("invalid_spatial_state", "Actor occupancy overlaps")
            for obj in self._conn.execute(
                "SELECT object_id,cell FROM objects WHERE session_id=? AND scene_id=?",
                (sid, scene.scene_id),
            ):
                if not cell_in_bounds(topology, obj[1]):
                    raise StateMachineError("invalid_spatial_state", "Invalid object location")
                if definitions[obj[0]].blocks_passage and obj[1] in cells:
                    raise StateMachineError("invalid_spatial_state", "Object overlaps an actor")
