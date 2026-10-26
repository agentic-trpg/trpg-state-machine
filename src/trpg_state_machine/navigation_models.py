"""Closed local navigation-activity/1 data; no executable text or trust claims."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StartNavigation:
    """Request a new navigation activity toward a scene/cell target."""

    activity_id: str
    target_scene_id: str
    target_cell: str
    schema_version: str = "navigation-activity/1"


@dataclass(frozen=True, slots=True)
class AdvanceNavigation:
    """Request bounded progress of an existing navigation activity."""

    activity_id: str
    expected_activity_version: int
    schema_version: str = "navigation-activity/1"


@dataclass(frozen=True, slots=True)
class ResumeNavigation:
    """Request resumption of blocked navigation with version checking."""

    activity_id: str
    expected_activity_version: int
    schema_version: str = "navigation-activity/1"


@dataclass(frozen=True, slots=True)
class CancelNavigation:
    """Request cancellation of a navigation activity."""

    activity_id: str
    expected_activity_version: int
    schema_version: str = "navigation-activity/1"


NAVIGATION_OPERATIONS = (StartNavigation, AdvanceNavigation, ResumeNavigation, CancelNavigation)


@dataclass(frozen=True, slots=True)
class NavigationResult:
    """Describe navigation progress or its blocking/terminal reason."""

    code: str
    operation: str | None = None


@dataclass(frozen=True, slots=True)
class NavigationActivity:
    """Expose detached plan, cursor, result and command/version associations."""

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
    """Group static scenes and passages into a grantable navigation region."""

    region_id: str
    scene_ids: tuple[str, ...]
    passage_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NavigationKnowledge:
    """Define the static layout knowledge available for navigation grants."""

    regions: tuple[NavigationRegion, ...]
    actor_grants: tuple[tuple[str, tuple[str, ...]], ...]
    pc_template_grants: tuple[tuple[str, tuple[str, ...]], ...]
