from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest
import yaml

from trpg_state_machine import StateMachineError, load_adventure_package
from trpg_state_machine.package import MAX_PACKAGE_BYTES, SYNTHETIC_RULESET


def write_data(tmp_path, data):
    path = tmp_path / "package.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")
    return path


def test_valid_package_is_immutable_and_pinned(package):
    assert package.adventure_id == "example.lantern_station"
    assert package.ruleset_binding == SYNTHETIC_RULESET
    assert package.package_digest.startswith("sha256:")
    assert len(package.package_digest) == 71
    assert len(package.pc_templates) == 2
    assert package.scene("scene.platform").objects[0].state == "closed"
    with pytest.raises(FrozenInstanceError):
        package.scenes[0].scene_id = "scene.changed"
    with pytest.raises(StateMachineError, match="Unknown scene"):
        package.scene("scene.unknown")


def test_digest_uses_semantics_and_includes_metadata(tmp_path, package, package_data):
    path = write_data(tmp_path, package_data)
    assert load_adventure_package(path).package.package_digest == package.package_digest
    # Whitespace, key order and YAML comments do not change approved content.
    path.write_text("# reordered\n" + yaml.safe_dump(package_data, sort_keys=True), "utf-8")
    assert load_adventure_package(path).package.package_digest == package.package_digest
    package_data["manifest"]["title"] = "A changed title"
    assert (
        load_adventure_package(write_data(tmp_path, package_data)).package.package_digest
        != package.package_digest
    )


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("manifest", "schema_version"), "future/2", "unsupported_schema"),
        (("manifest", "adventure_id"), "Invalid ID", "invalid_id"),
        (("manifest", "package_version"), "", "schema_error"),
        (("manifest", "entry_scene_id"), "scene.missing", "invalid_reference"),
        (("manifest", "ruleset_binding", "data_revision"), "different/1", "unsupported_ruleset"),
        (("manifest", "ruleset_binding", "evaluator_version"), "engine/1", "unsupported_ruleset"),
        (("manifest", "ruleset_binding", "ruleset_id"), "dnd.2024", "unsupported_ruleset"),
        (("manifest", "player_setup", "pc_count"), True, "unsupported_player_setup"),
        (("manifest", "player_setup", "player_count"), 2, "unsupported_player_setup"),
        (("manifest", "content_provenance", "license"), "", "schema_error"),
        (
            ("manifest", "compatibility", "unresolved_issues"),
            ["unreviewed"],
            "unsupported_mechanism",
        ),
        (("world", "starting_location_id"), "location.missing", "invalid_reference"),
        (("world", "initial_flags", "flag.station_ready"), 1, "schema_error"),
        (("world", "world_facts"), [{"secret": "hidden"}], "unsupported_field"),
        (("scenes", 0, "scene_id"), "npc.wrong", "invalid_id"),
        (("scenes", 0, "location_id"), "location.missing", "invalid_reference"),
        (("scenes", 0, "actor_ids"), ["npc.missing"], "invalid_reference"),
        (("scenes", 0, "exits", 0, "to_scene_id"), "scene.missing", "invalid_reference"),
        (("scenes", 0, "exits"), [], "unreachable_scene"),
        (("scenes", 0, "objects", 0, "state"), "exploded", "unsupported_mechanism"),
        (("scenes", 0, "access_policy"), "public", "unsupported_mechanism"),
        (("scenes", 0, "event_definition_ids"), ["event.unknown"], "invalid_reference"),
        (("actors", 0, "initial_scene_id"), "scene.missing", "invalid_reference"),
        (("actors", 0, "controller"), {"kind": "unknown_policy"}, "unsupported_mechanism"),
        (("pc_templates",), [], "schema_error"),
        (("pc_templates", 0, "character", "hp", "current"), 9, "schema_error"),
        (("pc_templates", 0, "character", "hp", "max"), True, "schema_error"),
        (("pc_templates", 0, "character", "resources"), {"slot": 1}, "unsupported_mechanism"),
        (("events",), [{"script": "eval(...)"}], "unsupported_field"),
        (("rule_bindings",), {"unknown": "binding"}, "unsupported_mechanism"),
    ],
)
def test_invalid_fields_return_structured_errors(tmp_path, package_data, path, value, code):
    node = package_data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    result = load_adventure_package(write_data(tmp_path, package_data))
    assert result.status == "invalid"
    assert result.package is None
    assert result.errors[0].code == code
    assert result.errors[0].path
    assert result.errors[0].message


@pytest.mark.parametrize("collection", ["scenes", "actors", "pc_templates"])
def test_duplicate_definition_ids_rejected(tmp_path, package_data, collection):
    package_data[collection].append(deepcopy(package_data[collection][0]))
    result = load_adventure_package(write_data(tmp_path, package_data))
    assert result.status == "invalid"
    assert result.errors[0].code == "duplicate_id"


def test_duplicate_object_ids_across_scenes_rejected(tmp_path, package_data):
    package_data["scenes"][1]["objects"][0]["object_id"] = "object.platform_gate"
    assert (
        load_adventure_package(write_data(tmp_path, package_data)).errors[0].code == "duplicate_id"
    )


@pytest.mark.parametrize(
    ("field", "code"), [("title", "missing_field"), ("script", "unsupported_field")]
)
def test_missing_and_extra_fields(tmp_path, package_data, field, code):
    if field == "title":
        del package_data["manifest"][field]
    else:
        package_data["manifest"][field] = "untrusted"
    assert load_adventure_package(write_data(tmp_path, package_data)).errors[0].code == code


@pytest.mark.parametrize(
    "text",
    [
        "!!python/object/apply:os.system ['echo untrusted']",
        "a: 1\na: 2\n",
        "a: &a [*a]\n",
        "? [a, b]\n: value\n",
        "a: [broken\n",
        "a: &source {}\nb: {<<: *source}\n",
        "x: " + "[" * 40 + "0" + "]" * 40,
        "x: !!int not_an_integer",
        "x: 2026-99-99",
        "x: " + "1" * 5000,
    ],
)
def test_unsafe_or_malformed_yaml_is_rejected(tmp_path, text):
    path = tmp_path / "unsafe.yaml"
    path.write_text(text, "utf-8")
    result = load_adventure_package(path)
    assert result.status == "invalid"
    assert result.package is None
    assert result.errors[0].code == "unsafe_or_invalid_yaml"


@pytest.mark.parametrize("text", ["x: 2026-10-10", "x: 1.5", "x: !!binary aGVsbG8="])
def test_non_plain_scalars_rejected(tmp_path, text):
    path = tmp_path / "scalar.yaml"
    path.write_text(text, "utf-8")
    assert load_adventure_package(path).errors[0].code == "unsupported_value"


def test_source_errors_and_size_limit(tmp_path):
    assert load_adventure_package(tmp_path / "missing.yaml").errors[0].code == "source_error"
    path = tmp_path / "bad.yaml"
    path.write_bytes(b"\xff\xfe")
    assert load_adventure_package(path).errors[0].code == "source_error"
    path.write_bytes(b" " * (MAX_PACKAGE_BYTES + 1))
    assert load_adventure_package(path).errors[0].code == "package_too_large"
