"""Immutable owner/identity indices over one encoded mechanical component row.

Building an index necessarily parses the existing aggregate JSON once. Selecting
a mechanical closure then visits the requested owners and identity dependencies,
not every other owner's entries. No state ownership or rule evaluation lives here.
"""

from .grid_maps import _freeze, _thaw


class ComponentInputs:
    __slots__ = ("data", "indices")

    def __init__(self, values):
        object.__setattr__(self, "data", _freeze(values))
        indices = {}
        for field, path, identity in (
            ("inventory_json", "entries", "owner_id"),
            ("resources_json", "pools", "owner_id"),
            ("casting_json", "actors", "actor_id"),
            ("effects_json", None, "target_id"),
        ):
            data = values[field]
            entries = () if data is None else data if path is None else data[path]
            by_owner, by_id, by_slug = {}, {}, {}
            for number, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                by_owner.setdefault(entry.get(identity), []).append(number)
                if field == "inventory_json":
                    by_id.setdefault(entry.get("instance_id"), []).append(number)
                    by_slug.setdefault(entry.get("item_slug"), []).append(number)
            indices[field] = (by_owner, by_id, by_slug)
        object.__setattr__(self, "indices", _freeze(indices))

    def __setattr__(self, name, value):
        raise AttributeError("Component inputs are immutable")

    def __deepcopy__(self, memo):
        return self

    def __repr__(self):
        return f"ComponentInputs({self.data!r}, {self.indices!r})"

    def inventory(self, owners, identities):
        raw = self.data["inventory_json"]
        by_owner, by_id, _ = self.indices["inventory_json"]
        selected = {n for owner in owners for n in by_owner.get(owner, ())}
        selected.update(n for identity in identities for n in by_id.get(identity, ()))
        return {
            **{k: _thaw(v) for k, v in raw.items() if k != "entries"},
            "entries": [_thaw(raw["entries"][n]) for n in sorted(selected)],
        }

    def select(self, owners, slugs, owner_exists):
        result = {}
        for field, path in (
            ("inventory_json", "entries"),
            ("resources_json", "pools"),
            ("casting_json", "actors"),
            ("effects_json", None),
        ):
            raw = self.data[field]
            if raw is None:
                result[field] = None
                continue
            entries = raw if path is None else raw[path]
            by_owner, by_id, by_slug = self.indices[field]
            selected = {n for owner in owners for n in by_owner.get(owner, ())}
            if field == "inventory_json":
                identities = {entries[n].get("instance_id") for n in selected}
                identities.discard(None)
                selected.update(n for iid in identities for n in by_id.get(iid, ()))
                for slug in slugs:
                    for n in by_slug.get(slug, ()):
                        if not owner_exists(entries[n]["owner_id"]):
                            selected.add(n)
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
