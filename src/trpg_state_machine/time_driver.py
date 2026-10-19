"""Small same-thread Runtime component; no thread, business rules or clock writes."""

import asyncio
import hashlib
import os
from contextlib import ExitStack
from fractions import Fraction
from pathlib import Path
from time import monotonic_ns
from uuid import uuid4

from .errors import StateMachineError
from .time_binding import MAX_TICK, TimeBinding
from .time_coordination import AdvanceSessionTime


class _DriverLock:
    """Local OS ownership, released on process death. No persisted monotonic epoch."""

    @staticmethod
    def path(database, sid):
        key = hashlib.sha256(sid.encode()).hexdigest()[:32]
        return Path(database).with_name(Path(database).name + f".time-{key}.lock")

    def __init__(self, database, sid):
        self.resources = ExitStack()
        self.file = self.resources.enter_context(self.path(database, sid).open("a+b"))
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0, os.SEEK_END)
                if self.file.tell() == 0:
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.resources.close()
            raise StateMachineError(
                "time_driver_owned", "Session already has a realtime driver"
            ) from exc

    def close(self):
        self.resources.close()


class TimeDriver:
    """Scheduler.call_later(seconds, callback) returns a cancellable handle.

    Clock is integer monotonic nanoseconds. Scheduler callbacks MUST run on the
    owning Store thread (e.g. asyncio loop); threading.Timer is not supported.
    """

    def __init__(
        self, runtime, sid, package, context, submit, *, clock_ns=monotonic_ns, scheduler=None
    ):
        self.runtime, self.time = runtime, runtime.time
        self.sid, self.package, self.context, self.submit = sid, package, context, submit
        self.clock_ns = clock_ns
        self.scheduler = scheduler if scheduler is not None else asyncio.get_running_loop()
        self.closed, self.handle, self.epoch = False, None, 0
        self.token, self.generation = uuid4().hex, None
        self.last_error = None
        with runtime.store.transaction():
            row = runtime.access._session(sid, package, context)
            runtime.access._authorize_command(
                row, row["pc_actor_id"], AdvanceSessionTime(0), context
            )
            if package.legacy_audit_only:
                raise StateMachineError("legacy_rules_audit_only", "Audit sessions cannot run")
            if type(self.time.world.facts.time_state(sid)[0]) is not TimeBinding:
                raise StateMachineError("unsupported_time_protocol", "Realtime needs seconds")
            database = self.time.db.execute("PRAGMA database_list").fetchone()[2]
        self.lock = _DriverLock(database, sid)
        try:
            if sid in self.time.drivers:
                raise StateMachineError("time_driver_owned", "Session already has a driver")
            with runtime.store.transaction(write=True):
                self.time.invalidate(sid, remainder=self.time.run(sid)["remainder"])
            self.time.drivers[sid] = self
            self.refresh()
        except BaseException:
            self.time.drivers.pop(sid, None)
            self.lock.close()
            raise

    def target(self, run):
        if self.generation != run["generation"]:
            self.rebase(run)
        elapsed = max(0, self.clock_ns() - self.base_ns)
        if run["mode"] != "running" or run["rate_numerator"] is None:
            return self.base_tick
        return min(
            Fraction(MAX_TICK),
            self.base_tick
            + Fraction(elapsed, 1_000_000_000)
            * Fraction(run["rate_numerator"], run["rate_denominator"]),
        )

    def rebase(self, run):
        self.base_tick = Fraction(self.time.world.facts.time_state(self.sid)[1]) + Fraction(
            run.get("remainder", "0")
        )
        self.base_ns = self.clock_ns()
        self.generation = run["generation"]

    def _cancel(self):
        self.epoch += 1
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None

    def refresh(self):
        if self.closed:
            return
        self._cancel()
        with self.runtime.store.transaction():
            run = self.time.run(self.sid)
            target = self.target(run)
            if run["mode"] != "running" or self.time.world._active_combat(self.sid):
                return
            pending = self.time.progress(self.sid)
            if self.time.db.execute(
                "SELECT 1 FROM time_command_admissions a JOIN sessions s USING(session_id) "
                "WHERE a.session_id=? AND a.ready=0 AND a.execution_version=s.world_version "
                "AND NOT EXISTS (SELECT 1 FROM command_receipts r WHERE r.session_id=a.session_id "
                "AND r.command_id=a.command_id)",
                (self.sid,),
            ).fetchone():
                return  # the admitted command owns its fixed synchronization target
            if pending and pending["status"] in ("waiting", "interrupted"):
                return
            if pending and pending["status"] == "pending":
                delay = 0
            else:
                nearest = min(
                    (t for _, t in self.time.boundaries(self.sid, self.package)), default=None
                )
                if nearest is None:
                    return  # display queries never require a per-second write/wakeup
                seconds = max(Fraction(0), Fraction(nearest) - target) / Fraction(
                    run["rate_numerator"], run["rate_denominator"]
                )
                nanoseconds = seconds * 1_000_000_000
                delay = (
                    (nanoseconds.numerator + nanoseconds.denominator - 1) // nanoseconds.denominator
                ) / 1_000_000_000
            epoch, generation = self.epoch, run["generation"]
        self.handle = self.scheduler.call_later(delay, lambda: self._wake(epoch, generation))

    def _wake(self, epoch, generation):
        if self.closed or epoch != self.epoch:
            return
        try:
            with self.runtime.store.transaction():
                run = self.time.run(self.sid)
                if generation != run["generation"] or run["mode"] != "running":
                    return
                if self.time.db.execute(
                    "SELECT 1 FROM time_command_admissions a JOIN sessions s USING(session_id) "
                    "WHERE a.session_id=? AND a.ready=0 AND a.execution_version=s.world_version "
                    "AND NOT EXISTS (SELECT 1 FROM command_receipts r "
                    "WHERE r.session_id=a.session_id AND r.command_id=a.command_id)",
                    (self.sid,),
                ).fetchone():
                    return
                row = self.runtime.access._session(self.sid, self.package, self.context)
                pending = self.time.progress(self.sid)
                target = (
                    pending["target_tick"]
                    if pending and pending["status"] == "pending"
                    else int(self.target(run))
                )
                source = (
                    pending["source"] if pending and pending["status"] == "pending" else "realtime"
                )
                from .commands import TypedCommand

                command = TypedCommand(
                    self.sid,
                    "time." + uuid4().hex,
                    row["pc_actor_id"],
                    "session.advance_time",
                    AdvanceSessionTime(
                        target,
                        source,
                        generation if source == "realtime" else None,
                        self.token if source == "realtime" else None,
                    ),
                    row["world_version"],
                )
            receipt = self.submit(command, self.package, context=self.context)
            self.last_error = receipt.error_code
            if receipt.status != "committed":
                self._cancel()
                return
        except Exception as exc:
            # No autonomous hot retry loop after faults; host explicitly retries
            # refresh(), preserving the baseline and durable progress.
            self.last_error = getattr(exc, "code", type(exc).__name__)
            self._cancel()
            return
        self.refresh()

    def close(self):
        if not self.closed:
            self.closed = True
            self._cancel()
            self.time.drivers.pop(self.sid, None)
            self.lock.close()
