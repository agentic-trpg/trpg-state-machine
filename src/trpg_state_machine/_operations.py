"""Closed world-command payloads, separate from mechanical StateDelta operations."""

from dataclasses import dataclass
from enum import StrEnum

from .world_commands import (
    ActivateMechanism,
    BarObject,
    LockObject,
    PutItem,
    RepositionObject,
    SetLightEnabled,
    SwitchLightMode,
    TakeItem,
    UnbarObject,
    UnlockObject,
)


class ObjectState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


@dataclass(frozen=True, slots=True)
class EnterScene:
    scene_id: str


@dataclass(frozen=True, slots=True)
class SetSceneFlag:
    scene_id: str
    flag_id: str
    value: bool


@dataclass(frozen=True, slots=True)
class SetObjectState:
    scene_id: str
    object_id: str
    state: ObjectState


@dataclass(frozen=True, slots=True)
class MoveActor:
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
    scene_id: str
    expected_cell: str
    to_cell: str
    schema_version: str = "actor-step/1"


@dataclass(frozen=True, slots=True)
class TraversePassage:
    passage_id: str
    from_scene_id: str
    expected_cell: str
    schema_version: str = "passage-traversal/1"


@dataclass(frozen=True, slots=True)
class PickupItem:
    instance_id: str
    scene_id: str
    expected_cell: str
    schema_version: str = "item-transfer/1"


@dataclass(frozen=True, slots=True)
class DropItem:
    instance_id: str
    scene_id: str
    to_cell: str
    schema_version: str = "item-transfer/1"


@dataclass(frozen=True, slots=True)
class UnlockDoor:
    scene_id: str
    object_id: str
    credential_instance_id: str
    expected_cell: str
    schema_version: str = "door-lock/1"


@dataclass(frozen=True, slots=True)
class LockDoor:
    scene_id: str
    object_id: str
    credential_instance_id: str
    expected_cell: str
    schema_version: str = "door-lock/1"


type FoundationOperation = (
    ActivateMechanism
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
