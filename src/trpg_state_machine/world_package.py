"""Explicit immutable object definitions, pinned by the Session package digest."""

from dataclasses import dataclass

from .package import _fail, _identifier, _list, _mapping, _reference, _text, _unique


@dataclass(frozen=True, slots=True)
class Openable:
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
class WorldObjectDefinition:
    object_id: str
    definition_ref: str
    display_name: str
    description_ref: str | None
    material: str | None
    size_category: str | None
    weight_lb: int | None
    capabilities: tuple[Openable | Durability, ...]

    def capability(self, kind):
        return next((c for c in self.capabilities if c.kind == kind), None)


def validate_world_objects(raw, scenes):
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
            c = _mapping(config, "kind", p + ".capabilities")
            if c["kind"] != "openable":
                _fail("unsupported_object_capability", p, "Unknown capability")
            if objects[d["object_id"]].state is None:
                _fail("invalid_reference", p, "Openable requires an initial open state")
            capabilities.append(Openable())
        _unique([c.kind for c in capabilities], p)
        if (objects[d["object_id"]].state is not None) != any(
            c.kind == "openable" for c in capabilities
        ):
            _fail("invalid_reference", p, "State and openable declaration must agree")
        result.append(WorldObjectDefinition(**{**d, "capabilities": tuple(capabilities)}))
    _unique([d.object_id for d in result], "world_objects")
    return tuple(result)
