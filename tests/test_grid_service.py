"""Current geometry through real Session facts/commands and SQLite read scopes."""

from dataclasses import FrozenInstanceError, replace

import pytest
from lock_support import DOOR, NPC, A, B, lock_loader
from spatial_support import create
from test_domain_runtime import components
from test_migrations import stored_rows
from test_object_spatial_service import changed_grid, oriented_data
from test_world_spatial import OBSTACLE, spatial_world_data
from world_support import command, setup, submit, world_data

from trpg_state_machine import (
    ObjectState,
    RepositionObject,
    SetObjectState,
    StartNavigation,
    StateMachine,
    StateMachineError,
    StepActor,
)
from trpg_state_machine.package import _validate
from trpg_state_machine.spatial import SpatialPosition


def test_coordinates_occupancy_distance_neighbors_and_templates(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, spatial_world_data())
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        before = stored_rows(db)
        changes = sm._conn.total_changes
        sm._conn.execute("PRAGMA query_only=ON")
        view = sm._world.grid.read_scene(sid, A, p)
        assert (view.width, view.height, view.cell_size_ft) == (3, 3, 5)
        assert view.normalize_cell(" +01, 02") == "1,2"
        assert view.entity_cells("actor", actor) == ("0,0",)
        assert view.entity_cells("object", OBSTACLE) == ("2,0", "2,1")
        assert ("object", OBSTACLE) in {(o.kind, o.entity_id) for o in view.occupied(("2,1",))}
        assert view.distance_ft("0,0", "2,2") == 10
        assert view.is_adjacent("1,0", "2,1")  # Occupied/blocked still adjacent geometrically.
        assert not view.can_step("1,0", "2,1")
        assert not view.can_step("2,1", "1,0")
        assert set(view.neighbors("0,0")) == {"0,1", "1,0", "1,1"}
        assert set(view.cells_within_range("0,0", 5)) == {"0,0", "0,1", "1,0", "1,1"}
        assert any(o.entity_id == OBSTACLE for o in view.entities_within_range("1,1", 5))
        assert set(view.cells_in_template("0,0", "sphere", 5)) == {"0,0", "0,1", "1,0", "1,1"}
        assert view.cells_in_template("0,0", "line", 10, direction=(1, 0)) == ("0,0", "1,0", "2,0")
        with pytest.raises(FrozenInstanceError):
            view.world_version = 999
        with pytest.raises(StateMachineError) as exc:
            view.entity_cells("actor", "unknown")
        assert exc.value.code == "invalid_target"
        assert sm._conn.total_changes == changes
        assert stored_rows(db) == before


@pytest.mark.parametrize("cell", [None, 1, True, "", "1", "1,2,3", "1.2,1", "-1,0", "3,0"])
def test_invalid_coordinate_never_means_open_space(tmp_path, context, cell):
    sm, sid, p, _ = setup(tmp_path, context)
    with sm:
        view = sm._world.grid.read_scene(sid, A, p)
        for query in (
            view.normalize_cell,
            lambda c: view.has_line_of_sight("0,0", c),
            lambda c: view.cover_between("0,0", c),
        ):
            with pytest.raises(StateMachineError) as exc:
                query(cell)
            assert exc.value.code == "invalid_cell"


def test_channel_separation_and_current_openable_properties(tmp_path, context):
    data = oriented_data(anchor="2,0", footprint=[[0, 0], [-1, 0]], openable=True)
    c = data["world_objects"][-1]["capabilities"][0]
    c.update(
        open_blocks_movement=False,
        closed_properties={"vision_occlusion": True, "cover": "three_quarters"},
        open_properties={"difficult_terrain": True, "cover": "half"},
    )
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        grid = sm._world.grid
        closed = grid.read_scene(sid, A, p)
        assert "1,0" in closed.movement_blocking_cells()
        assert closed.difficult_terrain_cells() == ()
        assert closed.cover_between("0,0", "2,0") == "three_quarters"
        with pytest.raises(StateMachineError) as exc:
            closed.has_line_of_sight("0,0", "2,0")
        assert exc.value.code == "unsupported_grid_vision"
        assert (
            submit(
                sm, sid, p, context, SetObjectState(A, OBSTACLE, ObjectState.OPEN), "open"
            ).status
            == "committed"
        )
        opened = grid.read_scene(sid, A, p)
        assert opened.world_version > closed.world_version
        assert opened.entity_cells("object", OBSTACLE) == closed.entity_cells("object", OBSTACLE)
        assert "1,0" not in opened.movement_blocking_cells()
        assert opened.difficult_terrain_cells() == ("1,0", "2,0")
        assert opened.can_step("0,0", "1,0")
        assert opened.has_line_of_sight("0,0", "2,0")
        assert opened.cover_between("0,0", "2,0") == "half"
        assert dict(opened.objects)[OBSTACLE].vision_occlusion_cells == ()


def test_movement_only_object_and_creature_are_not_vision_walls(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, spatial_world_data())
    with sm:
        view = sm._world.grid.read_scene(sid, A, p)
        assert not view.can_step("1,0", "2,0")
        assert view.has_line_of_sight("0,0", "2,0")
        assert view.cover_between("0,0", "2,0") == "none"
        assert "2,2" not in view.movement_blocking_cells()  # Character anchor, not terrain.
        assert view.can_step("1,2", "2,2")
        assert sm._world.grid.exploration_topology(sid, A, p).edge_distance("1,2", "2,2") is None
        assert view.cover_between("0,2", "2,2", creature_cover=True) == "none"
        # A creature strictly between endpoints contributes RE half cover only on request.
        with sm._transaction(write=True):
            sm._world.characters.move(sid, NPC, SpatialPosition(A, "1,2"))
            current = sm._world.grid.read_scene(sid, A, p)
            assert current.cover_between("0,2", "2,2") == "none"
            assert current.cover_between("0,2", "2,2", creature_cover=True) == "half"
            assert current.has_line_of_sight("0,2", "2,2")


@pytest.mark.parametrize("geometry", ["wall", "corner", "terrain"])
def test_wall_corner_and_difficult_terrain_through_public_step(tmp_path, context, geometry):
    data = oriented_data(anchor="2,0", footprint=[[0, 0]], blocking=False)
    if geometry == "wall":
        changed_grid(data, wall_segments=[{"x1": 1, "y1": 0, "x2": 1, "y2": 1}])
    elif geometry == "corner":
        changed_grid(data, blocked_cells=["1,0"])
    else:
        changed_grid(data, difficult_terrain_cells=["1,0"])
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        view = sm._world.grid.read_scene(sid, A, p)
        target = "1,1" if geometry == "corner" else "1,0"
        assert view.distance_ft("0,0", target) == 5
        before = sm._world.characters.rule_components(sid, {actor})
        tick = sm._world.facts.time_state(sid)
        result = submit(sm, sid, p, context, StepActor(A, "0,0", target), "step")
        assert view.can_step("0,0", target) == (geometry == "terrain")
        if geometry == "terrain":
            assert result.status == "committed"
            after = sm._world.characters.rule_components(sid, {actor})
            assert after == before  # No combat action economy/payment on an exploration step.
            assert sm._world.facts.time_state(sid) == tick
            assert sm._world.grid.read_scene(sid, A, p).entity_cells("actor", actor) == (target,)
        else:
            assert result.error_code == "movement_unavailable"
        if geometry == "wall":
            assert not view.has_line_of_sight("0,0", "1,0")
            assert view.cover_between("0,0", "1,0") == "total"


def test_shared_door_uses_both_endpoints_and_updates_after_real_open(tmp_path, context):
    data = world_data()
    data["lockable_doors"] = []
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        grid = sm._world.grid
        ports = [(e.scene_id, e.threshold_cell) for e in p.passages[0].endpoints]
        for scene, cell in ports:
            view = grid.read_scene(sid, scene, p)
            assert view.entity_cells("object", DOOR) == (cell,)
            assert cell in view.movement_blocking_cells()
        assert (
            submit(sm, sid, p, context, SetObjectState(A, DOOR, ObjectState.OPEN), "open").status
            == "committed"
        )
        for scene, cell in ports:
            assert cell not in grid.read_scene(sid, scene, p).movement_blocking_cells()


def test_placement_rotation_commit_rollback_and_frozen_view(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, oriented_data())
    gm = replace(context, can_place_world_objects=True)
    with sm:
        grid = sm._world.grid
        original = grid.read_scene(sid, A, p)
        before = stored_rows(db)
        with pytest.raises(RuntimeError), sm._transaction(write=True):
            sm._world.facts.place(sid, OBSTACLE, "1,1", orientation="east")
            interim = grid.read_scene(sid, A, p)
            assert interim.entity_cells("object", OBSTACLE) == ("1,1", "1,2")
            raise RuntimeError("rollback")
        assert grid.read_scene(sid, A, p) == original
        assert stored_rows(db) == before
        op = RepositionObject(A, OBSTACLE, "1,1", "1,1", "north", "east")
        assert submit(sm, sid, p, gm, op, "rotate").status == "committed"
        committed = grid.read_scene(sid, A, p)
        assert committed.entity_cells("object", OBSTACLE) == ("1,1", "1,2")
        assert original.entity_cells("object", OBSTACLE) == ("1,1", "2,1")
        assert committed.world_version > original.world_version


def test_one_sqlite_snapshot_during_concurrent_real_command(tmp_path, context, monkeypatch):
    sm, sid, p, db = setup(tmp_path, context, spatial_world_data())
    gm = replace(context, can_place_world_objects=True)
    with sm, StateMachine(db, dnd_loader=lock_loader()) as other:
        original = sm._world.grid.read_scene(sid, A, p)
        cmd = command(other, sid, p, gm, RepositionObject(A, OBSTACLE, "2,0", "1,0"), "concurrent")
        project = sm._world.objects.spatial.scene_contributions

        def interleave(*args):
            monkeypatch.setattr(sm._world.objects.spatial, "scene_contributions", project)
            assert other.submit_command(cmd, p, context=gm).status == "committed"
            return project(*args)

        monkeypatch.setattr(sm._world.objects.spatial, "scene_contributions", interleave)
        assert sm._world.grid.read_scene(sid, A, p) == original
        current = sm._world.grid.read_scene(sid, A, p)
        assert current.world_version > original.world_version
        assert current.entity_cells("object", OBSTACLE) == ("1,0", "1,1")


def test_missing_live_grid_or_position_does_not_fall_back_to_package(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context)
    with sm:
        grid = sm._world.grid
        before = stored_rows(db)
        with pytest.raises(StateMachineError) as exc, sm._transaction(write=True):
            # Fault injection only: simulate incomplete persisted spatial facts.
            sm._conn.execute(
                "DELETE FROM mechanical_scenes WHERE session_id=? AND scene_id=?", (sid, A)
            )
            grid.read_scene(sid, A, p)
        assert exc.value.code == "spatial_unconfigured"
        assert stored_rows(db) == before
        with pytest.raises(StateMachineError) as exc, sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE actors SET cell=NULL WHERE session_id=? AND actor_id=?", (sid, NPC)
            )
            grid.read_scene(sid, A, p)
        assert exc.value.code == "invalid_spatial_state"
        assert stored_rows(db) == before
        with pytest.raises(StateMachineError) as exc, sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE actors SET cell='01,0' WHERE session_id=? AND actor_id=?", (sid, NPC)
            )
            grid.exploration_topology(sid, A, p)
        assert exc.value.code == "invalid_spatial_state"
        assert stored_rows(db) == before


def test_uninitialized_scene_read_is_initial_facts_without_writes(tmp_path, package, context):
    with StateMachine(tmp_path / "abstract.db") as sm:
        sid = create(sm, package, context)
        before = sm._conn.total_changes
        with pytest.raises(StateMachineError) as exc:
            sm._world.grid.read_scene(sid, A, package)
        assert exc.value.code == "spatial_unconfigured"
        assert sm._conn.total_changes == before
    # Real shared-store components before any lifecycle initialization.
    c = components(tmp_path / "lazy.db", loader=lock_loader())
    p = _validate(world_data())
    sid = "session.lazy"
    with c.store.transaction(write=True):
        c.world.runtime.initialize(sid, p, context, p.pc_templates[0], "pc.lazy", "rng.lazy")
        c.facts.initialize_world(sid, p)
        c.world.scenes.ensure_scene(sid, A, p)
        c.characters.initialize_pc(sid, "pc.lazy", A, p.pc_templates[0])
        c.world.runtime.initialize_rng(sid, "rng.lazy", synthetic=False)
    try:
        before = stored_rows(tmp_path / "lazy.db")
        changes = c.store.connection.total_changes
        result = c.world.grid.read_scene(sid, B, p)
        assert result.width == 3
        assert c.store.connection.total_changes == changes
        assert stored_rows(tmp_path / "lazy.db") == before
        assert c.facts.spatial_scene(sid, B) is None
        # Existing instances in a different location must never be resurrected at defaults.
        with c.store.transaction(write=True):
            c.world.scenes.ensure_scene(sid, A, p)
            c.characters.move(sid, NPC, SpatialPosition(A, "1,2"))
            local = c.world.grid.read_scene(sid, A, p)
            assert local.entity_cells("actor", NPC) == ("1,2",)
    finally:
        c.store.close()


def test_authorized_views_hide_dark_objects_and_navigation_requires_map_grants(tmp_path, context):
    data = oriented_data(anchor="2,0", footprint=[[0, 0], [0, 1]])
    changed_grid(data, lighting={"2,0": "dark", "2,1": "dark", "2,2": "dark"})
    data["navigation"]["pc_template_grants"] = []
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        internal = sm._world.grid.read_scene(sid, A, p)
        assert internal.entity_cells("object", OBSTACLE) == ("2,0", "2,1")
        assert OBSTACLE not in {
            o["object_id"] for o in sm.list_scene_objects(sid, actor, p, context=context)
        }
        assert OBSTACLE not in {
            o[0] for o in sm.get_spatial_view(sid, actor, p, context=context).objects
        }
        with pytest.raises(StateMachineError):
            sm.get_object(sid, actor, OBSTACLE, p, context=context)
        receipt = submit(sm, sid, p, context, StartNavigation("activity.secret", B, "0,0"), "plan")
        assert receipt.error_code == "navigation_knowledge_unavailable"


def test_cover_sources_combine_without_movement_or_vision_inference(tmp_path, context):
    data = oriented_data(anchor="1,0", footprint=[[0, 0]], blocking=False)
    data["world_objects"][-1]["capabilities"][0]["closed_properties"] = {"cover": "half"}
    changed_grid(data, cover_cells={"2,0": "three_quarters"})
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        view = sm._world.grid.read_scene(sid, A, p)
        assert view.cover_between("0,0", "2,0") == "three_quarters"
        assert view.cover_between("0,0", "1,0") == "half"
        assert view.can_step("0,0", "1,0")
        assert view.has_line_of_sight("0,0", "2,0")


def test_invalid_ranges_templates_and_missing_direction_fail_explicitly(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context)
    with sm:
        view = sm._world.grid.read_scene(sid, A, p)
        for value in (-1, True, float("inf"), float("nan"), "5"):
            with pytest.raises(StateMachineError) as exc:
                view.cells_within_range("0,0", value)
            assert exc.value.code == "invalid_query"
        for args, kwargs in (
            (("sphere", -1), {}),
            (("sphere", True), {}),
            (("line", 5), {"direction": (0, 0)}),
            (("line", 5), {"direction": (True, 0)}),
            (("line", 5), {"width_ft": 0}),
        ):
            with pytest.raises(StateMachineError) as exc:
                view.cells_in_template("0,0", *args, **kwargs)
            assert exc.value.code == "invalid_query"
        for shape in ("line", "unknown"):
            with pytest.raises(StateMachineError) as exc:
                view.cells_in_template("0,0", shape, 5)
            assert exc.value.code == "unsupported_grid_template"
