"""Closed INTERNAL operations for foundation integration tests.

These are not public game commands or Rule Engine deltas. No arbitrary paths,
JSON Patch, SQL, HP changes, rule calculation, or event/Meta-only commits.
"""

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
