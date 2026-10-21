"""Domain-owned RAM fields, staged by the existing SessionMemory publication.

Owners and components are immutable replacements, including within savepoints.
They contain values or stable IDs, never a second writable copy of domain facts.
No validity key enumerates entities or serializes a candidate/snapshot.
"""

from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass, replace
from functools import wraps


@dataclass(frozen=True)
class FieldValue:
    parameter: object
    value: object
    dependencies: tuple
    stamps: tuple


@dataclass(frozen=True)
class ContainerFields:
    contents: tuple = ()
    own_weight: tuple = ()
    total_weight: tuple = ()
    contents_weight: tuple = ()


@dataclass(frozen=True)
class MechanismFields:
    configuration: tuple = ()
    index: tuple = ()


@dataclass(frozen=True)
class MechanismLinks:
    triggers: tuple
    components: tuple


@dataclass(frozen=True)
class SpatialFields:
    map: tuple = ()
    mechanical_map: tuple = ()
    occupants: tuple = ()
    object_positions: tuple = ()
    cells: tuple = ()
    blockers: tuple = ()


@dataclass(frozen=True)
class EnvironmentFields:
    facts: tuple = ()
    inputs: tuple = ()
    protection: tuple = ()
    cells: tuple = ()
    ranges: tuple = ()


@dataclass(frozen=True)
class LightingFields:
    sources: tuple = ()
    geometry: tuple = ()
    cells: tuple = ()
    projection: tuple = ()


@dataclass(frozen=True)
class ObjectDirectory:
    direct: tuple = ()
    contributors: tuple = ()


@dataclass(frozen=True)
class ObjectRuntime:
    session_id: str
    object_id: str
    facts: tuple = ()
    definition: tuple = ()
    container: ContainerFields = field(default_factory=ContainerFields)
    mechanism: MechanismFields = field(default_factory=MechanismFields)


@dataclass(frozen=True)
class SceneRuntime:
    session_id: str
    scene_id: str
    spatial: SpatialFields = field(default_factory=SpatialFields)
    environment: EnvironmentFields = field(default_factory=EnvironmentFields)
    lighting: LightingFields = field(default_factory=LightingFields)
    mechanisms: MechanismFields = field(default_factory=MechanismFields)
    objects: ObjectDirectory = field(default_factory=ObjectDirectory)


@dataclass(frozen=True)
class WorldRuntime:
    session_id: str
    mechanisms: MechanismFields = field(default_factory=MechanismFields)


def resolve_field(
    store,
    kind,
    sid,
    identity,
    component,
    name,
    parameter,
    compute,
    *,
    capacity=1,
    versioned_parameters=False,
):
    """Resolve one explicit owner field in the caller's consistent candidate scope."""
    if store._scope is None:
        with store.transaction():
            return resolve_field(
                store,
                kind,
                sid,
                identity,
                component,
                name,
                parameter,
                compute,
                capacity=capacity,
                versioned_parameters=versioned_parameters,
            )
    memory = store.memory
    memory.guard(sid)
    store._scope.memory_sessions.add(sid)
    key = ("instance", sid, kind, identity)

    def owner():
        cached = memory.get(key)
        return (
            cached[0]
            if cached
            else (
                ObjectRuntime(sid, identity)
                if kind == "object"
                else SceneRuntime(sid, identity)
                if kind == "scene"
                else WorldRuntime(sid)
            )
        )

    current = owner()
    group = getattr(current, component) if component else current
    entries = getattr(group, name)
    for entry in entries:
        if entry.parameter == parameter and entry.stamps == tuple(
            memory.version(d) for d in entry.dependencies
        ):
            for dependency in entry.dependencies:
                memory.dependency(*dependency)
            memory.stats["instance_hits"] += 1
            return deepcopy(entry.value)
    dependencies = set()
    memory.collectors.append(dependencies)
    try:
        memory.stats["instance_builds"] += 1
        value = compute()
    finally:
        memory.collectors.pop()
    # Environment's registered provider/source/protection protocol already owns
    # complete validity. Incidental decoder reads must not replace that protocol
    # with broader invalidation (e.g. lock metadata is not rain protection).
    dependencies = () if versioned_parameters else tuple(sorted(dependencies, key=repr))
    entry = FieldValue(
        parameter, value, dependencies, tuple(memory.version(d) for d in dependencies)
    )
    # Nested computations may have filled other fields of this same owner.
    current = owner()
    group = getattr(current, component) if component else current
    entries = (entry, *(e for e in getattr(group, name) if e.parameter != parameter))[:capacity]
    group = replace(group, **{name: entries})
    current = replace(current, **{component: group}) if component else group

    # Keep dependency metadata alive under the same byte/entry budget as facts.
    def retained_dependencies(group):
        for descriptor in fields(group):
            value = getattr(group, descriptor.name)
            if is_dataclass(value):
                yield from retained_dependencies(value)
            elif isinstance(value, tuple):
                for entry in value:
                    if isinstance(entry, FieldValue):
                        yield from entry.dependencies

    retained = set(retained_dependencies(current))
    memory.put(key, (current, None, tuple(retained), ()))
    return deepcopy(value)


def instance_read(kind, component, name, *, capacity=1):
    """Methods take (session_id, instance_id, ...); parameters are bounded scalars.

    A bound Package is immutable and belongs to the Session's existing binding;
    its digest is unnecessary in each owner field. Do not pass candidates here.
    """

    def decorate(method):
        @wraps(method)
        def read(self, sid, identity, *args, **kwargs):
            store = (
                self.h.store
                if hasattr(self, "h")
                else self.store
                if hasattr(self, "store")
                else self.facts.store
            )
            parameters = (
                tuple(
                    ("package", arg.package_digest) if hasattr(arg, "package_digest") else arg
                    for arg in args
                ),
                tuple(sorted(kwargs.items())),
            )
            return resolve_field(
                store,
                kind,
                sid,
                identity,
                component,
                name,
                parameters,
                lambda: method(self, sid, identity, *args, **kwargs),
                capacity=capacity,
            )

        return read

    return decorate


def world_read(component, name):
    """World-owned cross-scene relationships, not independent Scene facts."""

    def decorate(method):
        @wraps(method)
        def read(self, sid):
            return resolve_field(
                self.h.store, "world", sid, sid, component, name, (), lambda: method(self, sid)
            )

        return read

    return decorate


class SceneFieldCache:
    """Environment compatibility interface; values live in Scene.environment.

    Existing source/protection versions and provider cacheability remain the
    authority for external inputs. The existing transaction publishes the owner.
    """

    def __init__(self, store, name, capacity):
        self.store, self.name, self.capacity = store, name, capacity
        self.generation = 0
        self.session_generations = {}

    def clear(self):
        self.generation += 1

    def release(self, sid):
        self.session_generations[sid] = self.session_generations.get(sid, 0) + 1

    def __len__(self):
        names = ("cells", "ranges") if self.name == "cells" else (self.name,)
        return sum(
            1
            for key, value in self.store.memory.values.items()
            if key[0] == "instance" and key[2] == "scene"
            for name in names
            for entry in getattr(value[0].environment, name)
            if entry.parameter[0] == (self.generation, self.session_generations.get(key[1], 0))
        )

    def resolve(self, scope, key, compute, *, validator=None):
        if scope is None:
            return compute()
        identity, versions, parameters = key
        sid, _, _, _, _, scene, _ = identity
        name = "ranges" if self.name == "cells" and parameters[0] == "range" else self.name
        return resolve_field(
            self.store,
            "scene",
            sid,
            scene,
            "environment",
            name,
            (
                (self.generation, self.session_generations.get(sid, 0)),
                identity[1:],
                versions,
                parameters,
            ),
            compute,
            capacity=self.capacity,
            versioned_parameters=True,
        )
