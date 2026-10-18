"""Candidate SM spatial-world/1. SQLite facts, ephemeral upstream topology views.

No geometry algorithm or rule cost is implemented here. A movement-only projection
must never be passed to the Engine or perception as a mechanical Grid.
"""

import json
from dataclasses import asdict, dataclass, replace

from dnd5e_engine.evaluation_state import InventoryEntry, InventoryState
from dnd5e_engine.spatial import GridTopology, canonical_cell_id
from dnd5e_engine.specs import GridScene

from ._execution import _execution_data
from ._operations import (
    DropItem,
    EnterScene,
    FoundationOperation,
    LockDoor,
    MoveActor,
    PickupItem,
    PlaceObject,
    SetObjectState,
    SetSceneFlag,
    StepActor,
    TraversePassage,
    UnlockDoor,
)
from .environment_commands import ENVIRONMENT_OPERATIONS
from .errors import StateMachineError
from .game_time import AdvanceGameTime
from .models import TrustedContext
from .package import AdventurePackage, canonical_json
from .world_commands import WORLD_CAPABILITY_OPERATIONS


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
    locked: bool | None = None


@dataclass(frozen=True, slots=True)
class SpatialView:
    actor_position: SpatialPosition
    world_version: int
    objects: tuple[tuple[str, str, bool, bool], ...]
    exits: tuple[tuple[str, str, str, str], ...]
    doors: tuple[SpatialDoor, ...] = ()


@dataclass(frozen=True, slots=True)
class CarriedItemsView:
    actor_id: str
    world_version: int
    entries: tuple[InventoryEntry, ...]


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


class WorldHandler:
    def __init__(
        self,
        store,
        sessions,
        characters,
        facts,
        combats,
        capture,
        npc_bootstrap,
        activities=None,
        loader=None,
    ):
        from .activity_records import ActivityRecords

        self.store = store
        self.sessions = sessions
        self.characters = characters
        self.facts = facts
        self.combats = combats
        self.capture = capture
        self.npc_bootstrap = npc_bootstrap
        self.activities = activities if activities is not None else ActivityRecords(store)
        self.loader = loader
        from .world_objects import WorldObjectService

        self.objects = WorldObjectService(self)
        from .world_time import WorldTimeService

        self.time = WorldTimeService(store, sessions, facts)
        from .environment import EnvironmentService

        self.environment = EnvironmentService(self)
        self.capture.lights = self.objects.lights

    @property
    def _conn(self):
        return self.store.connection

    def _transaction(self, *, write=False):
        return self.store.transaction(write=write)

    def _session(self, sid, package, context):
        return self.sessions._session(sid, package, context)

    def _npc_authorized(self, sid, package, context):
        return self.sessions._npc_authorized(sid, package, context)

    def _active_combat(self, sid):
        return self.combats.active(sid)

    def _capture_event(self, *args, **kwargs):
        return self.capture._capture_event(*args, **kwargs)

    def _world_operation_capability(self, row, operation):
        return self.sessions._world_operation_capability(row, operation)

    def _initialize_scene(self, sid, scene_id, package):
        if not self.facts.initialize_scene(sid, scene_id, package):
            return
        self.facts.initialize_object_capabilities(sid, scene_id, package)
        for actor in package.actors:
            if actor.initial_scene_id == scene_id and self.characters.initialize_npc_actor(
                sid, actor
            ):
                self.npc_bootstrap.initialize(sid, actor)
        self.facts.initialize_mechanical_scene(sid, scene_id, package)

    def _world_actor_ready(self, sid, actor_id, *, navigation_scope=None):
        qualified = self.activities.qualifies(navigation_scope, sid, actor_id)
        if self.activities.active(sid, actor_id) and not qualified:
            raise StateMachineError("unsupported_actor_activity", "Navigation owns actor execution")
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
            and not qualified
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
            if oid != exclude_object
            and definitions[oid].blocks_passage
            and cell is not None
            and self.objects.spatial.config(package, oid) is None
        )
        cells.update(
            self.objects.spatial.movement_cells(
                sid, scene_id, package, exclude_object=exclude_object
            )
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
        topology = self._base_topology(sid, position.scene_id, package)
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
        base = self._base_topology(sid, position.scene_id, package)
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
        self.facts.require_ordinary_object(sid, operation.object_id)
        if self.objects.spatial.config(package, operation.object_id):
            raise StateMachineError(
                "unsupported_object_capability", "Use authorized placement policy"
            )
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
        self.objects.spatial.check_candidate(
            sid,
            operation.object_id,
            package,
            replace(
                self.objects.spatial.state(sid, operation.object_id, package),
                anchor=operation.to_cell,
            ),
        )
        return position

    def _validate_item_transfer(self, row, actor_id, operation, package):
        sid = row["session_id"]
        self._world_operation_capability(row, operation)
        self._world_actor_ready(sid, actor_id)
        position = self._public_scope(sid, actor_id, package)
        # Establish local visibility before reporting any target-specific reason.
        item = self._conn.execute(
            "SELECT * FROM objects WHERE session_id=? AND object_id=?", (sid, operation.instance_id)
        ).fetchone()
        if operation.scene_id != position.scene_id or item is None:
            raise StateMachineError("invalid_target", "No authorized local item")
        if type(operation) is PickupItem:
            if item["carrier_id"] is not None or item["scene_id"] != position.scene_id:
                raise StateMachineError("invalid_target", "No authorized local item")
            target = item["cell"]
            try:
                self._visible_target(sid, actor_id, target, package)
            except StateMachineError as exc:
                raise StateMachineError("invalid_target", "No authorized local item") from exc
        else:
            if item["carrier_id"] != actor_id:
                raise StateMachineError("invalid_target", "No authorized carried item")
            target = operation.to_cell
            self._visible_target(sid, actor_id, target, package)
        if item["item_json"] is None or operation.instance_id not in {
            i.instance_id for i in package.portable_items
        }:
            raise StateMachineError(
                "unsupported_object_capability", "No approved portable capability"
            )
        if type(operation) is PickupItem and target != operation.expected_cell:
            raise StateMachineError("position_conflict", "Item position precondition differs")
        base = self._base_topology(sid, position.scene_id, package)
        topology = self._movement_topology(sid, position.scene_id, package, exclude_actor=actor_id)
        if not base.is_valid_cell(target) or (
            target != position.cell and topology.edge_distance(position.cell, target) is None
        ):
            raise StateMachineError(
                "item_out_of_reach", "Item transfer requires clear adjacent reach"
            )
        if type(operation) is DropItem and any(
            aid != actor_id and cell == target
            for aid, cell in self._scene_occupants(sid, position.scene_id, package)[0]
        ):
            raise StateMachineError("item_out_of_reach", "Item destination is occupied")
        known = set(self.characters.rule_actors(sid))
        if actor_id not in known:
            raise StateMachineError(
                "unsupported_actor_state", "Carrier needs explicit mechanical facts"
            )
        components = self.characters.rule_components(sid, known)
        if any(e["target_id"] == actor_id for e in components["effect_states"]):
            raise StateMachineError(
                "unsupported_actor_state", "Active effects need unsupported adjudication"
            )
        if type(operation) is DropItem:
            if self.facts.contents(sid, operation.instance_id):
                self.objects.containers.require_unbound(sid, actor_id, operation.instance_id)
            state = self.characters.rule_actors(sid)[actor_id]
            casting = components["casting_state"]
            if casting is not None and any(
                material["inventory_instance_id"] == operation.instance_id
                for actor in casting["actors"]
                for material in actor["materials"]
            ):
                raise StateMachineError(
                    "unsupported_actor_activity", "Item has a casting material binding"
                )
            slug = json.loads(item["item_json"])["item_slug"]
            armor = state["worn_armor"]
            asset = self.loader.get_item(slug) if self.loader else None
            if (
                state["weapon_in_hands"] == slug
                or state["other_hand_occupied"]
                or (state["shield_equipped"] and slug == "shield")
                or (
                    armor is not None
                    and asset is not None
                    and asset.item_kind == "armor"
                    and slug != "shield"
                )
            ):
                raise StateMachineError(
                    "equipped_item_unsupported",
                    "Slug equipment cannot identify a safe instance to drop",
                )
        return position, item

    def _operate_item_transfer(self, row, actor_id, operation, package):
        sid = row["session_id"]
        position, before = self._validate_item_transfer(row, actor_id, operation, package)
        pickup = type(operation) is PickupItem
        payload = {
            "actor_id": actor_id,
            "actor_cell": position.cell,
            "instance_id": before["object_id"],
            "scene_id": position.scene_id,
            "cell": before["cell"] if pickup else operation.to_cell,
            "execution": _execution_data(actor_id, None),
        }
        events = []
        if pickup:
            events.append(
                self._capture_event(
                    sid,
                    package,
                    "item.picked_up",
                    payload,
                    phase="world.before_item_pickup",
                    source_actor_id=before["object_id"],
                )
            )
        after = self.facts.transfer_item(
            sid,
            before,
            carrier_id=actor_id if pickup else None,
            scene_id=None if pickup else position.scene_id,
            cell=None if pickup else operation.to_cell,
        )
        self.characters.project_item_transfer(sid, before, after)
        if not pickup:
            events.append(
                self._capture_event(
                    sid,
                    package,
                    "item.dropped",
                    payload,
                    phase="world.after_item_drop",
                    source_actor_id=before["object_id"],
                )
            )
        return events

    def read_carried_items(self, session_id, actor_id, package, *, context):
        with self._transaction():
            self._spatial_reader(session_id, actor_id, package, context)
            known = set(self.characters.rule_actors(session_id))
            if actor_id not in known:
                raise StateMachineError(
                    "unsupported_mechanical_version", "Explicit inventory is unavailable"
                )
            components = self.characters.rule_components(session_id, {actor_id})
            inventory = InventoryState.model_validate_json(
                canonical_json(components["inventory_state"])
            )
            version = self._conn.execute(
                "SELECT world_version FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            visible = tuple(
                entry
                for entry in inventory.entries
                if entry.accessible
                or not (
                    (obj := self.facts.object(session_id, entry.instance_id)) is not None
                    and obj["container_id"] is not None
                )
            )
            return CarriedItemsView(actor_id, version, visible)

    def _door_ports(self, scene_id, object_id, package):
        return tuple(
            (p, e)
            for p in package.passages
            if p.door_object_id == object_id
            for e in p.endpoints
            if e.scene_id == scene_id
        )

    def _manual_object_target(self, sid, actor_id, operation, package, *, navigation_scope=None):
        """A shared door is addressed at this actor's port, never at a foreign Cell."""
        position = self._actor_position(sid, actor_id)
        if operation.scene_id != position.scene_id:
            raise StateMachineError("invalid_target", "Interaction must be in the actor's scene")
        definition = self.facts.object_definition(package, operation.object_id)
        fine = package.scene(position.scene_id).grid_json is not None
        if fine:
            self._world_actor_ready(sid, actor_id, navigation_scope=navigation_scope)
            self._public_scope(sid, actor_id, package)
        obj = self._conn.execute(
            "SELECT scene_id,state,cell FROM objects WHERE session_id=? AND object_id=?",
            (sid, operation.object_id),
        ).fetchone()
        if obj is None:
            raise StateMachineError("invalid_target", "No authorized local object")
        full = self.facts.object(sid, operation.object_id)
        if full["container_id"] is not None or full["carrier_id"] is not None:
            self.objects.containers.reachable(sid, actor_id, operation.object_id, package)
            return obj, None
        ports = self._door_ports(position.scene_id, operation.object_id, package)
        if ports:
            base = self._base_topology(sid, position.scene_id, package)
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
        if (actor[0] != "pc" and definition is None) or obj[0] != position.scene_id:
            raise StateMachineError("invalid_target", "No authorized local switch")
        if fine:
            self._validate_switch_range(sid, actor_id, operation, package)
        return obj, None

    def _validate_switch_range(self, sid, actor_id, operation, package):
        self._world_actor_ready(sid, actor_id)
        self.objects.spatial.contact_cell(sid, actor_id, operation.object_id, package)

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

    def _world_destination(self, row, actor_id, operation, package, *, navigation_scope=None):
        sid = row["session_id"]
        self._world_operation_capability(row, operation)
        self._world_actor_ready(sid, actor_id, navigation_scope=navigation_scope)
        position = self._actor_position(sid, actor_id)
        # Establish caller capability before looking up any target reference.
        if package.scene(position.scene_id).grid_json is not None:
            self._public_scope(sid, actor_id, package)
        if type(operation) is StepActor:
            return self._validate_step(sid, actor_id, operation, package)
        if type(operation) is PlaceObject:
            return self._validate_placement(sid, actor_id, operation, package)
        if type(operation) in (PickupItem, DropItem):
            return self._validate_item_transfer(row, actor_id, operation, package)[0]
        if type(operation) in (UnlockDoor, LockDoor):
            return self.objects.openable.validate_lock(row, actor_id, operation, package)[0]
        if type(operation) is SetObjectState:
            self.objects.openable.validate_open(
                row, actor_id, operation, package, navigation_scope=navigation_scope
            )
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
        if type(operation) in WORLD_CAPABILITY_OPERATIONS:
            return self.objects.query(session_id, actor_id, operation, package, context=context)
        if type(operation) not in (
            StepActor,
            PlaceObject,
            PickupItem,
            DropItem,
            UnlockDoor,
            LockDoor,
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
                or (type(operation) is MoveActor and operation.actor_id != actor_id)
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
            topology = self._base_topology(session_id, position.scene_id, package)
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
                    blocking = self.objects.spatial.blocks(session_id, obj[0], package)
                    objects.append(
                        (
                            obj[0],
                            obj[1],
                            definition.movable,
                            definition.blocks_passage if blocking is None else blocking,
                        )
                    )
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
                    self.objects.openable.lock_status(session_id, p.door_object_id, package),
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

    def _operate_spatial(self, row, package, operation, actor_id, *, navigation_scope=None):
        sid = row["session_id"]
        if type(operation) in (PickupItem, DropItem):
            return self._operate_item_transfer(row, actor_id, operation, package)
        before = self._actor_position(sid, actor_id)
        destination = self._world_destination(
            row, actor_id, operation, package, navigation_scope=navigation_scope
        )
        if type(operation) is PlaceObject:
            if operation.expected_cell == operation.to_cell:
                return []
            self.facts.place(sid, operation.object_id, operation.to_cell)
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
        if before.scene_id != destination.scene_id:
            events.append(
                self._capture_event(
                    sid,
                    package,
                    "scene.exited" if is_pc else "actor.departed",
                    {
                        "scene_id": before.scene_id,
                        **({} if is_pc else {"actor_id": actor_id, "from_cell": before.cell}),
                    },
                    phase="scene.before_departure" if is_pc else "world.before_actor_departure",
                    source_actor_id=actor_id,
                )
            )
        existed = self._conn.execute(
            "SELECT 1 FROM scene_instances WHERE session_id=? AND scene_id=?",
            (sid, destination.scene_id),
        ).fetchone()
        self._initialize_scene(sid, destination.scene_id, package)
        self.characters.move(sid, actor_id, destination)
        if is_pc:
            self.sessions.move_pc_scene(sid, destination.scene_id)
            if before.scene_id != destination.scene_id and existed:
                self.facts.visit(sid, destination.scene_id)
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
        return self.characters.position(sid, actor_id)

    def _spatial_scene(self, sid, scene_id):
        return self.facts.spatial_scene(sid, scene_id)

    def _base_topology(self, sid, scene_id, package=None):
        scene = self._spatial_scene(sid, scene_id)
        if scene is None:
            raise StateMachineError("spatial_unconfigured", "Scene has no fine spatial capability")
        grid = scene["grid"]
        if package is not None:
            grid, _, _ = self.objects.lights.project(sid, scene_id, package, grid)
        return GridTopology(GridScene(**grid))

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
        if self.objects.lights.sources(sid, scene_id, package):
            raise StateMachineError(
                "unsupported_combat_dynamic_lighting",
                "Local emitter lighting needs combat integration",
            )
        if self.objects.spatial.movement_cells(sid, scene_id, package):
            raise StateMachineError(
                "unsupported_combat_dynamic_obstacles",
                "Dynamic object geometry needs RE integration",
            )
        if any(
            p.difficult_terrain_cells or p.vision_occlusion_cells or p.cover_cells
            for p in self.objects.spatial.scene_contributions(sid, scene_id, package).values()
        ):
            raise StateMachineError(
                "unsupported_combat_dynamic_obstacles",
                "Object spatial properties need RE integration",
            )
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
        blocking = self.objects.spatial.blocks(sid, passage.door_object_id, package)
        if blocking is not None:
            return blocking
        self.objects.openable.lock_status(sid, passage.door_object_id, package)
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
        self.objects.spatial.audit(sid, package)
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
                if (
                    self.objects.spatial.config(package, obj[0]) is None
                    and definitions[obj[0]].blocks_passage
                    and obj[1] in cells
                ):
                    raise StateMachineError("invalid_spatial_state", "Object overlaps an actor")

    def _operate(
        self,
        row,
        package: AdventurePackage,
        operation: FoundationOperation,
        *,
        actor_id=None,
        effect_origin=None,
        navigation_scope=None,
        source_command_id=None,
    ):
        self.store.require_write()
        if navigation_scope is not None and (
            effect_origin is not None
            or type(operation) not in (StepActor, TraversePassage, SetObjectState)
            or not self.activities.qualifies(
                navigation_scope, row["session_id"], actor_id, operation
            )
        ):
            raise StateMachineError("unauthorized", "No persisted navigation step qualification")
        execution = _execution_data(actor_id, effect_origin)
        automatic = effect_origin is not None
        if automatic and type(operation) not in (SetSceneFlag, SetObjectState):
            raise StateMachineError("unsupported_operation", "Unsupported automatic world effect")
        self._world_operation_capability(row, operation)
        sid = row["session_id"]
        if type(operation) in ENVIRONMENT_OPERATIONS:
            return self.environment.operate(row, operation, package)
        if type(operation) in WORLD_CAPABILITY_OPERATIONS:
            return self.objects.operate(
                row, actor_id, operation, package, source_command_id=source_command_id
            )
        if type(operation) in (UnlockDoor, LockDoor):
            return self.objects.openable.operate_lock(row, actor_id, operation, package)
        if type(operation) is AdvanceGameTime:
            return [
                self._capture_event(sid, package, kind, payload, phase="clock.after_advance")
                for kind, payload in self.time.advance(row, operation)
            ]
        if isinstance(
            operation,
            (EnterScene, MoveActor, StepActor, TraversePassage, PlaceObject, PickupItem, DropItem),
        ):
            return self._operate_spatial(
                row, package, operation, actor_id, navigation_scope=navigation_scope
            )
        if isinstance(operation, (SetSceneFlag, SetObjectState)):
            if (automatic or isinstance(operation, SetSceneFlag)) and operation.scene_id != row[
                "current_scene_id"
            ]:
                raise StateMachineError("invalid_target", "Only the current scene can be modified")
            if isinstance(operation, SetSceneFlag):
                if type(operation.value) is not bool:
                    raise StateMachineError("invalid_operation", "Flag value must be boolean")
                flags = json.loads(
                    self._conn.execute(
                        (
                            "SELECT flags_json FROM scene_instances WHERE session_id = ? AND "
                            "scene_id = ?"
                        ),
                        (sid, operation.scene_id),
                    ).fetchone()[0]
                )
                if operation.flag_id not in flags:
                    raise StateMachineError("invalid_target", "Flag must be declared in this scene")
                if flags[operation.flag_id] == operation.value:
                    return []
                flags[operation.flag_id] = operation.value
                self.facts.set_scene_flags(sid, operation.scene_id, flags)
                return [
                    self._capture_event(
                        sid,
                        package,
                        "scene.flag_changed",
                        {**asdict(operation), "execution": execution},
                        phase="world.after_flag_update",
                    )
                ]
            return self.objects.openable.operate_open(
                row,
                actor_id,
                operation,
                package,
                origin=effect_origin,
                navigation_scope=navigation_scope,
            )
        raise StateMachineError("unsupported_operation", "Unknown world operation")
