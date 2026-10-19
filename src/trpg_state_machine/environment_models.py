"""Bounded immutable Scene environment contracts; no rule or lifecycle authority."""

from dataclasses import dataclass
from math import isfinite
from typing import Literal, Protocol

from dnd5e_engine.spatial import canonical_cell_id

from .errors import StateMachineError
from .object_protection import ORIENTATIONS, Direction, validate_precipitation_kinds

MAX_CELLS = 256
MAX_SOURCES = 64
MAX_BATCH = 16


def invalid(message):
    raise StateMachineError("invalid_environment", message)


def identity(value):
    if type(value) is not str or not value.strip() or len(value) > 128:
        invalid("Expected bounded environment identity")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        invalid("Invalid Unicode identity")


def finite(value):
    if type(value) not in (int, float) or not isfinite(value):
        invalid("Expected finite numeric value, never bool")


@dataclass(frozen=True, slots=True)
class HeightInterval:
    lower_ft: float
    upper_ft: float

    def __post_init__(self):
        finite(self.lower_ft)
        finite(self.upper_ft)
        if self.lower_ft >= self.upper_ft:
            invalid("Height interval must have lower < upper; scene datum, [lower,upper)")


@dataclass(frozen=True, slots=True)
class EnvironmentArea:
    """Canonical shared Grid cells; empty means whole Scene, not an empty region."""

    cells: tuple[str, ...] = ()
    heights: HeightInterval | None = None

    def __post_init__(self):
        if type(self.cells) is not tuple or len(self.cells) > MAX_CELLS:
            invalid("Area requires at most 256 immutable cells")
        for cell in self.cells:
            if type(cell) is not str or len(cell) > 32:
                invalid("Invalid cell")
            try:
                if canonical_cell_id(cell) != cell:
                    invalid("Noncanonical cell")
            except ValueError:
                invalid("Invalid cell")
        if len(set(self.cells)) != len(self.cells):
            invalid("Duplicate area cells")
        object.__setattr__(self, "cells", tuple(sorted(self.cells)))
        if self.heights is not None and type(self.heights) is not HeightInterval:
            invalid("Expected typed height interval")


@dataclass(frozen=True, slots=True)
class Temperature:
    celsius: float | None = None  # None explicitly unknown, not safe.

    def __post_init__(self):
        if self.celsius is not None:
            finite(self.celsius)
            if self.celsius < -273.15:
                invalid("Temperature below absolute zero")


@dataclass(frozen=True, slots=True)
class Wind:
    speed_mph: float | None = None
    wind_from: Direction | None = None

    def __post_init__(self):
        if self.wind_from is not None and self.wind_from not in ORIENTATIONS:
            invalid("Wind requires cardinal incoming direction")
        if self.speed_mph is not None:
            finite(self.speed_mph)
            if self.speed_mph < 0:
                invalid("Wind speed cannot be negative")
        if self.speed_mph == 0 and self.wind_from is not None:
            invalid("Explicit calm has no direction")


@dataclass(frozen=True, slots=True)
class Precipitation:
    kind: Literal["unknown", "none", "rain", "snow", "sleet", "hail"] = "unknown"
    intensity: Literal["unknown", "none", "light", "moderate", "heavy"] = "unknown"

    def __post_init__(self):
        if self.kind not in ("unknown", "none", "rain", "snow", "sleet", "hail"):
            invalid("Unknown precipitation kind")
        if self.intensity not in ("unknown", "none", "light", "moderate", "heavy"):
            invalid("Unknown precipitation intensity")
        if (self.kind == "none") != (self.intensity == "none"):
            invalid("Explicit no precipitation requires none/none")


@dataclass(frozen=True, slots=True)
class Obscurant:
    kind: Literal["unknown", "none", "fog", "smoke"] = "unknown"
    density: Literal["unknown", "none", "thin", "dense"] = "unknown"

    def __post_init__(self):
        if self.kind not in ("unknown", "none", "fog", "smoke"):
            invalid("Unknown fog/smoke kind")
        if self.density not in ("unknown", "none", "thin", "dense"):
            invalid("Unknown fog/smoke density")
        if (self.kind == "none") != (self.density == "none"):
            invalid("Explicit no obscurant requires none/none")


@dataclass(frozen=True, slots=True)
class EnvironmentConditions:
    """None field contributes nothing; typed unknown explicitly overrides lower priority."""

    temperature: Temperature | None = None
    wind: Wind | None = None
    precipitation: Precipitation | None = None
    obscurant: Obscurant | None = None

    def __post_init__(self):
        for field, cls in CONDITION_TYPES.items():
            if getattr(self, field) is not None and type(getattr(self, field)) is not cls:
                invalid("Expected typed environment condition")


CONDITION_TYPES = {
    "temperature": Temperature,
    "wind": Wind,
    "precipitation": Precipitation,
    "obscurant": Obscurant,
}


@dataclass(frozen=True, slots=True)
class EnvironmentSource:
    source_id: str
    conditions: EnvironmentConditions
    area: EnvironmentArea = EnvironmentArea()
    priority: int = 0
    exposure: Literal["outdoor", "local"] = "local"
    observable: bool = False

    def __post_init__(self):
        identity(self.source_id)
        if self.source_id == "base":
            invalid("base is reserved")
        if (
            type(self.conditions) is not EnvironmentConditions
            or type(self.area) is not EnvironmentArea
        ):
            invalid("Expected typed source conditions and area")
        if type(self.priority) is not int or not -1000 <= self.priority <= 1000:
            invalid("Source priority must be an integer in [-1000,1000]")
        if self.exposure not in ("outdoor", "local") or type(self.observable) is not bool:
            invalid("Invalid source exposure/disclosure policy")


@dataclass(frozen=True, slots=True)
class StaticWindProtection:
    cell: str
    wind_from: Direction
    heights: HeightInterval

    def __post_init__(self):
        EnvironmentArea((self.cell,))
        if self.wind_from not in ORIENTATIONS or type(self.heights) is not HeightInterval:
            invalid("Expected directional static wind protection with explicit heights")


@dataclass(frozen=True, slots=True)
class StaticPrecipitationProtection:
    cells: tuple[str, ...]
    kinds: tuple[str, ...]

    def __post_init__(self):
        area = EnvironmentArea(self.cells)
        object.__setattr__(self, "cells", area.cells)
        try:
            validate_precipitation_kinds(self.kinds)
        except ValueError as exc:
            invalid(str(exc))
        object.__setattr__(self, "kinds", tuple(sorted(self.kinds)))


@dataclass(frozen=True, slots=True)
class SceneEnvironmentProtection:
    """None is unconfigured; empty tuple is explicitly no static contribution."""

    rain_cells: tuple[str, ...] | None = None
    wind: tuple[StaticWindProtection, ...] | None = None
    precipitation: tuple[StaticPrecipitationProtection, ...] | None = None

    def __post_init__(self):
        if self.precipitation is not None:
            if (
                type(self.precipitation) is not tuple
                or len(self.precipitation) > MAX_CELLS
                or any(type(p) is not StaticPrecipitationProtection for p in self.precipitation)
            ):
                invalid("Expected at most 256 typed static precipitation contributions")
            object.__setattr__(
                self,
                "precipitation",
                tuple(sorted(set(self.precipitation), key=lambda p: (p.cells, p.kinds))),
            )
        if self.rain_cells is not None:
            EnvironmentArea(self.rain_cells)
            object.__setattr__(self, "rain_cells", tuple(sorted(self.rain_cells)))
        if self.wind is not None and (
            type(self.wind) is not tuple
            or len(self.wind) > MAX_CELLS
            or any(type(w) is not StaticWindProtection for w in self.wind)
        ):
            invalid("Expected at most 256 typed static wind contributions")
        if self.wind is not None:
            object.__setattr__(
                self,
                "wind",
                tuple(
                    sorted(
                        set(self.wind),
                        key=lambda w: (w.cell, w.wind_from, w.heights.lower_ft, w.heights.upper_ft),
                    )
                ),
            )


@dataclass(frozen=True, slots=True)
class EnvironmentSceneFacts:
    base: EnvironmentConditions | None = None
    sources: tuple[EnvironmentSource, ...] = ()
    protection: SceneEnvironmentProtection = SceneEnvironmentProtection()

    def __post_init__(self):
        if self.base is not None and type(self.base) is not EnvironmentConditions:
            invalid("Expected typed base conditions")
        validate_sources(self.sources)
        object.__setattr__(self, "sources", tuple(sorted(self.sources, key=lambda s: s.source_id)))
        if type(self.protection) is not SceneEnvironmentProtection:
            invalid("Expected typed static protection")


def validate_sources(sources):
    if (
        type(sources) is not tuple
        or len(sources) > MAX_SOURCES
        or any(type(s) is not EnvironmentSource for s in sources)
    ):
        invalid("Expected at most 64 immutable typed sources")
    if len({s.source_id for s in sources}) != len(sources):
        invalid("Duplicate environment source identity")


class EnvironmentEffectsProvider(Protocol):
    """Current effective contributions only. Read-only, no I/O, inside read transaction.

    Effects owns scope, movement, concentration and lifecycle. None means provider
    unavailable; () explicitly reports no active contributions. Not a spell adapter.
    """

    def environment_sources(
        self, sid, scene_id, package
    ) -> tuple[EnvironmentSource, ...] | None: ...


class UnitHeightProvider(Protocol):
    """Character/position owner supplies actual scene-datum body interval, never size labels."""

    def environment_heights(self, sid, actor_id, package, *, position) -> HeightInterval | None: ...
