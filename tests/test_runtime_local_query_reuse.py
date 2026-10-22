"""Public-path and fixed-view work counters, including RAM and geometry work."""

import json
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace

import pytest
from dnd5e_engine import spatial as re_spatial
from lock_support import CRED, A
from test_object_spatial_service import changed_grid, oriented_data
from test_scoped_runtime_validation import measure
from test_world_lights import lighting_data
from test_world_spatial import OBSTACLE
from world_support import setup, submit, world_data

from trpg_state_machine import PlaceObject, RepositionObject, grid_maps, world_spatial
from trpg_state_machine.object_protection import (
    CurrentProtectionProperties,
    RainProtection,
    WindProtection,
)


@contextmanager
def work(sm, monkeypatch):
    counts = Counter()
    memory = sm._store.memory
    before = dict(memory.stats)
    with monkeypatch.context() as patch, measure(sm) as (sql, _):
        for obj, name, label in (
            (memory, "version", "dependency_checks"),
            (memory, "select", "ram_queries"),
            (grid_maps.ReadOnlyTopology, "__init__", "topology_builds"),
            (re_spatial, "_segments_intersect", "wall_tests"),
        ):
            original = getattr(obj, name)

            def counted(*args, _original=original, _label=label, **kwargs):
                counts[_label] += 1
                return _original(*args, **kwargs)

            patch.setattr(obj, name, counted)
        original_copy = grid_maps.PreparedGrid.copy_data

        def copied(prepared):
            counts["map_copies"] += 1
            counts["copied_blocks"] += len(prepared.data["blocked_cells"])
            return original_copy(prepared)

        patch.setattr(grid_maps.PreparedGrid, "copy_data", copied)

        def wall_coordinate(value):
            counts["wall_qualification_coordinates"] += 1
            return float(value)

        patch.setattr(world_spatial, "float", wall_coordinate, raising=False)
        patch.setattr(grid_maps, "float", wall_coordinate, raising=False)
        if hasattr(grid_maps, "_WallIndex"):
            original_walls = grid_maps._WallIndex.query

            def wall_candidates(index, bounds):
                counts["wall_index_nodes"] += 1
                counts["wall_index_leaf_records"] += len(index.walls)
                yield from original_walls(index, bounds)

            patch.setattr(grid_maps._WallIndex, "query", wall_candidates)
        original_encode = getattr(world_spatial, "canonical_json", None)
        if original_encode:

            def encoded(value):
                counts["map_encodings"] += int(isinstance(value, dict) and "width" in value)
                return original_encode(value)

            patch.setattr(world_spatial, "canonical_json", encoded)
        original_load = json.loads

        def loaded(*args, **kwargs):
            value = original_load(*args, **kwargs)
            counts["map_parses"] += int(isinstance(value, dict) and "width" in value)
            return value

        patch.setattr(json, "loads", loaded)
        original_occupants = sm._world.grid.reader.scene_occupants

        def occupants(*args, **kwargs):
            actors, objects = original_occupants(*args, **kwargs)
            counts["scene_actor_records"] += len(actors)
            counts["scene_object_records"] += len(objects)
            return actors, objects

        patch.setattr(sm._world.grid.reader, "scene_occupants", occupants)
        yield counts
        counts.update(sql)
        for key, value in before.items():
            if not key.endswith("_ns"):
                counts[key] = memory.stats[key] - value


def many_lights(n):
    data = lighting_data()
    obj = next(o for o in data["scenes"][0]["objects"] if o["object_id"] == CRED)
    profile = next(o for o in data["world_objects"] if o["object_id"] == CRED)
    for i in range(n):
        oid = f"object.off_light_{i}"
        data["scenes"][0]["objects"].append({**deepcopy(obj), "object_id": oid})
        data["world_objects"].append(
            {**deepcopy(profile), "object_id": oid, "definition_ref": f"definition.off_{i}"}
        )
    return data


def mapped_data(size):
    data = oriented_data(anchor="1,1", footprint=[[0, 0]], blocking=False)
    changed_grid(
        data,
        width=size,
        height=size,
        blocked_cells=[f"{x},{y}" for y in range(5, size) for x in range(size)],
        wall_segments=[
            {"x1": x, "y1": y, "x2": x + 1, "y2": y} for y in range(6, size) for x in range(size)
        ],
    )
    return data


def add_actors(sm, sid, n):
    with sm._transaction(write=True):
        for i in range(n):
            sm._conn.execute(
                "INSERT INTO actors(session_id,actor_id,kind,current_scene_id,cell) "
                "VALUES (?,?,'npc',?,?)",
                (sid, f"npc.far_{i}", A, f"{i + 5},4"),
            )
            sm._world_facts.instances.put(
                sid,
                "actor",
                f"npc.far_{i}",
                {"actor_id": f"npc.far_{i}", "senses": (("visual", True), ("auditory", True))},
            )


@pytest.mark.parametrize("n", (10, 100))
def test_light_scalar_hit_cost(tmp_path, context, monkeypatch, n):
    sm, sid, package, _ = setup(tmp_path, context, many_lights(n))
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id

        def read(cell="1,0"):
            return sm.get_illumination(sid, actor, cell, package, context=context)

        assert read()["illumination"] == "dark"
        with work(sm, monkeypatch) as counts:
            result = read()
        print("LOCAL light_hit", n, dict(counts))
        assert result["contributing_sources"] == ()
        assert counts["instance_builds"] == 0
        assert counts["dependency_checks"] <= 20
        assert counts["map_copies"] == counts["map_parses"] == 0
        with work(sm, monkeypatch) as other:
            assert read("0,1")["illumination"] == "dark"
        print("LOCAL light_new_cell", n, dict(other))
        assert other["dependency_checks"] <= 25


@pytest.mark.parametrize("n", (0, 100))
def test_public_place_uses_target_occupancy(tmp_path, context, monkeypatch, n):
    data = world_data()
    data["scenes"][0]["objects"].append(
        {
            "object_id": "object.crate",
            "state": None,
            "movable": True,
            "blocks_passage": True,
            "initial_cell": "1,0",
        }
    )
    data["world_objects"].append(
        {
            **deepcopy(data["world_objects"][1]),
            "object_id": "object.crate",
            "definition_ref": "definition.crate",
            "capabilities": [],
        }
    )
    changed_grid(data, width=n + 10, height=8)
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        add_actors(sm, sid, n)
        op = PlaceObject("object.crate", A, "1,0", "0,1")

        def query():
            return sm.query_world_action(sid, actor, op, package, context=context)

        first = query()
        assert first.status == "available", first
        with work(sm, monkeypatch) as counts:
            assert query().status == "available"
        print("LOCAL place", n, dict(counts))
        assert counts["scene_actor_records"] == counts["scene_object_records"] == 0
        receipt = submit(sm, sid, package, context, op, "place.local")
        assert receipt.status == "committed", receipt


class ObservedOccupants(tuple):
    counts = None

    def __iter__(self):
        for value in super().__iter__():
            self.counts["occupant_visits"] += 1
            yield value


@pytest.mark.parametrize("n", (0, 100))
def test_fixed_view_point_indices(tmp_path, context, monkeypatch, n):
    data = mapped_data(8)
    changed_grid(data, width=n + 10)
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        add_actors(sm, sid, n)
        view = sm._world.grid.read_scene(sid, A, package)
        observed = ObservedOccupants(view.occupants)
        object.__setattr__(view, "occupants", observed)
        with work(sm, monkeypatch) as counts:
            observed.counts = counts
            assert len(view.occupied(("0,0",))) == 1
            assert view.entity_cells("object", OBSTACLE) == ("1,1",)
        print("LOCAL view_point", n, dict(counts))
        assert counts["occupant_visits"] == 0


@pytest.mark.parametrize("size", (8, 64))
@pytest.mark.parametrize("name", ("can_step", "cover_between"))
def test_fixed_view_geometry_reuse(tmp_path, context, monkeypatch, size, name):
    sm, sid, package, _ = setup(tmp_path, context, mapped_data(size))
    with sm:
        view = sm._world.grid.read_scene(sid, A, package)
        with work(sm, monkeypatch) as counts:
            getattr(view, name)("0,0", "1,0")
        print("LOCAL view_" + name, size, dict(counts))
        assert counts["map_copies"] == counts["topology_builds"] == 0
        assert counts["wall_tests"] <= 4
        assert counts["wall_index_leaf_records"] <= 8


@pytest.mark.parametrize("size", (8, 64))
def test_public_shape_check_reuses_map_qualification(tmp_path, context, monkeypatch, size):
    sm, sid, package, _ = setup(tmp_path, context, mapped_data(size))
    gm = replace(context, can_place_world_objects=True)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        op = RepositionObject(A, OBSTACLE, "1,1", "2,1")

        def query():
            return sm.query_world_action(sid, actor, op, package, context=gm)

        assert query().status == "available"
        with work(sm, monkeypatch) as counts:
            assert query().status == "available"
        print("LOCAL shape", size, dict(counts))
        assert counts["map_copies"] == counts["map_encodings"] == 0
        assert counts["wall_qualification_coordinates"] == 0
        assert counts["wall_tests"] <= 4
        assert counts["wall_index_leaf_records"] <= 8
        assert submit(sm, sid, package, gm, op, "shape.local").status == "committed"


@pytest.mark.parametrize("n", (10, 100))
def test_batch_protection_does_not_encode_per_object(tmp_path, context, monkeypatch, n):
    data = mapped_data(8)
    obj, profile = data["scenes"][0]["objects"][-1], data["world_objects"][-1]
    for i in range(n):
        oid = f"object.empty_shelter_{i}"
        data["scenes"][0]["objects"].append({**deepcopy(obj), "object_id": oid})
        data["world_objects"].append(
            {**deepcopy(profile), "object_id": oid, "definition_ref": f"definition.empty_{i}"}
        )
    sm, sid, package, _ = setup(tmp_path, context, data)
    empty = CurrentProtectionProperties(RainProtection(()), WindProtection(()))

    class Provider:
        def current_protection(self, *args, **kwargs):
            return empty

    with sm:
        spatial = sm._objects.spatial
        spatial.protection_provider = Provider()
        sm._world.grid.reader.prepared_scene(sid, A, package)
        with work(sm, monkeypatch) as counts:
            prepared = spatial.prepare_scene_protection(sid, A, package)
            for direction in ("north", "east"):
                spatial.scene_protection_contributions(
                    sid, A, package, wind_from=direction, prepared=prepared
                )
        print("LOCAL protection", n, dict(counts))
        assert counts["map_copies"] == counts["map_encodings"] == counts["map_parses"] == 0
