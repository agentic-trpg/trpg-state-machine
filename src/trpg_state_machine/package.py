"""Strict, immutable, deliberately synthetic-only Adventure Package subset."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml

from ._operations import ObjectState, SetObjectState, SetSceneFlag
from .errors import Issue, StateMachineError
from .time_binding import TimeBinding, time_binding_from_data

if TYPE_CHECKING:
    from .item_package import PortableItem
    from .lock_package import LockableDoor
    from .navigation_models import NavigationKnowledge
    from .npc_state import NPCInitialState, WorldFactDefinition
    from .world_package import WorldObjectDefinition

SCHEMA_VERSION = "state-machine-foundation/1"
INTERACTION_SCHEMA_VERSION = "state-machine-foundation/2"
NAVIGATION_SCHEMA_VERSION = "state-machine-foundation/3"
ITEM_SCHEMA_VERSION = "state-machine-foundation/4"
LOCK_SCHEMA_VERSION = "state-machine-foundation/5"
WORLD_SCHEMA_VERSION = "state-machine-foundation/6"
OPENABLE_SCHEMA_VERSION = "state-machine-foundation/7"
OBJECT_SPATIAL_SCHEMA_VERSION = "state-machine-foundation/8"
MECHANISM_SCHEMA_VERSION = "state-machine-foundation/10"
MAX_PACKAGE_BYTES = 1_048_576
_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")


def canonical_json(value: object) -> str:
    """foundation-json/1: UTF-8, sorted keys, compact JSON; no floats/NaN."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RulesetBinding:
    ruleset_id: str
    data_revision: str
    evaluator_version: str


# This is a fixture data contract, NOT an Engine evaluator or D&D ruleset.
SYNTHETIC_RULESET = RulesetBinding("synthetic.foundation", "synthetic-data/1", "none/1")
SYNTHETIC_EVALUATION_RULESET = RulesetBinding(
    "synthetic.atomic", "synthetic-data/1", "synthetic-evaluator/1"
)


@dataclass(frozen=True, slots=True)
class ObjectDefinition:
    object_id: str
    state: Literal["closed", "open"] | None
    movable: bool = False
    blocks_passage: bool = False
    initial_cell: str | None = None
    initial_orientation: str = "north"


@dataclass(frozen=True, slots=True)
class PassageEndpoint:
    scene_id: str
    standing_cell: str
    threshold_cell: str


@dataclass(frozen=True, slots=True)
class PassageDefinition:
    passage_id: str
    endpoints: tuple[PassageEndpoint, PassageEndpoint]
    door_object_id: str | None


@dataclass(frozen=True, slots=True)
class SceneDefinition:
    scene_id: str
    location_id: str
    exits: tuple[str, ...]
    flags: tuple[tuple[str, bool], ...]
    actor_ids: tuple[str, ...]
    objects: tuple[ObjectDefinition, ...]
    exit_passages: tuple[tuple[str, str], ...] = ()
    public_interactions: bool = False
    grid_json: str | None = None


@dataclass(frozen=True, slots=True)
class ActorDefinition:
    actor_id: str
    name: str
    initial_scene_id: str
    senses: tuple[tuple[str, bool], ...] | None = None
    controller_kind: str = "scripted_event"
    npc_state: "NPCInitialState | None" = None
    initial_cell: str | None = None


@dataclass(frozen=True, slots=True)
class PCTemplate:
    pc_template_id: str
    name: str
    current_hp: int
    max_hp: int
    resources: tuple[tuple[str, int], ...] = ()
    inventory: tuple[str, ...] = ()
    initial_cell: str | None = None


@dataclass(frozen=True, slots=True)
class SignalScope:
    scene_id: str
    visibility: str
    kind: str = "scene"


@dataclass(frozen=True, slots=True)
class SignalOrigin:
    scene_id: str
    entity_id: str | None = None
    cell: str | None = None


@dataclass(frozen=True, slots=True)
class SignalStimulus:
    kind: Literal["ordinary_activity", "light_emission", "sound"]


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str
    channel: str
    content: str
    scope: SignalScope
    origin: SignalOrigin | None = None
    stimulus: SignalStimulus | None = None


@dataclass(frozen=True, slots=True)
class EventDefinition:
    event_definition_id: str
    trigger_type: str
    scene_id: str
    condition_flag: str
    condition_value: bool
    effect: SetSceneFlag | SetObjectState
    signals: tuple[Signal, ...]


@dataclass(frozen=True, slots=True)
class AdventurePackage:
    adventure_id: str
    package_version: str
    package_digest: str
    ruleset_binding: RulesetBinding
    entry_scene_id: str
    world_flags: tuple[tuple[str, bool], ...]
    scenes: tuple[SceneDefinition, ...]
    actors: tuple[ActorDefinition, ...]
    pc_templates: tuple[PCTemplate, ...]
    events: tuple[EventDefinition, ...] = ()
    combat_foundation_json: str | None = None
    noncombat_foundation_json: str | None = None
    world_facts: tuple["WorldFactDefinition", ...] = ()
    passages: tuple[PassageDefinition, ...] = ()
    navigation: "NavigationKnowledge | None" = None
    legacy_audit_only: bool = False
    portable_items: tuple["PortableItem", ...] = ()
    lockable_doors: tuple["LockableDoor", ...] = ()
    world_objects: tuple["WorldObjectDefinition", ...] = ()
    time_binding: TimeBinding | None = None

    def scene(self, scene_id: str) -> SceneDefinition:
        for scene in self.scenes:
            if scene.scene_id == scene_id:
                return scene
        raise StateMachineError("unknown_scene", f"Unknown scene: {scene_id}")


@dataclass(frozen=True, slots=True)
class PackageLoadResult:
    status: Literal["valid", "invalid"]
    package: AdventurePackage | None = None
    errors: tuple[Issue, ...] = ()
    warnings: tuple[Issue, ...] = ()


class _DataLoader(yaml.SafeLoader):
    """SafeLoader plus duplicate-key, alias, depth and node limits."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0
        self._nodes = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are unsupported")
        self._depth += 1
        self._nodes += 1
        try:
            if self._depth > 32 or self._nodes > 20_000:
                raise yaml.YAMLError("YAML nesting/node limit exceeded")
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise yaml.YAMLError("Mapping keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _fail(code: str, path: str, message: str):
    raise StateMachineError(code, message, path=path)


def _plain(value: object, path: str = "$") -> None:
    if type(value) in (str, int, bool, type(None)):
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _plain(item, f"{path}[{i}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _plain(item, f"{path}.{key}")
    else:
        _fail("unsupported_value", path, "Only strings, integers, booleans, null, lists/maps")


def _mapping(value, fields: str, path: str) -> dict:
    if not isinstance(value, dict):
        _fail("schema_error", path, "Expected a mapping")
    expected = set(fields.split())
    if extra := value.keys() - expected:
        _fail("unsupported_field", path, f"Unsupported fields: {sorted(extra)}")
    if missing := expected - value.keys():
        _fail("missing_field", path, f"Missing fields: {sorted(missing)}")
    return value


def _list(value, path: str, *, nonempty: bool = False) -> list:
    if not isinstance(value, list) or (nonempty and not value):
        _fail(
            "schema_error",
            path,
            "Expected a list" + (" with at least one item" if nonempty else ""),
        )
    return value


def _text(value, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        _fail("schema_error", path, "Expected a nonblank string, maximum 4096 characters")
    return value


def _identifier(value, path: str, namespace: str | None = None) -> str:
    text = _text(value, path)
    if len(text) > 128 or not _ID.fullmatch(text):
        _fail("invalid_id", path, "Expected a lowercase dotted ID, maximum 128 characters")
    if namespace and not text.startswith(namespace + "."):
        _fail("invalid_id", path, f"Expected {namespace}. namespace")
    return text


def _empty(value, expected, path: str) -> None:
    if type(value) is not type(expected) or value != expected:
        _fail("unsupported_mechanism", path, "This foundation requires this field to be empty")


def _flags(value, path: str) -> tuple[tuple[str, bool], ...]:
    if not isinstance(value, dict):
        _fail("schema_error", path, "Expected boolean flags")
    for key, flag in value.items():
        _identifier(key, f"{path}.{key}", "flag")
        if type(flag) is not bool:
            _fail("schema_error", f"{path}.{key}", "Flags must be booleans")
    return tuple(sorted(value.items()))


def _unique(values, path: str) -> None:
    if len(values) != len(set(values)):
        _fail("duplicate_id", path, "IDs/references must be unique in their scope")


def _reference(value, targets, path: str) -> None:
    if not isinstance(value, str) or value not in targets:
        _fail("invalid_reference", path, f"Unknown reference: {value}")


def _validate(data: dict, *, allow_legacy_audit=False) -> AdventurePackage:
    interactions = (
        isinstance(data, dict)
        and isinstance(data.get("manifest"), dict)
        and data["manifest"].get("schema_version")
        in (
            INTERACTION_SCHEMA_VERSION,
            NAVIGATION_SCHEMA_VERSION,
            ITEM_SCHEMA_VERSION,
            LOCK_SCHEMA_VERSION,
            WORLD_SCHEMA_VERSION,
            OPENABLE_SCHEMA_VERSION,
            OBJECT_SPATIAL_SCHEMA_VERSION,
            MECHANISM_SCHEMA_VERSION,
        )
    )
    world_enabled = interactions and data["manifest"]["schema_version"] in (
        WORLD_SCHEMA_VERSION,
        OPENABLE_SCHEMA_VERSION,
        OBJECT_SPATIAL_SCHEMA_VERSION,
        MECHANISM_SCHEMA_VERSION,
    )
    locks = interactions and data["manifest"]["schema_version"] in (
        LOCK_SCHEMA_VERSION,
        WORLD_SCHEMA_VERSION,
        OPENABLE_SCHEMA_VERSION,
        OBJECT_SPATIAL_SCHEMA_VERSION,
        MECHANISM_SCHEMA_VERSION,
    )
    items = interactions and data["manifest"]["schema_version"] in (
        ITEM_SCHEMA_VERSION,
        LOCK_SCHEMA_VERSION,
        WORLD_SCHEMA_VERSION,
        OPENABLE_SCHEMA_VERSION,
        OBJECT_SPATIAL_SCHEMA_VERSION,
        MECHANISM_SCHEMA_VERSION,
    )
    navigation = interactions and data["manifest"]["schema_version"] in (
        NAVIGATION_SCHEMA_VERSION,
        ITEM_SCHEMA_VERSION,
        LOCK_SCHEMA_VERSION,
        WORLD_SCHEMA_VERSION,
        OPENABLE_SCHEMA_VERSION,
        OBJECT_SPATIAL_SCHEMA_VERSION,
        MECHANISM_SCHEMA_VERSION,
    )
    root = _mapping(
        data,
        "manifest world scenes actors pc_templates events rule_bindings"
        + (" passages" if interactions else "")
        + (" navigation" if navigation else "")
        + (" item_transfers" if items else "")
        + (" lockable_doors" if locks else "")
        + (" world_objects" if world_enabled else "")
        + (" time_binding" if isinstance(data, dict) and "time_binding" in data else ""),
        # Package versioning keeps legacy raw-data digests unchanged.
        "$",
    )
    m = _mapping(
        root["manifest"],
        "schema_version adventure_id package_version title entry_scene_id "
        "ruleset_binding player_setup content_provenance compatibility",
        "manifest",
    )
    if m["schema_version"] not in (
        SCHEMA_VERSION,
        INTERACTION_SCHEMA_VERSION,
        NAVIGATION_SCHEMA_VERSION,
        ITEM_SCHEMA_VERSION,
        LOCK_SCHEMA_VERSION,
        WORLD_SCHEMA_VERSION,
        OPENABLE_SCHEMA_VERSION,
        OBJECT_SPATIAL_SCHEMA_VERSION,
        MECHANISM_SCHEMA_VERSION,
    ):
        _fail("unsupported_schema", "manifest.schema_version", f"Expected {SCHEMA_VERSION}")
    adventure_id = _identifier(m["adventure_id"], "manifest.adventure_id")
    version = _text(m["package_version"], "manifest.package_version")
    _text(m["title"], "manifest.title")
    entry = _identifier(m["entry_scene_id"], "manifest.entry_scene_id", "scene")
    binding = RulesetBinding(
        **_mapping(
            m["ruleset_binding"],
            "ruleset_id data_revision evaluator_version",
            "manifest.ruleset_binding",
        )
    )
    dnd = binding.ruleset_id == "dnd-2024-srd-5.2.1"
    legacy_audit = (
        dnd
        and allow_legacy_audit
        and binding.evaluator_version in ("dnd5e-evaluation/4", "dnd5e-evaluation/8")
    )
    if dnd and (
        not isinstance(binding.data_revision, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", binding.data_revision)
        or (binding.evaluator_version != "dnd5e-evaluation/36" and not legacy_audit)
    ):
        _fail("unsupported_ruleset", "manifest.ruleset_binding", "Invalid pinned D&D binding")
    if not dnd and binding not in (SYNTHETIC_RULESET, SYNTHETIC_EVALUATION_RULESET):
        _fail(
            "unsupported_ruleset",
            "manifest.ruleset_binding",
            "Only the synthetic fixture binding is supported",
        )
    setup = _mapping(m["player_setup"], "player_count pc_count", "manifest.player_setup")
    if any(type(v) is not int or v != 1 for v in setup.values()):
        _fail("unsupported_player_setup", "manifest.player_setup", "Exactly one player and one PC")
    provenance = _mapping(
        m["content_provenance"],
        "source_type license reviewed_by redistribution_allowed",
        "manifest.content_provenance",
    )
    for key in ("source_type", "license", "reviewed_by"):
        _text(provenance[key], f"manifest.content_provenance.{key}")
    if interactions and provenance["source_type"] != "original_synthetic":
        _fail(
            "unsupported_mechanism", "manifest.content_provenance", "GC-1 requires original content"
        )
    if type(provenance["redistribution_allowed"]) is not bool:
        _fail(
            "schema_error", "manifest.content_provenance.redistribution_allowed", "Expected boolean"
        )
    compatibility = _mapping(
        m["compatibility"], "unresolved_issues adaptation_notes", "manifest.compatibility"
    )
    for key in compatibility:
        _empty(compatibility[key], [], f"manifest.compatibility.{key}")
    foundation = noncombat = None
    if dnd:
        from .combat import validate_foundation, validate_noncombat_foundation

        rules = root["rule_bindings"]
        if (
            type(rules) is not dict
            or not rules
            or set(rules) - {"combat_foundation", "noncombat_foundation"}
        ):
            _fail("schema_error", "rule_bindings", "Expected reviewed mechanical foundations")
        if "combat_foundation" in rules:
            if not legacy_audit:
                validate_foundation(rules["combat_foundation"])
            else:
                _legacy_audit_foundation(rules["combat_foundation"], combat=True)
            foundation = canonical_json(rules["combat_foundation"])
        if "noncombat_foundation" in rules:
            if not legacy_audit:
                validate_noncombat_foundation(rules["noncombat_foundation"])
            else:
                _legacy_audit_foundation(rules["noncombat_foundation"], combat=False)
            noncombat = canonical_json(rules["noncombat_foundation"])
    else:
        _empty(root["rule_bindings"], {}, "rule_bindings")

    world = _mapping(
        root["world"], "locations starting_location_id initial_flags world_facts", "world"
    )
    flags = _flags(world["initial_flags"], "world.initial_flags")
    locations = {}
    for i, value in enumerate(_list(world["locations"], "world.locations", nonempty=True)):
        path = f"world.locations[{i}]"
        loc = _mapping(value, "location_id name scene_ids", path)
        key = _identifier(loc["location_id"], path + ".location_id", "location")
        if key in locations:
            _fail("duplicate_id", path, "Duplicate location ID")
        _text(loc["name"], path + ".name")
        ids = tuple(
            _identifier(v, path + ".scene_ids", "scene")
            for v in _list(loc["scene_ids"], path + ".scene_ids", nonempty=True)
        )
        _unique(ids, path + ".scene_ids")
        locations[key] = ids

    scenes = []
    for i, value in enumerate(_list(root["scenes"], "scenes", nonempty=True)):
        path = f"scenes[{i}]"
        s = _mapping(
            value,
            "scene_id location_id description exits initial_state actor_ids objects "
            "event_definition_ids access_policy" + (" grid" if interactions else ""),
            path,
        )
        key = _identifier(s["scene_id"], path + ".scene_id", "scene")
        location = _identifier(s["location_id"], path + ".location_id", "location")
        _text(s["description"], path + ".description")
        if s["access_policy"] not in (
            ("internal_only", "public_interactions") if interactions else ("internal_only",)
        ):
            _fail(
                "unsupported_mechanism",
                path + ".access_policy",
                "No player/NPC projection API in this batch",
            )
        _list(s["event_definition_ids"], path + ".event_definition_ids")
        initial = _mapping(s["initial_state"], "local_flags", path + ".initial_state")
        grid_json = _grid_config(s["grid"], path + ".grid") if interactions else None
        exits, exit_passages = [], []
        for j, value in enumerate(_list(s["exits"], path + ".exits")):
            ex = _mapping(
                value,
                "to_scene_id" + (" passage_id" if interactions else ""),
                f"{path}.exits[{j}]",
            )
            exits.append(_identifier(ex["to_scene_id"], path + ".exits", "scene"))
            if interactions and ex["passage_id"] is not None:
                exit_passages.append(
                    (exits[-1], _identifier(ex["passage_id"], path + ".exits", "passage"))
                )
        _unique(exits, path + ".exits")
        actors = tuple(
            _identifier(v, path + ".actor_ids", "npc")
            for v in _list(s["actor_ids"], path + ".actor_ids")
        )
        _unique(actors, path + ".actor_ids")
        objects = []
        for j, value in enumerate(_list(s["objects"], path + ".objects")):
            ob = _mapping(
                {"initial_orientation": "north", **value}
                if m["schema_version"] in (OBJECT_SPATIAL_SCHEMA_VERSION, MECHANISM_SCHEMA_VERSION)
                and isinstance(value, dict)
                else value,
                "object_id state"
                + (" movable blocks_passage initial_cell" if interactions else "")
                + (
                    " initial_orientation"
                    if m["schema_version"]
                    in (OBJECT_SPATIAL_SCHEMA_VERSION, MECHANISM_SCHEMA_VERSION)
                    else ""
                ),
                f"{path}.objects[{j}]",
            )
            oid = _identifier(ob["object_id"], path + ".objects", "object")
            if ob["state"] not in (
                (None, "closed", "open") if interactions else ("closed", "open")
            ):
                _fail(
                    "unsupported_mechanism", path + ".objects", "Only closed/open synthetic objects"
                )
            movable, blocks = ob.get("movable", False), ob.get("blocks_passage", False)
            if type(movable) is not bool or type(blocks) is not bool or (blocks and not movable):
                _fail(
                    "unsupported_mechanism",
                    path + ".objects",
                    "Blocking requires explicit movable capability",
                )
            cell = _initial_cell(
                ob.get("initial_cell"), grid_json, path + ".objects", standing=False
            )
            orientation = ob.get("initial_orientation", "north")
            if orientation not in ("north", "east", "south", "west"):
                _fail("schema_error", path + ".objects", "Invalid initial orientation")
            objects.append(ObjectDefinition(oid, ob["state"], movable, blocks, cell, orientation))
        scenes.append(
            SceneDefinition(
                key,
                location,
                tuple(exits),
                _flags(initial["local_flags"], path + ".initial_state.local_flags"),
                actors,
                tuple(objects),
                tuple(exit_passages),
                s["access_policy"] == "public_interactions",
                grid_json,
            )
        )
    scene_ids = {s.scene_id for s in scenes}
    _unique([s.scene_id for s in scenes], "scenes")
    _unique([o.object_id for s in scenes for o in s.objects], "scenes.objects")
    passages = _passage_definitions(root["passages"], scenes) if interactions else ()
    _reference(entry, scene_ids, "manifest.entry_scene_id")
    for scene in scenes:
        _reference(scene.location_id, locations, f"scenes.{scene.scene_id}.location_id")
        if scene.scene_id not in locations[scene.location_id]:
            _fail(
                "invalid_reference",
                "world.locations.scene_ids",
                "Scene/location membership mismatch",
            )
        for target in scene.exits:
            _reference(target, scene_ids, f"scenes.{scene.scene_id}.exits")
    for location, members in locations.items():
        for member in members:
            _reference(member, scene_ids, "world.locations.scene_ids")
            if next(s for s in scenes if s.scene_id == member).location_id != location:
                _fail(
                    "invalid_reference",
                    "world.locations.scene_ids",
                    "Scene listed in wrong location",
                )
    entry_location = next(s for s in scenes if s.scene_id == entry).location_id
    if world["starting_location_id"] != entry_location:
        _fail("invalid_reference", "world.starting_location_id", "Must match entry scene location")
    reachable = {entry}
    pending = [entry]
    while pending:
        current_scene = pending.pop()
        scene = next(s for s in scenes if s.scene_id == current_scene)
        for target in scene.exits:
            if target not in reachable:
                reachable.add(target)
                pending.append(target)
    if reachable != scene_ids:
        _fail("unreachable_scene", "scenes", "All foundation scenes must be reachable from entry")

    actors = []
    for i, value in enumerate(_list(root["actors"], "actors")):
        path = f"actors[{i}]"
        fields = "actor_id name kind controller initial_scene_id"
        if isinstance(value, dict) and "senses" in value:
            fields += " senses"
        if isinstance(value, dict) and "npc_state" in value:
            fields += " npc_state"
        if interactions:
            fields += " initial_cell"
        a = _mapping(value, fields, path)
        key = _identifier(a["actor_id"], path + ".actor_id", "npc")
        initial_scene = _identifier(a["initial_scene_id"], path + ".initial_scene_id", "scene")
        _reference(initial_scene, scene_ids, path + ".initial_scene_id")
        controller = _mapping(a["controller"], "kind", path + ".controller")
        if a["kind"] != "npc" or controller["kind"] not in (
            "sub_agent",
            "deterministic_policy",
            "scripted_event",
        ):
            _fail(
                "unsupported_mechanism", path, "Only NPC identity and reviewed controller metadata"
            )
        if key not in next(s for s in scenes if s.scene_id == initial_scene).actor_ids:
            _fail("invalid_reference", path, "Actor must be listed in its initial scene")
        senses = None
        if "senses" in a:
            sense_data = _mapping(a["senses"], "visual auditory", path + ".senses")
            if any(type(v) is not bool for v in sense_data.values()):
                _fail("schema_error", path + ".senses", "Senses must be explicit booleans")
            senses = tuple(sorted(sense_data.items()))
        actors.append(
            ActorDefinition(
                key,
                _text(a["name"], path + ".name"),
                initial_scene,
                senses,
                controller["kind"],
                initial_cell=_initial_cell(
                    a.get("initial_cell"),
                    next(s for s in scenes if s.scene_id == initial_scene).grid_json,
                    path,
                ),
            )
        )
    _unique([a.actor_id for a in actors], "actors")
    for scene in scenes:
        for actor in scene.actor_ids:
            _reference(actor, {a.actor_id for a in actors}, f"scenes.{scene.scene_id}.actor_ids")

    from dataclasses import replace

    from .npc_schema import parse_npc_state, parse_world_facts

    npc_ids = {a.actor_id for a in actors}
    facts = parse_world_facts(world["world_facts"], npc_ids)
    actors = [
        replace(
            actor,
            npc_state=parse_npc_state(raw["npc_state"], actor.actor_id, npc_ids, scene_ids, facts),
        )
        if "npc_state" in raw
        else actor
        for actor, raw in zip(actors, root["actors"], strict=True)
    ]

    templates = []
    for i, value in enumerate(_list(root["pc_templates"], "pc_templates", nonempty=True)):
        path = f"pc_templates[{i}]"
        pc = _mapping(
            value, "pc_template_id name character" + (" initial_cell" if interactions else ""), path
        )
        key = _identifier(pc["pc_template_id"], path + ".pc_template_id", "pc_template")
        char = _mapping(pc["character"], "hp resources inventory", path + ".character")
        hp = _mapping(char["hp"], "current max", path + ".character.hp")
        if (
            any(type(v) is not int for v in hp.values())
            or not 0 <= hp["current"] <= hp["max"] <= 1_000_000
            or hp["max"] == 0
        ):
            _fail(
                "schema_error",
                path + ".character.hp",
                "Expected 0 <= current <= max <= 1000000, max > 0",
            )
        resources, inventory = (), ()
        if binding == SYNTHETIC_RULESET:
            _empty(char["resources"], {}, path + ".character.resources")
            _empty(char["inventory"], [], path + ".character.inventory")
        else:
            if not isinstance(char["resources"], dict):
                _fail("schema_error", path, "Expected declared synthetic resource counters")
            for resource, count in char["resources"].items():
                _identifier(resource, path, "resource")
                if type(count) is not int or not 0 <= count <= 1_000_000:
                    _fail("schema_error", path, "Invalid synthetic resource count")
            resources = tuple(sorted(char["resources"].items()))
            inventory = tuple(
                _text(v, path) if dnd else _identifier(v, path, "item")
                for v in _list(char["inventory"], path)
            )
            if dnd and any(not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", v) for v in inventory):
                _fail("invalid_id", path, "Expected a canonical or reviewed item identity")
            _unique(inventory, path)
        templates.append(
            PCTemplate(
                key,
                _text(pc["name"], path + ".name"),
                hp["current"],
                hp["max"],
                resources,
                inventory,
                _initial_cell(
                    pc.get("initial_cell"),
                    next(s for s in scenes if s.scene_id == entry).grid_json,
                    path,
                ),
            )
        )
    _unique([pc.pc_template_id for pc in templates], "pc_templates")
    if interactions:
        _spatial_initial_consistency(scenes, actors, templates, entry)
    definitions = _event_definitions(root["events"], scenes)
    for scene_data in root["scenes"]:
        ids = scene_data["event_definition_ids"]
        for event_id in ids:
            _identifier(event_id, "scenes.event_definition_ids", "event")
        _unique(ids, "scenes.event_definition_ids")
        for event_id in ids:
            _reference(
                event_id,
                {e.event_definition_id for e in definitions},
                "scenes.event_definition_ids",
            )
        expected_ids = {
            e.event_definition_id for e in definitions if e.scene_id == scene_data["scene_id"]
        }
        if set(ids) != expected_ids:
            _fail("invalid_reference", "scenes.event_definition_ids", "Event membership mismatch")
    for encoded in (foundation, noncombat):
        if encoded is None:
            continue
        initial = json.loads(encoded)["snapshot"]
        known = {a.actor_id for a in actors} | {p.pc_template_id for p in templates}
        for actor in initial["character_states"]:
            _reference(actor["entity_id"], known, "rule_bindings.combat_foundation.actor")
        _reference(
            initial["scene_state"]["scene_id"],
            {s.scene_id for s in scenes},
            "rule_bindings.combat_foundation.scene",
        )
        scene = next(s for s in scenes if s.scene_id == initial["scene_state"]["scene_id"])
        if (
            scene.grid_json is not None
            and json.loads(scene.grid_json) != initial["scene_state"]["grid"]
        ):
            _fail(
                "invalid_reference", "rule_bindings", "Spatial and mechanical base grid must agree"
            )
    if noncombat is not None:
        initial = json.loads(noncombat)
        snapshot = initial["snapshot"]
        if snapshot["scene_state"]["scene_id"] != entry:
            _fail("invalid_reference", "rule_bindings.noncombat_foundation", "Expected entry scene")
        for scene in initial["additional_scenes"]:
            _reference(scene["scene_id"], scene_ids, "rule_bindings.noncombat_foundation.scenes")
            configured = next(s for s in scenes if s.scene_id == scene["scene_id"])
            if (
                configured.grid_json is not None
                and json.loads(configured.grid_json) != scene["grid"]
            ):
                _fail(
                    "invalid_reference",
                    "rule_bindings",
                    "Spatial and mechanical base grid must agree",
                )
        for actor in snapshot["character_states"]:
            pc = next((p for p in templates if p.pc_template_id == actor["entity_id"]), None)
            if pc is not None and (
                actor["entity_type"] != "Character"
                or (pc.current_hp, pc.max_hp, list(pc.inventory))
                != (actor["hp_current"], actor["hp_max"], actor["carried_item_slugs"])
                or pc.resources
            ):
                _fail("invalid_reference", "pc_templates", "Core and mechanical facts disagree")
            if (
                pc is None
                and next(a for a in actors if a.actor_id == actor["entity_id"]).initial_scene_id
                != entry
            ):
                _fail(
                    "invalid_reference",
                    "rule_bindings.noncombat_foundation",
                    "Initial actors must be present",
                )
        if foundation is not None:
            combat_initial = json.loads(foundation)["snapshot"]
            if (
                combat_initial["snapshot_schema_version"] != snapshot["snapshot_schema_version"]
                or combat_initial["character_states"] != snapshot["character_states"]
                or combat_initial["scene_state"] != snapshot["scene_state"]
                or (
                    not legacy_audit
                    and any(
                        combat_initial[f] != snapshot[f]
                        for f in (
                            "inventory_state",
                            "resource_state",
                            "casting_state",
                            "effect_states",
                        )
                    )
                )
            ):
                _fail("invalid_reference", "rule_bindings", "Initial mechanical facts disagree")
    from .navigation_package import validate_navigation

    knowledge = (
        validate_navigation(root["navigation"], scenes, passages, actors, templates)
        if navigation
        else None
    )
    from .item_package import validate_items

    portable = (
        validate_items(root["item_transfers"], scenes, noncombat, definitions, world=world_enabled)
        if items
        else ()
    )
    from .lock_package import validate_locks

    lockable = validate_locks(root["lockable_doors"], scenes, passages, portable) if locks else ()
    from .world_package import validate_world_objects

    world_objects = (
        validate_world_objects(
            root["world_objects"],
            scenes,
            portable,
            lockable,
            passages,
            extended=m["schema_version"]
            in (OPENABLE_SCHEMA_VERSION, OBJECT_SPATIAL_SCHEMA_VERSION, MECHANISM_SCHEMA_VERSION),
            oriented=m["schema_version"]
            in (OBJECT_SPATIAL_SCHEMA_VERSION, MECHANISM_SCHEMA_VERSION),
            mechanisms=m["schema_version"] == MECHANISM_SCHEMA_VERSION,
        )
        if world_enabled
        else ()
    )
    spatial_definitions = {d.object_id for d in world_objects if d.capability("spatial")}
    if any(
        o.initial_orientation != "north" and o.object_id not in spatial_definitions
        for s in scenes
        for o in s.objects
    ):
        _fail("invalid_reference", "scenes.objects", "Orientation requires spatial capability")
    return AdventurePackage(
        adventure_id,
        version,
        _digest(data),
        binding,
        entry,
        flags,
        tuple(scenes),
        tuple(actors),
        tuple(templates),
        definitions,
        foundation,
        noncombat,
        facts,
        passages,
        knowledge,
        legacy_audit,
        portable,
        lockable,
        world_objects,
        time_binding_from_data(root["time_binding"]) if "time_binding" in root else None,
    )


def _grid_config(raw, path):
    if raw is None:
        return None
    from dnd5e_engine.evaluation_state import MechanicalGrid
    from dnd5e_engine.specs import GridScene
    from pydantic import ValidationError

    try:
        # Closed upstream schema plus its canonical geometry validation.
        grid = MechanicalGrid.model_validate_json(canonical_json(raw))
        GridScene(**grid.model_dump())
        if grid.width * grid.height > 4096:
            _fail("unsupported_mechanism", path, "GC-1 supports at most 4096 cells per Scene")
        return grid.model_dump_json()
    except (ValidationError, ValueError, TypeError) as exc:
        _fail("schema_error", path, f"Invalid mechanical grid: {type(exc).__name__}")


def _initial_cell(cell, grid_json, path, *, standing=True):
    from dnd5e_engine.spatial import GridTopology, canonical_cell_id
    from dnd5e_engine.specs import GridScene

    if grid_json is None:
        if cell is not None:
            _fail("invalid_reference", path, "Abstract Scene has no local coordinates")
        return None
    try:
        topology = GridTopology(GridScene(**json.loads(grid_json)))
        if type(cell) is not str or canonical_cell_id(cell) != cell:
            raise ValueError("Noncanonical cell")
        # distance_ft checks bounds; is_valid_cell additionally checks walkability.
        if topology.distance_ft(cell, cell) is None or (
            standing and not topology.is_valid_cell(cell)
        ):
            raise ValueError("Invalid position")
    except (ValueError, TypeError):
        _fail("invalid_reference", path, "Expected a canonical cell in this Scene's grid")
    return cell


def _spatial_initial_consistency(scenes, actors, templates, entry):
    for scene in scenes:
        if scene.grid_json is None:
            if any(o.movable for o in scene.objects):
                _fail(
                    "unsupported_mechanism", "scenes.objects", "Movable objects require coordinates"
                )
            continue
        occupied = [a.initial_cell for a in actors if a.initial_scene_id == scene.scene_id]
        _unique(occupied, "actors.initial_cell")
        blocked = {o.initial_cell for o in scene.objects if o.blocks_passage}
        if blocked & set(occupied):
            _fail("invalid_reference", "scenes.objects", "Blocking object overlaps an actor")
        if scene.scene_id == entry:
            for template in templates:
                if template.initial_cell in set(occupied) | blocked:
                    _fail("invalid_reference", "pc_templates.initial_cell", "PC occupancy overlaps")


def _passage_definitions(raw, scenes):
    definitions = []
    objects = {o.object_id: (s.scene_id, o) for s in scenes for o in s.objects}
    scene_ids = {s.scene_id for s in scenes}
    for value in _list(raw, "passages"):
        p = _mapping(value, "passage_id endpoints door_object_id", "passages")
        pid = _identifier(p["passage_id"], "passages.passage_id", "passage")
        endpoints = []
        for raw_endpoint in _list(p["endpoints"], "passages.endpoints"):
            e = _mapping(
                raw_endpoint, "scene_id standing_cell threshold_cell", "passages.endpoints"
            )
            _reference(e["scene_id"], scene_ids, "passages.endpoints.scene_id")
            scene = next(s for s in scenes if s.scene_id == e["scene_id"])
            if scene.grid_json is None:
                _fail(
                    "unsupported_mechanism", "passages.endpoints", "Fine passage requires two grids"
                )
            for field in ("standing_cell", "threshold_cell"):
                _initial_cell(e[field], scene.grid_json, "passages.endpoints")
            from dnd5e_engine.spatial import GridTopology
            from dnd5e_engine.specs import GridScene

            if (
                GridTopology(GridScene(**json.loads(scene.grid_json))).edge_distance(
                    e["standing_cell"], e["threshold_cell"]
                )
                is None
            ):
                _fail(
                    "invalid_reference",
                    "passages.endpoints",
                    "Standing and threshold cells must share a legal step",
                )
            endpoints.append(PassageEndpoint(**e))
        if len(endpoints) != 2:
            _fail("schema_error", "passages.endpoints", "Exactly two distinct endpoints required")
        _unique([e.scene_id for e in endpoints], "passages.endpoints")
        door = p["door_object_id"]
        if door is not None:
            _reference(door, objects, "passages.door_object_id")
            scene, obj = objects[door]
            if scene not in {e.scene_id for e in endpoints} or obj.movable or obj.state is None:
                _fail(
                    "unsupported_mechanism",
                    "passages.door_object_id",
                    "Door must be a fixed endpoint switch",
                )
        definitions.append(PassageDefinition(pid, tuple(endpoints), door))
    _unique([p.passage_id for p in definitions], "passages")
    by_id = {p.passage_id: p for p in definitions}
    used = set()
    for scene in scenes:
        for target, pid in scene.exit_passages:
            _reference(pid, by_id, "scenes.exits.passage_id")
            if {scene.scene_id, target} != {e.scene_id for e in by_id[pid].endpoints}:
                _fail("invalid_reference", "scenes.exits", "Exit must match passage endpoints")
            used.add(pid)
        if scene.grid_json is not None and set(scene.exits) != dict(scene.exit_passages).keys():
            _fail(
                "unsupported_mechanism",
                "scenes.exits",
                "Fine Scene exits require explicit endpoints",
            )
    if used != set(by_id):
        _fail("invalid_reference", "passages", "Every passage needs an explicit directed exit")
    return tuple(definitions)


def _event_definitions(raw, scenes):
    definitions = []
    for index, value in enumerate(_list(raw, "events")):
        path = f"events[{index}]"
        event = _mapping(value, "event_definition_id trigger condition effect signals once", path)
        eid = _identifier(event["event_definition_id"], path, "event")
        if event["once"] != "per_session":
            _fail("unsupported_mechanism", path, "Only once-per-session events")
        trigger = _mapping(event["trigger"], "event_type scene_id", path + ".trigger")
        if trigger["event_type"] not in (
            "scene.entered",
            "scene.flag_changed",
            "object.state_changed",
            "actor.moved",
            "combat.engine_event",
        ):
            _fail("unsupported_mechanism", path, "Unknown authoritative event trigger")
        scene = next((s for s in scenes if s.scene_id == trigger["scene_id"]), None)
        if scene is None:
            _fail("invalid_reference", path, "Unknown event scene")
        condition = _mapping(event["condition"], "kind flag_id value", path + ".condition")
        if condition["kind"] != "flag_equals" or type(condition["value"]) is not bool:
            _fail("unsupported_mechanism", path, "Only typed flag_equals conditions")
        _reference(condition["flag_id"], dict(scene.flags), path)
        effect_data = event["effect"]
        if isinstance(effect_data, dict) and effect_data.get("kind") == "set_scene_flag":
            effect_data = _mapping(effect_data, "kind flag_id value", path + ".effect")
            _reference(effect_data["flag_id"], dict(scene.flags), path)
            if type(effect_data["value"]) is not bool:
                _fail("schema_error", path, "Effect flag must be boolean")
            effect = SetSceneFlag(scene.scene_id, effect_data["flag_id"], effect_data["value"])
        elif isinstance(effect_data, dict) and effect_data.get("kind") == "set_object_state":
            effect_data = _mapping(effect_data, "kind object_id state", path + ".effect")
            _reference(effect_data["object_id"], {o.object_id for o in scene.objects}, path)
            if (
                next(o for o in scene.objects if o.object_id == effect_data["object_id"]).state
                is None
            ):
                _fail("unsupported_mechanism", path, "Object has no switch capability")
            if effect_data["state"] not in ("closed", "open"):
                _fail("unsupported_mechanism", path, "Unknown object state")
            effect = SetObjectState(
                scene.scene_id, effect_data["object_id"], ObjectState(effect_data["state"])
            )
        else:
            _fail("unsupported_mechanism", path, "Only closed world effects; no downstream rules")
        signals = []
        for signal_data in _list(event["signals"], path + ".signals"):
            fields = "signal_id channel content scope"
            if isinstance(signal_data, dict):
                fields += " origin" if "origin" in signal_data else ""
                fields += " stimulus" if "stimulus" in signal_data else ""
            signal_data = _mapping(signal_data, fields, path + ".signals")
            scope = _mapping(signal_data["scope"], "kind scene_id visibility", path + ".scope")
            signal_scene = next((s for s in scenes if s.scene_id == scope["scene_id"]), None)
            if (
                scope["kind"] != "scene"
                or signal_scene is None
                or scope["visibility"] not in ("clear", "blocked", "undetermined")
                or signal_data["channel"] not in ("visual", "auditory")
            ):
                _fail("unsupported_mechanism", path, "Unsupported signal scope/channel")
            origin = None
            if "origin" in signal_data:
                raw = signal_data["origin"]
                fields = "scene_id"
                if isinstance(raw, dict):
                    fields += " entity_id" if "entity_id" in raw else ""
                    fields += " cell" if "cell" in raw else ""
                raw = _mapping(raw, fields, path + ".origin")
                if raw["scene_id"] != signal_scene.scene_id:
                    _fail("invalid_reference", path, "Signal origin must be in its Scene scope")
                entity = raw.get("entity_id")
                if entity is not None:
                    _reference(
                        entity,
                        {*signal_scene.actor_ids, *(o.object_id for o in signal_scene.objects)},
                        path,
                    )
                cell = raw.get("cell")
                if cell is not None:
                    from dnd5e_engine.spatial import canonical_cell_id

                    try:
                        if type(cell) is not str or canonical_cell_id(cell) != cell:
                            raise ValueError("Expected canonical grid cell")
                    except ValueError:
                        _fail("schema_error", path + ".origin.cell", "Expected canonical grid cell")
                origin = SignalOrigin(signal_scene.scene_id, entity, cell)
            stimulus = None
            if "stimulus" in signal_data:
                raw = _mapping(signal_data["stimulus"], "kind", path + ".stimulus")
                allowed = (
                    ("sound",)
                    if signal_data["channel"] == "auditory"
                    else ("ordinary_activity", "light_emission")
                )
                if raw["kind"] not in allowed:
                    _fail("unsupported_mechanism", path + ".stimulus", "Channel/stimulus mismatch")
                stimulus = SignalStimulus(raw["kind"])
            signals.append(
                Signal(
                    _identifier(signal_data["signal_id"], path, "signal"),
                    signal_data["channel"],
                    _text(signal_data["content"], path),
                    SignalScope(signal_scene.scene_id, scope["visibility"]),
                    origin,
                    stimulus,
                )
            )
        _unique([s.signal_id for s in signals], path + ".signals")
        definitions.append(
            EventDefinition(
                eid,
                trigger["event_type"],
                scene.scene_id,
                condition["flag_id"],
                condition["value"],
                effect,
                tuple(signals),
            )
        )
    _unique([e.event_definition_id for e in definitions], "events")
    return tuple(definitions)


def _legacy_audit_foundation(value, *, combat):
    """Opaque old mechanics for authorized audit only; not a new-schema conversion."""
    expected = (
        {"snapshot", "rng_state"}
        if combat
        else {"snapshot", "rng_state", "xp", "additional_scenes"}
    )
    if type(value) is not dict or set(value) != expected:
        _fail("invalid_combat_foundation", "rule_bindings", "Incomplete Legacy audit envelope")
    snapshot = value["snapshot"]
    required = {"snapshot_schema_version", "character_states", "scene_state"} | (
        {"combat_state"} if combat else set()
    )
    if (
        type(snapshot) is not dict
        or not required <= snapshot.keys()
        or snapshot["snapshot_schema_version"] not in ("engine-snapshot/1", "engine-snapshot/2")
    ):
        _fail("unsupported_mechanical_version", "rule_bindings", "Unknown Legacy audit schema")
    if type(snapshot["character_states"]) is not list or any(
        type(a) is not dict
        or not {"entity_id", "entity_type", "hp_current", "hp_max", "carried_item_slugs"}
        <= a.keys()
        for a in snapshot["character_states"]
    ):
        _fail("invalid_combat_foundation", "rule_bindings", "Incomplete Legacy audit identities")
    if (
        type(snapshot["scene_state"]) is not dict
        or not {"scene_id", "grid"} <= snapshot["scene_state"].keys()
    ):
        _fail("invalid_combat_foundation", "rule_bindings", "Incomplete Legacy audit scene")


def load_adventure_package(source: str | Path, *, allow_legacy_audit=False) -> PackageLoadResult:
    """Read one local UTF-8 YAML file; invalid results expose no partial package.

    allow_legacy_audit retains original /1-/2 content and digest for authorized
    audit reads. It never upgrades a Session or admits new gameplay/creation.
    Fail fast with one structured issue. Provenance is metadata, not a license check.
    """
    try:
        with Path(source).open("rb") as stream:
            raw = stream.read(MAX_PACKAGE_BYTES + 1)
        if len(raw) > MAX_PACKAGE_BYTES:
            _fail("package_too_large", str(source), "Maximum package size is 1 MiB")
        data = yaml.load(raw.decode("utf-8"), Loader=_DataLoader)
        _plain(data)
        return PackageLoadResult("valid", _validate(data, allow_legacy_audit=allow_legacy_audit))
    except StateMachineError as exc:
        return PackageLoadResult("invalid", errors=(exc.issue,))
    except (OSError, UnicodeError) as exc:
        return PackageLoadResult("invalid", errors=(Issue("source_error", str(source), str(exc)),))
    except (yaml.YAMLError, ValueError, OverflowError) as exc:
        return PackageLoadResult(
            "invalid", errors=(Issue("unsafe_or_invalid_yaml", str(source), str(exc)),)
        )


def _creation_fingerprint(request_content: dict, binding: RulesetBinding) -> str:
    return _digest(
        {
            "fingerprint_schema": "foundation-create/1",
            **request_content,
            "ruleset_binding": asdict(binding),
        }
    )
