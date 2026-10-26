"""Runtime coordination. Ports own deadlines and rules; WorldFacts owns time."""

from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol

from .errors import StateMachineError
from .game_time import AdvanceGameTime
from .time_binding import TimeBinding, integer
from .time_waits import DomainTimeWait, TimeWaitReference, TimeWaits


@dataclass(frozen=True, slots=True)
class AdvanceSessionTime:
    """Request bounded advancement through unified time coordination."""

    target_tick: int
    source: str = "manual"
    generation: int | None = None
    driver_token: str | None = None
    budget: int = 8


@dataclass(frozen=True, slots=True)
class SetTimeRunning:
    """Request explicit time mode/rate control."""

    action: str
    rate_numerator: int | None = None
    rate_denominator: int | None = None


def validate_time_operation(op):
    """Validate supported time operations and integer parameters."""
    if type(op) is AdvanceSessionTime:
        integer(op.target_tick, "target_tick", code="invalid_operation")
        integer(op.budget, "budget", minimum=1, maximum=16, code="invalid_operation")
        if op.source not in ("manual", "activity", "realtime"):
            raise StateMachineError("invalid_operation", "Unknown time source")
        if op.source == "realtime":
            integer(op.generation, "generation", code="invalid_operation")
            if type(op.driver_token) is not str or not op.driver_token:
                raise StateMachineError("invalid_operation", "Realtime requires driver ownership")
        elif op.generation is not None or op.driver_token is not None:
            raise StateMachineError("invalid_operation", "Only realtime carries driver identity")
    else:
        if op.action not in ("start", "pause", "rate", "cancel"):
            raise StateMachineError("invalid_operation", "Unknown time control")
        if (op.rate_numerator is None) != (op.rate_denominator is None):
            raise StateMachineError("invalid_operation", "Provide both rate fields")
        if op.rate_numerator is not None:
            for name in ("rate_numerator", "rate_denominator"):
                integer(getattr(op, name), name, minimum=1, code="invalid_operation")
        if op.action == "rate" and op.rate_numerator is None:
            raise StateMachineError("invalid_operation", "Rate must be explicit")
        if op.action in ("pause", "cancel") and op.rate_numerator is not None:
            raise StateMachineError("invalid_operation", "This control has no rate")


@dataclass(frozen=True, slots=True)
class DueResult:
    """Return one consumer's settled boundary events and progress outcome."""

    events: tuple = ()
    status: str = "continue"  # waiting / interrupted end this short transaction
    reason: str | None = None
    wait: DomainTimeWait | None = None


class TimeBoundaryConsumer(Protocol):
    """Internal domain port. Reads current authoritative state on every call.

    settle_one handles one bounded item at/before committed time, including late
    additions, or returns waiting/interrupted. It must not perform external I/O.
    Persist external work in this transaction; resume with another time command.
    """

    def next_boundary(self, session_id, package) -> int | None:
        """Return the consumer's next time boundary."""
        ...

    def settle_one(self, row, package, command_id) -> DueResult:
        """Settle one bounded consumer time boundary."""
        ...


@dataclass(frozen=True, slots=True)
class TimeSync:
    """Expose current time mode, progress and pending synchronization work."""

    committed_tick: int
    estimated_target_tick: int
    display_base_tick: int
    display_fraction_numerator: int
    display_fraction_denominator: int
    rate_numerator: int | None
    rate_denominator: int | None
    state: str
    generation: int
    world_version: int
    pending_target_tick: int | None
    progress_status: str | None
    progress_reason: str | None
    schema_version: str = "session-time-sync/1"
    time_wait: TimeWaitReference | None = None


class SessionTime:
    """Coordinate the sole world clock, bounded consumers, waits and attached drivers."""

    def __init__(self, runtime, world, effects):
        """Bind unified time coordination, consumers, waits and driver registry."""
        self.runtime, self.world, self.effects = runtime, world, effects
        self.store = runtime.store
        self.consumers = {"mechanisms": world.objects.mechanisms}
        self.drivers = {}
        self.waits = TimeWaits(self)
        # Multiple domain owners at one instant require a reviewed dependency policy.
        self.resolve_simultaneous = None

    @property
    def db(self):
        """Return the unified time connection."""
        return self.store.connection

    def run(self, sid):
        """Read persisted time mode, defaulting to paused when absent."""
        row = self.db.execute(
            "SELECT * FROM session_time_runs WHERE session_id=?", (sid,)
        ).fetchone()
        return (
            dict(row)
            if row
            else dict(
                mode="paused",
                rate_numerator=None,
                rate_denominator=None,
                generation=0,
                remainder="0",
            )
        )

    def progress(self, sid):
        """Read persisted time advancement progress."""
        return self.db.execute(
            "SELECT * FROM session_time_progress WHERE session_id=?", (sid,)
        ).fetchone()

    def initialize(self, sid):
        """Initialize time runtime and progress records."""
        self.store.require_write()
        self.db.execute(
            "INSERT INTO session_time_runs(session_id,mode) VALUES (?,'paused') "
            "ON CONFLICT DO NOTHING",
            (sid,),
        )

    def invalidate(self, sid, *, remainder="0"):
        """Invalidate time runtime generation and progress."""
        self.initialize(sid)
        self.db.execute(
            "UPDATE session_time_runs SET generation=generation+1,remainder=? WHERE session_id=?",
            (str(remainder), sid),
        )

    def boundaries(self, sid, package):
        """Collect the next valid time boundary from each consumer."""
        result = [
            (name, tick)
            for name, port in self.consumers.items()
            if (tick := port.next_boundary(sid, package)) is not None
        ]
        for name, tick in result:
            integer(tick, name, code="invalid_time_consumer")
        return result

    def _advance(self, row, package, target, command_id, source, budget):
        """Advance the sole world tick within bounded write work, settling consumers and
        waits.
        """
        self.store.require_write()
        sid = row["session_id"]
        if self.waits.active(sid) is not None:
            raise StateMachineError(
                "time_wait_unresolved", "Domain reply must release the current wait"
            )
        current = self.world.facts.time_state(sid)[1]
        integer(target, "target_tick", minimum=current, code="invalid_operation")
        pending = self.progress(sid)
        if pending and pending["reason"] == "wait_interrupt":
            raise StateMachineError(
                "time_advance_interrupted", "Abandon the interrupted target first"
            )
        if (
            pending
            and pending["status"] != "completed"
            and (pending["target_tick"] != target or pending["source"] != source)
        ):
            raise StateMachineError("time_advance_pending", "Continue or cancel the saved target")
        events, status, reason = [], "completed", None
        for _ in range(budget):
            boundaries = self.boundaries(sid, package)
            nearest = min((t for _, t in boundaries), default=None)
            next_tick = target if nearest is None else min(target, max(current, nearest))
            staged_row = dict(row)
            staged_row["event_seq"] += len(events)
            events.extend(
                self.effects._expand_events(
                    staged_row,
                    package,
                    [
                        self.world._capture_event(
                            sid, package, kind, payload, phase="clock.after_advance"
                        )
                        for kind, payload in self.world.time.advance(
                            row, AdvanceGameTime(current, next_tick)
                        )
                    ],
                    command_id,
                )
            )
            current = next_tick
            due = [name for name, tick in boundaries if tick <= current]
            if not due:
                break
            if len(due) > 1:
                chosen = (
                    self.resolve_simultaneous(sid, tuple(due))
                    if self.resolve_simultaneous
                    else None
                )
                if chosen not in due:
                    status, reason = "waiting", "simultaneous_time_choice"
                    break
            else:
                chosen = due[0]
            staged_row = dict(row)
            staged_row["event_seq"] += len(events)
            result = self.consumers[chosen].settle_one(staged_row, package, command_id)
            if result.status not in ("continue", "waiting", "interrupted"):
                raise StateMachineError("invalid_time_consumer", "Invalid boundary outcome")
            if result.wait is not None:
                if result.status != "waiting":
                    raise StateMachineError(
                        "invalid_time_wait", "Only waiting outcomes declare a wait"
                    )
                self.waits.open(row, package, chosen, result.wait, command_id)
            events.extend(
                self.effects._expand_events(staged_row, package, list(result.events), command_id)
            )
            if result.status != "continue":
                status, reason = result.status, result.reason
                break
        else:
            status, reason = "pending", "budget_exhausted"
        # Even an equal target must drain due work; late additions are clamped to
        # current, consume the same budget and never rewind or run without a bound.
        self.db.execute(
            "INSERT INTO session_time_progress VALUES (?,?,?,?,?) ON CONFLICT(session_id) "
            "DO UPDATE SET target_tick=excluded.target_tick,source=excluded.source,"
            "status=excluded.status,reason=excluded.reason",
            (sid, target, source, status, reason),
        )
        if status in ("waiting", "interrupted") or (
            pending and pending["status"] in ("waiting", "interrupted")
        ):
            self.invalidate(sid)  # business continuation never adds time spent waiting
        return events

    def advance(self, row, op, package, command_id):
        """Check combat/mode prerequisites and advance time, returning resulting events."""
        sid = row["session_id"]
        if self.world._active_combat(sid):
            raise StateMachineError("unsupported_combat_world_operation", "Combat owns time")
        if type(op) is AdvanceGameTime:
            if self.world.facts.time_state(sid)[1] != op.expected_tick:
                raise StateMachineError("game_time_conflict", "Game time precondition differs")
            target, source, budget = op.next_tick, "manual", 8
        else:
            target, source, budget = op.target_tick, op.source, op.budget
            if source == "realtime":
                driver = self.drivers.get(sid)
                run = self.run(sid)
                if (
                    driver is None
                    or driver.closed
                    or driver.token != op.driver_token
                    or run["generation"] != op.generation
                    or run["mode"] != "running"
                ):
                    raise StateMachineError(
                        "stale_time_driver", "Driver no longer owns this request"
                    )
                saved = self.progress(sid)
                continuation = (
                    saved
                    and saved["status"] != "completed"
                    and saved["source"] == source
                    and saved["target_tick"] == target
                )
                if not continuation and target > int(driver.target(run)):
                    raise StateMachineError("invalid_operation", "Target exceeds elapsed realtime")
        events = self._advance(row, package, target, command_id, source, budget)
        if source != "realtime":
            self.invalidate(sid)  # discard wall time spent performing a jump
        return events

    def control(self, row, op, package, command_id):
        """Change realtime/manual/paused mode through controlled persistence and driver
        coordination.
        """
        sid, events = row["session_id"], []
        remainder = Fraction(self.run(sid)["remainder"])
        if type(self.world.facts.time_state(sid)[0]) is not TimeBinding:
            raise StateMachineError("unsupported_time_protocol", "Realtime requires world-time/1")
        run, driver = self.run(sid), self.drivers.get(sid)
        if self.world._active_combat(sid):
            raise StateMachineError("time_mode_conflict", "Combat owns time")
        if op.action == "cancel":
            saved = self.progress(sid)
            if saved is None or saved["status"] == "completed":
                return []  # Receipt only: elapsed realtime and its epoch are untouched.
            if self.waits.active(sid) is not None:
                raise StateMachineError(
                    "time_wait_unresolved", "Only the waiting domain may release its wait"
                )
            if saved["source"] == "realtime" and saved["status"] == "pending":
                raise StateMachineError(
                    "time_advance_pending", "Elapsed realtime must finish its bounded settlement"
                )
            current = self.world.facts.time_state(sid)[1]
            if any(tick <= current for _, tick in self.boundaries(sid, package)):
                raise StateMachineError(
                    "time_advance_pending",
                    "Settle or cancel domain work before dropping its target",
                )
            self.db.execute("DELETE FROM session_time_progress WHERE session_id=?", (sid,))
            remainder = Fraction(0)
        else:
            self.initialize(sid)
            saved = self.progress(sid)
            suspended = saved and saved["status"] != "completed"
            if suspended and saved["source"] != "realtime" and op.action != "pause":
                raise StateMachineError(
                    "time_advance_pending", "Continue or cancel saved work first"
                )
            if (
                run["mode"] == "running"
                and driver
                and not (suspended and saved["source"] != "realtime")
            ):
                admission = self.db.execute(
                    "SELECT target_tick,target_fraction FROM time_command_admissions "
                    "WHERE session_id=? AND command_id=?",
                    (sid, command_id),
                ).fetchone()
                sampled = (
                    Fraction(admission["target_tick"]) + Fraction(admission["target_fraction"])
                    if admission
                    else driver.target(run)
                )
                saved = self.progress(sid)
                target = (
                    saved["target_tick"]
                    if saved and saved["status"] != "completed"
                    else int(sampled)
                )
                events = self._advance(row, package, target, command_id, "realtime", 8)
                remainder = sampled - int(sampled)
                if self.progress(sid)["status"] != "completed":
                    return events  # caller continues saved work before changing configuration
            if op.rate_numerator is not None:
                rate = Fraction(op.rate_numerator, op.rate_denominator)
                self.db.execute(
                    "UPDATE session_time_runs SET rate_numerator=?,rate_denominator=? "
                    "WHERE session_id=?",
                    (rate.numerator, rate.denominator, sid),
                )
            if op.action == "start" and self.run(sid)["rate_numerator"] is None:
                raise StateMachineError("time_rate_unconfigured", "No default realtime rate")
            if op.action in ("start", "pause"):
                self.db.execute(
                    "UPDATE session_time_runs SET mode=? WHERE session_id=?",
                    ("running" if op.action == "start" else "paused", sid),
                )
        self.invalidate(sid, remainder=remainder)
        events.append(
            self.world._capture_event(
                sid,
                package,
                "session.time_configuration_changed",
                {"action": op.action, **self.run(sid)},
                phase="session.after_time_control",
            )
        )
        return events

    def sync(self, sid, package, *, context):
        """Read only. Display base is sampled at response, never a host epoch."""
        with self.store.transaction():
            row = self.runtime.access._session(sid, package, context)
            run, pending = self.run(sid), self.progress(sid)
            tick = self.world.facts.time_state(sid)[1]
            driver = self.drivers.get(sid)
            state = (
                "combat"
                if self.world._active_combat(sid)
                else "suspended"
                if pending and pending["status"] != "completed"
                else "running"
                if run["mode"] == "running" and driver
                else "detached"
                if run["mode"] == "running"
                else "paused"
            )
            target = (
                driver.target(run)
                if state == "running"
                else Fraction(tick) + Fraction(run["remainder"])
                if state in ("paused", "detached")
                else Fraction(tick)
            )
            fraction = target - int(target)
            return TimeSync(
                tick,
                int(target),
                int(target),
                fraction.numerator,
                fraction.denominator,
                run["rate_numerator"],
                run["rate_denominator"],
                state,
                run["generation"],
                row["world_version"],
                pending["target_tick"] if pending else None,
                pending["status"] if pending else None,
                pending["reason"] if pending else None,
                time_wait=self.waits.reference(sid),
            )
