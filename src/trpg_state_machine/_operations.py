"""Closed world-command payloads, separate from mechanical StateDelta operations."""

from dataclasses import dataclass
from enum import StrEnum

from .world_commands import (
    ActivateMechanism,
    BarObject,
    CheckMechanism,
    ClearJam,
    ForceOpen,
    LockObject,
    PickLock,
    PutItem,
    RepositionObject,
    ServiceMechanism,
    SetLightEnabled,
    SwitchLightMode,
    TakeItem,
    UnbarObject,
    UnlockObject,
)


class ObjectState(StrEnum):
    """Supported open/closed state values for foundation object commands."""

    CLOSED = "closed"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class EnterScene:
    """Request entry into a selected scene through lifecycle coordination."""

    scene_id: str


@dataclass(frozen=True, slots=True)
class SetSceneFlag:
    """Request a boolean flag change on one scene."""

    scene_id: str
    flag_id: str
    value: bool


@dataclass(frozen=True, slots=True)
class SetObjectState:
    """Request an object's open/closed transition in a scene."""

    scene_id: str
    object_id: str
    state: ObjectState


@dataclass(frozen=True, slots=True)
class MoveActor:
    """Request an actor scene transition through controlled world operations."""

    actor_id: str
    to_scene_id: str


@dataclass(frozen=True, slots=True)
class PlaceObject:
    """One adjacent object adjustment, including its derived passage blocking."""

    object_id: str
    scene_id: str
    expected_cell: str
    to_cell: str
    schema_version: str = "object-placement/1"


@dataclass(frozen=True, slots=True)
class StepActor:
    """Request one spatial step guarded by the expected starting cell."""

    scene_id: str
    expected_cell: str
    to_cell: str
    schema_version: str = "actor-step/1"


@dataclass(frozen=True, slots=True)
class TraversePassage:
    """Request passage traversal from a specified scene and expected cell."""

    passage_id: str
    from_scene_id: str
    expected_cell: str
    schema_version: str = "passage-traversal/1"


@dataclass(frozen=True, slots=True)
class PickupItem:
    """Request pickup of one portable instance at its expected ground location."""

    instance_id: str
    scene_id: str
    expected_cell: str
    schema_version: str = "item-transfer/1"


@dataclass(frozen=True, slots=True)
class DropItem:
    """Request placement of a carried instance at a ground cell."""

    instance_id: str
    scene_id: str
    to_cell: str
    schema_version: str = "item-transfer/1"


@dataclass(frozen=True, slots=True)
class UnlockDoor:
    """Request door unlocking using a credential instance and expected actor cell."""

    scene_id: str
    object_id: str
    credential_instance_id: str
    expected_cell: str
    schema_version: str = "door-lock/1"


@dataclass(frozen=True, slots=True)
class LockDoor:
    """Request door locking using a credential instance and expected actor cell."""

    scene_id: str
    object_id: str
    credential_instance_id: str
    expected_cell: str
    schema_version: str = "door-lock/1"


type FoundationOperation = (
    PickLock
    | ForceOpen
    | ClearJam
    | ActivateMechanism
    | ServiceMechanism
    | CheckMechanism
    | BarObject
    | UnbarObject
    | LockObject
    | UnlockObject
    | PutItem
    | TakeItem
    | RepositionObject
    | SetLightEnabled
    | SwitchLightMode
    | EnterScene
    | SetSceneFlag
    | SetObjectState
    | MoveActor
    | PlaceObject
    | StepActor
    | TraversePassage
    | PickupItem
    | DropItem
    | UnlockDoor
    | LockDoor
)
