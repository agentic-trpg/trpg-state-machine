"""Closed Package /5 public mechanisms; credentials are approved instance IDs."""

from dataclasses import dataclass

from .package import _fail, _list, _mapping, _reference, _unique


@dataclass(frozen=True, slots=True)
class LockableDoor:
    object_id: str
    initial_locked: bool
    credential_instance_ids: tuple[str, ...]
    policy: str


def validate_locks(raw, scenes, passages, portable):
    path = "lockable_doors"
    values = _list(raw, path)
    if len(values) > 64:
        _fail("unsupported_mechanism", path, "At most 64 public door mechanisms")
    objects = {o.object_id: o for s in scenes for o in s.objects}
    doors = {p.door_object_id for p in passages if p.door_object_id}
    items = {i.instance_id for i in portable}
    result = []
    for value in values:
        data = _mapping(value, "object_id initial_locked credential_instance_ids policy", path)
        oid = data["object_id"]
        _reference(oid, doors, path)
        if type(data["initial_locked"]) is not bool:
            _fail("schema_error", path, "Lock state must be boolean")
        if data["policy"] != "public_owned_credential":
            _fail("unsupported_mechanism", path, "Only public owned credentials")
        obj = objects[oid]
        if obj.movable or obj.state not in ("open", "closed") or oid in items:
            _fail("unsupported_mechanism", path, "Lock requires a fixed shared door")
        if data["initial_locked"] and obj.state != "closed":
            _fail("invalid_reference", path, "An open door cannot be locked")
        for p in passages:
            if p.door_object_id == oid and any(
                not next(s for s in scenes if s.scene_id == e.scene_id).public_interactions
                for e in p.endpoints
            ):
                _fail("unsupported_mechanism", path, "Both ports need public interactions")
        credentials = _list(data["credential_instance_ids"], path, nonempty=True)
        if len(credentials) > 128:
            _fail("unsupported_mechanism", path, "At most 128 credential instances")
        for iid in credentials:
            _reference(iid, items, path)
        _unique(credentials, path)
        result.append(LockableDoor(oid, data["initial_locked"], tuple(credentials), data["policy"]))
    _unique([d.object_id for d in result], path)
    return tuple(result)
