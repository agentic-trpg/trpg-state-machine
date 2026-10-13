"""Closed Package /4 portable instance admission; no inferred item capabilities."""

import json
from dataclasses import dataclass

from .item_units import valid_item_unit
from .package import _fail, _identifier, _initial_cell, _list, _mapping, _reference, _unique


@dataclass(frozen=True, slots=True)
class PortableItem:
    instance_id: str
    item_slug: str
    initial_owner_id: str | None
    quantity: int
    charges_remaining_per_unit: int
    policy: str


def validate_items(raw, scenes, foundation, definitions):
    path = "item_transfers"
    if foundation is None:
        _fail("unsupported_mechanism", path, "Portable items require complete noncombat facts")
    snapshot = json.loads(foundation)["snapshot"]
    if snapshot["snapshot_schema_version"] != "engine-snapshot/17":
        _fail("unsupported_mechanism", path, "Portable items require current component facts")
    known = {a["entity_id"] for a in snapshot["character_states"]}
    objects = {o.object_id: (s, o) for s in scenes for o in s.objects}
    entries = {e["instance_id"]: e for e in snapshot["inventory_state"]["entries"]}
    result = []
    values = _list(raw, path)
    if len(values) > 128:
        _fail("unsupported_mechanism", path, "At most 128 portable instances")
    for value in values:
        item = _mapping(
            value,
            "instance_id item_slug initial_owner_id quantity charges_remaining_per_unit policy",
            path,
        )
        iid = _identifier(item["instance_id"], path, "object")
        _reference(iid, objects, path)
        scene, obj = objects[iid]
        if (
            not scene.public_interactions
            or scene.grid_json is None
            or obj.state is not None
            or obj.movable
            or obj.blocks_passage
        ):
            _fail("unsupported_mechanism", path, "Portable objects need ordinary public scope")
        if item["policy"] != "ordinary_public" or type(item["quantity"]) is not int:
            _fail("unsupported_mechanism", path, "Only reviewed ordinary portable units")
        if item["quantity"] != 1:
            _fail("unsupported_mechanism", path, "New portable instances have quantity one")
        owner = item["initial_owner_id"]
        if owner is not None:
            _reference(owner, known, path)
        else:
            _initial_cell(obj.initial_cell, scene.grid_json, path)
        unit = {
            "item_slug": item["item_slug"],
            "quantity": item["quantity"],
            "charges_remaining_per_unit": item["charges_remaining_per_unit"],
            "accessible": True,
        }
        if not valid_item_unit(unit):
            _fail("schema_error", path, "Invalid typed portable item balance")
        if (owner is None and iid in entries) or (
            owner is not None
            and entries.get(iid) != {"instance_id": iid, "owner_id": owner, **unit}
        ):
            _fail("invalid_reference", path, "Portable ownership must match initial inventory")
        if any(getattr(e.effect, "object_id", None) == iid for e in definitions):
            _fail("unsupported_mechanism", path, "Portable items cannot be automatic switches")
        result.append(PortableItem(**item))
    _unique([i.instance_id for i in result], path)
    return tuple(result)
