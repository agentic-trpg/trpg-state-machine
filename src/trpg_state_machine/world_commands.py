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


@dataclass(frozen=True, slots=True)
class RepositionObject:
    scene_id: str
    object_id: str
    expected_cell: str
    to_cell: str


@dataclass(frozen=True, slots=True)
class SetLightEnabled:
    scene_id: str
    object_id: str
    expected_cell: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class SwitchLightMode:
    scene_id: str
    object_id: str
    expected_cell: str
    mode: str


@dataclass(frozen=True, slots=True)
class ActivateMechanism:
    scene_id: str
    object_id: str
    expected_cell: str


WORLD_CAPABILITY_COMMANDS = {
    "world.activate_mechanism": ActivateMechanism,
    "world.set_light_enabled": SetLightEnabled,
    "world.switch_light_mode": SwitchLightMode,
    "world.reposition_object": RepositionObject,
    "world.put_item": PutItem,
    "world.take_item": TakeItem,
    "world.lock_object": LockObject,
    "world.unlock_object": UnlockObject,
    "world.bar_object": BarObject,
    "world.unbar_object": UnbarObject,
}
WORLD_CAPABILITY_OPERATIONS = (
    BarObject,
    UnbarObject,
    LockObject,
    UnlockObject,
    PutItem,
    TakeItem,
    RepositionObject,
    SetLightEnabled,
    SwitchLightMode,
    ActivateMechanism,
)
