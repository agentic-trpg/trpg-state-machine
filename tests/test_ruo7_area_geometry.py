"""Continuous light and regional blockers through the public World commands."""

from copy import deepcopy
from dataclasses import replace

import pytest
from lock_support import DOOR, A
from test_light_geometry import coverage_area
from test_world_lights import light_config, lighting_data
from test_world_spatial import OBSTACLE, spatial_world_data
from world_support import setup, submit

from trpg_state_machine import RepositionObject, StateMachineError

LAMP = "object.light_geometry_lamp"
TARGETS = ("4,1", "4,2")


def screen_data(growth):
    data, props = lighting_data(), spatial_world_data()
    data["manifest"]["schema_version"] = "state-machine-foundation/8"
    grid = data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"]
    for configured in (data["scenes"][0]["grid"], grid):
        configured.update(width=24, height=12)

    fixture = deepcopy(props["scenes"][0]["objects"][-1])
    fixture["initial_cell"] = "2,2"
    screen = deepcopy(props["world_objects"][-1])
    screen["capabilities"][0].update(
        closed_properties={"light_opaque": True},
        closed_blocks_movement=False,
        open_blocks_movement=False,
    )
    data["scenes"][0]["objects"].append(fixture)
    data["world_objects"].append(screen)

    lamp = light_config()
    lamp["initial_enabled"] = True
    lamp["modes"][0].update(bright_radius_ft=25, dim_outer_radius_ft=35)
    ground = {
        "object_id": LAMP,
        "state": None,
        "initial_cell": "0,2",
        "movable": False,
        "blocks_passage": False,
    }
    lamp_profile = {
        **deepcopy(screen),
        "object_id": LAMP,
        "definition_ref": "definition.light_geometry_lamp",
        "capabilities": [lamp],
    }
    data["scenes"][0]["objects"].append(ground)
    data["world_objects"].append(lamp_profile)
    for i in range(growth):
        oid = f"object.unrelated_prop_{i}"
        definition = f"definition.unrelated_prop_{i}"
        data["scenes"][0]["objects"].append(
            {**ground, "object_id": oid, "initial_cell": f"{10 + i % 12},{6 + i // 12}"}
        )
        data["world_objects"].append(
            {**lamp_profile, "object_id": oid, "definition_ref": definition, "capabilities": []}
        )
    return data


@pytest.mark.parametrize("growth", (0, 60))
def test_external_screen_projects_partial_light_and_keeps_lookup_local(
    tmp_path, context, monkeypatch, growth
):
    sm, sid, package, _ = setup(tmp_path, context, screen_data(growth))
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        gm = replace(context, can_place_world_objects=True)
        lookup = sm._world.grid.reader.region_occupants
        visits = []

        def count(*args, **kwargs):
            actors, objects = lookup(*args, **kwargs)
            objects = tuple(objects)
            visits.append(objects)
            return actors, objects

        monkeypatch.setattr(sm._world.grid.reader, "region_occupants", count)

        def read(*, geometry=True):
            return sm.get_illumination_area(
                sid, actor, TARGETS, package, context=context, include_geometry=geometry
            )

        # The ordinary query computes the same light that a later map request draws.
        assert tuple(r["illumination"] for r in read(geometry=False)) == ("bright", "dark")
        # Source disclosure can also query the lamp's own cell.
        assert [tuple(sorted(row[0] for row in rows)) for rows in visits] == [
            (OBSTACLE, LAMP, DOOR),
            (LAMP,),
        ]
        initial = read()
        assert len(visits) == 2
        partial, shaded = (result["geometry"] for result in initial)
        assert 0 < coverage_area(partial["bright"]) < 1
        assert not shaded["bright"]
        assert any(
            x1 != x2 and y1 != y2
            for piece in partial["bright"]
            for (x1, y1), (x2, y2) in zip(piece, (*piece[1:], piece[0]), strict=True)
        )

        operation = RepositionObject(A, OBSTACLE, "2,2", "2,5")
        assert submit(sm, sid, package, gm, operation, "move.screen").status == "committed"
        clear = read()
        assert tuple(r["illumination"] for r in clear) == ("bright", "bright")
        assert all(coverage_area(r["geometry"]["bright"]) == 1 for r in clear)
        assert 0 < coverage_area(initial[0]["geometry"]["bright"]) < 1


def test_candidate_occluder_move_does_not_publish_polygons_on_rollback(tmp_path, context):
    sm, sid, package, _ = setup(tmp_path, context, screen_data(0))
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id

        def read():
            return sm.get_illumination_area(
                sid, actor, TARGETS, package, context=context, include_geometry=True
            )

        original = read()
        with pytest.raises(RuntimeError, match="rollback"), sm._transaction(write=True):
            sm._conn.execute(
                "UPDATE objects SET cell=? WHERE session_id=? AND object_id=?",
                ("2,5", sid, OBSTACLE),
            )
            candidate = sm._objects.lights.current_cells(sid, A, package, TARGETS)
            assert candidate[TARGETS[1]].level == "bright"
            raise RuntimeError("rollback")
        assert read() == original
        with pytest.raises(StateMachineError) as invalid:
            sm.get_illumination_area(
                sid, actor, TARGETS, package, context=context, include_geometry="yes"
            )
        assert invalid.value.code == "invalid_query"


def test_separated_targets_do_not_read_between_disjoint_light_paths(tmp_path, context, monkeypatch):
    data = screen_data(1)
    data["scenes"][0]["objects"][-1]["initial_cell"] = "3,5"
    sm, sid, package, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, package, context=context).pc_actor_id
        lookup = sm._world.grid.reader.region_occupants
        selected = []

        def count(sid, scene, cells, **kwargs):
            selected.append(set(cells))
            return lookup(sid, scene, cells, **kwargs)

        monkeypatch.setattr(sm._world.grid.reader, "region_occupants", count)
        result = sm.get_illumination_area(sid, actor, ("4,2", "0,6"), package, context=context)
        assert tuple(r["illumination"] for r in result) == ("dark", "bright")
        assert selected == [
            {*(f"{x},2" for x in range(5)), *(f"0,{y}" for y in range(3, 7))},
            {"0,2"},
        ]
