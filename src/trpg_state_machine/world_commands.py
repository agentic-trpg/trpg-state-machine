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


WORLD_CAPABILITY_COMMANDS = {
    "world.bar_object": BarObject,
    "world.unbar_object": UnbarObject,
}
WORLD_CAPABILITY_OPERATIONS = (BarObject, UnbarObject)
