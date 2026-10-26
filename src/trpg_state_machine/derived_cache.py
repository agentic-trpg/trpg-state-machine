"""Bounded, disposable derived values; publication belongs to a successful read scope."""

from collections import OrderedDict


class DerivedCache:
    """Bound retained derived values and stage publication in successful read scopes."""

    def __init__(self, capacity):
        """Initialize an ordered derived-value cache with a capacity and publication
        generation.
        """
        self.capacity = capacity
        self._values = OrderedDict()
        self._generation = 0

    def clear(self):
        """Discard derived values and advance the generation to fence pending publication."""
        self._values.clear()
        self._generation += 1

    def release(self, sid):
        """Drop this Session's entries and advance the publication generation."""
        for key in tuple(self._values):
            if key[0][0] == sid:
                del self._values[key]
        self._generation += 1

    def __len__(self):
        """Return the retained entry count."""
        return len(self._values)

    def _put(self, values, key, value):
        """Insert or refresh an entry and evict oldest values above capacity."""
        values[key] = value
        values.move_to_end(key)
        while len(values) > self.capacity:
            values.popitem(last=False)

    def resolve(self, scope, key, compute, *, validator=None):
        """Reuse a scoped derived value or compute and retain it pending acceptance.

        Without a scope, compute directly and retain nothing. Within a scope, consult
        its pending generation before the published cache. New values and optional
        publication validators remain pending; this lookup does not itself commit or
        publish them.
        """
        if scope is None:
            return compute()
        token = (self, self._generation)
        pending = scope.derived_pending.get(token)
        if pending is None:
            pending = OrderedDict()
            scope.derived_pending[token] = pending
        if key in pending:
            pending.move_to_end(key)
            return pending[key][0]
        if key in self._values:
            self._values.move_to_end(key)
            return self._values[key]
        value = compute()
        self._put(pending, key, (value, validator))
        return value

    def publish(self, generation, pending):
        """Publish pending values only for the current generation and successful
        validators.
        """
        if generation == self._generation:
            checks = {}
            for key, (value, validator) in pending.items():
                if validator not in checks:
                    checks[validator] = validator is None or validator()
                if checks[validator]:
                    # Retain current revisions only, never build a history store.
                    for old in tuple(self._values):
                        if old[0] == key[0] and old[1] != key[1]:
                            del self._values[old]
                    self._put(self._values, key, value)
