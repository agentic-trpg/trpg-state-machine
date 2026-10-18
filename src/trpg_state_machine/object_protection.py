"""Immutable current WO inputs and derived ground-cell environmental protection.

This contract is independent of Package loading and of weather/exposure rules.
Lengths/heights use feet, with heights relative to an explicit scene datum.
"""

from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING, Literal, Protocol

from dnd5e_engine.spatial import canonical_cell_id

if TYPE_CHECKING:
    from .package import AdventurePackage
    from .world_spatial import ObjectSpatialState

Direction = Literal["north", "east", "south", "west"]
ProtectionStatus = Literal["ready", "none", "unconfigured", "missing_input", "unsupported"]
ORIENTATIONS = ("north", "east", "south", "west")
DOWNWIND = {"north": (0, 1), "east": (-1, 0), "south": (0, -1), "west": (1, 0)}
MAX_LOCAL_CELLS = 64
MAX_LOCAL_OFFSET = 32
MAX_DOWNWIND_CELLS = 256
MAX_WIND_WORK = 4096


def _height(value):
    if value is not None and (type(value) not in (int, float) or not isfinite(value)):
        raise ValueError("Protection heights must be finite feet or explicitly missing")


def _offset(cell):
    if (
        type(cell) is not tuple
        or len(cell) != 2
        or any(type(v) is not int or abs(v) > MAX_LOCAL_OFFSET for v in cell)
    ):
        raise ValueError("Protection geometry requires bounded integer local cells")


def transform_local_cells(anchor, orientation, offsets):
    """Shared clockwise full-cell transform about the authoritative anchor."""
    if orientation not in ORIENTATIONS:
        raise ValueError("Unknown object orientation")
    if type(anchor) is not str or canonical_cell_id(anchor) != anchor:
        raise ValueError("Noncanonical object anchor")
    x, y = map(int, anchor.split(","))
    result = []
    for dx, dy in offsets:
        for _ in range(ORIENTATIONS.index(orientation)):
            dx, dy = -dy, dx
        result.append(f"{x + dx},{y + dy}")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class RainProtection:
    """Explicit vertical rain coverage; empty means explicitly no contribution."""

    local_cells: tuple[tuple[int, int], ...]

    def __post_init__(self):
        if type(self.local_cells) is not tuple or len(self.local_cells) > MAX_LOCAL_CELLS:
            raise ValueError("Rain protection needs at most 64 immutable local cells")
        for cell in self.local_cells:
            _offset(cell)


@dataclass(frozen=True, slots=True)
class WindBoundary:
    """Leeward face of local_cell, for one local incoming wind direction.

    local_cell is the LAST full solid cell of the object's thickness in that
    lane. Contribution starts one cell beyond its downstream edge, ends at N,
    and excludes local_cell. WO must supply this face explicitly.
    """

    local_cell: tuple[int, int]
    wind_from: Direction
    downwind_cells: int
    lower_height_ft: float | None
    upper_height_ft: float | None

    def __post_init__(self):
        _offset(self.local_cell)
        if self.wind_from not in ORIENTATIONS:
            raise ValueError("Wind boundary requires a cardinal incoming direction")
        if (
            type(self.downwind_cells) is not int
            or not 1 <= self.downwind_cells <= MAX_DOWNWIND_CELLS
        ):
            raise ValueError("Wind reach N must be an integer in [1, 256]")
        _height(self.lower_height_ft)
        _height(self.upper_height_ft)
        if (
            self.lower_height_ft is not None
            and self.upper_height_ft is not None
            and self.lower_height_ft >= self.upper_height_ft
        ):
            raise ValueError("Wind height interval must have lower < upper")


@dataclass(frozen=True, slots=True)
class WindProtection:
    """Empty boundaries explicitly declare no wind protection."""

    boundaries: tuple[WindBoundary, ...]

    def __post_init__(self):
        if (
            type(self.boundaries) is not tuple
            or len(self.boundaries) > MAX_LOCAL_CELLS
            or any(type(b) is not WindBoundary for b in self.boundaries)
        ):
            raise ValueError("Wind protection needs at most 64 immutable boundaries")
        if sum(b.downwind_cells for b in self.boundaries) > MAX_WIND_WORK:
            raise ValueError("Wind projection work exceeds 4096 cells per object")


@dataclass(frozen=True, slots=True)
class CurrentProtectionProperties:
    """WO-owned current properties; None means unconfigured, never false/zero.

    WO-01 chooses these values after opening/deployment/damage. OSS does not
    derive them from state, HP, movement, vision or combat cover properties.
    """

    rain: RainProtection | None = None
    wind: WindProtection | None = None
    base_height_ft: float | None = None

    def __post_init__(self):
        if self.rain is not None and type(self.rain) is not RainProtection:
            raise ValueError("Expected typed rain properties")
        if self.wind is not None and type(self.wind) is not WindProtection:
            raise ValueError("Expected typed wind properties")
        _height(self.base_height_ft)


class CurrentProtectionProvider(Protocol):
    """Read-only, uncached WO seam; called inside the consumer's read snapshot.

    A candidate-aware provider must return properties for that exact proposed
    pose or return None. No writes, events, I/O or guessed state transitions.
    """

    def current_protection(
        self,
        sid: str,
        oid: str,
        package: "AdventurePackage",
        *,
        candidate: "ObjectSpatialState",
    ) -> CurrentProtectionProperties | None: ...


@dataclass(frozen=True, slots=True)
class RainContribution:
    object_id: str
    cell: str


@dataclass(frozen=True, slots=True)
class WindContribution:
    object_id: str
    cell: str
    lower_height_ft: float
    upper_height_ft: float


@dataclass(frozen=True, slots=True)
class ObjectProtectionProjection:
    object_id: str
    scene_id: str | None
    wind_from: Direction
    rain_status: ProtectionStatus = "unconfigured"
    wind_status: ProtectionStatus = "unconfigured"
    rain_cells: tuple[RainContribution, ...] = ()
    wind_cells: tuple[WindContribution, ...] = ()
    rain_reason: str | None = None
    wind_reason: str | None = None

    def wind_at(self, cell: str, height_ft: float) -> tuple[WindContribution, ...]:
        """Point geometry only: [lower, upper), not a unit exposure decision."""
        _height(height_ft)
        if height_ft is None:
            raise ValueError("Scene height is required")
        return tuple(
            c
            for c in self.wind_cells
            if c.cell == cell and c.lower_height_ft <= height_ft < c.upper_height_ft
        )


@dataclass(frozen=True, slots=True)
class SceneProtectionProjection:
    scene_id: str
    wind_from: Direction
    objects: tuple[ObjectProtectionProjection, ...]

    def wind_at(self, cell: str, height_ft: float) -> tuple[WindContribution, ...]:
        _height(height_ft)
        if height_ft is None:
            raise ValueError("Scene height is required")
        return tuple(c for p in self.objects for c in p.wind_at(cell, height_ft))


def project_protection(oid, state, properties, wind_from, width, height):
    """Bounded pure computation; clip only to scene bounds, not movement cells."""
    rain, wind = (), ()
    rain_status = wind_status = "unconfigured"
    wind_reason = None

    def in_bounds(cell):
        x, y = map(int, cell.split(","))
        return 0 <= x < width and 0 <= y < height

    if properties.rain is not None:
        rain = tuple(
            RainContribution(oid, c)
            for c in sorted(
                set(
                    transform_local_cells(
                        state.anchor, state.orientation, properties.rain.local_cells
                    )
                )
            )
            if in_bounds(c)
        )
        rain_status = "ready" if rain else "none"
    if properties.wind is not None:
        turns = ORIENTATIONS.index(state.orientation)
        boundaries = [
            b
            for b in properties.wind.boundaries
            if ORIENTATIONS[(ORIENTATIONS.index(b.wind_from) + turns) % 4] == wind_from
        ]
        wind_status = "none"
        if boundaries and (
            properties.base_height_ft is None
            or any(b.lower_height_ft is None or b.upper_height_ft is None for b in boundaries)
        ):
            wind_status, wind_reason = "missing_input", "wind_height_required"
        elif boundaries:
            contributions = set()
            dx, dy = DOWNWIND[wind_from]
            for b in boundaries:
                lower = properties.base_height_ft + b.lower_height_ft
                upper = properties.base_height_ft + b.upper_height_ft
                if not isfinite(lower) or not isfinite(upper) or lower >= upper:
                    raise ValueError("Scene height interval is not representable")
                (cell,) = transform_local_cells(state.anchor, state.orientation, (b.local_cell,))
                x, y = map(int, cell.split(","))
                # An out-of-scene boundary cannot shelter this scene from outside.
                if not in_bounds(cell):
                    continue
                for distance in range(1, b.downwind_cells + 1):
                    target = f"{x + dx * distance},{y + dy * distance}"
                    if not in_bounds(target):
                        break
                    contributions.add((target, lower, upper))
            wind = tuple(WindContribution(oid, *c) for c in sorted(contributions))
            wind_status = "ready" if wind else "none"
    return ObjectProtectionProjection(
        oid,
        state.scene_id,
        wind_from,
        rain_status,
        wind_status,
        rain,
        wind,
        wind_reason=wind_reason,
    )
