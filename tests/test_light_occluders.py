from copy import deepcopy
from dataclasses import replace

import pytest
from lock_support import DOOR, A, B
from test_object_spatial_service import changed_grid, oriented_data
from test_world_spatial import OBSTACLE
from world_support import setup, submit

from trpg_state_machine import RepositionObject
from trpg_state_machine.grid_maps import PreparedGrid
from trpg_state_machine.instance_data import passage_index


@pytest.mark.parametrize("unrelated", (0, 48))
def test_light_occluders_select_only_intersecting_footprints(
    tmp_path, context, monkeypatch, unrelated
):
    data = oriented_data(anchor="1,1", blocking=False)
    changed_grid(data, width=50, height=8)
    data["world_objects"][-1]["capabilities"][0]["closed_properties"] = {"light_opaque": True}
    for x in range(unrelated):
        other = f"object.far_opaque_{x}"
        data["scenes"][0]["objects"].append(
            {
                **data["scenes"][0]["objects"][-1],
                "object_id": other,
                "initial_cell": f"{x},6",
            }
        )
        definition = deepcopy(data["world_objects"][-1])
        definition.update(object_id=other, definition_ref=f"definition.far_opaque_{x}")
        definition["capabilities"][0]["footprint"] = [[0, 0]]
        data["world_objects"].append(definition)
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        grid = sm._world.grid
        spatial = sm._objects.spatial
        opacity = spatial.light_occlusion
        read_cells = grid.reader.region_occupants
        visited, fetched, plans = [], [], []

        def count_object(sid, oid, package):
            visited.append(oid)
            return opacity(sid, oid, package)

        def count_cells(sid, scene, cells, *, include_actors=False):
            assert not include_actors
            actors, objects = read_cells(sid, scene, cells)
            assert actors == ()
            rows = list(objects)
            fetched.append(len(rows))
            return actors, rows

        def trace(statement):
            if statement.startswith("SELECT object_id,cell,blocks FROM object_spatial_cells"):
                plans.extend(
                    r[3] for r in sm._store._conn.execute("EXPLAIN QUERY PLAN " + statement)
                )

        monkeypatch.setattr(spatial, "light_occlusion", count_object)
        monkeypatch.setattr(grid.reader, "region_occupants", count_cells)
        sm._store._conn.set_trace_callback(trace)
        try:
            remote_arm = grid.light_occluders(sid, A, package, ("2,1",))
            both_cells = grid.light_occluders(sid, A, package, ("1,1", "2,1", "2,1"))
        finally:
            sm._store._conn.set_trace_callback(None)
        assert remote_arm.opaque_objects == ((OBSTACLE, ("2,1",)),)
        assert both_cells.opaque_objects == ((OBSTACLE, ("1,1", "2,1")),)
        assert visited == [OBSTACLE, OBSTACLE]
        assert fetched == [1, 2]
        assert sum("spatial_cell_lookup" in plan and "cell=?" in plan for plan in plans) == 2

        gm = replace(context, can_place_world_objects=True)
        rotated = RepositionObject(A, OBSTACLE, "1,1", "1,1", "north", "east")
        assert submit(sm, sid, package, gm, rotated, "rotate.opaque").status == "committed"
        assert grid.light_occluders(sid, A, package, ("2,1",)).opaque_objects == ()
        assert grid.light_occluders(sid, A, package, ("1,2",)).opaque_objects == (
            (OBSTACLE, ("1,2",)),
        )
        assert remote_arm.opaque_objects == ((OBSTACLE, ("2,1",)),)


def test_regional_occupancy_can_find_actors_using_the_cell_index(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, oriented_data(blocking=False))
    with sm:
        actor_id = sm.read_session(sid, package, context=context).pc_actor_id
        reader = sm._world.grid.reader
        connection = sm._store._conn
        plans = []

        def trace(statement):
            if statement.startswith("SELECT actor_id,cell FROM actors"):
                plans.extend(r[3] for r in connection.execute("EXPLAIN QUERY PLAN " + statement))

        connection.set_trace_callback(trace)
        try:
            with reader.read_scope():
                actors, objects = reader.region_occupants(
                    sid, A, ("0,0", "2,1"), include_actors=True
                )
                assert [tuple(actor) for actor in actors] == [(actor_id, "0,0")]
                assert [tuple(obj) for obj in objects] == [(OBSTACLE, "2,1", 0)]
        finally:
            connection.set_trace_callback(None)
        assert sum("actors_cell_lookup" in plan and "cell=?" in plan for plan in plans) == 1


def test_fixed_map_and_legacy_uncertainty_share_the_selected_bounds(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, oriented_data())
    with sm:
        grid = sm._world.grid
        prepared = grid.reader.current_grid(sid, A, package)
        raw = prepared.copy_data()
        touching = (2.0, 2.0, 2.5, 2.5)
        raw["wall_segments"] = [
            dict(zip(("x1", "y1", "x2", "y2"), wall, strict=True))
            for wall in (touching, (0.0, 2.0, 0.5, 2.0))
        ]
        raw["blocked_cells"] = ["2,1"]
        raw["cover_cells"] = {"2,1": "total", "0,0": "total"}
        fixed = PreparedGrid.build(raw)
        result = grid.light_occluders(sid, A, package, ("2,1",), fixed_grid=fixed)
        assert result.walls == (touching,)
        assert result.opaque_cells == {"2,1"}
        assert result.uncertain_cells == {"2,1"}
        assert result.opaque_objects == ()
        assert grid.light_occluders(sid, A, package, ("2,1",)).opaque_cells == set()


def test_declared_opacity_changes_without_movement_and_invalidates_light(tmp_path, context):
    data = oriented_data(openable=True)
    props = data["world_objects"][-1]["capabilities"][0]
    props.update(closed_properties={"light_opaque": True}, open_properties={"light_opaque": False})
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        grid = sm._world.grid
        memory = sm._store.memory
        cells = ("2,1",)
        with sm._transaction(write=True):
            dep = memory.maintained_dependency("light_candidates", sid, A, ())
            old_stamp = memory.version(dep)
            assert grid.light_occluders(sid, A, package, cells).opaque_objects == (
                (OBSTACLE, cells),
            )
            with pytest.raises(ValueError, match="discard"), sm._store.savepoint():
                sm._world_facts.switch(sid, OBSTACLE, "open")
                updated = grid.light_occluders(sid, A, package, cells)
                assert not updated.opaque_objects and not updated.uncertain_cells
                assert memory.version(dep) > old_stamp
                # The footprint still blocks movement in both spatial states.
                assert sm._objects.spatial.blocks(sid, OBSTACLE, package)
                raise ValueError("discard")
            assert memory.version(dep) == old_stamp
            assert grid.light_occluders(sid, A, package, cells).opaque_objects == (
                (OBSTACLE, cells),
            )


def test_shared_door_lookup_uses_remote_threshold_and_rolls_back(tmp_path, context):
    data = oriented_data(blocking=False)
    data["lockable_doors"] = []
    scene = data["scenes"][0]
    next(o for o in scene["objects"] if o["object_id"] == DOOR)["state"] = "open"
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        grid = sm._world.grid
        with sm._transaction(write=True):
            sm.scene_lifecycle.ensure_scene(sid, B, package)
        endpoints = passage_index(sm._store, sid).by_door[DOOR][0].endpoints
        baseline = {
            endpoint.scene_id: grid.light_occluders(
                sid, endpoint.scene_id, package, (endpoint.threshold_cell,)
            )
            for endpoint in endpoints
        }
        assert all(not result.opaque_cells for result in baseline.values())
        with sm._transaction(write=True):
            with pytest.raises(ValueError, match="discard"), sm._store.savepoint():
                sm._world_facts.switch(sid, DOOR, "closed")
                for endpoint in endpoints:
                    result = grid.light_occluders(
                        sid, endpoint.scene_id, package, (endpoint.threshold_cell,)
                    )
                    assert result.opaque_cells == {endpoint.threshold_cell}
                raise ValueError("discard")
            for endpoint in endpoints:
                assert (
                    grid.light_occluders(
                        sid, endpoint.scene_id, package, (endpoint.threshold_cell,)
                    )
                    == baseline[endpoint.scene_id]
                )
