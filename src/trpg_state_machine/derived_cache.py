"""Bounded, disposable derived values; publication belongs to a successful read scope."""

from collections import OrderedDict


class DerivedCache:
    def __init__(self, capacity):
        self.capacity = capacity
        self._values = OrderedDict()
        self._generation = 0

    def clear(self):
        self._values.clear()
        self._generation += 1

    def __len__(self):
        return len(self._values)

    def _put(self, values, key, value):
        values[key] = value
        values.move_to_end(key)
        while len(values) > self.capacity:
            values.popitem(last=False)

    def resolve(self, scope, key, compute):
        if scope is None:
            return compute()
        token = (self, self._generation)
        pending = scope.derived_pending.get(token)
        if pending is None:
            pending = OrderedDict()
            scope.derived_pending[token] = pending
        if key in pending:
            pending.move_to_end(key)
            return pending[key]
        if key in self._values:
            self._values.move_to_end(key)
            return self._values[key]
        value = compute()
        self._put(pending, key, value)
        return value

    def publish(self, generation, pending):
        if generation == self._generation:
            for key, value in pending.items():
                self._put(self._values, key, value)
