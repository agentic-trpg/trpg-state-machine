"""Explicit immutable object definitions, pinned by the Session package digest."""

from dataclasses import dataclass

from .package import _fail, _identifier, _list, _mapping, _reference, _text, _unique


@dataclass(frozen=True, slots=True)
class Openable:
    initial_jammed: bool = False
    initial_barred: bool | None = None
    jam_policy: str = "prevent_transitions"
    operation_sides: tuple[str, ...] = ()
    bar_operable_sides: tuple[str, ...] = ()
    kind: str = "openable"


@dataclass(frozen=True, slots=True)
class Durability:
    armor_class: int
    max_hp: int
    initial_hp: int
    damage_profile_ref: str
    damage_threshold: int | None
    destruction_policy_ref: str
    kind: str = "durability"


@dataclass(frozen=True, slots=True)
class LockDefinition:
    lock_id: str
    initial_locked: bool
    credential_instance_ids: tuple[str, ...]
    operable_sides: tuple[str, ...]
    policy: str = "public_owned_credential"


@dataclass(frozen=True, slots=True)
class LockSet:
    locks: tuple[LockDefinition, ...]
    kind: str = "locks"


@dataclass(frozen=True, slots=True)
class WorldObjectDefinition:
    object_id: str
    definition_ref: str
    display_name: str
    description_ref: str | None
    material: str | None
    size_category: str | None
    weight_lb: int | None
    capabilities: tuple[Openable | Durability | LockSet, ...]

    def capability(self, kind):
        return next((c for c in self.capabilities if c.kind == kind), None)


def validate_world_objects(raw, scenes, portable=(), legacy_locks=()):
    result = []
    objects = {o.object_id: o for s in scenes for o in s.objects}
    for value in _list(raw, "world_objects"):
        p = "world_objects"
        d = _mapping(
            value,
            "object_id definition_ref display_name description_ref material "
            "size_category weight_lb capabilities",
            p,
        )
        _reference(d["object_id"], objects, p)
        _identifier(d["definition_ref"], p + ".definition_ref", "definition")
        _text(d["display_name"], p + ".display_name")
        for field in ("description_ref", "material"):
            if d[field] is not None:
                _text(d[field], p + "." + field)
        if d["size_category"] not in (
            None,
            "tiny",
            "small",
            "medium",
            "large",
            "huge",
            "gargantuan",
        ):
            _fail("schema_error", p, "Unknown size category")
        if d["weight_lb"] is not None and (
            type(d["weight_lb"]) is not int or not 0 <= d["weight_lb"] <= 2147483647
        ):
            _fail("schema_error", p, "Weight is a nonnegative integer in pounds or unknown")
        capabilities = []
        for config in _list(d["capabilities"], p + ".capabilities"):
            if not isinstance(config, dict):
                _fail("schema_error", p, "Capability must be a mapping")
            if config.get("kind") == "locks":
                c = _mapping(config, "kind locks", p)
                locks = []
                for value in _list(c["locks"], p, nonempty=True):
                    lock = _mapping(
                        value,
                        "lock_id initial_locked credential_instance_ids operable_sides policy",
                        p,
                    )
                    _identifier(lock["lock_id"], p, "lock")
                    if (
                        type(lock["initial_locked"]) is not bool
                        or lock["policy"] != "public_owned_credential"
                    ):
                        _fail("schema_error", p, "Unsupported ordinary lock policy")
                    if lock["initial_locked"] and objects[d["object_id"]].state != "closed":
                        _fail("invalid_reference", p, "Locked object must be closed")
                    creds = _list(lock["credential_instance_ids"], p, nonempty=True)
                    for iid in creds:
                        _reference(iid, {i.instance_id for i in portable}, p)
                    sides = _list(lock["operable_sides"], p)
                    for side in sides:
                        _reference(side, {s.scene_id for s in scenes}, p)
                    _unique(creds, p)
                    _unique(sides, p)
                    locks.append(
                        LockDefinition(
                            lock["lock_id"], lock["initial_locked"], tuple(creds), tuple(sides)
                        )
                    )
                if d["object_id"] in {lock.object_id for lock in legacy_locks}:
                    _fail("invalid_reference", p, "Duplicate legacy and capability locks")
                capabilities.append(LockSet(tuple(locks)))
                continue
            if isinstance(config, dict) and config.get("kind") == "durability":
                c = _mapping(
                    config,
                    "kind armor_class max_hp initial_hp damage_profile_ref "
                    "damage_threshold destruction_policy_ref",
                    p,
                )
                if any(
                    type(c[f]) is not int for f in ("armor_class", "max_hp", "initial_hp")
                ) or not (
                    1 <= c["armor_class"] <= 100
                    and 0 <= c["initial_hp"] <= c["max_hp"] <= 2147483647
                    and c["max_hp"] > 0
                ):
                    _fail("schema_error", p, "Invalid durability bounds")
                if c["damage_threshold"] is not None and (
                    type(c["damage_threshold"]) is not int
                    or not 0 <= c["damage_threshold"] <= 2147483647
                ):
                    _fail("schema_error", p, "Invalid threshold")
                if (
                    c["damage_profile_ref"] != "scenario.explicit_settled_damage"
                    or c["destruction_policy_ref"] != "disable_preserve_obstruction"
                ):
                    _fail("unsupported_mechanism", p, "Unknown damage/destruction policy")
                capabilities.append(Durability(**c))
                continue
            c = _mapping(
                {
                    "initial_jammed": False,
                    "initial_barred": None,
                    "jam_policy": "prevent_transitions",
                    "operation_sides": [],
                    "bar_operable_sides": [],
                    **config,
                },
                "kind initial_jammed initial_barred jam_policy operation_sides bar_operable_sides",
                p + ".capabilities",
            )
            if c["kind"] != "openable":
                _fail("unsupported_object_capability", p, "Unknown capability")
            if objects[d["object_id"]].state is None:
                _fail("invalid_reference", p, "Openable requires an initial open state")
            jammed, barred = c.get("initial_jammed", False), c.get("initial_barred")
            if type(jammed) is not bool or (barred is not None and type(barred) is not bool):
                _fail("schema_error", p, "Invalid openable state")
            if c.get("jam_policy", "prevent_transitions") != "prevent_transitions":
                _fail("unsupported_mechanism", p, "Unsupported jam policy")
            sides, bar_sides = [], []
            for field, target in (("operation_sides", sides), ("bar_operable_sides", bar_sides)):
                for scene_id in _list(c.get(field, []), p):
                    _reference(scene_id, {s.scene_id for s in scenes}, p)
                    target.append(scene_id)
                _unique(target, p)
            if barred is True and objects[d["object_id"]].state != "closed":
                _fail("invalid_reference", p, "Barred object must be closed")
            if barred is None and bar_sides:
                _fail("invalid_reference", p, "Bar sides need a bar capability")
            capabilities.append(
                Openable(jammed, barred, "prevent_transitions", tuple(sides), tuple(bar_sides))
            )
        _unique([c.kind for c in capabilities], p)
        if (objects[d["object_id"]].state is not None) != any(
            c.kind == "openable" for c in capabilities
        ):
            _fail("invalid_reference", p, "State and openable declaration must agree")
        if any(c.kind == "locks" for c in capabilities) and not any(
            c.kind == "openable" for c in capabilities
        ):
            _fail("invalid_reference", p, "Locks require an openable capability")
        result.append(WorldObjectDefinition(**{**d, "capabilities": tuple(capabilities)}))
    _unique([d.object_id for d in result], "world_objects")
    _unique(
        [lock.lock_id for d in result if (c := d.capability("locks")) for lock in c.locks], "locks"
    )
    return tuple(result)
