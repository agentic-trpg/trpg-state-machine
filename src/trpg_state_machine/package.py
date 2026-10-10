"""Strict, immutable, deliberately synthetic-only Adventure Package subset."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import yaml

from ._operations import ObjectState, SetObjectState, SetSceneFlag
from .errors import Issue, StateMachineError

SCHEMA_VERSION = "state-machine-foundation/1"
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
    state: Literal["closed", "open"]


@dataclass(frozen=True, slots=True)
class SceneDefinition:
    scene_id: str
    location_id: str
    exits: tuple[str, ...]
    flags: tuple[tuple[str, bool], ...]
    actor_ids: tuple[str, ...]
    objects: tuple[ObjectDefinition, ...]


@dataclass(frozen=True, slots=True)
class ActorDefinition:
    actor_id: str
    name: str
    initial_scene_id: str
    senses: tuple[tuple[str, bool], ...] | None = None


@dataclass(frozen=True, slots=True)
class PCTemplate:
    pc_template_id: str
    name: str
    current_hp: int
    max_hp: int
    resources: tuple[tuple[str, int], ...] = ()
    inventory: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SignalScope:
    scene_id: str
    visibility: str
    kind: str = "scene"


@dataclass(frozen=True, slots=True)
class Signal:
    signal_id: str
    channel: str
    content: str
    scope: SignalScope


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


def _validate(data: dict) -> AdventurePackage:
    root = _mapping(data, "manifest world scenes actors pc_templates events rule_bindings", "$")
    m = _mapping(
        root["manifest"],
        "schema_version adventure_id package_version title entry_scene_id "
        "ruleset_binding player_setup content_provenance compatibility",
        "manifest",
    )
    if m["schema_version"] != SCHEMA_VERSION:
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
    if dnd and (
        not isinstance(binding.data_revision, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", binding.data_revision)
        or binding.evaluator_version not in ("dnd5e-evaluation/4", "dnd5e-evaluation/8")
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
            validate_foundation(rules["combat_foundation"])
            foundation = canonical_json(rules["combat_foundation"])
        if "noncombat_foundation" in rules:
            if binding.evaluator_version != "dnd5e-evaluation/8":
                _fail("unsupported_ruleset", "rule_bindings", "Noncombat requires evaluator /8")
            validate_noncombat_foundation(rules["noncombat_foundation"])
            noncombat = canonical_json(rules["noncombat_foundation"])
    else:
        _empty(root["rule_bindings"], {}, "rule_bindings")

    world = _mapping(
        root["world"], "locations starting_location_id initial_flags world_facts", "world"
    )
    _empty(world["world_facts"], [], "world.world_facts")
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
            "event_definition_ids access_policy",
            path,
        )
        key = _identifier(s["scene_id"], path + ".scene_id", "scene")
        location = _identifier(s["location_id"], path + ".location_id", "location")
        _text(s["description"], path + ".description")
        if s["access_policy"] != "internal_only":
            _fail(
                "unsupported_mechanism",
                path + ".access_policy",
                "No player/NPC projection API in this batch",
            )
        _list(s["event_definition_ids"], path + ".event_definition_ids")
        initial = _mapping(s["initial_state"], "local_flags", path + ".initial_state")
        exits = []
        for j, value in enumerate(_list(s["exits"], path + ".exits")):
            ex = _mapping(value, "to_scene_id", f"{path}.exits[{j}]")
            exits.append(_identifier(ex["to_scene_id"], path + ".exits", "scene"))
        _unique(exits, path + ".exits")
        actors = tuple(
            _identifier(v, path + ".actor_ids", "npc")
            for v in _list(s["actor_ids"], path + ".actor_ids")
        )
        _unique(actors, path + ".actor_ids")
        objects = []
        for j, value in enumerate(_list(s["objects"], path + ".objects")):
            ob = _mapping(value, "object_id state", f"{path}.objects[{j}]")
            oid = _identifier(ob["object_id"], path + ".objects", "object")
            if ob["state"] not in ("closed", "open"):
                _fail(
                    "unsupported_mechanism", path + ".objects", "Only closed/open synthetic objects"
                )
            objects.append(ObjectDefinition(oid, ob["state"]))
        scenes.append(
            SceneDefinition(
                key,
                location,
                tuple(exits),
                _flags(initial["local_flags"], path + ".initial_state.local_flags"),
                actors,
                tuple(objects),
            )
        )
    scene_ids = {s.scene_id for s in scenes}
    _unique([s.scene_id for s in scenes], "scenes")
    _unique([o.object_id for s in scenes for o in s.objects], "scenes.objects")
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
        a = _mapping(value, fields, path)
        key = _identifier(a["actor_id"], path + ".actor_id", "npc")
        initial_scene = _identifier(a["initial_scene_id"], path + ".initial_scene_id", "scene")
        _reference(initial_scene, scene_ids, path + ".initial_scene_id")
        if a["kind"] != "npc" or a["controller"] != {"kind": "scripted_event"}:
            _fail(
                "unsupported_mechanism", path, "Only inert synthetic NPCs; no controller execution"
            )
        if key not in next(s for s in scenes if s.scene_id == initial_scene).actor_ids:
            _fail("invalid_reference", path, "Actor must be listed in its initial scene")
        senses = None
        if "senses" in a:
            sense_data = _mapping(a["senses"], "visual auditory", path + ".senses")
            if any(type(v) is not bool for v in sense_data.values()):
                _fail("schema_error", path + ".senses", "Senses must be explicit booleans")
            senses = tuple(sorted(sense_data.items()))
        actors.append(ActorDefinition(key, _text(a["name"], path + ".name"), initial_scene, senses))
    _unique([a.actor_id for a in actors], "actors")
    for scene in scenes:
        for actor in scene.actor_ids:
            _reference(actor, {a.actor_id for a in actors}, f"scenes.{scene.scene_id}.actor_ids")

    templates = []
    for i, value in enumerate(_list(root["pc_templates"], "pc_templates", nonempty=True)):
        path = f"pc_templates[{i}]"
        pc = _mapping(value, "pc_template_id name character", path)
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
            )
        )
    _unique([pc.pc_template_id for pc in templates], "pc_templates")
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
    if noncombat is not None:
        initial = json.loads(noncombat)
        snapshot = initial["snapshot"]
        if snapshot["scene_state"]["scene_id"] != entry:
            _fail("invalid_reference", "rule_bindings.noncombat_foundation", "Expected entry scene")
        for scene in initial["additional_scenes"]:
            _reference(scene["scene_id"], scene_ids, "rule_bindings.noncombat_foundation.scenes")
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
            ):
                _fail("invalid_reference", "rule_bindings", "Initial mechanical facts disagree")
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
    )


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
            if effect_data["state"] not in ("closed", "open"):
                _fail("unsupported_mechanism", path, "Unknown object state")
            effect = SetObjectState(
                scene.scene_id, effect_data["object_id"], ObjectState(effect_data["state"])
            )
        else:
            _fail("unsupported_mechanism", path, "Only closed world effects; no downstream rules")
        signals = []
        for signal_data in _list(event["signals"], path + ".signals"):
            signal_data = _mapping(
                signal_data, "signal_id channel content scope", path + ".signals"
            )
            scope = _mapping(signal_data["scope"], "kind scene_id visibility", path + ".scope")
            if (
                scope["kind"] != "scene"
                or scope["scene_id"] != scene.scene_id
                or scope["visibility"] not in ("clear", "blocked", "undetermined")
                or signal_data["channel"] not in ("visual", "auditory")
            ):
                _fail("unsupported_mechanism", path, "Unsupported signal scope/channel")
            signals.append(
                Signal(
                    _identifier(signal_data["signal_id"], path, "signal"),
                    signal_data["channel"],
                    _text(signal_data["content"], path),
                    SignalScope(scene.scene_id, scope["visibility"]),
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


def load_adventure_package(source: str | Path) -> PackageLoadResult:
    """Read one local UTF-8 YAML file; invalid results expose no partial package.

    Fail fast with one structured issue. Provenance is metadata, not a license check.
    """
    try:
        with Path(source).open("rb") as stream:
            raw = stream.read(MAX_PACKAGE_BYTES + 1)
        if len(raw) > MAX_PACKAGE_BYTES:
            _fail("package_too_large", str(source), "Maximum package size is 1 MiB")
        data = yaml.load(raw.decode("utf-8"), Loader=_DataLoader)
        _plain(data)
        return PackageLoadResult("valid", _validate(data))
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
