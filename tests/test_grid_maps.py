"""Map content identity, lifecycle and bounded geometry work (no timing assertions)."""

import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from lock_support import A, B, lock_loader
from spatial_support import create
from test_domain_runtime import components, seed
from test_migrations import stored_rows
from world_support import setup, world_data

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.grid import GridOccupant, GridView
from trpg_state_machine.grid_maps import PreparedGrid, ReadOnlyTopology
from trpg_state_machine.package import _validate, canonical_json


def raw_grid(width=100, height=100, cell_size=5, **changes):
    return GridScene(width=width, height=height, cell_size_ft=cell_size, **changes).model_dump(
        mode="json"
    )


def view_of(raw, occupants=()):
    return GridView(A, 0, canonical_json(raw), occupants, ())


def full_scan(view, origin, distance):
    topology = GridTopology(GridScene(**json.loads(view.grid_json)))
    return tuple(
        f"{x},{y}"
        for y in range(view.height)
        for x in range(view.width)
        if topology.within_range(origin, f"{x},{y}", distance)
    )


@pytest.mark.parametrize("shape", [(1, 1), (4, 7), (11, 3), (100, 100)])
@pytest.mark.parametrize("size", [1, 5, 10])
@pytest.mark.parametrize("distance", [0, 0.5, 1, 4.99, 5, 5.01, 10, 19.5, 1e308, 10**400])
def test_range_matches_old_scan_with_order_boundaries_and_huge_finite_values(shape, size, distance):
    width, height = shape
    view = view_of(raw_grid(width, height, size, blocked_cells=["0,0"]))
    for origin in dict.fromkeys(
        ("0,0", f"{width // 2},{height // 2}", f"{width - 1},{height - 1}")
    ):
        assert view.cells_within_range(origin, distance) == full_scan(view, origin, distance)
        assert view.neighbors(origin) == tuple(
            c for c in full_scan(view, origin, size) if c != origin
        )


def test_small_range_nine_candidates_neighbors_eight_entities_only_actual_footprints(monkeypatch):
    occupants = (
        GridOccupant("object", "wide", ("51,50", "52,50", "53,50")),
        GridOccupant("actor", "near", ("49,50",)),
        GridOccupant("actor", "far", ("80,80",)),
    )
    view = view_of(raw_grid(), occupants)
    calls, within = [], ReadOnlyTopology.within_range

    def counted(self, origin, cell, distance):
        calls.append(cell)
        return within(self, origin, cell, distance)

    monkeypatch.setattr(ReadOnlyTopology, "within_range", counted)
    assert view.cells_within_range("50,50", 5) == full_scan(view, "50,50", 5)
    assert len(calls) == 9

    def forbidden(*args):
        raise AssertionError("Range scan must not be used")

    monkeypatch.setattr(GridView, "cells_within_range", forbidden)
    calls.clear()
    assert view.entities_within_range("50,50", 5) == occupants[:2]
    assert len(calls) == 3  # first hit short-circuits the multi-cell object
    calls.clear()
    assert view.neighbors("50,50") == (
        "49,49",
        "50,49",
        "51,49",
        "49,50",
        "51,50",
        "49,51",
        "50,51",
        "51,51",
    )
    assert len(calls) == 8
    calls.clear()
    assert len(view.neighbors("0,0")) == 3 and len(calls) == 3


@pytest.mark.parametrize("distance", [-1, True, None, "5", float("inf"), float("nan")])
def test_empty_entities_still_validate_range_and_center(distance):
    view = view_of(raw_grid())
    for method in (view.cells_within_range, view.entities_within_range):
        with pytest.raises(StateMachineError) as exc:
            method("bad", distance)
        assert exc.value.code == "invalid_query"
        with pytest.raises(StateMachineError) as exc:
            method("100,0", 5)
        assert exc.value.code == "invalid_cell"


def test_map_prepared_by_creation_and_public_views_reuse_parse_and_topology(
    tmp_path, context, monkeypatch
):
    calls, build = [], ReadOnlyTopology.__init__

    def counted(self, scene):
        calls.append((scene.width, scene.height))
        build(self, scene)

    monkeypatch.setattr(ReadOnlyTopology, "__init__", counted)
    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        maps = sm._world_facts.maps
        assert maps._records  # creation, before any test query
        prepared = maps.scene(sid, A).grid
        assert prepared.topology is sm._world.grid.base_topology(sid, A)
        calls.clear()
        import trpg_state_machine.grid_maps as module

        parses, loads = [], module.json.loads

        def decoded(raw, *args, **kwargs):
            if raw in (prepared.grid_json, canonical_json(maps.scene(sid, A).copy_data())):
                parses.append(raw)
            return loads(raw, *args, **kwargs)

        monkeypatch.setattr(module.json, "loads", decoded)
        before = stored_rows(db)
        first = sm._world.grid.read_scene(sid, A, package)
        second = sm._world.grid.read_scene(sid, A, package)
        assert first._base() is second._base() is prepared.topology
        assert set(asdict(first)) == {
            "scene_id",
            "world_version",
            "grid_json",
            "occupants",
            "objects",
        }
        for _ in range(3):
            assert (first.width, first.height, first.cell_size_ft) == (3, 3, 5)
            assert first.normalize_cell("1,1") == "1,1"
            assert first.distance_ft("0,0", "1,1") == 5
            assert first.cells_within_range("1,1", 0) == ("1,1",)
        assert sm._world.grid.base_topology(sid, A, package) is prepared.topology
        assert sm._world_facts.spatial_scene(sid, A)["grid"]["width"] == 3
        from trpg_state_machine.environment_models import EnvironmentArea

        with sm._transaction():
            assert sm.environment._area(sid, A, EnvironmentArea(("0,0",)), package) == ("0,0",)
        assert not parses and not calls
        assert stored_rows(db) == before


def update_grid(sm, sid, scene=A, **changes):
    # Fault/configuration injection: no map update command is added by this batch.
    row = sm._conn.execute(
        "SELECT state_json FROM mechanical_scenes WHERE session_id=? AND scene_id=?", (sid, scene)
    ).fetchone()
    data = json.loads(row[0])
    data["grid"].update(changes)
    sm._conn.execute(
        "UPDATE mechanical_scenes SET state_json=? WHERE session_id=? AND scene_id=?",
        (canonical_json(data), sid, scene),
    )


def test_changed_map_savepoint_rollback_and_same_world_version_are_content_scoped(
    tmp_path, context
):
    sm, sid, package, db = setup(tmp_path, context)
    with sm:
        grid = sm._world.grid
        original = grid.read_scene(sid, A, package)
        before = stored_rows(db)
        with pytest.raises(ValueError), sm._transaction(write=True):
            update_grid(sm, sid, width=9)
            candidate = grid.read_scene(sid, A, package)
            assert candidate.width == 9 and candidate.world_version == original.world_version
            with pytest.raises(ValueError), sm._store.savepoint():
                update_grid(sm, sid, width=7)
                assert grid.read_scene(sid, A, package).width == 7
                raise ValueError("savepoint rollback")
            assert grid.read_scene(sid, A, package)._map is candidate._map
            raise ValueError("outer rollback")
        assert stored_rows(db) == before
        assert grid.read_scene(sid, A, package)._map is original._map
        with sm._transaction(write=True):
            update_grid(sm, sid, width=5)
        committed = grid.read_scene(sid, A, package)
        assert committed.width == 5 and committed.world_version == candidate.world_version
        assert original.width == 3 and candidate.width == 9


def test_old_reader_and_other_connection_new_map_do_not_alias(tmp_path, context):
    sm, sid, package, db = setup(tmp_path, context)
    with sm, StateMachine(db, dnd_loader=lock_loader()) as other:
        old = sm._world.grid.read_scene(sid, A, package)
        with sm._transaction():
            assert sm._world.grid.read_scene(sid, A, package)._map is old._map
            with other._transaction(write=True):
                update_grid(other, sid, width=6)
            assert other._world.grid.read_scene(sid, A, package).width == 6
            assert sm._world.grid.read_scene(sid, A, package)._map is old._map
        assert sm._world.grid.read_scene(sid, A, package).width == 6
        assert old.width == 3


def test_nested_input_outputs_and_topology_are_read_only_and_old_view_survives_close(
    tmp_path, context
):
    raw = raw_grid(4, 4, wall_segments=[dict(x1=1, y1=0, x2=1, y2=4)])
    view = view_of(raw)
    raw["wall_segments"][0]["x1"] = 100
    raw["blocked_cells"].append("0,0")
    assert view.can_step("0,0", "0,1")
    assert not view.has_line_of_sight("0,0", "1,0")
    copy = view._map.copy_data()
    copy["wall_segments"][0]["x1"] = 200
    for mutate in (
        lambda: view._base()._blocked.add("0,0"),
        lambda: view._base()._lighting.update({"0,0": "dark"}),
        lambda: setattr(view._base()._walls[0], "x1", 300),
        lambda: setattr(view._base(), "_width", 999),
        lambda: view._base().__dict__.update({"_width": 999}),
    ):
        with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
            mutate()
    assert not view.has_line_of_sight("0,0", "1,0")
    sm, sid, package, _ = setup(tmp_path, context)
    saved = sm._world.grid.read_scene(sid, A, package)
    maps = sm._world_facts.maps
    detached = sm._world_facts.spatial_scene(sid, A)
    detached["grid"]["width"] = 999
    assert sm._world.grid.read_scene(sid, A, package).width == 3
    sm.close()
    assert not maps._records and not maps._grids
    assert saved.distance_ft("0,0", "1,1") == 5
    assert replace(saved, grid_json=canonical_json(raw_grid(6, 6))).width == 6


def test_restore_lazy_initialization_and_failed_creation_keep_authority(
    tmp_path, context, package_data
):
    from dnd_support import create as create_dnd
    from modern_support import modern_data, modern_loader

    assets = modern_loader()
    package = _validate(modern_data(package_data, assets))
    db = tmp_path / "lazy.db"
    with StateMachine(db, dnd_loader=assets) as sm:
        sid = create_dnd(sm, package, context)
        assert sm._world_facts.maps.scene(sid, B) is None
        before = stored_rows(db)
        with pytest.raises(ValueError), sm._transaction(write=True):
            sm.scene_lifecycle.ensure_scene(sid, B, package)
            assert sm._world_facts.maps.scene(sid, B).grid is not None
            raise ValueError("lazy failure")
        assert sm._world_facts.maps.scene(sid, B) is None
        assert stored_rows(db) == before
        with sm._transaction(write=True):
            sm.scene_lifecycle.ensure_scene(sid, B, package)
        assert sm.read_session(sid, package, context=context).scenes[B].visited_count == 0
    with StateMachine(db, dnd_loader=assets) as sm:
        assert not sm._world_facts.maps._records
        sm.resume_session(sid, package, context=context)
        assert sm._world_facts.maps._records
    with StateMachine(tmp_path / "creation-fault.db", dnd_loader=assets) as sm:
        sm._conn.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON creation_commands "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        with pytest.raises(StateMachineError):
            create_dnd(sm, package, context)
        assert sm._conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        for key in sm._world_facts.maps._records:
            assert sm._world_facts.maps.scene(key[0], key[1]) is None


def test_session_database_isolation_release_and_bounded_pool(tmp_path, context):
    first = components(tmp_path / "one.db", lock_loader())
    second = components(tmp_path / "two.db", lock_loader())
    package = _validate(world_data())
    try:
        sid, _ = seed(first, package, context, sid="same")
        seed(second, package, context, sid="same")
        seed(first, package, context, sid="other", pc="pc.other")
        maps = first.facts.maps
        assert maps is not second.facts.maps
        grid = first.world.grid
        old = grid.read_scene(sid, A, package)
        raw = first.facts.mechanical_scene(sid, A)
        raw["grid"]["width"] = 8
        with first.store.transaction(write=True):
            first.store.connection.execute(
                "UPDATE mechanical_scenes SET state_json=? WHERE session_id=? AND scene_id=?",
                (canonical_json(raw), sid, A),
            )
        assert grid.read_scene(sid, A, package).width == 8
        assert grid.read_scene("other", A, package).width == 3
        assert second.world.grid.read_scene(sid, A, package).width == 3
        maps.capacity = 3
        maps.clear()
        for width in range(4, 15):
            maps.for_grid(sid, A, raw_grid(width, 3))
        assert len(maps._grids) <= 3 and len(maps._records) <= 3
        first.world.runtime.release_scene_maps(sid, maps)
        assert all(k[0] != sid for k in maps._grids)
        assert grid.read_scene(sid, A, package).width == 8
        assert old.width == 3
    finally:
        first.store.close()
        second.store.close()


def test_template_maps_stay_separate_and_navigation_reuses_them(tmp_path, context):
    from navigation_support import navigation_package, start

    package = navigation_package()
    with StateMachine(tmp_path / "navigation.db") as sm:
        sid = create(sm, package, context)
        grid = sm._world.grid
        static = grid.prepared_package_topology(sid, package, A)
        assert static is grid.prepared_package_topology(sid, package, A)
        live = grid.read_scene(sid, A, package)
        with sm._transaction(write=True):
            update_grid(sm, sid, width=12)
        assert grid.read_scene(sid, A, package).width == 12
        assert static is grid.prepared_package_topology(sid, package, A)
        assert live.width != 12
        assert start(sm, sid, package, context).status == "committed"


def test_encoding_budget_skips_oversized_values(tmp_path):
    from trpg_state_machine.grid_maps import GridMaps
    from trpg_state_machine.storage import SqliteStore

    with pytest.raises(ValueError):
        PreparedGrid.build({"width": 0, "height": 1})
    store = SqliteStore(tmp_path / "budget.db")
    maps = GridMaps(store)
    try:
        maps.byte_limit = 1
        assert maps.for_grid("session", A, raw_grid(2, 2)).topology.cell_size_ft == 5
        assert not maps._grids
    finally:
        store.close()
