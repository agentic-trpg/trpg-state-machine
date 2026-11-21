"""Derive an outdoor ambient source from scene configuration and WorldTime."""

from dataclasses import dataclass
from typing import Literal

from dnd5e_engine.spatial import parse_cell

from .errors import StateMachineError


@dataclass(frozen=True, slots=True)
class OutdoorLightScope:
    """Share a scene's outdoor cell membership across phases and old reads."""

    cells: frozenset[str] | None

    def __deepcopy__(self, memo):
        """Reuse this immutable configuration across instance-field reads."""
        return self


@dataclass(frozen=True, slots=True)
class SceneAmbientSource:
    """Fixed day-phase source with either whole-map or explicit outdoor coverage."""

    source_id: str
    width: int
    height: int
    cells: frozenset[str] | None
    phase: Literal["day", "night"]

    @property
    def level(self) -> Literal["bright", "dark"]:
        """Return the outdoor baseline under this phase."""
        return "bright" if self.phase == "day" else "dark"

    @property
    def sunlight(self) -> bool:
        """Identify daytime light as sunlight."""
        return self.phase == "day"

    def covers(self, cell: str) -> bool:
        """Check a canonical cell's outdoor membership before fixed map overrides."""
        if self.cells is not None:
            return cell in self.cells
        x, y = parse_cell(cell)
        return 0 <= x < self.width and 0 <= y < self.height

    def __deepcopy__(self, memo):
        """Share the fixed source and its immutable region across readers."""
        return self


def scene_ambient_source(world, sid, scene, grid) -> SceneAmbientSource | None:
    """Resolve outdoor lighting in the caller's consistent facts scope.

    Absent configuration leaves the map baseline in effect. Explicit per-cell
    grid lighting has priority over the outdoor baseline on covered cells.
    """
    scope = world.facts.instances.scene_daylight(sid, scene)
    if scope is None:
        return None
    phase = world.time.current_phase(sid)
    if phase is None:
        raise StateMachineError("incomplete_session", "Scene daylight requires a day/night cycle")
    return SceneAmbientSource(
        f"scene_daylight:{scene}", grid.data["width"], grid.data["height"], scope.cells, phase
    )
