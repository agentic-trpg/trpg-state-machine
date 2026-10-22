"""Lifecycle/isolation and pinned-geometry equivalence for local query reuse."""

from collections.abc import Mapping
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
from itertools import product

import pytest
from dnd5e_engine.spatial import GridTopology
from dnd5e_engine.specs import GridScene
from lock_support import CRED, DOOR, A, B, lock_loader
from test_grid_maps import raw_grid, update_grid
from test_runtime_local_query_reuse import mapped_data, work
from test_world_containers import BAG, BOX
from test_world_lights import lighting_data
from test_world_spatial import OBSTACLE
from world_support import setup, submit, world_data

from trpg_state_machine import (
    PlaceObject,
    RepositionObject,
    SetLightEnabled,
    StateMachine,
    StateMachineError,
)
from trpg_state_machine.grid import GridOccupant, GridView
from trpg_state_machine.grid_maps import PreparedGrid
from trpg_state_machine.package import canonical_json
from trpg_state_machine.world_spatial import ObjectSpatialProjection


def test_light_candidate_map_shading_rollback_and_reload(tmp_path, context, monkeypatch):
    sm, sid, package, db = setup(tmp_path, context, lighting_data())
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        lights = sm._objects.lights

        def current():
            return lights.current_cell(sid, A, package, "1,0")

        assert current()[0] == "dark"
        with pytest.raises(ValueError), sm._transaction(write=True):
            update_grid(sm, sid, default_lighting="bright")
            assert current()[0] == "bright"  # Also works when no source is enabled.
            raise ValueError("rollback ambient")
        assert current()[0] == "dark"
        assert (
            submit(
                sm, sid, package, context, SetLightEnabled(A, CRED, "0,0", True), "light.on"
            ).status
            == "committed"
        )
        original = current()
        old_sources = lights.sources(sid, A, package)
        old_snapshot = sm.read_noncombat_snapshot(sid, package, context=context)
        assert original[0] == "bright"
        with pytest.raises(ValueError), sm._transaction(write=True):
            sm._world_facts.transfer_item(sid, sm._world_facts.object(sid, CRED), container_id=BOX)
            sm._conn.execute(
                "UPDATE objects SET state='closed' WHERE session_id=? AND object_id=?", (sid, BOX)
            )
            assert current()[0] == "dark"
            with pytest.raises(ValueError), sm._store.savepoint():
                sm._conn.execute(
                    "UPDATE objects SET state='open' WHERE session_id=? AND object_id=?", (sid, BOX)
                )
                assert current()[0] == "bright"
                raise ValueError("savepoint")
            assert current()[0] == "dark"
            raise ValueError("whole candidate")
        assert current() == original
        # Moving a nonblocking object without emitter descendants does not expire light.
        with sm._transaction(write=True):
            sm._world_facts.transfer_item(
                sid, sm._world_facts.object(sid, BAG), scene_id=A, cell="0,1"
            )
        with work(sm, monkeypatch) as counts:
            assert current() == original
        assert counts["instance_builds"] == 0
        assert old_sources == lights.sources(sid, A, package)
        assert old_snapshot.scene_state.grid.lighting["1,0"] == "bright"
        sm.unload_session(sid, package, context=context)
        assert current() == original
    with StateMachine(db, dnd_loader=lock_loader()) as recovered:
        recovered.resume_session(sid, package, context=context)
        assert (
            recovered.get_illumination(sid, actor, "1,0", package, context=context)["illumination"]
            == "bright"
        )


def test_light_geometry_only_invalidates_relevant_ground_changes(tmp_path, context, monkeypatch):
    data = lighting_data()
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        lights, facts = sm._objects.lights, sm._world_facts
        with sm._transaction(write=True):
            facts.set_light_state(sid, CRED, True, "normal")
        old = lights.current_cell(sid, A, package, "2,0")
        assert old[0] == "dim" and not old[1]
        with sm._transaction(write=True):
            config = facts.instances.get(sid, "object", BAG)
            config["blocks_passage"] = True
            sm._conn.execute(
                "UPDATE instance_configs SET config_json=? WHERE session_id=? "
                "AND domain='object' AND instance_id=?",
                (canonical_json(config), sid, BAG),
            )
        assert lights.current_cell(sid, A, package, "2,0")[1]
        with sm._transaction(write=True):
            facts.transfer_item(sid, facts.object(sid, BAG), scene_id=A, cell="0,1")
        assert lights.current_cell(sid, A, package, "2,0") == old
        with sm._transaction(write=True):
            config = facts.instances.get(sid, "object", BAG)
            config["blocks_passage"] = False
            sm._conn.execute(
                "UPDATE instance_configs SET config_json=? WHERE session_id=? "
                "AND domain='object' AND instance_id=?",
                (canonical_json(config), sid, BAG),
            )
        lights.current_cell(sid, A, package, "2,0")
        with sm._transaction(write=True):
            facts.transfer_item(sid, facts.object(sid, BAG), scene_id=A, cell="1,0")
        with work(sm, monkeypatch) as counts:
            assert lights.current_cell(sid, A, package, "2,0") == old
        assert counts["instance_builds"] == 0


def test_shared_door_light_geometry_and_fixed_old_result(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, lighting_data())
    with sm:
        lights = sm._objects.lights
        with sm._transaction(write=True):
            sm.scene_lifecycle.ensure_scene(sid, B, package)
        before_a = lights.geometry(sid, A, package)
        before_b = lights.geometry(sid, B, package)
        with sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE objects SET state='closed' WHERE session_id=? AND object_id=?", (sid, DOOR)
            )
            closed_a = lights.geometry(sid, A, package)
            closed_b = lights.geometry(sid, B, package)
            assert closed_a != before_a and closed_b != before_b
        assert before_a.candidates == frozenset()
        assert "0,1" not in before_b.candidates
        assert lights.geometry(sid, A, package) == closed_a
        assert lights.geometry(sid, B, package) == closed_b


@pytest.mark.parametrize("failure", ("savepoint", "transaction"))
def test_map_qualification_is_version_scoped_and_old_views_stay_fixed(tmp_path, context, failure):
    sm, sid, package, db = setup(tmp_path, context, mapped_data(8))
    gm = replace(context, can_place_world_objects=True)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        grid = sm._world.grid
        view = grid.read_scene(sid, A, package)
        op = RepositionObject(A, OBSTACLE, "1,1", "2,1")

        def query():
            return sm.query_world_action(sid, actor, op, package, context=gm)

        assert query().status == "available"
        with sm._transaction(write=True) if failure == "savepoint" else nullcontext():
            boundary = (
                sm._store.savepoint() if failure == "savepoint" else sm._transaction(write=True)
            )
            with pytest.raises(ValueError), boundary:
                update_grid(sm, sid, wall_segments=[{"x1": 1.5, "y1": 0, "x2": 2, "y2": 3}])
                spatial = sm._objects.spatial
                candidate = replace(spatial.state(sid, OBSTACLE, package), anchor="2,1")
                with pytest.raises(StateMachineError) as error:
                    spatial.check_candidate(sid, OBSTACLE, package, candidate)
                assert error.value.code == "unsupported_object_geometry"
                raise ValueError("reject unsupported candidate")
        assert query().status == "available"
        with sm._transaction(write=True):
            update_grid(sm, sid, wall_segments=[{"x1": 2, "y1": 0, "x2": 2, "y2": 3}])
        assert not grid.read_scene(sid, A, package).can_step("1,0", "2,0")
        assert view.can_step("1,0", "2,0")
        sm.unload_session(sid, package, context=context)
        assert not grid.read_scene(sid, A, package).can_step("1,0", "2,0")
    with StateMachine(db, dnd_loader=lock_loader()) as recovered:
        recovered.resume_session(sid, package, context=context)
        assert not recovered._world.grid.read_scene(sid, A, package).can_step("1,0", "2,0")


@pytest.mark.parametrize(
    "walls",
    (
        [{"x1": 2, "y1": 0, "x2": 2, "y2": 5}],
        [{"x1": 0.25, "y1": 1.25, "x2": 4.75, "y2": 3.5}],
        [{"x1": 1, "y1": 1, "x2": 1, "y2": 1}],
        [{"x1": 0, "y1": 1, "x2": 5, "y2": 1}, {"x1": 2, "y1": 0, "x2": 2, "y2": 5}],
    ),
)
def test_wall_index_matches_pinned_re_including_touches_diagonals_and_fractional(walls):
    raw = raw_grid(
        5, 5, wall_segments=walls, blocked_cells=["3,3"], difficult_terrain_cells=["1,1"]
    )
    expected, actual = GridTopology(GridScene(**raw)), PreparedGrid.build(raw).topology
    cells = [f"{x},{y}" for y in range(5) for x in range(5)] + ["-1,0", "5,0"]
    for a, b in product(cells, repeat=2):
        assert actual.edge_distance(a, b) == expected.edge_distance(a, b)
        assert actual.has_line_of_sight(a, b) == expected.has_line_of_sight(a, b)
        assert actual.cover_between(a, b) == expected.cover_between(a, b)


def test_view_index_order_multicell_cover_maxima_dto_and_query_reuse(monkeypatch):
    raw = raw_grid(5, 5, cover_cells={"1,0": "half", "2,0": "three_quarters"})
    objects = (
        ("wide", ObjectSpatialProjection(("1,0", "2,0"), cover_cells=(("1,0", "three_quarters"),))),
        ("half", ObjectSpatialProjection(("1,0",), cover_cells=(("1,0", "half"),))),
    )
    occupants = (
        GridOccupant("object", "wide", ("1,0", "2,0")),
        GridOccupant("actor", "middle", ("1,1",)),
        GridOccupant("object", "half", ("1,0",)),
        GridOccupant("actor", "target", ("3,1",)),
    )
    view = GridView(A, 1, canonical_json(raw), occupants, objects)
    assert view.occupied(("2,0", "1,0", "1,0")) == (occupants[0], occupants[2])
    assert set(asdict(view)) == {"scene_id", "world_version", "grid_json", "occupants", "objects"}
    assert deepcopy(view) == view
    with pytest.raises(StateMachineError, match="Entity has no local"):
        view.entity_cells("actor", "wide")
    base = GridTopology(GridScene(**raw))
    for a, b in product(("0,0", "1,0", "2,0", "3,0", "0,1", "3,1"), repeat=2):
        for creatures in (False, True):
            occupied = (
                tuple(c for o in occupants if o.kind == "actor" for c in o.cells)
                if creatures
                else ()
            )
            expected = (
                "total"
                if not base.has_line_of_sight(a, b)
                else max(
                    [base.cover_between(a, b, occupied_cells=occupied)]
                    + [
                        GridTopology(
                            GridScene(**{**raw, "cover_cells": dict(p.cover_cells)})
                        ).cover_between(a, b)
                        for _, p in objects
                    ],
                    key=("none", "half", "three_quarters", "total").index,
                )
            )
            assert view.cover_between(a, b, creature_cover=creatures) == expected

    class NoEnumeration(Mapping):
        def __getitem__(self, key):
            return cover[key]

        def __iter__(self):
            raise AssertionError("A fixed-view query must not enumerate the prepared cover map")

        def __len__(self):
            return len(cover)

    cover = view._creature_cover._cover_cells
    object.__setattr__(view._creature_cover, "_cover_cells", NoEnumeration())
    assert view.cover_between("0,1", "3,1", creature_cover=True) == "half"


def test_place_rejects_actor_destination_even_nonblocking_object(tmp_path, context):
    data = world_data()
    data["scenes"][0]["objects"].append(
        {
            "object_id": "object.marker",
            "state": None,
            "movable": True,
            "blocks_passage": False,
            "initial_cell": "1,0",
        }
    )
    data["world_objects"].append(
        {
            **data["world_objects"][1],
            "object_id": "object.marker",
            "definition_ref": "definition.marker",
            "capabilities": [],
        }
    )
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        op = PlaceObject("object.marker", A, "1,0", "0,0")
        assert (
            sm.query_world_action(sid, actor, op, package, context=context).reason
            == "movement_unavailable"
        )
        assert (
            submit(sm, sid, package, context, op, "actor.occupied").error_code
            == "movement_unavailable"
        )


def test_environment_cold_rebuild_hot_and_prepared_direction_inputs(tmp_path, context, monkeypatch):
    from test_environment_service import project
    from test_environment_versions import ready

    sm, sid, package, _, wo, _ = ready(tmp_path, context)
    with sm:
        sm._world.grid.reader.prepared_scene(sid, A, package)
        with work(sm, monkeypatch) as cold:
            original = project(sm, sid, package, "3,4", height=3)
        with work(sm, monkeypatch) as hot:
            assert project(sm, sid, package, "3,4", height=3) == original
        wo.current = replace(wo.current, base_height_ft=8)
        with work(sm, monkeypatch) as rebuilt:
            updated = project(sm, sid, package, "3,4", height=3)
        with sm._transaction():
            prepared = sm._objects.spatial.prepare_scene_protection(sid, A, package)
        wo.current = replace(wo.current, base_height_ft=8)
        with work(sm, monkeypatch) as directions:
            for direction in ("north", "east", "south", "west"):
                sm._objects.spatial.scene_protection_contributions(
                    sid, A, package, wind_from=direction, prepared=prepared
                )
        for label, counts in (
            ("cold", cold),
            ("rebuild", rebuilt),
            ("directions", directions),
            ("hit", hot),
        ):
            print("LOCAL environment", label, dict(counts))
            assert counts["map_copies"] == counts["map_encodings"] == counts["map_parses"] == 0
        assert hot["instance_builds"] == hot["parsed_builds"] == 0
        assert updated != original


@pytest.mark.parametrize("failure", ("publish", "unknown"))
def test_light_recovery_invalidates_old_context_and_preserves_old_input(
    tmp_path, context, monkeypatch, failure
):
    from world_support import command

    first, sid, package, db = setup(tmp_path, context, lighting_data())
    with first, StateMachine(db, dnd_loader=lock_loader()) as second:
        actor = first.read_session(sid, package, context=context).pc_actor_id
        old = first.get_illumination(sid, actor, "1,0", package, context=context)
        second.read_session(sid, package, context=context)
        op = command(
            first,
            sid,
            package,
            context,
            SetLightEnabled(A, CRED, "0,0", True),
            "light.failed.publish",
        )
        commit = first._store._commit

        def fail_publish(scope):
            raise MemoryError("after durable light commit")

        def unknown():
            commit()
            raise OSError("lost acknowledgement")

        with monkeypatch.context() as patch:
            patch.setattr(
                first._store.memory, "publish", fail_publish
            ) if failure == "publish" else patch.setattr(first._store, "_commit", unknown)
            with pytest.raises(StateMachineError) as error:
                first.submit_command(op, package, context=context)
        assert error.value.code == (
            "committed_publication_pending" if failure == "publish" else "commit_outcome_unknown"
        )
        with pytest.raises(StateMachineError, match="reconciliation"):
            first.get_illumination(sid, actor, "1,0", package, context=context)
        second.resume_session(sid, package, context=context)
        assert (
            first.get_illumination(sid, actor, "1,0", package, context=context)["illumination"]
            == "bright"
        )
        assert old["illumination"] == "dark" and old["contributing_sources"] == ()
        assert first.submit_command(op, package, context=context).status == "committed"
