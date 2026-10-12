"""Closed local navigation-activity/1 data; no executable text or trust claims."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StartNavigation:
    activity_id: str
    target_scene_id: str
    target_cell: str
    schema_version: str = "navigation-activity/1"


@dataclass(frozen=True, slots=True)
class AdvanceNavigation:
    activity_id: str
    expected_activity_version: int
    schema_version: str = "navigation-activity/1"


@dataclass(frozen=True, slots=True)
class ResumeNavigation:
    activity_id: str
    expected_activity_version: int
    schema_version: str = "navigation-activity/1"


@dataclass(frozen=True, slots=True)
class CancelNavigation:
    activity_id: str
    expected_activity_version: int
    schema_version: str = "navigation-activity/1"


NAVIGATION_OPERATIONS = (StartNavigation, AdvanceNavigation, ResumeNavigation, CancelNavigation)


@dataclass(frozen=True, slots=True)
class NavigationResult:
    code: str
    operation: str | None = None


@dataclass(frozen=True, slots=True)
class NavigationActivity:
    session_id: str
    activity_id: str
    actor_id: str
    target_scene_id: str
    target_cell: str
    status: str
    activity_version: int
    cursor: int
    plan_length: int
    plan: tuple[dict, ...]
    last_result: NavigationResult
    source_command_id: str
    last_command_id: str
    world_version: int


@dataclass(frozen=True, slots=True)
class NavigationRegion:
    region_id: str
    scene_ids: tuple[str, ...]
    passage_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NavigationKnowledge:
    regions: tuple[NavigationRegion, ...]
    actor_grants: tuple[tuple[str, tuple[str, ...]], ...]
    pc_template_grants: tuple[tuple[str, tuple[str, ...]], ...]
