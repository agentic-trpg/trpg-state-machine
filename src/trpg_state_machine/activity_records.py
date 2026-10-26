"""Durable execution progress; never a second position, map or character store."""

from dataclasses import asdict, dataclass

from .errors import StateMachineError
from .navigation_models import NavigationActivity, NavigationResult
from .package import canonical_json
from .scoped_reads import scoped_read


@dataclass(frozen=True, slots=True)
class _NavigationExecution:
    """Private navigation authority tied to issuer, transaction, activity and version."""

    issuer: object
    transaction: object
    session_id: str
    activity_id: str
    actor_id: str
    version: int
    operation: object


class ActivityRecords:
    """Persist navigation progress and coordinate NPC activity claims through one Store."""

    def __init__(self, store):
        """Bind the shared Store and a private execution-credential issuer."""
        self.store = store
        self._issuer = object()

    @property
    def _conn(self):
        """Return the Store-managed connection."""
        return self.store.connection

    def get(self, sid, aid):
        """Read a navigation row by Session and activity identity, or return None."""
        return self._conn.execute(
            "SELECT * FROM navigation_activities WHERE session_id=? AND activity_id=?", (sid, aid)
        ).fetchone()

    def active(self, sid, actor):
        """Read the actor's active navigation row, or return None."""
        return self._conn.execute(
            "SELECT * FROM navigation_activities WHERE session_id=? "
            "AND actor_id=? AND status='active'",
            (sid, actor),
        ).fetchone()

    @scoped_read
    def snapshot(self, record, world_version):
        """Decode durable plan/results into an activity snapshot at the supplied world
        version.
        """
        plan = tuple(self.store.memory.json(record["plan_json"]))
        return NavigationActivity(
            record["session_id"],
            record["activity_id"],
            record["actor_id"],
            record["target_scene_id"],
            record["target_cell"],
            record["status"],
            record["activity_version"],
            record["cursor"],
            len(plan),
            plan,
            NavigationResult(**self.store.memory.json(record["last_result_json"])),
            record["source_command_id"],
            record["last_command_id"],
            world_version,
        )

    def create(self, sid, actor, operation, plan, command_id, controller):
        """Create an activity in the write scope and claim its NPC activity marker."""
        self.store.require_write()
        self._conn.execute(
            "INSERT INTO navigation_activities VALUES (?,?,?,?,?,'active',1,0,?,?,?,?,?)",
            (
                sid,
                operation.activity_id,
                actor,
                operation.target_scene_id,
                operation.target_cell,
                canonical_json(plan),
                canonical_json(asdict(NavigationResult("started"))),
                command_id,
                command_id,
                controller,
            ),
        )
        record = self.get(sid, operation.activity_id)
        self._claim(record)
        return record

    def transition(self, record, status, cursor, result, command_id):
        """Update progress with version checking and reconcile the NPC activity claim."""
        self.store.require_write()
        if status not in {"active", "blocked", "completed", "cancelled"}:
            raise StateMachineError("invalid_activity", "Unknown navigation state")
        changed = self._conn.execute(
            "UPDATE navigation_activities SET status=?,cursor=?,"
            "activity_version=activity_version+1,"
            "last_result_json=?,last_command_id=? WHERE session_id=? AND activity_id=? "
            "AND activity_version=?",
            (
                status,
                cursor,
                canonical_json(asdict(result)),
                command_id,
                record["session_id"],
                record["activity_id"],
                record["activity_version"],
            ),
        ).rowcount
        if changed != 1:
            raise StateMachineError("activity_version_conflict", "Activity progress changed")
        updated = self.get(record["session_id"], record["activity_id"])
        if record["status"] != "active" and status == "active":
            self._claim(updated)
        elif record["status"] == "active" and status != "active":
            self._release(record)
        return updated

    @staticmethod
    def marker(record):
        """Return the NPC activity marker for this navigation record."""
        return "navigation:" + record["activity_id"]

    def _runtime(self, record):
        """Read the NPC runtime row associated with the activity, or return None."""
        return self._conn.execute(
            "SELECT runtime_json FROM npc_states WHERE session_id=? AND npc_id=?",
            (record["session_id"], record["actor_id"]),
        ).fetchone()

    def _claim(self, record):
        """Claim the NPC activity slot, rejecting an existing activity."""
        self.store.require_write()
        npc = self._runtime(record)
        if npc and npc[0]:
            data = self.store.memory.json(npc[0])
            if data["current_activity"] is not None:
                raise StateMachineError("activity_conflict", "Actor already has an activity")
            data["current_activity"] = self.marker(record)
            self._write_runtime(record, data)

    def _release(self, record):
        """Release the NPC slot only if it still names this activity."""
        self.store.require_write()
        npc = self._runtime(record)
        if npc and npc[0]:
            data = self.store.memory.json(npc[0])
            if data["current_activity"] == self.marker(record):
                data["current_activity"] = None
                self._write_runtime(record, data)

    def _write_runtime(self, record, data):
        """Persist NPC runtime, increment its version and cancel running evaluation tasks."""
        self.store.require_write()
        self._conn.execute(
            "UPDATE npc_states SET runtime_json=?,npc_state_version=npc_state_version+1 "
            "WHERE session_id=? AND npc_id=?",
            (canonical_json(data), record["session_id"], record["actor_id"]),
        )
        self._conn.execute(
            "UPDATE npc_evaluation_tasks SET status='cancelled',last_error='activity_changed' "
            "WHERE session_id=? AND npc_id=? AND status='running'",
            (record["session_id"], record["actor_id"]),
        )

    def execution(self, record, operation):
        """Issue an internal navigation credential bound to the current write scope and
        version.
        """
        self.store.require_write()
        return _NavigationExecution(
            self._issuer,
            self.store._scope,
            record["session_id"],
            record["activity_id"],
            record["actor_id"],
            record["activity_version"],
            operation,
        )

    def qualifies(self, scope, sid, actor, operation=None):
        """Return whether credential, transaction, activity version and NPC claim still
        match.
        """
        if type(scope) is not _NavigationExecution or scope.issuer is not self._issuer:
            return False
        self.store.require_write()
        if scope.transaction is not self.store._scope:
            return False
        record = self.active(sid, actor)
        if (
            record is None
            or scope.session_id != sid
            or scope.actor_id != actor
            or scope.activity_id != record["activity_id"]
            or scope.version != record["activity_version"]
            or operation is not None
            and operation != scope.operation
        ):
            return False
        npc = self._runtime(record)
        return not (npc and npc[0]) or self.store.memory.json(npc[0])[
            "current_activity"
        ] == self.marker(record)
