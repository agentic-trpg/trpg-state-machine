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
class Container:
    capacity_weight_lb: int | None
    capacity_volume_cu_ft: int | None
    opening_profiles: tuple[str, ...]
    allowed_content_kinds: tuple[str, ...]
    nesting_limit: int
    light_transmission_open: str
    light_transmission_closed: str
    kind: str = "container"


@dataclass(frozen=True, slots=True)
class SpatialObject:
    footprint: tuple[tuple[int, int], ...]
    mobility_policy: str
    open_blocks_movement: bool
    closed_blocks_movement: bool
    destroyed_blocks_movement: bool
    kind: str = "spatial"


@dataclass(frozen=True, slots=True)
class LightMode:
    mode_id: str
    bright_radius_ft: int | None
    dim_outer_radius_ft: int


@dataclass(frozen=True, slots=True)
class LightEmitter:
    modes: tuple[LightMode, ...]
    initial_enabled: bool
    initial_mode: str
    emission_shape: str
    source_kind: str
    sunlight: bool
    fuel_policy: str
    kind: str = "light"


@dataclass(frozen=True, slots=True)
class WorldObjectDefinition:
    object_id: str
    definition_ref: str
    display_name: str
    description_ref: str | None
    material: str | None
    size_category: str | None
    weight_lb: int | None
    capabilities: tuple[
        Openable | Durability | LockSet | Container | SpatialObject | LightEmitter, ...
    ]
    volume_cu_ft: int | None = None
    fit_profile: str | None = None
    content_kind: str | None = None

    def capability(self, kind):
        return next((c for c in self.capabilities if c.kind == kind), None)


def validate_world_objects(raw, scenes, portable=(), legacy_locks=()):
    result = []
    objects = {o.object_id: o for s in scenes for o in s.objects}
    for value in _list(raw, "world_objects"):
        p = "world_objects"
        d = _mapping(
            {"volume_cu_ft": None, "fit_profile": None, "content_kind": None, **value},
            "volume_cu_ft fit_profile content_kind object_id definition_ref "
            "display_name description_ref material "
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
        if d["volume_cu_ft"] is not None and (
            type(d["volume_cu_ft"]) is not int or not 0 <= d["volume_cu_ft"] <= 2147483647
        ):
            _fail("schema_error", p, "Invalid external volume")
        for field in ("fit_profile", "content_kind"):
            if d[field] is not None:
                _text(d[field], p)
        capabilities = []
        for config in _list(d["capabilities"], p + ".capabilities"):
            if not isinstance(config, dict):
                _fail("schema_error", p, "Capability must be a mapping")
            if config.get("kind") == "light":
                c = _mapping(
                    config,
                    "kind modes initial_enabled initial_mode emission_shape "
                    "source_kind sunlight fuel_policy",
                    p,
                )
                if (
                    c["emission_shape"] != "grid_radius_static_los"
                    or c["source_kind"] != "scenario"
                    or c["fuel_policy"] != "scenario_unmetered"
                    or c["sunlight"] is not False
                    or type(c["initial_enabled"]) is not bool
                ):
                    _fail(
                        "unsupported_mechanism",
                        p,
                        "Only explicit ordinary unmetered scenario lights",
                    )
                modes = []
                for value in _list(c["modes"], p, nonempty=True):
                    mode = _mapping(value, "mode_id bright_radius_ft dim_outer_radius_ft", p)
                    _text(mode["mode_id"], p)
                    bright, dim = mode["bright_radius_ft"], mode["dim_outer_radius_ft"]
                    if (
                        type(dim) is not int
                        or not 0 <= dim <= 1000
                        or (
                            bright is not None
                            and (type(bright) is not int or not 0 <= bright <= dim)
                        )
                    ):
                        _fail("schema_error", p, "Ranges are feet; dim is the outer radius")
                    modes.append(LightMode(**mode))
                _unique([m.mode_id for m in modes], p)
                _reference(c["initial_mode"], {m.mode_id for m in modes}, p)
                capabilities.append(LightEmitter(**{**c, "modes": tuple(modes)}))
                continue
            if config.get("kind") == "spatial":
                c = _mapping(
                    config,
                    "kind footprint mobility_policy open_blocks_movement "
                    "closed_blocks_movement destroyed_blocks_movement",
                    p,
                )
                offsets = _list(c["footprint"], p, nonempty=True)
                if (
                    len(offsets) > 64
                    or any(
                        type(v) is not list
                        or len(v) != 2
                        or any(type(n) is not int or abs(n) > 32 for n in v)
                        for v in offsets
                    )
                    or [0, 0] not in offsets
                ):
                    _fail("schema_error", p, "Bounded explicit relative grid footprint required")
                c["footprint"] = tuple(tuple(v) for v in offsets)
                _unique(c["footprint"], p)
                if c["mobility_policy"] not in ("fixed", "gm_placement") or any(
                    type(c[f]) is not bool
                    for f in (
                        "open_blocks_movement",
                        "closed_blocks_movement",
                        "destroyed_blocks_movement",
                    )
                ):
                    _fail("unsupported_mechanism", p, "Unsupported spatial policy")
                if d["object_id"] in {i.instance_id for i in portable}:
                    _fail(
                        "unsupported_mechanism",
                        p,
                        "Independent spatial footprints cannot be carried",
                    )
                capabilities.append(SpatialObject(**c))
                continue
            if config.get("kind") == "container":
                c = _mapping(
                    config,
                    "kind capacity_weight_lb capacity_volume_cu_ft "
                    "opening_profiles allowed_content_kinds nesting_limit "
                    "light_transmission_open light_transmission_closed",
                    p,
                )
                for field in ("capacity_weight_lb", "capacity_volume_cu_ft"):
                    if c[field] is not None and (
                        type(c[field]) is not int or not 0 <= c[field] <= 2147483647
                    ):
                        _fail("schema_error", p, "Capacity is a known limit or explicitly unset")
                if type(c["nesting_limit"]) is not int or not 1 <= c["nesting_limit"] <= 8:
                    _fail("schema_error", p, "Engineering nesting bound is 1..8")
                for field in ("opening_profiles", "allowed_content_kinds"):
                    values = _list(c[field], p, nonempty=True)
                    for entry in values:
                        _text(entry, p)
                    _unique(values, p)
                    c[field] = tuple(values)
                for field in ("light_transmission_open", "light_transmission_closed"):
                    if c[field] not in ("transmit", "block", "unknown"):
                        _fail("schema_error", p, "Explicit container transmission required")
                capabilities.append(Container(**c))
                continue
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
                if c["damage_profile_ref"] != "scenario.explicit_settled_damage" or c[
                    "destruction_policy_ref"
                ] not in ("disable_preserve_obstruction", "disable_spatial_policy"):
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
        if any(c.kind == "light" for c in capabilities) and any(
            c.kind == "spatial" for c in capabilities
        ):
            _fail(
                "unsupported_mechanism",
                p,
                "Light anchors on independent spatial geometry are not supported",
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
