"""Dependency-tracked parsed facts, isolated by mutation stage and publication."""

from copy import deepcopy
from functools import partial, wraps


def _key(value):
    if hasattr(value, "runtime_key"):
        return value.runtime_key()
    if hasattr(value, "package_digest"):
        return ("package", id(value), value.package_digest)
    if isinstance(value, dict):
        return tuple((k, _key(v)) for k, v in sorted(value.items()))
    if isinstance(value, (set, frozenset)):
        return frozenset(_key(v) for v in value)
    if isinstance(value, (tuple, list)):
        return tuple(_key(v) for v in value)
    if hasattr(value, "keys"):
        return tuple((k, _key(value[k])) for k in sorted(value.keys()))
    return value


def scoped_read(method=None, *, membership=()):
    if method is None:
        return partial(scoped_read, membership=membership)

    @wraps(method)
    def read(self, *args, **kwargs):
        store = (
            self.h.store
            if hasattr(self, "h")
            else self.store
            if hasattr(self, "store")
            else self._store
            if hasattr(self, "_store")
            else self.facts.store
            if hasattr(self, "facts")
            else None
        )
        if store is None or not hasattr(store, "memory"):
            # Historical migration readers borrow the decoder before runtime
            # construction and deliberately stay within the persisted audit scope.
            return method(self, *args, **kwargs)
        scope = store._scope
        if scope is None:
            with store.transaction():
                return read(self, *args, **kwargs)
        key = ("parsed", self, method, _key(args), _key(kwargs))
        try:
            hash(key)
        except TypeError:
            return method(self, *args, **kwargs)
        stage = store.connection.total_changes
        if scope.read_stage != stage:
            scope.read_memo.clear()
            scope.read_stage = stage
        memory = store.memory
        memory.guard()
        if args and isinstance(args[0], str) and args[0].startswith("session."):
            memory.guard(args[0])
        for sid in scope.memory_sessions:
            memory.guard(sid)
        if key in scope.read_memo:
            value, dependencies = scope.read_memo[key]
            for dependency in dependencies:
                memory.dependency(*dependency)
            return value
        cached = memory.get(key)
        if cached is not None:
            value, dependencies, stamps = cached
            if stamps == tuple(memory.version(d) for d in dependencies):
                memory.stats["parsed_hits"] += 1
                for dependency in dependencies:
                    memory.dependency(*dependency)
                value = deepcopy(value)
                scope.read_memo[key] = (value, dependencies)
                return value
        dependencies = set()
        memory.collectors.append(dependencies)
        try:
            memory.stats["parsed_builds"] += 1
            if method.__name__ in ("_snapshot", "_noncombat_snapshot", "_combat_snapshot"):
                memory.stats["snapshot_builds"] += 1
            value = method(self, *args, **kwargs)
        finally:
            memory.collectors.pop()
        dependencies = tuple(
            sorted(
                (
                    (
                        table.partition("#")[0].partition("@")[0] + ":membership"
                        if table.partition("#")[0].partition("@")[0] in membership
                        else table,
                        sid,
                    )
                    for table, sid in dependencies
                ),
                key=repr,
            )
        )
        # SceneConfig still owns lazy domain properties. Cache its detached source
        # through InstanceData.get, not a live reference to a service/connection.
        if not hasattr(value, "_instances"):
            memory.put(
                key, (deepcopy(value), dependencies, tuple(memory.version(d) for d in dependencies))
            )
        scope.read_memo[key] = (value, dependencies)
        return value

    return read
