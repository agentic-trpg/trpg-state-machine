"""One durable navigation activity; world execution stays in WorldHandler."""

import json

from ._operations import ObjectState, SetObjectState, StepActor, TraversePassage
from .errors import StateMachineError
from .navigation_models import (
    AdvanceNavigation,
    CancelNavigation,
    NavigationResult,
    ResumeNavigation,
    StartNavigation,
)
from .navigation_plan import granted_layout, plan_navigation


class ActivityHandler:
    """Coordinate navigation lifecycle and world primitives through shared domain
    interfaces.
    """

    def __init__(self, store, records, characters, world, capture):
        """Bind the records, character and world interfaces used by navigation operations."""
        self.store = store
        self.records = records
        self.characters = characters
        self.world = world
        self.capture = capture

    def _controller(self, row, actor):
        """Resolve the actor's controller, rejecting missing or ineligible control."""
        if actor == row["pc_actor_id"]:
            return None
        grant = self.store.connection.execute(
            "SELECT principal_id FROM npc_access WHERE session_id=? AND observer_id=?",
            (row["session_id"], actor),
        ).fetchone()
        if grant is None:
            raise StateMachineError(
                "unauthorized", "Navigation requires a registered NPC controller"
            )
        return grant[0]

    def _event(self, record, package, result):
        """Capture navigation lifecycle events from the activity record."""
        return self.capture._capture_event(
            record["session_id"],
            package,
            "activity.navigation_" + result.code,
            {
                "activity_id": record["activity_id"],
                "actor_id": record["actor_id"],
                "activity_version": record["activity_version"],
                "cursor": record["cursor"],
                "status": record["status"],
                "result": result.code,
                "operation": result.operation,
            },
            phase="activity.after_progress",
            source_actor_id=record["actor_id"],
        )

    def _finish(self, record, package, status, result, command_id, *, cursor=None):
        """Transition to a terminal activity state and return its lifecycle event."""
        updated = self.records.transition(
            record, status, record["cursor"] if cursor is None else cursor, result, command_id
        )
        return [self._event(updated, package, result)]

    def _ready(self, row, actor, package):
        """Require noncombat readiness and an available public position."""
        if self.world._active_combat(row["session_id"]):
            raise StateMachineError(
                "unsupported_combat_world_operation", "Navigation is noncombat only"
            )
        self.world._world_actor_ready(row["session_id"], actor)
        self.world._public_scope(row["session_id"], actor, package)

    def operate(self, row, package, operation, *, actor_id, command_id):
        """Start, continue or stop navigation within the caller's transaction, returning
        events.
        """
        self.store.require_write()
        sid = row["session_id"]
        if type(operation) is StartNavigation:
            if self.records.get(sid, operation.activity_id):
                raise StateMachineError(
                    "activity_exists", "Activity identity is historical and immutable"
                )
            if self.records.active(sid, actor_id):
                raise StateMachineError("activity_conflict", "One executing navigation per actor")
            self._ready(row, actor_id, package)
            controller = self._controller(row, actor_id)
            plan = plan_navigation(
                row,
                actor_id,
                self.characters.position(sid, actor_id),
                operation,
                package,
                grid=self.world.grid,
            )
            record = self.records.create(sid, actor_id, operation, plan, command_id, controller)
            return [self._event(record, package, NavigationResult("started"))]
        record = self.records.get(sid, operation.activity_id)
        if record is None or record["actor_id"] != actor_id:
            raise StateMachineError("invalid_activity", "No authorized actor activity")
        if record["activity_version"] != operation.expected_activity_version:
            raise StateMachineError("activity_version_conflict", "Activity cursor/version changed")
        if type(operation) is CancelNavigation:
            if record["status"] == "cancelled":
                return []
            if record["status"] == "completed":
                raise StateMachineError(
                    "activity_inactive", "Completed activity cannot be cancelled"
                )
            return self._finish(
                record, package, "cancelled", NavigationResult("cancelled"), command_id
            )
        if self._controller(row, actor_id) != record["controller_principal_id"]:
            raise StateMachineError("unauthorized", "Original navigation controller was revoked")
        if type(operation) is ResumeNavigation:
            if record["status"] != "blocked":
                raise StateMachineError("activity_inactive", "Only a blocked activity can resume")
            self._ready(row, actor_id, package)
            self._check_plan_position(record, package, row)
            return self._finish(record, package, "active", NavigationResult("resumed"), command_id)
        if type(operation) is not AdvanceNavigation or record["status"] != "active":
            raise StateMachineError("activity_inactive", "Activity cannot execute another step")
        try:
            with self.store.savepoint():
                self._check_plan_position(record, package, row)
                plan = json.loads(record["plan_json"])
                if record["cursor"] == len(plan):
                    # Check current actor state even for a zero-distance completion.
                    scope = self.records.execution(record, None)
                    self.world._world_actor_ready(sid, actor_id, navigation_scope=scope)
                    if self.world._active_combat(sid):
                        raise StateMachineError(
                            "unsupported_combat_world_operation", "Navigation is noncombat only"
                        )
                    return self._finish(
                        record, package, "completed", NavigationResult("arrived"), command_id
                    )
                primitive, advances = self._next_primitive(record, plan[record["cursor"]], package)
                scope = self.records.execution(record, primitive)
                events = self.world._operate(
                    row, package, primitive, actor_id=actor_id, navigation_scope=scope
                )
                result = NavigationResult(
                    "progressed", "open_door" if not advances else plan[record["cursor"]]["kind"]
                )
                events.extend(
                    self._finish(
                        record,
                        package,
                        "active",
                        result,
                        command_id,
                        cursor=record["cursor"] + int(advances),
                    )
                )
                return events
        except StateMachineError as exc:
            if exc.code in {"storage_error", "unauthorized"}:
                raise
            # No blocker identity or far-side layout is exposed by this result.
            code = "capability_unavailable" if exc.code.startswith("unsupported_") else "blocked"
            return self._finish(record, package, "blocked", NavigationResult(code), command_id)

    def _check_plan_position(self, record, package, row):
        """Reject a plan step inconsistent with the actor's current scene and cell."""
        scenes, passages = granted_layout(
            row, record["actor_id"], package, instances=self.world.facts.instances
        )
        plan = json.loads(record["plan_json"])
        if record["cursor"] < len(plan):
            step = plan[record["cursor"]]
            expected = (step.get("scene_id", step.get("from_scene_id")), step["expected_cell"])
            if step["kind"] == "passage" and step["passage_id"] not in passages:
                raise StateMachineError(
                    "navigation_knowledge_unavailable", "Route authorization changed"
                )
        else:
            expected = (record["target_scene_id"], record["target_cell"])
        position = self.characters.position(record["session_id"], record["actor_id"])
        if position.scene_id not in scenes or record["target_scene_id"] not in scenes:
            raise StateMachineError(
                "navigation_knowledge_unavailable", "Route authorization changed"
            )
        if (position.scene_id, position.cell) != expected:
            raise StateMachineError("position_conflict", "Plan position precondition changed")

    def _next_primitive(self, record, step, package):
        """Translate the next plan step into a move, opening or passage primitive."""
        data = {k: v for k, v in step.items() if k != "kind"}
        if step["kind"] == "step":
            return StepActor(**data), True
        passage = next(
            p
            for p in self.world.facts.instances.passages(
                record["session_id"], passage_id=step["passage_id"]
            )
            if p.passage_id == step["passage_id"]
        )
        if passage.door_object_id and self.world._door_closed(
            record["session_id"], passage, package
        ):
            return SetObjectState(
                step["from_scene_id"], passage.door_object_id, ObjectState.OPEN
            ), False
        return TraversePassage(**data), True

    def read(self, session_id, actor_id, activity_id, package, *, context):
        """Authorize activity access and return a detached snapshot at the current version."""
        with self.store.transaction():
            self.world._spatial_reader(session_id, actor_id, package, context)
            record = self.records.get(session_id, activity_id)
            if record is None or record["actor_id"] != actor_id:
                raise StateMachineError("invalid_activity", "No authorized actor activity")
            version = self.store.connection.execute(
                "SELECT world_version FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            return self.records.snapshot(record, version)
