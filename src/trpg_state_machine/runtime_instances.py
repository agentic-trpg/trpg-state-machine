"""Domain-owned RAM fields, staged by the existing SessionMemory publication.

Owners and components are immutable replacements, including within savepoints.
They contain values or stable IDs, never a second writable copy of domain facts.
No validity key enumerates entities or serializes a candidate/snapshot.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass, replace
from functools import wraps
from types import MappingProxyType


@dataclass(frozen=True)
class FieldValue:
    """Pair a derived field with dependency stamps and retained-payload accounting."""

    parameter: object
    value: object
    dependencies: tuple
    stamps: tuple
    retained_bytes: int = field(init=False, repr=False)

    def __post_init__(self):
        # Charge the actual value once, when it is produced/replaced. Unrelated
        # fields on the same owner must not re-serialize every retained value.
        # Dependencies are charged twice to cover the owner's retention metadata.
        """Compute field dependency and retained-size metadata."""
        size = len(repr((self.parameter, self.value, self.dependencies, self.stamps)).encode())
        size += len(repr(self.dependencies).encode()) + 256
        object.__setattr__(self, "retained_bytes", size)


@dataclass(frozen=True)
class DependencyGroups:
    """Retention metadata sharing immutable dependency tuples, not copying leaves."""

    groups: tuple

    def __iter__(self):
        """Iterate structurally shared dependency groups."""
        for group in self.groups:
            yield from group


def retained_size(owner):
    """Conservative scalar-payload charge; never walk a cached value's records."""
    size = 256
    for descriptor in fields(owner):
        value = getattr(owner, descriptor.name)
        if is_dataclass(value):
            size += retained_size(value)
        elif isinstance(value, tuple):
            size += 128 + sum(entry.retained_bytes for entry in value)
        else:
            size += 64 + len(repr(value).encode())
    return size


@dataclass(frozen=True)
class ContainerFields:
    """Retain immutable contents/weight fields on the existing runtime owner."""

    contents: tuple = ()
    own_weight: tuple = ()
    total_weight: tuple = ()
    contents_weight: tuple = ()


@dataclass(frozen=True)
class MechanismFields:
    """Retain immutable mechanism configuration and index fields."""

    configuration: tuple = ()
    index: tuple = ()


@dataclass(frozen=True)
class MechanismLinks:
    """Pair immutable trigger and component associations."""

    triggers: tuple
    components: tuple


@dataclass(frozen=True, slots=True, eq=False)
class TriggerIndex(Mapping):
    """Scene-owned immutable buckets of frozen MechanismTrigger records.

    Sharing a lookup result cannot mutate an old scope or a candidate index.
    Only index construction copies the supplied mapping, never a lookup.
    """

    _entries: Mapping

    def __post_init__(self):
        """Expose mechanism event buckets through a read-only mapping."""
        object.__setattr__(self, "_entries", MappingProxyType(dict(self._entries)))

    def __getitem__(self, key):
        """Read a mechanism index bucket by key."""
        return self._entries[key]

    def __iter__(self):
        """Iterate mechanism index keys."""
        return iter(self._entries)

    def __len__(self):
        """Return the mechanism bucket count."""
        return len(self._entries)

    def __deepcopy__(self, memo):
        """Share this immutable mechanism index when deep-copied."""
        return self


@dataclass(frozen=True, slots=True, eq=False)
class PassageIndex:
    """One World layout with immutable, ordered reverse buckets.

    Records are frozen PassageDefinitions from persisted instance configuration.
    Negative lookups are complete results, shared across objects and commands.
    """

    passages: tuple
    by_door: Mapping
    by_scene: Mapping
    by_id: Mapping
    door_scenes: Mapping

    @classmethod
    def build(cls, passages):
        """Build shared passage identity, door, scene and endpoint reverse associations."""
        doors, scenes, ids, door_scenes = {}, {}, {}, {}
        for passage in passages:
            ids.setdefault(passage.passage_id, []).append(passage)
            endpoints = dict.fromkeys(e.scene_id for e in passage.endpoints)
            for scene in endpoints:
                scenes.setdefault(scene, []).append(passage)
            if passage.door_object_id is not None:
                door = passage.door_object_id
                doors.setdefault(door, []).append(passage)
                door_scenes.setdefault(door, {}).update(endpoints)

        def freeze(buckets):
            """Freeze association buckets as read-only mappings of tuples."""
            return MappingProxyType({key: tuple(value) for key, value in buckets.items()})

        return cls(passages, freeze(doors), freeze(scenes), freeze(ids), freeze(door_scenes))

    def select(self, *, scene_id=None, object_id=None, passage_id=None):
        """Select the passage/door/scene bucket, applying combined filters within that
        scope.
        """
        if passage_id is not None:
            candidates = self.by_id.get(passage_id, ())
        elif object_id is not None:
            candidates = self.by_door.get(object_id, ())
        elif scene_id is not None:
            return self.by_scene.get(scene_id, ())
        else:
            return self.passages
        return tuple(
            p
            for p in candidates
            if (object_id is None or p.door_object_id == object_id)
            and (scene_id is None or any(e.scene_id == scene_id for e in p.endpoints))
        )

    def __deepcopy__(self, memo):
        """Share this immutable passage index when deep-copied."""
        return self


@dataclass(frozen=True)
class SpatialFields:
    """Retain prepared spatial records, maps, occupancy and blocker fields."""

    record: tuple = ()
    sensory: tuple = ()
    map: tuple = ()
    mechanical_map: tuple = ()
    occupants: tuple = ()
    object_positions: tuple = ()
    cells: tuple = ()
    blockers: tuple = ()


@dataclass(frozen=True)
class EnvironmentFields:
    """Retain environment facts, prepared inputs and composed cell/range fields."""

    facts: tuple = ()
    inputs: tuple = ()
    protection: tuple = ()
    cells: tuple = ()
    ranges: tuple = ()


@dataclass(frozen=True)
class LightingFields:
    """Retain light sources, geometry and illumination result fields."""

    sources: tuple = ()
    geometry: tuple = ()
    candidates: tuple = ()
    cells: tuple = ()
    projection: tuple = ()
    topology: tuple = ()


@dataclass(frozen=True)
class ObjectDirectory:
    """Retain direct-object, contributor and capability association fields."""

    direct: tuple = ()
    contributors: tuple = ()
    capabilities: tuple = ()


@dataclass(frozen=True)
class ObjectRuntime:
    """Own immutable object fact/definition and capability fields within SessionMemory."""

    session_id: str
    object_id: str
    facts: tuple = ()
    definition: tuple = ()
    container: ContainerFields = field(default_factory=ContainerFields)
    mechanism: MechanismFields = field(default_factory=MechanismFields)


@dataclass(frozen=True)
class SceneRuntime:
    """Own immutable scene spatial/environment/light and object-association fields."""

    session_id: str
    scene_id: str
    spatial: SpatialFields = field(default_factory=SpatialFields)
    environment: EnvironmentFields = field(default_factory=EnvironmentFields)
    lighting: LightingFields = field(default_factory=LightingFields)
    mechanisms: MechanismFields = field(default_factory=MechanismFields)
    objects: ObjectDirectory = field(default_factory=ObjectDirectory)


@dataclass(frozen=True)
class WorldRuntime:
    """Own Session-wide mechanism and passage associations through shared publication."""

    session_id: str
    mechanisms: MechanismFields = field(default_factory=MechanismFields)
    passages: tuple = ()


@dataclass(frozen=True)
class CharacterRuntime:
    """Own the actor's retained inventory projection field."""

    session_id: str
    actor_id: str
    inventory: tuple = ()


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
    maintained=None,
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
                maintained=maintained,
            )
    memory = store.memory
    memory.guard(sid)
    store._scope.memory_sessions.add(sid)
    key = ("instance", sid, kind, identity)

    def owner():
        """Resolve the runtime owner from an existing field or instance kind."""
        cached = memory.get(key)
        return (
            cached[0]
            if cached
            else (
                ObjectRuntime(sid, identity)
                if kind == "object"
                else SceneRuntime(sid, identity)
                if kind == "scene"
                else CharacterRuntime(sid, identity)
                if kind == "actor"
                else WorldRuntime(sid)
            )
        )

    current = owner()
    group = getattr(current, component) if component else current
    entries = getattr(group, name)
    # These fields have an explicit mutation protocol in SessionMemory.changed.
    # Hide their computation's raw dependencies from enclosing field/parsed reads:
    # those consumers depend on the maintained field version, not every leaf.
    dependency = (
        memory.maintained_dependency(maintained, sid, identity, parameter) if maintained else None
    )
    for entry in entries:
        if entry.parameter == parameter and entry.stamps == tuple(
            memory.version(d) for d in entry.dependencies
        ):
            for dependency in entry.dependencies:
                memory.dependency(*dependency)
            memory.stats["instance_hits"] += 1
            return deepcopy(entry.value)
    dependencies = set()
    collectors = memory.collectors
    if maintained:
        memory.collectors = []
    memory.collectors.append(dependencies)
    stamp = memory.version(dependency) if maintained else None
    try:
        memory.stats["instance_builds"] += 1
        value = compute()
    finally:
        memory.collectors.pop()
        memory.collectors = collectors
    # Environment's registered provider/source/protection protocol already owns
    # complete validity. Incidental decoder reads must not replace that protocol
    # with broader invalidation (e.g. lock metadata is not rain protection).
    dependencies = (
        (dependency,)
        if maintained
        else ()
        if versioned_parameters
        else tuple(sorted(dependencies, key=repr))
    )
    entry = FieldValue(
        parameter,
        value,
        dependencies,
        (stamp,) if maintained else tuple(memory.version(d) for d in dependencies),
    )
    # Nested computations may have filled other fields of this same owner.
    current = owner()
    group = getattr(current, component) if component else current
    entries = (entry, *(e for e in getattr(group, name) if e.parameter != parameter))[:capacity]
    group = replace(group, **{name: entries})
    current = replace(current, **{component: group}) if component else group

    # Keep dependency metadata alive under the same byte/entry budget as facts.
    def retained_dependencies(group):
        """Collect dependency version groups retained by the field structure."""
        for descriptor in fields(group):
            value = getattr(group, descriptor.name)
            if is_dataclass(value):
                yield from retained_dependencies(value)
            elif isinstance(value, tuple):
                for entry in value:
                    if isinstance(entry, FieldValue):
                        yield entry.dependencies

    retained = DependencyGroups(tuple(retained_dependencies(current)))
    memory.put(key, (current, None, retained, ()))
    return deepcopy(value)


def instance_read(kind, component, name, *, capacity=1, maintained=None):
    """Methods take (session_id, instance_id, ...); parameters are bounded scalars.

    A bound Package is immutable and belongs to the Session's existing binding;
    its digest is unnecessary in each owner field. Do not pass candidates here.
    """

    def decorate(method):
        """Wrap the instance method with runtime field resolution."""

        @wraps(method)
        def read(self, sid, identity, *args, **kwargs):
            """Read or compute the instance field using identity, arguments and maintained
            versions.
            """
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
            if maintained == "capabilities" and not args and set(kwargs) == {"table"}:
                parameters = ((kwargs["table"],), ())
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
                maintained=maintained,
            )

        return read

    return decorate


def world_read(component, name):
    """World-owned cross-scene relationships, not independent Scene facts."""

    def decorate(method):
        """Wrap the World method with shared field resolution."""

        @wraps(method)
        def read(self, sid):
            """Resolve cross-scene associations through the Session's World field owner."""
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
        """Initialize the scene field cache's name, capacity and generation."""
        self.store, self.name, self.capacity = store, name, capacity
        self.generation = 0
        self.session_generations = {}

    def clear(self):
        """Advance the cache generation to invalidate existing values."""
        self.generation += 1

    def release(self, sid):
        """Advance the Session field-cache generation."""
        self.session_generations[sid] = self.session_generations.get(sid, 0) + 1

    def __len__(self):
        """Count scene fields still valid in the current generation."""
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
        """Resolve a versioned field in the consistent scope, computing when reuse is
        unavailable.
        """
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
