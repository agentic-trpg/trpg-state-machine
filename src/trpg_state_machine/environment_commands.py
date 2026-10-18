"""Closed typed environment mutations and strict data adapter."""

from dataclasses import asdict, dataclass, fields

from .environment_models import (
    CONDITION_TYPES,
    MAX_BATCH,
    EnvironmentArea,
    EnvironmentConditions,
    EnvironmentSceneFacts,
    EnvironmentSource,
    HeightInterval,
    SceneEnvironmentProtection,
    StaticWindProtection,
    identity,
    invalid,
)


@dataclass(frozen=True, slots=True)
class InitializeEnvironment:
    scene_id: str
    facts: EnvironmentSceneFacts


@dataclass(frozen=True, slots=True)
class SetEnvironmentBase:
    scene_id: str
    conditions: EnvironmentConditions


@dataclass(frozen=True, slots=True)
class AddEnvironmentSource:
    scene_id: str
    source: EnvironmentSource


@dataclass(frozen=True, slots=True)
class ReplaceEnvironmentSource:
    scene_id: str
    source: EnvironmentSource


@dataclass(frozen=True, slots=True)
class RemoveEnvironmentSource:
    scene_id: str
    source_id: str


@dataclass(frozen=True, slots=True)
class SetEnvironmentProtection:
    scene_id: str
    protection: SceneEnvironmentProtection


EnvironmentMutation = (
    InitializeEnvironment
    | SetEnvironmentBase
    | AddEnvironmentSource
    | ReplaceEnvironmentSource
    | RemoveEnvironmentSource
    | SetEnvironmentProtection
)


@dataclass(frozen=True, slots=True)
class BatchEnvironment:
    changes: tuple[EnvironmentMutation, ...]


ENVIRONMENT_COMMANDS = {
    "world.initialize_environment": InitializeEnvironment,
    "world.set_environment_base": SetEnvironmentBase,
    "world.add_environment_source": AddEnvironmentSource,
    "world.replace_environment_source": ReplaceEnvironmentSource,
    "world.remove_environment_source": RemoveEnvironmentSource,
    "world.set_environment_protection": SetEnvironmentProtection,
    "world.batch_environment": BatchEnvironment,
}
ENVIRONMENT_OPERATIONS = tuple(ENVIRONMENT_COMMANDS.values())


def validate_environment_operation(op):
    if type(op) is BatchEnvironment:
        if type(op.changes) is not tuple or not 1 <= len(op.changes) <= MAX_BATCH:
            invalid("Batch requires 1..16 immutable mutations")
        for change in op.changes:
            if type(change) is BatchEnvironment:
                invalid("Nested batches unsupported")
            validate_environment_operation(change)
        return
    if type(op) not in ENVIRONMENT_OPERATIONS:
        invalid("Unknown environment mutation")
    identity(op.scene_id)
    expected = {
        InitializeEnvironment: ("facts", EnvironmentSceneFacts),
        SetEnvironmentBase: ("conditions", EnvironmentConditions),
        AddEnvironmentSource: ("source", EnvironmentSource),
        ReplaceEnvironmentSource: ("source", EnvironmentSource),
        SetEnvironmentProtection: ("protection", SceneEnvironmentProtection),
    }.get(type(op))
    if expected and type(getattr(op, expected[0])) is not expected[1]:
        invalid("Invalid typed mutation value")
    if type(op) is RemoveEnvironmentSource:
        identity(op.source_id)


def environment_operation_to_data(op):
    """Canonical tagged wire encoding; preserve mutation identity AND list order.

    The existing registry is the sole type/name authority. Single payloads keep
    their historical asdict shape; only batches need recursive union tags.
    """
    validate_environment_operation(op)
    kind = next(name for name, cls in ENVIRONMENT_COMMANDS.items() if type(op) is cls)
    payload = (
        {"changes": [environment_operation_to_data(change) for change in op.changes]}
        if type(op) is BatchEnvironment
        else asdict(op)
    )
    return {"command_type": kind, "payload": payload}


def _decode(cls, raw, converters=None):
    if type(raw) is not dict or set(raw) - {f.name for f in fields(cls)}:
        invalid("Unknown environment mapping fields")
    values = dict(raw)
    for name, convert in (converters or {}).items():
        if name in values and values[name] is not None:
            values[name] = convert(values[name])
    try:
        return cls(**values)
    except (TypeError, ValueError, KeyError) as exc:
        invalid(f"Invalid {cls.__name__}: {exc}")


def _tuple(raw, convert, maximum):
    if type(raw) not in (list, tuple) or len(raw) > maximum:
        invalid("Invalid bounded environment sequence")
    return tuple(convert(v) for v in raw)


def conditions_from_data(raw):
    return _decode(
        EnvironmentConditions,
        raw,
        {key: lambda v, cls=cls: _decode(cls, v) for key, cls in CONDITION_TYPES.items()},
    )


def area_from_data(raw):
    return _decode(
        EnvironmentArea,
        raw,
        {
            "cells": lambda v: _tuple(v, lambda c: c, 256),
            "heights": lambda v: _decode(HeightInterval, v),
        },
    )


def source_from_data(raw):
    return _decode(
        EnvironmentSource,
        raw,
        {
            "conditions": conditions_from_data,
            "area": area_from_data,
        },
    )


def protection_from_data(raw):
    return _decode(
        SceneEnvironmentProtection,
        raw,
        {
            "rain_cells": lambda v: _tuple(v, lambda c: c, 256),
            "wind": lambda v: _tuple(
                v,
                lambda w: _decode(
                    StaticWindProtection,
                    w,
                    {
                        "heights": lambda h: _decode(HeightInterval, h),
                    },
                ),
                256,
            ),
        },
    )


def facts_from_data(raw):
    return _decode(
        EnvironmentSceneFacts,
        raw,
        {
            "base": conditions_from_data,
            "sources": lambda v: _tuple(v, source_from_data, 64),
            "protection": protection_from_data,
        },
    )


def environment_operation_from_data(kind, raw):
    if type(kind) is not str:
        invalid("Environment command type must be a string")
    cls = ENVIRONMENT_COMMANDS.get(kind)
    if cls is None:
        invalid("Unknown environment command")
    if cls is BatchEnvironment:

        def mutation(v):
            if type(v) is not dict or set(v) != {"command_type", "payload"}:
                invalid("Batch wire mutations require command_type and payload")
            if v["command_type"] == "world.batch_environment":
                invalid("Nested batch unsupported")
            return environment_operation_from_data(v["command_type"], v["payload"])

        op = _decode(cls, raw, {"changes": lambda v: _tuple(v, mutation, MAX_BATCH)})
    else:
        op = _decode(
            cls,
            raw,
            {
                "facts": facts_from_data,
                "conditions": conditions_from_data,
                "source": source_from_data,
                "protection": protection_from_data,
            },
        )
    validate_environment_operation(op)
    return op
