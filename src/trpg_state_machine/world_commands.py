"""Closed capability operations; identities never carry execution authority."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BarObject:
    scene_id: str
    object_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class UnbarObject:
    scene_id: str
    object_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class LockObject:
    scene_id: str
    object_id: str
    lock_id: str
    credential_instance_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class UnlockObject:
    scene_id: str
    object_id: str
    lock_id: str
    credential_instance_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class PutItem:
    scene_id: str
    instance_id: str
    container_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class TakeItem:
    scene_id: str
    instance_id: str
    container_id: str
    expected_cell: str


WORLD_CAPABILITY_COMMANDS = {
    "world.put_item": PutItem,
    "world.take_item": TakeItem,
    "world.lock_object": LockObject,
    "world.unlock_object": UnlockObject,
    "world.bar_object": BarObject,
    "world.unbar_object": UnbarObject,
}
WORLD_CAPABILITY_OPERATIONS = (BarObject, UnbarObject, LockObject, UnlockObject, PutItem, TakeItem)
