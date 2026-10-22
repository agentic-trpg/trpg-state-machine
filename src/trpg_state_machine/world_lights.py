"""Ordinary scenario emitters; no fuel, magic, sunlight or effect lifecycle."""

from dataclasses import asdict

from ._execution import _execution_data
from .errors import StateMachineError
from .runtime_instances import instance_read
from .world_commands import SetLightEnabled

_RANK = {"dark": 0, "dim": 1, "bright": 2}


class LightService:
    def __init__(self, handler):
        self.h = handler

    def config(self, sid, oid):
        d = self.h.facts.object_definition(sid, oid)
        return d.capability("light") if d else None

    @instance_read("scene", "lighting", "sources")
    def sources(self, sid, scene, package):
        result = []
        for oid in self.h.facts.scene_capability_ids(sid, scene, "object_lights"):
            light = self.h.facts.light_state(sid, oid)
            c = self.config(sid, oid)
            if not light["enabled"] or self.h.facts.object_hp(sid, oid) == 0:
                continue
            location = self.h.facts.placement(sid, oid)
            if location["scene_id"] != scene:
                continue
            transmission = "transmit"
            for parent in location["ancestry"]:
                config = self.h.objects.containers.config(sid, parent)
                obj = self.h.facts.object(sid, parent)
                if self.h.facts.object_hp(sid, parent) == 0:
                    continue  # The supported destroyed shell no longer encloses light.
                value = (
                    config.light_transmission_closed
                    if obj["state"] == "closed"
                    else config.light_transmission_open
                )
                if value == "block":
                    transmission = "block"
                    break
                if value == "unknown":
                    transmission = "unknown"
            if transmission == "block":
                continue
            mode = next(m for m in c.modes if m.mode_id == light["mode"])
            result.append(
                {
                    "object_id": oid,
                    "cell": location["cell"],
                    "transmission": transmission,
                    "mode": mode,
                }
            )
        return result

    @instance_read("scene", "lighting", "projection")
    def current_project(self, sid, scene, package):
        raw = self.h.grid.reader.current_grid(sid, scene, package).copy_data()
        geometry = self.geometry(sid, scene, package) if self.sources(sid, scene, package) else None
        return self.project(sid, scene, package, raw, geometry=geometry)

    @instance_read("scene", "lighting", "cells", capacity=128)
    def current_cell(self, sid, scene, package, cell):
        prepared = self.h.grid.reader.current_grid(sid, scene, package)
        sources = self.sources(sid, scene, package)
        if not sources:
            return (
                prepared.data["lighting"].get(cell, prepared.data["default_lighting"]),
                False,
                (),
            )
        candidates, geometry = self.geometry(sid, scene, package)
        return self._illuminate_cell(prepared.topology, geometry, candidates, sources, cell)

    @staticmethod
    def _illuminate_cell(topology, uncertain_geometry, candidates, sources, cell):
        """Original light composition for one requested cell, shared by full maps."""
        level = topology.light_on_cell(cell)
        contributions, unknown = [], False
        for source in sources:
            origin, mode = source["cell"], source["mode"]
            distance = topology.distance_ft(origin, cell) if origin is not None else None
            if distance is None:
                unknown = True
                continue
            if distance > mode.dim_outer_radius_ft:
                continue
            if (
                not topology.has_line_of_sight(origin, cell)
                or topology.cover_between(origin, cell) == "total"
            ):
                continue
            if (
                source["transmission"] == "unknown"
                or cell in candidates
                or not uncertain_geometry.has_line_of_sight(origin, cell)
            ):
                unknown = True
                continue
            value = (
                "bright"
                if mode.bright_radius_ft is not None and distance <= mode.bright_radius_ft
                else "dim"
            )
            if _RANK[value] > _RANK[level]:
                level = value
            contributions.append(source["object_id"])
        return level, unknown, tuple(contributions)

    @instance_read("scene", "lighting", "geometry")
    def geometry(self, sid, scene, package):
        raw = self.h.grid.reader.current_grid(sid, scene, package).copy_data()
        return self._geometry(sid, scene, package, raw)

    @instance_read("scene", "lighting", "candidates", maintained="light_candidates")
    def _candidates(self, sid, scene, package):
        """Ground-object uncertainty, maintained separately from source membership."""
        candidates = self.h.objects.spatial.movement_cells(sid, scene, package)
        actual = self.h.grid.reader.scene_object_occupants(sid, scene, package)
        candidates.update(
            cell
            for oid, cell in actual
            if cell is not None and self.h.facts.instances.legacy_object(sid, oid).blocks_passage
        )
        return frozenset(candidates)

    def _geometry(self, sid, scene, package, raw):
        candidates = set(self._candidates(sid, scene, package))
        for passage in self.h.facts.instances.passages(sid, scene_id=scene):
            if passage.door_object_id and self.h._door_closed(sid, passage, package):
                candidates.update(
                    e.threshold_cell for e in passage.endpoints if e.scene_id == scene
                )
        uncertain_geometry = self.h.grid.reader.topology_for_grid(
            sid, scene, {**raw, "blocked_cells": sorted(set(raw["blocked_cells"]) | candidates)}
        )
        return frozenset(candidates), uncertain_geometry

    def project(self, sid, scene, package, raw, *, cells=None, geometry=None):
        """Explicit fixed-map inputs remain separate from current scene fields."""
        grid = {**raw, "lighting": dict(raw["lighting"])}
        topology = self.h.grid.reader.topology_for_grid(sid, scene, raw)
        sources = self.sources(sid, scene, package)
        if not sources:
            return grid, (), {}
        candidates, uncertain_geometry = (
            self._geometry(sid, scene, package, raw) if geometry is None else geometry
        )
        uncertain, evidence = [], {}
        selected = (
            cells
            if cells is not None
            else (f"{x},{y}" for y in range(raw["height"]) for x in range(raw["width"]))
        )
        for cell in selected:
            level, unknown, contributions = self._illuminate_cell(
                topology, uncertain_geometry, candidates, sources, cell
            )
            if unknown:
                uncertain.append(cell)
            grid["lighting"][cell] = level
            if contributions:
                evidence[cell] = list(contributions)
        return grid, tuple(uncertain), evidence

    def validate(self, row, actor, op, package):
        h, sid = self.h, row["session_id"]
        h._world_operation_capability(row, op)
        h._world_actor_ready(sid, actor)
        position = h._public_scope(sid, actor, package)
        if (position.scene_id, position.cell) != (op.scene_id, op.expected_cell):
            raise StateMachineError("position_conflict", "Actor position changed")
        h.objects.containers.reachable(sid, actor, op.object_id, package)
        c = self.config(row["session_id"], op.object_id)
        if c is None:
            raise StateMachineError(
                "unsupported_object_capability", "Object has no light capability"
            )
        if h.facts.object_hp(sid, op.object_id) == 0:
            raise StateMachineError("object_destroyed", "Destroyed emitter is unusable")
        if type(op) is not SetLightEnabled and op.mode not in {m.mode_id for m in c.modes}:
            raise StateMachineError("unsupported_light_mode", "Undeclared light mode")

    def operate(self, row, actor, op, package, *, origin=None):
        h, sid = self.h, row["session_id"]
        if origin is None:
            self.validate(row, actor, op, package)
        else:
            h.objects.mechanisms.qualify_link(
                sid, origin, op.object_id, "set_light_enabled", op.enabled, package
            )
        c = self.config(row["session_id"], op.object_id)
        if c is None or h.facts.object_hp(sid, op.object_id) == 0:
            raise StateMachineError("unsupported_object_capability", "Emitter unavailable")
        old = h.facts.light_state(sid, op.object_id)
        enabled = op.enabled if type(op) is SetLightEnabled else old["enabled"]
        mode = old["mode"] if type(op) is SetLightEnabled else op.mode
        if mode not in {m.mode_id for m in c.modes}:
            raise StateMachineError("unsupported_light_mode", "Undeclared light mode")
        if old == {"enabled": enabled, "mode": mode}:
            return []
        payload = {
            **asdict(op),
            "actor_id": actor,
            "enabled": enabled,
            "mode": mode,
            **({"execution": _execution_data(None, origin)} if origin else {}),
        }
        events = [
            h._capture_event(
                sid,
                package,
                "object.light_changed",
                payload,
                phase="world.before_light_update",
                source_actor_id=op.object_id,
            )
        ]
        h.facts.set_light_state(sid, op.object_id, enabled, mode)
        events.append(
            h._capture_event(
                sid,
                package,
                "object.light_changed",
                payload,
                phase="world.after_light_update",
                source_actor_id=op.object_id,
            )
        )
        return events

    def query_illumination(self, sid, actor, cell, package, *, context):
        if type(cell) is not str or not 0 < len(cell) <= 128:
            raise StateMachineError("invalid_query", "Cell must be a bounded coordinate string")
        h = self.h
        with h._transaction():
            position = h._spatial_reader(sid, actor, package, context)
            h._public_scope(sid, actor, package)
            raw = h._spatial_scene(sid, position.scene_id)["grid"]
            topology = h.grid.reader.topology_for_grid(sid, position.scene_id, raw)
            try:
                visible_cell = topology.has_line_of_sight(position.cell, cell)
            except (ValueError, TypeError) as exc:
                raise StateMachineError("invalid_query", "Invalid grid coordinate") from exc
            if not visible_cell:
                raise StateMachineError("invalid_target", "No authorized local position")
            level, uncertain, sources = self.current_cell(sid, position.scene_id, package, cell)
            visible = []
            for oid in sources:
                try:
                    h.objects._visible(sid, actor, oid, package)
                    visible.append(oid)
                except StateMachineError:
                    pass
            return {
                "cell": cell,
                "illumination": None if uncertain else level,
                "status": "unknown" if uncertain else "known",
                "contributing_sources": tuple(visible),
                "sunlight": raw["sunlight"],
            }
