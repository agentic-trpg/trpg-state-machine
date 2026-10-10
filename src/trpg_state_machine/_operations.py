"""Closed world-command payloads, separate from mechanical StateDelta operations."""

from dataclasses import dataclass
from enum import StrEnum


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


type FoundationOperation = EnterScene | SetSceneFlag | SetObjectState | MoveActor
