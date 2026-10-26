"""Versioned, immutable Session time interpretation (never a second clock)."""

from dataclasses import asdict, dataclass

from .errors import StateMachineError

MAX_TICK = 2147483647  # Preserve the existing command/storage range.


def integer(value, path, *, maximum=MAX_TICK, minimum=0, code="invalid_time_query"):
    """Validate a nonnegative integer within the time value range."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise StateMachineError(code, "Expected a bounded integer", path=path)
    return value


@dataclass(frozen=True, slots=True)
class LegacyTimeBinding:
    """Identify the legacy tick clock without assigning unsupported second-based semantics."""

    schema_version: str = "game-clock/1"


@dataclass(frozen=True, slots=True)
class TimeBinding:
    """world-time/1: one tick is one world second; tick zero is the origin."""

    schema_version: str = "world-time/1"
    day_length: int = 86400
    start_day: int = 1
    start_day_tick: int = 0
    cycle: str = "alternating"
    day_start: int | None = 21600
    night_start: int | None = 64800

    def __post_init__(self):
        """Validate time mode, day length and conversion-field combinations."""
        if type(self.schema_version) is not str or self.schema_version != "world-time/1":
            self._fail("schema_version", "Unsupported time protocol")
        integer(self.day_length, "day_length", minimum=1, code="invalid_time_binding")
        for field, minimum, maximum in (
            ("start_day", 1, MAX_TICK),
            ("start_day_tick", 0, self.day_length - 1),
        ):
            integer(
                getattr(self, field),
                field,
                minimum=minimum,
                maximum=maximum,
                code="invalid_time_binding",
            )
        if type(self.cycle) is not str or self.cycle not in ("alternating", "none"):
            self._fail("cycle", "Expected alternating or none")
        if self.cycle == "none":
            if self.day_start is not None or self.night_start is not None:
                self._fail("cycle", "No cycle must have no phase boundaries")
        else:
            for field in ("day_start", "night_start"):
                integer(
                    getattr(self, field),
                    field,
                    maximum=self.day_length - 1,
                    code="invalid_time_binding",
                )
            if self.day_start == self.night_start:
                self._fail("night_start", "Alternating phase boundaries must differ")

    @staticmethod
    def _fail(path, message):
        """Raise a time binding structure error."""
        raise StateMachineError("invalid_time_binding", message, path=path)


def time_binding_from_data(data):
    """Restore and validate a time binding from its closed field representation."""
    if type(data) is not dict or set(data) != set(asdict(TimeBinding())):
        raise StateMachineError(
            "invalid_time_binding", "Expected complete world-time/1 fields", path="time_binding"
        )
    return TimeBinding(**data)
