"""Session world time business; WorldFacts alone reads/writes the clock facts."""

from dataclasses import dataclass
from fractions import Fraction

from .errors import StateMachineError
from .time_binding import LegacyTimeBinding, TimeBinding, integer


@dataclass(frozen=True, slots=True)
class TimeDuration:
    ticks: int
    seconds: int | None
    minutes: Fraction | None
    hours: Fraction | None
    days: Fraction | None


@dataclass(frozen=True, slots=True)
class TimeBoundary:
    tick: int
    kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BoundaryPage:
    boundaries: tuple[TimeBoundary, ...]
    next_cursor: int | None


@dataclass(frozen=True, slots=True)
class WorldTimeSnapshot:
    binding: TimeBinding | LegacyTimeBinding
    game_tick: int
    elapsed: TimeDuration
    current_day: int | None
    day_tick: int | None
    phase: str | None
    next_calendar_boundary: int | None
    next_phase_boundary: TimeBoundary | None


class WorldTimeService:
    def __init__(self, store, sessions, facts):
        self.store, self.sessions, self.facts = store, sessions, facts

    def initialize(self, sid, binding):
        self.store.require_write()
        if binding is not None:
            if type(binding) is not TimeBinding:
                raise StateMachineError("invalid_time_binding", "Expected typed TimeBinding")
            self.facts.initialize_time_binding(sid, binding)

    @staticmethod
    def _duration(ticks, binding):
        if type(binding) is LegacyTimeBinding:
            return TimeDuration(ticks, None, None, None, None)
        return TimeDuration(
            ticks,
            ticks,
            Fraction(ticks, 60),
            Fraction(ticks, 3600),
            Fraction(ticks, binding.day_length),
        )

    @staticmethod
    def _next(binding, tick, offset):
        # Strictly after tick, including when tick is exactly on a boundary.
        return tick + (offset - binding.start_day_tick - tick - 1) % binding.day_length + 1

    @classmethod
    def _phase(cls, binding, tick):
        if binding.cycle == "none":
            return None, None
        tod = (binding.start_day_tick + tick) % binding.day_length
        day = (tod - binding.day_start) % binding.day_length
        day_span = (binding.night_start - binding.day_start) % binding.day_length
        phase = "day" if day < day_span else "night"
        candidates = [
            (cls._next(binding, tick, offset), kind)
            for offset, kind in (
                (binding.day_start, "day_started"),
                (binding.night_start, "night_started"),
            )
        ]
        next_tick, kind = min(candidates)
        return phase, TimeBoundary(next_tick, (kind,))

    def read(self, session_id, package, *, context):
        with self.store.transaction():
            self.sessions._session(session_id, package, context)
            binding, tick = self.facts.time_state(session_id)
            duration = self._duration(tick, binding)
            if type(binding) is LegacyTimeBinding:
                return WorldTimeSnapshot(binding, tick, duration, None, None, None, None, None)
            days, tod = divmod(binding.start_day_tick + tick, binding.day_length)
            phase, next_phase = self._phase(binding, tick)
            return WorldTimeSnapshot(
                binding,
                tick,
                duration,
                binding.start_day + days,
                tod,
                phase,
                self._next(binding, tick, 0),
                next_phase,
            )

    def elapsed(self, session_id, package, *, context, since_tick=0):
        integer(since_tick, "since_tick")
        current = self.read(session_id, package, context=context)
        if since_tick > current.game_tick:
            raise StateMachineError("invalid_time_query", "Origin is later than current time")
        return self._duration(current.game_tick - since_tick, current.binding)

    def remaining(self, session_id, target_tick, package, *, context):
        integer(target_tick, "target_tick")
        current = self.read(session_id, package, context=context)
        return self._duration(max(0, target_tick - current.game_tick), current.binding)

    def reached(self, session_id, target_tick, package, *, context):
        integer(target_tick, "target_tick")
        return self.read(session_id, package, context=context).game_tick >= target_tick

    def to_ticks(self, session_id, amount, unit, package, *, context):
        integer(amount, "amount")
        current = self.read(session_id, package, context=context)
        if type(current.binding) is LegacyTimeBinding:
            raise StateMachineError(
                "unsupported_time_protocol", "Legacy ticks have no second scale"
            )
        scales = {"seconds": 1, "minutes": 60, "hours": 3600, "days": current.binding.day_length}
        if type(unit) is not str or unit not in scales:
            raise StateMachineError("invalid_time_query", "Unknown duration unit")
        return integer(amount * scales[unit], "duration")

    def boundaries(
        self, session_id, start_tick, end_tick, package, *, context, limit=100, after_tick=None
    ):
        """Sorted unique boundaries in (start,end]; cursor excludes its own tick.

        At most limit+1 occurrences computed, independent of the interval length.
        Simultaneous calendar/phase boundaries share one entry and one cursor.
        """
        integer(start_tick, "start_tick")
        integer(end_tick, "end_tick", minimum=start_tick)
        integer(limit, "limit", minimum=1, maximum=1000)
        if after_tick is not None:
            integer(after_tick, "after_tick", minimum=start_tick, maximum=end_tick)
        current = self.read(session_id, package, context=context)
        binding = current.binding
        if type(binding) is LegacyTimeBinding:
            raise StateMachineError("unsupported_time_protocol", "Legacy ticks have no calendar")
        offsets = [(0, "day_rolled")]
        if binding.cycle == "alternating":
            offsets += [(binding.day_start, "day_started"), (binding.night_start, "night_started")]
        cursor = start_tick if after_tick is None else after_tick
        nexts = [(self._next(binding, cursor, offset), kind) for offset, kind in offsets]
        result = []
        while len(result) <= limit:
            tick = min(t for t, _ in nexts)
            if tick > end_tick:
                break
            result.append(TimeBoundary(tick, tuple(kind for t, kind in nexts if t == tick)))
            nexts = [(t + binding.day_length if t == tick else t, kind) for t, kind in nexts]
        more = len(result) > limit
        return BoundaryPage(tuple(result[:limit]), result[limit - 1].tick if more else None)

    def advance(self, row, operation):
        """Internal command step in the caller's authorized, versioned savepoint."""
        self.store.require_write()
        integer(operation.expected_tick, "expected_tick", code="invalid_operation")
        integer(
            operation.next_tick,
            "next_tick",
            minimum=operation.expected_tick,
            code="invalid_operation",
        )
        sid = row["session_id"]
        binding, tick = self.facts.time_state(sid)
        if tick != operation.expected_tick:
            raise StateMachineError("game_time_conflict", "Game time precondition differs")
        if operation.next_tick == tick:
            return []
        self.facts.set_tick(sid, operation.next_tick)
        payload = {"previous_tick": tick, "game_tick": operation.next_tick}
        if type(binding) is TimeBinding:
            payload["time_protocol"] = binding.schema_version
        return [("game.time_advanced", payload)]
