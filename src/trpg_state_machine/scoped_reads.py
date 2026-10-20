"""Reuse derived reads only within one UnitOfWork and one mutation stage.

No values survive COMMIT/ROLLBACK. SQLite total_changes fences writes, and
savepoint rollback explicitly discards candidate values (total_changes does not
rewind). This is a conservative operation-local memo, never a dynamic cache.
"""

from functools import wraps


def _key(value):
    if hasattr(value, "package_digest"):
        return ("package", id(value), value.package_digest)
    if isinstance(value, dict):
        return tuple((k, _key(v)) for k, v in sorted(value.items()))
    if isinstance(value, (set, frozenset)):
        return frozenset(_key(v) for v in value)
    if isinstance(value, (tuple, list)):
        return tuple(_key(v) for v in value)
    return value


def scoped_read(method):
    @wraps(method)
    def read(self, *args, **kwargs):
        store = (
            self.h.store
            if hasattr(self, "h")
            else self.store
            if hasattr(self, "store")
            else self.facts.store
        )
        scope = store._scope
        if scope is None:
            return method(self, *args, **kwargs)
        key = (self, method, _key(args), _key(kwargs))
        try:
            hash(key)
        except TypeError:
            return method(self, *args, **kwargs)
        stage = store.connection.total_changes
        if scope.read_stage != stage:
            scope.read_memo.clear()
            scope.read_stage = stage
        if key not in scope.read_memo:
            scope.read_memo[key] = method(self, *args, **kwargs)
        return scope.read_memo[key]

    return read
