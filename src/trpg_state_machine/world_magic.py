"""Resolve World anchors and hand actual rule footprints back to Effects."""

from dataclasses import dataclass, replace
from typing import Literal

from dnd5e_engine.areas import AreaTemplate, area_cells, has_line_of_effect
from dnd5e_engine.environment import EnvironmentalSource, environmental_dispels
from dnd5e_engine.evaluation_charged_item import healing_staff_area_reaches
from dnd5e_engine.evaluation_contracts import EnvironmentalItemFacts, ExplorationSpellPositions
from dnd5e_engine.evaluation_environmental_contracts import (
    EnvironmentalCreatureTarget,
    EnvironmentalMagicEffectFacts,
    EnvironmentalMagicEffectTarget,
)
from dnd5e_engine.spatial import parse_cell
from dnd5e_engine.spellcasting import spell_range_ft
from dnd5e_srd_data.schema.environment import EnvironmentalSpec

from .errors import StateMachineError
from .grid import cell_in_bounds
from .instance_data import passage_index
from .perception import visible_actor_in_range
from .runtime_instances import instance_read
from .scoped_reads import scoped_read


@dataclass(frozen=True, slots=True)
class SpellLightAnchor:
    """The current position and typed range of one admitted spatial spell."""

    effect_id: str
    item_id: str | None
    cell: str
    template: AreaTemplate
    spec: EnvironmentalSpec
    unknown: bool

    def __deepcopy__(self, memo):
        """Share the immutable position and rules profile across read scopes."""
        return self


@dataclass(frozen=True, slots=True)
class MagicLightSource(EnvironmentalSource):
    """An immutable contribution with the full Effects application and spell provenance."""

    spell_id: str = ""
    spell_source_id: str = ""
    unknown: bool = False

    @property
    def effect_id(self):
        """Return the application ID used by Effects."""
        return self.area_id

    @property
    def item_id(self):
        """Return the actual item anchor, when present."""
        return self.origin_object_id

    @property
    def cell(self):
        """Return the current anchor cell in this projection's scene."""
        return self.origin

    def __deepcopy__(self, memo):
        """Share only immutable geometry, profile and provenance across read scopes."""
        return self


class WorldMagic:
    """Render accepted spatial Effects from actual World placement without writing on reads."""

    def __init__(self, world):
        """Bind spatial and rule-provider collaborators to the same Store."""
        self.h = world
        self.store = world.store
        self.owner = world.characters.effects.spatial_lights

    def object_spells(self, sid, oid):
        """Return rules references for ongoing spells on the selected object only."""
        return tuple(sorted({record["spell_id"] for record in self.owner.attached(sid, oid)}))

    def _transmission(self, sid, oid, placement) -> Literal["block", "transmit", "unknown"]:
        """Preserve known enclosure, transmission and undecided fixture data independently."""
        if self.h.facts.object_hp(sid, oid) == 0:
            return "block"
        result = "transmit"
        for parent in placement["ancestry"]:
            if self.h.facts.object_hp(sid, parent) == 0:
                continue
            config = self.h.objects.containers.config(sid, parent)
            obj = self.h.facts.object(sid, parent)
            transmission = (
                config.light_transmission_closed
                if obj["state"] == "closed"
                else config.light_transmission_open
            )
            if transmission == "block":
                return "block"
            if transmission == "unknown":
                result = "unknown"
        return result

    def cast_positions(self, row, package, command):
        """Resolve asserted target IDs into fixed, authoritative local facts for RE."""
        sid, scene = row["session_id"], row["current_scene_id"]
        facts = self.h.facts
        source = facts.carrier_position(sid, command.actor_id)
        spatial = facts.maps.scene(sid, scene, spatial_only=True)
        if source is None or source[0] != scene or source[1] is None or spatial is None:
            return None
        grid = spatial.grid
        if grid is None or not cell_in_bounds(grid.topology, source[1]):
            return None
        target = command.payload.target
        item_facts, effect_facts = None, None
        applications = ()
        if isinstance(target, EnvironmentalCreatureTarget):
            placement = facts.carrier_position(sid, target.target_id)
            if placement is None or placement[0] != scene or placement[1] is None:
                return None
            cell = placement[1]
            spell = self.h.loader.get_spell(command.payload.spell_id)
            reach = spell_range_ft(spell) if spell else None
            if reach is None or not visible_actor_in_range(
                self.h,
                sid,
                command.actor_id,
                target.target_id,
                cell,
                package,
                range_ft=reach,
            ):
                return None
            applications = self.h.characters.effects.all_target_spell_applications(
                sid, target.target_id
            )
        elif isinstance(target, EnvironmentalMagicEffectTarget):
            try:
                self.h._public_scope(sid, command.actor_id, package)
            except StateMachineError as exc:
                if exc.code in ("unsupported_target_visibility", "unsupported_interaction_scope"):
                    return None
                raise
            record = self.owner.get(sid, target.effect_id)
            if record is None:
                return None
            anchor_scene, anchor_cell, transmission = self._anchor(sid, record)
            if anchor_scene != scene or anchor_cell is None or transmission == "unknown":
                return None
            effect_facts = self._ending_facts(record, current_cell=anchor_cell)
            cell = effect_facts.cell_id
        elif target.kind == "point":
            cell = target.cell_id
        else:
            oid = target.item_id
            obj = facts.object(sid, oid)
            if obj is None:
                return None
            placement = facts.placement(sid, oid, row=obj)
            if placement["scene_id"] != scene:
                return None
            if placement["carrier_id"] not in (None, command.actor_id):
                return None
            try:
                observer = self.h._public_scope(sid, command.actor_id, package)
                self.h.objects._require_visible(sid, command.actor_id, observer, obj, package)
            except StateMachineError as exc:
                if exc.code in (
                    "invalid_target",
                    "unsupported_target_visibility",
                    "unsupported_interaction_scope",
                ):
                    return None
                raise
            definition = facts.object_definition(sid, oid)
            cell = placement["cell"]
            transmission = self._transmission(sid, oid, placement)
            if transmission == "unknown":
                return None
            item_facts = EnvironmentalItemFacts(
                item_id=oid,
                size=definition.size_category if definition else None,
                worn_or_carried_by=placement["carrier_id"],
                opaque_cover=transmission == "block",
            )
        if not cell_in_bounds(grid.topology, cell):
            return None
        from .world_lights import _corridor_cells

        if source[1] != cell:
            corridor = _corridor_cells(((source[1], (parse_cell(cell),)),))
            blocked = self._opaque_cells(
                sid, scene, package, grid, corridor, item_facts.item_id if item_facts else None
            ) - {source[1]}
            if cell in blocked or (
                blocked
                and not has_line_of_effect(grid.topology.with_blocked(blocked), source[1], cell)
            ):
                return None
        return ExplorationSpellPositions(
            source_cell=source[1],
            target_cell=cell,
            target_item=item_facts,
            target_effect=effect_facts,
            target_applications=applications,
            visible_target_id=target.target_id
            if isinstance(target, EnvironmentalCreatureTarget)
            else None,
        )

    def healing_sphere_reaches(self, sid, scene_id, package, spell, source, center, targets):
        """Check bounded healing rays against fixed geometry and local opaque objects."""
        from .world_lights import _corridor_cells

        spatial = self.h.facts.maps.scene(sid, scene_id, spatial_only=True)
        if spatial is None or (grid := spatial.grid) is None:
            return False
        topology = grid.topology
        if not healing_staff_area_reaches(topology, spell, source, center, targets):
            return False
        rays = ((source, (parse_cell(center),)), (center, tuple(map(parse_cell, targets))))
        corridor = _corridor_cells(rays)
        cover_cells = self._total_cover_cells(sid, scene_id, package, corridor)
        for cell in sorted(cover_cells):
            topology = topology.with_cover_at(cell, "total")
        return not cover_cells or healing_staff_area_reaches(
            topology, spell, source, center, targets
        )

    def _total_cover_cells(self, sid, scene_id, package, corridor):
        """Project only the affected object footprints and local shared-door thresholds."""
        passages = passage_index(self.h.store, sid)
        door_cells = {}
        for cell in corridor:
            for door in passages.doors_by_threshold.get((scene_id, cell), ()):
                door_cells.setdefault(door, set()).add(cell)
        _, occupants = self.h.grid.reader.region_occupants(sid, scene_id, corridor)
        objects = {oid for oid, _, _ in occupants if oid not in passages.by_door}
        spatial = self.h.objects.spatial
        total = set()
        for oid in sorted(objects | door_cells.keys()):
            cover = spatial.project(sid, oid, package).cover_cells
            if oid in door_cells:
                if any(degree == "total" for _, degree in cover):
                    total.update(door_cells[oid])
            else:
                total.update(
                    cell for cell, degree in cover if degree == "total" and cell in corridor
                )
        return total

    @staticmethod
    def _ending_facts(record, *, current_cell=None):
        """Project exact spatial provenance with its current World anchor cell."""
        return EnvironmentalMagicEffectFacts(
            effect_id=record["effect_id"],
            caster_id=record["caster_id"],
            spell_source_id=record["spell_source_id"],
            spell_id=record["spell_id"],
            slot_level=record["slot_level"],
            cell_id=current_cell or record["fixed_cell"],
            expires_tick=record["expires_tick"],
            concentration_id=record["concentration_id"],
            anchor_item_id=record["object_id"],
        )

    def _anchor(self, sid, record):
        """Read the fixed point or resolve the actual carried item's current place."""
        oid = record["object_id"]
        if oid is None:
            return record["fixed_scene_id"], record["fixed_cell"], "transmit"
        placement = self.h.facts.placement(sid, oid)
        return placement["scene_id"], placement["cell"], self._transmission(sid, oid, placement)

    @instance_read("scene", "lighting", "magic_records", maintained="light_sources")
    def _records(self, sid, scene):
        """Reuse one fixed collection of applications attached to the current Scene."""
        fixed = self.owner.fixed(sid, scene)
        objects = self.h.facts.magic_light_anchors(sid, scene)
        return (*fixed, *(record for oid in objects for record in self.owner.attached(sid, oid)))

    def has_scene_effects(self, sid, scene):
        """Guard entry to combat, whose snapshot has no spatial Effects state."""
        return bool(self._records(sid, scene))

    @scoped_read
    def may_have_darkness(self, sid, scene):
        """Detect possible active darkness from indexed facts without rendering any footprint."""
        return any(
            self.owner.spec(record).kind == "magical_darkness"
            and self._anchor(sid, record)[2] != "block"
            for record in self._records(sid, scene)
        )

    @scoped_read
    def may_have_obscurement(self, sid, scene):
        """Detect active spell obscuration before a scene-wide clear-sight shortcut."""
        return any(
            self.owner.spec(record).obscurement != "none" for record in self._records(sid, scene)
        )

    def has_exploration_concentration(self, sid):
        """Check the selected caster's elapsed group even when its effect is in another Scene."""
        actor = self.store.connection.execute(
            "SELECT pc_actor_id FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        return actor is not None and self.h.characters.effects.has_world_concentration(
            sid, actor[0]
        )

    @instance_read("scene", "lighting", "magic_anchors", maintained="light_sources")
    def illumination_anchors(self, sid, scene):
        """Resolve effective spell anchors once for the current source version."""
        return tuple(
            anchor
            for record in self._records(sid, scene)
            if (anchor := self._illumination_anchor(sid, scene, record)) is not None
        )

    def _illumination_anchor(self, sid, scene, record):
        """Resolve a stored application to its present, transmissive World anchor."""
        actual_scene, cell, transmission = self._anchor(sid, record)
        if actual_scene != scene or transmission == "block" or cell is None:
            return None
        return SpellLightAnchor(
            record["effect_id"],
            record["object_id"],
            cell,
            # Exploration admits only self-inclusive radial spell templates.
            AreaTemplate(record["shape"], "sphere", record["radius_ft"], "target", True),
            self.owner.spec(record),
            transmission == "unknown",
        )

    @scoped_read
    def active_magic_sources(self, sid, scene, package):
        """Return current spell applications with their RE rules footprints."""
        return self._project_sources(sid, scene, package, self._records(sid, scene))

    def obscuring_cells(self, sid, scene, package, cells=None):
        """Project active spell obscuration without replacing independent map sources."""
        selected = None if cells is None else frozenset(cells)
        records = tuple(
            record
            for record in self._records(sid, scene)
            if self.owner.spec(record).obscurement != "none"
        )
        return {
            cell: source.spec.obscurement
            for source in self._project_sources(sid, scene, package, records)
            for cell in source.cells
            if selected is None or cell in selected
        }

    def _project_sources(self, sid, scene, package, records):
        """Build rules footprints for overlap and dispels from the current candidate facts."""
        if not records:
            return ()
        stored = self.h.facts.maps.scene(sid, scene, spatial_only=True)
        if stored is None or stored.grid is None:
            return ()
        grid = stored.grid
        sources = []
        for record in records:
            anchor = self._illumination_anchor(sid, scene, record)
            if anchor is not None and cell_in_bounds(grid.topology, anchor.cell):
                sources.append(self._source(sid, scene, package, grid, anchor, record))
        return tuple(sources)

    def _source(self, sid, scene, package, grid, anchor, record):
        """Project a typed RE rules area for overlap and dispels."""
        cell, spec, template = anchor.cell, anchor.spec, anchor.template
        radius = template.size_ft
        topology = grid.topology
        size = radius + spec.dim_extension_ft
        candidates = topology.cells_in_template(cell, template.grid_shape, size)
        blocked = self._opaque_cells(sid, scene, package, grid, candidates, record["object_id"]) - {
            cell
        }
        topology = topology.with_blocked(blocked)
        primary = area_cells(topology, template, cell, None) - blocked
        dim = (
            area_cells(topology, replace(template, size_ft=size), cell, None) - primary - blocked
            if spec.dim_extension_ft
            else frozenset()
        )

        concentration = record["concentration_id"]
        return MagicLightSource(
            area_id=record["effect_id"],
            source_id=record["caster_id"],
            source_entity_id=record["caster_id"],
            spell_level=record["slot_level"],
            spec=spec,
            cells=primary,
            origin=cell,
            radius_ft=radius,
            duration_rounds=None,
            concentration_identity=(record["caster_id"], concentration, record["effect_id"])
            if concentration
            else None,
            dim_cells=dim,
            origin_object_id=record["object_id"],
            spell_id=record["spell_id"],
            spell_source_id=record["spell_source_id"],
            unknown=anchor.unknown,
        )

    def _opaque_cells(self, sid, scene, package, grid, cells, origin_item):
        """Read known opaque fixtures only in the selected spell footprint or targeting ray."""
        blockers = self.h.grid.light_occluders(sid, scene, package, cells, fixed_grid=grid)
        return blockers.solid_cells(exclude_object=origin_item)

    def end_event(self, sid, package, record, reason, *, cause=None):
        """Write the exact terminal removal and capture its actual post-removal stage."""
        scene, _, _ = self._anchor(sid, record)
        self.owner.remove(record)
        return self.h._capture_event(
            sid,
            package,
            "effect.spatial_light_ended",
            {
                "effect_id": record["effect_id"],
                "actor_id": record["caster_id"],
                "scene_id": scene,
                "spell_id": record["spell_id"],
                "reason": reason,
                "cause_effect_id": cause,
                "boundary_tick": record["expires_tick"],
            },
            phase="effects.after_spatial_end",
        )

    def reconcile_scene(self, sid, scene, package):
        """Apply RE's overlap decisions only on a candidate mutation stage."""
        self.store.require_write()
        records = self._records(sid, scene)
        if len(records) < 2:
            return []
        positions = tuple(
            source
            for source in self._project_sources(sid, scene, package, records)
            if not source.unknown
        )
        if len(positions) < 2:
            return []
        return [
            self.end_event(sid, package, self.owner.get(sid, victim), "dispelled", cause=cause)
            for victim, cause in environmental_dispels(positions).items()
        ]

    def reconcile_world_event(self, sid, package, event):
        """Settle overlap after an admitted world movement, covering, or obstruction change."""
        if event.event_type not in {
            "actor.stepped",
            "actor.moved",
            "scene.entered",
            "object.placed",
            "object.state_changed",
            "object.durability_changed",
            "item.picked_up",
            "item.dropped",
            "item.container_transferred",
        }:
            return []
        payload = event.payload
        if event.event_type == "item.container_transferred" and payload.get("phase") != "after":
            return []
        scene = payload.get("scene_id", payload.get("to_scene_id"))
        if scene is None:
            return []
        scenes = {scene}
        if event.event_type == "object.state_changed" and payload.get("passage_id"):
            scenes.update(passage_index(self.store, sid).door_scenes.get(payload["object_id"], ()))
        return [
            event
            for actual in sorted(scenes)
            for event in self.reconcile_scene(sid, actual, package)
        ]

    def drop_concentration(self, sid, caster, package, *, command_id):
        """Voluntarily end only the caster's current exploration group."""
        casting = self.h.spell_casts.active(sid, caster)
        if casting is not None:
            if casting["status"] == "waiting":
                # Active deadline waits finish only through the authorized reply finalizer.
                return []
            return [self.h.spell_casts.interrupt(casting, package, command_id)]
        events = self.h.runtime.time.consumers["effects"].end_world_concentration(
            sid, package, caster, "concentration_ended"
        )
        if not events:
            raise StateMachineError(
                "no_spatial_concentration", "No active exploration concentration"
            )
        return events

    def apply(self, row, package, command_id, proposal):
        """Adopt a verified spatial application after authorized concentration replacement."""
        sid, scene = row["session_id"], row["current_scene_id"]
        ended = []
        if proposal.replace_same_caster_on_recast:
            for record in self.owner.by_spell(sid, proposal.source_id, proposal.spell_id):
                ended.append(self.end_event(sid, package, record, "recast"))
        deadline = self.h.time.seconds_deadline(sid, proposal.duration_seconds)
        identity = self.owner.add(sid, scene, command_id, proposal, deadline)
        ended.extend(self.reconcile_scene(sid, scene, package))
        return identity, ended

    def apply_end(self, row, package, proposal):
        """End a verified application within the caster's existing write transaction."""
        sid = row["session_id"]
        expected = proposal.expected
        record = self.owner.get(sid, expected.effect_id)
        if (
            record is None
            or self._anchor(sid, record)[0] != row["current_scene_id"]
            or self._ending_facts(record, current_cell=self._anchor(sid, record)[1]) != expected
        ):
            raise StateMachineError("version_conflict", "Spatial spell selection changed")
        return self.end_event(sid, package, record, proposal.reason)
