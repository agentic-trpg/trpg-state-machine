"""Domain-authorized replies at a durable time boundary, through the same executor."""

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from .errors import StateMachineError
from .time_binding import integer


@dataclass(frozen=True, slots=True)
class TimeWaitReference:
    """Bind a wait identity to the domain version expected by a reply."""

    wait_id: str
    version: int


@dataclass(frozen=True, slots=True)
class DomainTimeWait:
    """Describe a domain-owned blocking time wait."""

    wait_id: str
    version: int
    status: str = "waiting"


@dataclass(frozen=True, slots=True)
class TimeWaitResolution:
    """Return the domain consequence of resolving a wait."""

    outcome: str  # continue / waiting / interrupt / cancel
    events: tuple = ()


class TimeWaitConsumer(Protocol):
    """Optional internal port on a registered boundary consumer.

    Authorization is read-only, repeated before replay and final adoption. It
    checks command kind, subject/choice syntax and business grants, not whether
    the old wait is still open (Runtime checks that after receipt replay).
    Resolution joins the existing command savepoint, may update only this
    domain's facts, and must not wait on external I/O. RE uses RuleExecution's
    unchanged detached prepare/evaluate/adopt path before this finalizer.
    """

    def read_time_wait(self, sid, wait_id, package) -> DomainTimeWait | None:
        """Read domain wait state."""
        ...

    def authorize_time_wait_command(self, row, package, command, context, wait):
        """Authorize a reply command against domain wait state."""
        ...

    def resolve_time_wait(self, row, package, command, wait, *, events) -> TimeWaitResolution:
        """Resolve the domain wait and return its result."""
        ...


def validate_wait_reference(ref):
    """Validate wait identity and domain-version reference."""
    if (
        type(ref) is not TimeWaitReference
        or type(ref.wait_id) is not str
        or not ref.wait_id.strip()
        or len(ref.wait_id) > 256
    ):
        raise StateMachineError("invalid_time_wait", "Expected a typed bounded wait reference")
    integer(ref.version, "time_wait.version", minimum=1, code="invalid_time_wait")


class TimeWaits:
    """Persist unified wait coordination while delegating domain authority to consumers."""

    def __init__(self, time):
        """Bind waits to the unified time coordinator."""
        self.time = time

    @property
    def db(self):
        """Return the wait persistence connection."""
        return self.time.db

    def active(self, sid):
        """Read the Session's active wait, or return None."""
        return self.db.execute(
            "SELECT * FROM session_time_waits WHERE session_id=? AND status='waiting'", (sid,)
        ).fetchone()

    def reference(self, sid):
        """Build a stable wait identity and domain-version reference."""
        wait = self.active(sid)
        return None if wait is None else TimeWaitReference(wait["wait_id"], wait["domain_version"])

    def _consumer(self, name):
        """Require the domain consumer to implement the complete wait protocol."""
        port = self.time.consumers.get(name)
        if port is None or any(
            not callable(getattr(port, method, None))
            for method in ("read_time_wait", "authorize_time_wait_command", "resolve_time_wait")
        ):
            raise StateMachineError("unsupported_time_wait", "Waiting domain port is not installed")
        return port

    def open(self, row, package, consumer, domain, command_id):
        """Check domain wait state and persist the unified time wait in the write scope."""
        self.time.store.require_write()
        if type(domain) is not DomainTimeWait or domain.status != "waiting":
            raise StateMachineError("invalid_time_wait", "Domain must identify an open wait")
        validate_wait_reference(TimeWaitReference(domain.wait_id, domain.version))
        port = self._consumer(consumer)
        if port.read_time_wait(row["session_id"], domain.wait_id, package) != domain:
            raise StateMachineError("invalid_time_wait", "Wait differs from domain facts")
        if self.active(row["session_id"]) is not None:
            raise StateMachineError("time_wait_unresolved", "Resolve the current wait first")
        tick = self.time.world.facts.time_state(row["session_id"])[1]
        self.db.execute(
            "INSERT INTO session_time_waits VALUES (?,?,?,?,?,?,?,'waiting')",
            (
                row["session_id"],
                "time-wait." + uuid4().hex,
                consumer,
                domain.wait_id,
                domain.version,
                tick,
                command_id,
            ),
        )

    def authorize(self, row, package, command, context):
        """Match the reply command to the persisted wait and delegate domain authorization."""
        if command.time_wait is None:
            return None
        ref = command.time_wait
        wait = self.db.execute(
            "SELECT * FROM session_time_waits WHERE session_id=? AND wait_id=?",
            (command.session_id, ref.wait_id),
        ).fetchone()
        if wait is None:
            raise StateMachineError("invalid_time_wait", "Wait does not belong to this Session")
        self._consumer(wait["consumer"]).authorize_time_wait_command(
            row,
            package,
            command,
            context,
            DomainTimeWait(wait["domain_wait_id"], ref.version),
        )
        return wait

    def validate(self, row, package, command):
        """Revalidate wait state, domain version and current time advancement
        prerequisites.
        """
        ref = command.time_wait
        wait = self.db.execute(
            "SELECT * FROM session_time_waits WHERE session_id=? AND wait_id=?",
            (command.session_id, ref.wait_id),
        ).fetchone()
        if wait is None or wait["status"] != "waiting" or wait["domain_version"] != ref.version:
            raise StateMachineError("stale_time_wait", "Reply belongs to an obsolete waiting state")
        domain = DomainTimeWait(wait["domain_wait_id"], wait["domain_version"])
        if (
            self._consumer(wait["consumer"]).read_time_wait(
                command.session_id, domain.wait_id, package
            )
            != domain
        ):
            raise StateMachineError("stale_time_wait", "Domain waiting state changed")
        if self.time.world.facts.time_state(command.session_id)[1] != wait["boundary_tick"]:
            raise StateMachineError("stale_time_wait", "Committed boundary changed")
        progress = self.time.progress(command.session_id)
        if progress is None or progress["status"] != "waiting":
            raise StateMachineError("stale_time_wait", "Waiting continuation changed")
        return wait

    def finish(self, row, package, command, events=()):
        """Delegate domain resolution and atomically update unified wait state."""
        if command.time_wait is None:
            return []
        self.time.store.require_write()
        wait = self.validate(row, package, command)
        port = self._consumer(wait["consumer"])
        domain = DomainTimeWait(wait["domain_wait_id"], wait["domain_version"])
        result = port.resolve_time_wait(row, package, command, domain, events=tuple(events))
        state = port.read_time_wait(command.session_id, domain.wait_id, package)
        statuses = {
            "continue": "resolved",
            "waiting": "waiting",
            "interrupt": "interrupted",
            "cancel": "cancelled",
        }
        if (
            type(result) is not TimeWaitResolution
            or type(result.outcome) is not str
            or result.outcome not in statuses
            or type(state) is not DomainTimeWait
            or state.wait_id != domain.wait_id
            or type(state.version) is not int
            or state.version <= domain.version
            or state.status != statuses[result.outcome]
            or self.time.world.facts.time_state(command.session_id)[1] != wait["boundary_tick"]
        ):
            raise StateMachineError(
                "invalid_time_wait_result", "Domain did not commit the claimed reply"
            )
        integer(state.version, "domain_wait.version", minimum=1, code="invalid_time_wait_result")
        self.db.execute(
            "UPDATE session_time_waits SET domain_version=?,status=? "
            "WHERE session_id=? AND wait_id=?",
            (state.version, state.status, command.session_id, wait["wait_id"]),
        )
        progress = self.time.progress(command.session_id)
        if result.outcome in ("continue", "waiting"):
            # Only this accepted reply translates its origin admission. Any
            # unrelated intervening version still makes that admission stale.
            self.db.execute(
                "UPDATE time_command_admissions SET execution_version=? "
                "WHERE session_id=? AND command_id=? AND ready=0 AND execution_version=? "
                "AND target_tick=? AND source=?",
                (
                    row["world_version"] + 1,
                    command.session_id,
                    wait["origin_command_id"],
                    row["world_version"],
                    progress["target_tick"],
                    progress["source"],
                ),
            )
        if result.outcome != "waiting":
            self.db.execute(
                "UPDATE session_time_progress SET status=?,reason=?,target_tick=? "
                "WHERE session_id=?",
                (
                    "interrupted" if result.outcome == "interrupt" else "pending",
                    "wait_" + result.outcome,
                    wait["boundary_tick"]
                    if result.outcome == "cancel"
                    else progress["target_tick"],
                    command.session_id,
                ),
            )
        self.time.invalidate(command.session_id)
        return [
            *result.events,
            self.time.world._capture_event(
                command.session_id,
                package,
                "session.time_wait_replied",
                {
                    "wait_id": wait["wait_id"],
                    "version": state.version,
                    "outcome": result.outcome,
                    "game_tick": wait["boundary_tick"],
                },
                phase="session.after_time_wait_reply",
            ),
        ]
