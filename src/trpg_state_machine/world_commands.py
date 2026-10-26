"""Closed capability operations; identities never carry execution authority."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BarObject:
    """Request a bar state change through opening capability admission."""

    scene_id: str
    object_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class UnbarObject:
    """Request removal of a bar through opening capability admission."""

    scene_id: str
    object_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class LockObject:
    """Request an instance lock operation with credential/placement prerequisites."""

    scene_id: str
    object_id: str
    lock_id: str
    credential_instance_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class UnlockObject:
    """Request an instance unlock operation with credential/placement prerequisites."""

    scene_id: str
    object_id: str
    lock_id: str
    credential_instance_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class PutItem:
    """Request transfer of a portable instance into a container."""

    scene_id: str
    instance_id: str
    container_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class TakeItem:
    """Request removal of a portable instance from a container."""

    scene_id: str
    instance_id: str
    container_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class RepositionObject:
    """Request privileged object placement or facing adjustment."""

    scene_id: str
    object_id: str
    expected_cell: str
    to_cell: str
    expected_orientation: str | None = None
    to_orientation: str | None = None


@dataclass(frozen=True, slots=True)
class SetLightEnabled:
    """Request changing a light instance's enabled state."""

    scene_id: str
    object_id: str
    expected_cell: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class SwitchLightMode:
    """Request selection of a configured light mode."""

    scene_id: str
    object_id: str
    expected_cell: str
    mode: str


@dataclass(frozen=True, slots=True)
class ActivateMechanism:
    """Request a configured manual mechanism operation."""

    scene_id: str
    object_id: str
    expected_cell: str
    operation_id: str = "activate"


@dataclass(frozen=True, slots=True)
class ServiceMechanism:
    """Request a supported mechanism service method."""

    scene_id: str
    object_id: str
    expected_cell: str
    method_id: str
    tool_instance_id: str | None = None


@dataclass(frozen=True, slots=True)
class CheckMechanism(ServiceMechanism):
    """A configured RE check, evaluated outside the write transaction."""


@dataclass(frozen=True, slots=True)
class PickLock:
    """Request a rules-backed lock-picking check."""

    scene_id: str
    object_id: str
    lock_id: str
    tool_instance_id: str
    expected_cell: str


@dataclass(frozen=True, slots=True)
class ForceOpen:
    """Request a rules-backed forced opening check."""

    scene_id: str
    object_id: str
    method_id: str
    tool_instance_id: str | None
    expected_cell: str


@dataclass(frozen=True, slots=True)
class ClearJam:
    """Request a rules-backed jam-clearing check."""

    scene_id: str
    object_id: str
    method_id: str
    tool_instance_id: str | None
    expected_cell: str


OPENABLE_CHECK_OPERATIONS = (PickLock, ForceOpen, ClearJam)


WORLD_CAPABILITY_COMMANDS = {
    "world.pick_lock": PickLock,
    "world.force_open": ForceOpen,
    "world.clear_jam": ClearJam,
    "world.activate_mechanism": ActivateMechanism,
    "world.service_mechanism": ServiceMechanism,
    "world.check_mechanism": CheckMechanism,
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
    *OPENABLE_CHECK_OPERATIONS,
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
    ServiceMechanism,
    CheckMechanism,
)
