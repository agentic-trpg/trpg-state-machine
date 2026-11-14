"""Immutable owner indices over the remaining encoded mechanical components.

Building an index parses each remaining aggregate JSON value once. Selecting
a mechanical closure then visits requested owners, not every other owner's
entries. No state ownership or rule evaluation lives here.
"""

from .grid_maps import _freeze, _thaw


class ComponentInputs:
    """Frozen noninventory component inputs with owner lookup indices."""

    __slots__ = ("data", "indices")

    def __init__(self, values):
        """Freeze noninventory components and build owner lookup indices."""
        object.__setattr__(self, "data", _freeze(values))
        indices = {}
        for field, path, identity in (
            ("casting_json", "actors", "actor_id"),
            ("effects_json", None, "target_id"),
        ):
            data = values[field]
            entries = () if data is None else data if path is None else data[path]
            by_owner = {}
            for number, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                by_owner.setdefault(entry.get(identity), []).append(number)
            indices[field] = by_owner
        object.__setattr__(self, "indices", _freeze(indices))

    def __setattr__(self, name, value):
        """Reject reassignment of frozen component inputs."""
        raise AttributeError("Component inputs are immutable")

    def __deepcopy__(self, memo):
        """Share this immutable component input when deep-copied."""
        return self

    def __repr__(self):
        """Return a diagnostic representation of component data and indices."""
        return f"ComponentInputs({self.data!r}, {self.indices!r})"

    def select(self, owners):
        """Select casting and effects for the required owner closure."""
        result = {}
        for field, path in (
            ("casting_json", "actors"),
            ("effects_json", None),
        ):
            raw = self.data[field]
            if raw is None:
                result[field] = None
                continue
            entries = raw if path is None else raw[path]
            by_owner = self.indices[field]
            selected = {n for owner in owners for n in by_owner.get(owner, ())}
            chosen = [_thaw(entries[n]) for n in sorted(selected)]
            result[field] = (
                chosen
                if path is None
                else {
                    **{k: _thaw(v) for k, v in raw.items() if k != path},
                    path: chosen,
                }
            )
        return result
