"""Compose current World and Effects facts for a private passive detection clue."""

from collections import defaultdict

from dnd5e_engine.passive_detection import PASSIVE_DETECTIONS, is_magic_item
from dnd5e_engine.spatial import _bresenham_cells, parse_cell

from .grid import cell_in_bounds, cells_within_range
from .world_openable import OpenableService


class PassiveDetection:
    """Read bounded current candidate paths inside the caller's authorized view scope."""

    def __init__(self, world):
        """Share the current World/Effects owners and frozen ruleset."""
        self.world = world
        self.facts = world.facts
        self.effects = world.characters.effects

    def clues(self, sid, scene, observer):
        """Return only proven existence clues, without candidate identities or uncertainty."""
        active = tuple(
            profile
            for profile in PASSIVE_DETECTIONS
            if self.effects.grants_passive_detection(sid, observer, profile.spell_id)
        )
        if not active or self.world._active_combat(sid):
            return ()
        position = self.world._actor_position(sid, observer)
        stored = self.facts.maps.scene(sid, scene, spatial_only=True)
        if (
            stored is None
            or stored.grid is None
            or position.scene_id != scene
            or not cell_in_bounds(stored.grid.topology, position.cell)
        ):
            return ()
        return tuple(
            profile.clue
            for profile in active
            if self._present(sid, scene, observer, position.cell, stored.grid, profile)
        )

    def _present(self, sid, scene, observer, origin, prepared, profile):
        """Evaluate indexed in-range actors, ground roots, and anchor cells through RE."""
        cells = cells_within_range(prepared, origin, profile.range_ft)
        actors, objects = self.world.grid.reader.region_occupants(
            sid, scene, cells, include_actors=True
        )
        roots, footprints = defaultdict(set), defaultdict(list)
        for object_id, cell, blocks in objects:
            roots[cell].add(object_id)
            footprints[cell].append((object_id, blocks))

        topology = prepared.topology
        paths, blockers = {}, {}

        def detectable(source, cell, enclosing=()):
            """Reuse the same current ray for all candidate facts in one cell."""
            if cell not in paths:
                paths[cell] = self._path(sid, origin, cell, topology, footprints, blockers)
            return profile.detects(
                source, topology.distance_ft(origin, cell), paths[cell] + enclosing
            )

        for actor_id, cell in actors:
            if self.effects.has_detectable_spell(
                sid, actor_id, observer, profile.spell_id
            ) and detectable("spell_effect", cell):
                return True
            roots[cell].update(self.facts.carried_root_ids(sid, actor_id))
        for effect in self.effects.spatial_lights.fixed_obscuring_at(sid, scene, cells):
            if detectable("spell_effect", effect["fixed_cell"]):
                return True
        for cell, object_ids in roots.items():
            for source, enclosing in self._object_sources(sid, object_ids):
                if detectable(source, cell, enclosing):
                    return True
        return False

    def _path(self, sid, origin, cell, topology, footprints, blockers):
        """Resolve static unknown barriers and current intermediate object footprints.

        The grid's existing center ray locates crossed cells. Endpoint objects do
        not imply an enclosure; independently verified ancestor shells supply that
        fact. Unclassified static walls/solid cover carry an unknown profile.
        """
        if (
            not topology.has_line_of_sight(origin, cell)
            or topology.cover_between(origin, cell) == "total"
        ):
            return (None,)
        result = []
        for intermediate in _bresenham_cells(*parse_cell(origin), *parse_cell(cell))[1:-1]:
            for oid, blocks in footprints.get(intermediate, ()):
                if oid not in blockers:
                    barrier = self.facts.instances.detection_barrier(sid, oid)
                    sealed = (
                        (barrier is not None or blocks)
                        and self.facts.object_hp(sid, oid) != 0
                        and self.facts.object(sid, oid)["state"] != "open"
                    )
                    blockers[oid] = (barrier,) if sealed else ()
                result.extend(blockers[oid])
        return tuple(result)

    def _object_sources(self, sid, roots):
        """Read carried or ground roots and their exact (including hidden) contents."""
        pending = [(oid, ()) for oid in roots]
        seen = set()
        while pending:
            oid, enclosing = pending.pop()
            if oid in seen:
                continue
            seen.add(oid)
            obj = self.facts.object(sid, oid)
            if obj is None:
                continue
            if self.effects.spatial_lights.attached(sid, oid):
                yield "spell_effect", enclosing
            if obj["item_slug"] is not None and is_magic_item(
                self.world.loader.get_item(obj["item_slug"])
            ):
                yield "magic_item", enclosing
            children = self.facts.contents(sid, oid)
            if children and not OpenableService.allows_contents(self.facts, sid, oid):
                enclosing += (self.facts.instances.detection_barrier(sid, oid),)
            pending.extend((child[0], enclosing) for child in children)
