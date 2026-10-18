"""Closed, immutable mechanism declarations and WorldFacts runtime projection."""

from dataclasses import dataclass
from typing import Literal, get_args

from dnd5e_engine.rules.skills import Skill

from .errors import StateMachineError
from .mechanism_effects import RuleEffectAction, RuleEffectDefinition


@dataclass(frozen=True, slots=True)
class MechanismAction:
    kind: Literal["set_open_state", "set_light_enabled"]
    target_object_id: str
    value: Literal["open", "closed"] | bool


@dataclass(frozen=True, slots=True)
class MechanismOperation:
    operation_id: str
    actions: tuple[MechanismAction | RuleEffectAction, ...]
    consume_on_noop: bool


@dataclass(frozen=True, slots=True)
class MechanismTrigger:
    kind: Literal["enter_region", "object_operation", "check_result"]
    scene_id: str
    operation_id: str
    cells: tuple[str, ...] = ()
    target_object_id: str | None = None
    event_type: str | None = None
    succeeded: bool | None = None
    value: Literal["open", "closed"] | bool | None = None


@dataclass(frozen=True, slots=True)
class MechanismMethod:
    method_id: str
    operation: Literal["disarm", "block", "unblock", "prepare", "reset"]
    required_tool: str | None = None
    ability: str | None = None
    skill: Skill | None = None
    dc: int | None = None
    trigger_on_failure: str | None = None


@dataclass(frozen=True, slots=True)
class Mechanism:
    trigger_kind: Literal["manual", "configured"]
    trigger_conditions: Literal["actor_adjacent", "configured"]
    initial_status: Literal["armed", "unprepared"]
    reset_policy: Literal["none", "manual", "after_seconds"]
    actions: tuple[MechanismAction, ...]
    kind: str = "mechanism"
    schema_version: Literal["mechanism/1", "mechanism/2", "mechanism/3"] = "mechanism/1"
    operations: tuple[MechanismOperation, ...] = ()
    triggers: tuple[MechanismTrigger, ...] = ()
    methods: tuple[MechanismMethod, ...] = ()
    max_uses: int | None = 1
    frequency: Literal["unlimited", "once_per_actor"] = "unlimited"
    placement_policy: Literal["ground_prepared", "accessible"] = "accessible"
    reset_delay_seconds: int | None = None
    components: tuple[str, ...] = ()
    destruction_policy: Literal["disable"] = "disable"
    rule_effects: tuple[RuleEffectDefinition, ...] = ()

    def operation(self, identity):
        if self.schema_version == "mechanism/1" and identity == "activate":
            return MechanismOperation("activate", self.actions, True)
        value = next((op for op in self.operations if op.operation_id == identity), None)
        if value is None:
            raise StateMachineError("unsupported_mechanism_operation", "Undeclared operation")
        return value

    def all_actions(self):
        return self.actions + tuple(a for op in self.operations for a in op.actions)


@dataclass(frozen=True, slots=True)
class MechanismState:
    status: Literal["armed", "spent", "unprepared", "blocked", "disarmed", "broken"]
    remaining_uses: int | None
    activation_count: int
    reset_at_tick: int | None
    resume_status: Literal["armed", "spent", "unprepared"] | None


def parse_mechanism(config, objects, scenes, *, enabled):
    # Local import avoids the Package validator's definition import cycle.
    from .package import _fail, _list, _mapping, _reference, _text, _unique

    p = "world_objects.mechanism"
    if not enabled:
        _fail("schema_error", p, "mechanism/2 requires Package /10")
    version = config.get("schema_version")
    c = _mapping(
        config,
        "kind schema_version initial_status operations triggers methods "
        "max_uses reset_policy reset_delay_seconds frequency placement_policy "
        "components destruction_policy" + (" rule_effects" if version == "mechanism/3" else ""),
        p,
    )
    if c["schema_version"] not in ("mechanism/2", "mechanism/3"):
        _fail("unsupported_mechanism", p, "Unknown mechanism definition version")
    from pydantic import TypeAdapter, ValidationError

    from .package import canonical_json

    effects = []
    for raw in _list(c.get("rule_effects", []), p):
        try:
            effects.append(TypeAdapter(RuleEffectDefinition).validate_json(canonical_json(raw)))
        except (ValidationError, ValueError, TypeError) as exc:
            raise StateMachineError(
                "unsupported_mechanism_effect_definition", "Invalid typed effect parameters"
            ) from exc
    _unique([e.effect_ref for e in effects], p)
    if len(effects) > 16:
        _fail("schema_error", p, "At most sixteen rule effect definitions")
    if c["initial_status"] not in ("armed", "unprepared"):
        _fail("schema_error", p, "Invalid initial readiness")
    if c["max_uses"] is not None and (
        type(c["max_uses"]) is not int or not 1 <= c["max_uses"] <= 2147483647
    ):
        _fail("schema_error", p, "Uses must be a positive bounded integer or null")
    if c["reset_policy"] not in ("none", "manual", "after_seconds"):
        _fail("unsupported_mechanism", p, "Reset boundary unsupported; Turn belongs to Combat")
    delay = c["reset_delay_seconds"]
    if c["reset_policy"] == "after_seconds":
        if type(delay) is not int or not 1 <= delay <= 2147483647:
            _fail("schema_error", p, "Reset needs a positive duration in seconds")
    elif delay is not None:
        _fail("schema_error", p, "Unexpected reset duration")
    if c["frequency"] not in ("unlimited", "once_per_actor"):
        _fail("unsupported_mechanism", p, "Turn frequency requires Combat admission")
    if c["placement_policy"] not in ("ground_prepared", "accessible"):
        _fail("unsupported_mechanism", p, "Unsupported placement policy")
    if c["destruction_policy"] != "disable":
        _fail("unsupported_mechanism", p, "Only explicit disable on component destruction")
    operations = []
    for value in _list(c["operations"], p, nonempty=True):
        op = _mapping(value, "operation_id actions consume_on_noop", p)
        _text(op["operation_id"], p)
        if len(op["operation_id"]) > 64 or type(op["consume_on_noop"]) is not bool:
            _fail("schema_error", p, "Invalid operation identity or noop policy")
        actions = []
        for value in _list(op["actions"], p, nonempty=True):
            if (
                version == "mechanism/3"
                and isinstance(value, dict)
                and value.get("kind") == "rule_effect"
            ):
                try:
                    action = TypeAdapter(RuleEffectAction).validate_json(canonical_json(value))
                except (ValidationError, TypeError, ValueError) as exc:
                    raise StateMachineError(
                        "invalid_mechanism_targets", "Invalid effect selector"
                    ) from exc
                _reference(action.effect_ref, {e.effect_ref for e in effects}, p)
                # Area is absolute in the instance's declared scene, never an
                # untrusted client target list. Runtime rechecks a portable pose.
                for cell in action.targets.cells:
                    from dnd5e_engine.spatial import canonical_cell_id

                    try:
                        if canonical_cell_id(cell) != cell:
                            raise ValueError("Noncanonical cell")
                    except (TypeError, ValueError, AttributeError) as exc:
                        raise StateMachineError(
                            "invalid_mechanism_targets", "Invalid area cell"
                        ) from exc
                actions.append(action)
                continue
            a = _mapping(value, "kind target_object_id value", p)
            _reference(a["target_object_id"], objects, p)
            if not (
                a["kind"] == "set_open_state"
                and a["value"] in ("open", "closed")
                or a["kind"] == "set_light_enabled"
                and type(a["value"]) is bool
            ):
                _fail("unsupported_mechanism", p, "Unsupported consequence; rules.effect is OPEN")
            actions.append(MechanismAction(**a))
        if len(actions) > 8:
            _fail("unsupported_mechanism", p, "At most eight synchronous consequences")
        operations.append(MechanismOperation(**{**op, "actions": tuple(actions)}))
    if len(operations) > 16:
        _fail("schema_error", p, "At most sixteen operations")
    ids = [op.operation_id for op in operations]
    _unique(ids, p)
    triggers = []
    scene_map = {s.scene_id: s for s in scenes}
    object_events = (
        "object.state_changed",
        "object.light_changed",
        "door.lock_changed",
        "object.bar_changed",
        "object.jam_cleared",
        "item.picked_up",
        "item.dropped",
        "item.container_transferred",
    )
    for value in _list(c["triggers"], p):
        t = _mapping(
            {"value": None, **value} if type(value) is dict else value,
            "kind scene_id operation_id cells target_object_id event_type succeeded value",
            p,
        )
        _reference(t["scene_id"], scene_map, p)
        _reference(t["operation_id"], ids, p)
        cells = _list(t["cells"], p)
        if t["kind"] == "enter_region":
            import json

            from dnd5e_engine.spatial import canonical_cell_id

            grid = scene_map[t["scene_id"]].grid_json
            if grid is None or not cells:
                _fail("schema_error", p, "Region requires a nonempty local grid region")
            grid = json.loads(grid)
            for cell in cells:
                try:
                    x, y = map(int, canonical_cell_id(cell).split(","))
                    valid = (
                        canonical_cell_id(cell) == cell
                        and 0 <= x < grid["width"]
                        and 0 <= y < grid["height"]
                    )
                except (TypeError, ValueError, AttributeError):
                    valid = False
                if not valid:
                    _fail("schema_error", p, "Invalid region cell")
            if any(
                t[k] is not None for k in ("target_object_id", "event_type", "succeeded", "value")
            ):
                _fail("schema_error", p, "Region has unexpected selectors")
        elif t["kind"] == "object_operation":
            _reference(t["target_object_id"], objects, p)
            if t["event_type"] not in object_events or cells or t["succeeded"] is not None:
                _fail("unsupported_mechanism", p, "Unsupported object operation selector")
            if t["value"] is not None and not (
                t["event_type"] == "object.state_changed"
                and t["value"] in ("open", "closed")
                or t["event_type"]
                in ("object.light_changed", "door.lock_changed", "object.bar_changed")
                and type(t["value"]) is bool
            ):
                _fail("schema_error", p, "Invalid operation value selector")
        elif t["kind"] == "check_result":
            _reference(t["target_object_id"], objects, p)
            if (
                cells
                or t["event_type"] is not None
                or type(t["succeeded"]) is not bool
                or t["value"] is not None
            ):
                _fail("schema_error", p, "Check selector needs explicit target and success")
        else:
            _fail("unsupported_mechanism", p, "Unknown automatic trigger")
        triggers.append(MechanismTrigger(**{**t, "cells": tuple(cells)}))
    if len(triggers) > 16:
        _fail("schema_error", p, "At most sixteen triggers")
    methods = []
    for value in _list(c["methods"], p):
        m = _mapping(
            value, "method_id operation required_tool ability skill dc trigger_on_failure", p
        )
        _text(m["method_id"], p)
        if m["operation"] not in ("disarm", "block", "unblock", "prepare", "reset"):
            _fail("unsupported_mechanism", p, "Unknown lifecycle operation")
        if m["required_tool"] is not None:
            _text(m["required_tool"], p)
        if m["ability"] is not None:
            if (
                m["ability"] not in ("str", "dex", "con", "int", "wis", "cha")
                or type(m["dc"]) is not int
                or not 0 <= m["dc"] <= 2147483647
            ):
                _fail("schema_error", p, "Check requires explicit ability and DC")
            # Generic RE CheckPayload validates skill compatibility in prepare.
            if m["skill"] is not None and m["skill"] not in get_args(Skill):
                _fail("schema_error", p, "Unknown skill")
        elif m["dc"] is not None or m["skill"] is not None or m["trigger_on_failure"] is not None:
            _fail("schema_error", p, "Deterministic method has unexpected check fields")
        if m["trigger_on_failure"] is not None:
            _reference(m["trigger_on_failure"], ids, p)
        methods.append(MechanismMethod(**m))
    if len(methods) > 16:
        _fail("schema_error", p, "At most sixteen methods")
    _unique([m.method_id for m in methods], p)
    if c["reset_policy"] == "manual" and not any(m.operation == "reset" for m in methods):
        _fail("schema_error", p, "Manual reset requires a declared method")
    if c["initial_status"] == "unprepared" and not any(m.operation == "prepare" for m in methods):
        _fail("schema_error", p, "Unprepared instance requires a declared prepare method")
    components = _list(c["components"], p)
    for identity in components:
        _reference(identity, objects, p)
    _unique(components, p)
    if len(components) > 16:
        _fail("schema_error", p, "At most sixteen components")
    return Mechanism(
        trigger_kind="configured",
        trigger_conditions="configured",
        actions=(),
        **{
            **c,
            "operations": tuple(operations),
            "triggers": tuple(triggers),
            "methods": tuple(methods),
            "components": tuple(components),
            "rule_effects": tuple(effects),
        },
    )
