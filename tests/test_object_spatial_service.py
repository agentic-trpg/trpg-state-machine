"""Object-side geometry through real commands, shared commits and Package bindings."""

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from lock_support import CRED, A, lock_loader
from test_migrations import assert_original_rows_equal, stored_rows
from test_world_durability import adopt, durability_data
from test_world_spatial import OBSTACLE, spatial_world_data
from world_support import command, setup, submit

from trpg_state_machine import (
    ActivateMechanism,
    ObjectState,
    RepositionObject,
    SetObjectState,
    StateMachine,
    StateMachineError,
    StepActor,
)
from trpg_state_machine.commands import world_command_from_data
from trpg_state_machine.object_durability import _ObjectHPSettlement
from trpg_state_machine.package import _validate, canonical_json
from trpg_state_machine.world_spatial import ORIENTATIONS


def oriented_data(*, anchor="1,1", footprint=None, blocking=True, openable=False):
    data = spatial_world_data()
    data["manifest"]["schema_version"] = "state-machine-foundation/8"
    obj = data["scenes"][0]["objects"][-1]
    obj.update(initial_cell=anchor, state="closed" if openable else None)
    profile = data["world_objects"][-1]
    spatial = profile["capabilities"][0]
    spatial.update(
        footprint=footprint or [[0, 0], [1, 0]],
        allowed_orientations=list(ORIENTATIONS),
        open_blocks_movement=blocking,
        closed_blocks_movement=blocking,
    )
    if openable:
        profile["capabilities"].append({"kind": "openable"})
    return data


def changed_grid(data, **fields):
    # Package requires static spatial and mechanical grids to agree exactly.
    data["scenes"][0]["grid"].update(deepcopy(fields))
    data["rule_bindings"].pop("combat_foundation", None)
    data["rule_bindings"]["noncombat_foundation"]["snapshot"]["scene_state"]["grid"].update(
        deepcopy(fields)
    )


def facts_without_receipts(db):
    return {k: v for k, v in stored_rows(db).items() if k != "command_receipts"}


def test_four_orientations_rotation_movement_replay_conflicts_and_process_restart(
    tmp_path, context
):
    data = oriented_data()
    sm, sid, p, db = setup(tmp_path, context, data)
    gm = replace(context, can_place_world_objects=True)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        expected = {
            "north": ("1,1", "2,1"),
            "east": ("1,1", "1,2"),
            "south": ("1,1", "0,1"),
            "west": ("1,1", "1,0"),
        }
        # Pure calculation includes all four; a passage reserves the south result.
        for direction, cells in expected.items():
            assert sm._objects.spatial.cells(p, OBSTACLE, "1,1", direction) == cells
        op = RepositionObject(A, OBSTACLE, "1,1", "1,1", "north", "east")
        cmd = command(sm, sid, p, gm, op, "rotate")
        encoded = asdict(cmd)
        encoded.pop("check_world_effects")
        assert world_command_from_data(encoded) == cmd
        assert sm.query_world_action(sid, actor, op, p, context=gm).status == "available"
        before = facts_without_receipts(db)
        with pytest.raises(StateMachineError, match="grant"):
            sm.submit_command(cmd, p, context=context)
        assert facts_without_receipts(db) == before
        receipt = sm.submit_command(cmd, p, context=gm)
        assert receipt.status == "committed"
        assert sm.submit_command(cmd, p, context=gm) == receipt
        with pytest.raises(StateMachineError, match="different content"):
            sm.submit_command(
                replace(cmd, payload=replace(op, to_orientation="west")), p, context=gm
            )
        assert (
            sm.get_object(sid, actor, OBSTACLE, p, context=context)["occupied_cells"]
            == expected["east"]
        )
        assert (
            submit(
                sm, sid, p, gm, replace(op, to_orientation="west"), "stale.orientation"
            ).error_code
            == "orientation_conflict"
        )
        stale = replace(cmd, command_id="stale.version")
        assert sm.submit_command(stale, p, context=gm).error_code == "version_conflict"
        moved = RepositionObject(A, OBSTACLE, "1,1", "2,1", "east", "west")
        assert submit(sm, sid, p, gm, moved, "move.rotate").status == "committed"
        final = sm.get_object(sid, actor, OBSTACLE, p, context=context)
        assert final["occupied_cells"] == ("2,1", "2,0")
        assert final["orientation"] == final["location"]["orientation"] == "west"
    # New interpreter, not just a reopened object sharing Python state.
    document = tmp_path / "package.json"
    document.write_text(json.dumps(data), encoding="utf-8")
    code = (
        "import json,sys; from lock_support import lock_loader; "
        "from trpg_state_machine import StateMachine,TrustedContext; "
        "from trpg_state_machine.package import _validate; "
        "p=_validate(json.load(open(sys.argv[2]))); "
        "c=TrustedContext('authenticated:alice','player.alice'); "
        "s=StateMachine(sys.argv[1],dnd_loader=lock_loader()); "
        "r=s.resume_session(sys.argv[3],p,context=c); "
        "print(json.dumps(s.get_object(sys.argv[3],r.pc_actor_id,'object.barrier',p,context=c))); "
        "s.close()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(db), str(document), sid],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(Path(__file__).parents[1] / "src"), str(Path(__file__).parent)]
            ),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    recovered = json.loads(result.stdout)
    assert recovered["orientation"] == "west"
    assert recovered["occupied_cells"] == ["2,1", "2,0"]


def test_symmetric_orientation_is_a_fact_and_event_even_when_cells_unchanged(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, oriented_data(footprint=[[0, 0]]))
    gm = replace(context, can_place_world_objects=True)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        op = RepositionObject(A, OBSTACLE, "1,1", "1,1", "north", "south")
        result = submit(sm, sid, p, gm, op, "symmetric")
        assert result.status == "committed" and len(result.event_seqs) == 1
        view = sm.get_object(sid, actor, OBSTACLE, p, context=context)
        assert view["occupied_cells"] == ("1,1",) and view["orientation"] == "south"
        assert sm.read_session(sid, p, context=context).world_version == 1
        before = facts_without_receipts(db)
        no_op = replace(op, expected_orientation="south")
        assert submit(sm, sid, p, gm, no_op, "no.op").event_seqs == ()
        assert facts_without_receipts(db) == before


@pytest.mark.parametrize(
    "target,direction", [("2,1", "north"), ("1,2", "north"), ("1,0", "south"), ("0,1", "east")]
)
def test_candidate_bounds_actor_and_reserved_passage_fail_without_partial_effects(
    tmp_path, context, target, direction
):
    sm, sid, p, db = setup(tmp_path, context, oriented_data())
    gm = replace(context, can_place_world_objects=True)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        op = RepositionObject(A, OBSTACLE, "1,1", target, "north", direction)
        query = sm.query_world_action(sid, actor, op, p, context=gm)
        assert query.reason == "object_placement_blocked"
        before = facts_without_receipts(db)
        result = submit(sm, sid, p, gm, op, "bad")
        assert result.error_code == query.reason and result.event_seqs == ()
        assert facts_without_receipts(db) == before


def test_rigid_object_cross_wall_reproducer_candidate_and_audit_use_same_rule(tmp_path, context):
    data = oriented_data(anchor="1,0")
    changed_grid(data, wall_segments=[{"x1": 2, "y1": 1, "x2": 2, "y2": 2}])
    sm, sid, p, db = setup(tmp_path, context, data)
    gm = replace(context, can_place_world_objects=True)
    with sm:
        spatial = sm._objects.spatial
        base = sm._world.grid.base_topology(sid, A)
        assert base.is_valid_cell("1,1") and base.is_valid_cell("2,1")
        assert base.edge_distance("1,1", "2,1") is None  # Old per-cell test admits this.
        candidate = replace(spatial.state(sid, OBSTACLE, p), anchor="1,1")
        with pytest.raises(StateMachineError, match="crosses a wall"):
            spatial.check_candidate(sid, OBSTACLE, p, candidate)
        before = facts_without_receipts(db)
        result = submit(sm, sid, p, gm, RepositionObject(A, OBSTACLE, "1,0", "1,1"), "wall")
        assert result.error_code == "object_placement_blocked"
        assert facts_without_receipts(db) == before
        # Deliberately bypass admission only to prove that final audit rejects identically.
        with pytest.raises(StateMachineError, match="crosses a wall"), sm._transaction(write=True):
            sm._world_facts.place(sid, OBSTACLE, "1,1")
            spatial.audit(sid, p)
        assert facts_without_receipts(db) == before


@pytest.mark.parametrize("occlusion", ["wall", "dark", "total_cover"])
def test_partial_geometry_visible_and_list_agrees_with_single_query(tmp_path, context, occlusion):
    data = oriented_data(anchor="2,0", footprint=[[0, 0], [0, 1]])
    fields = {
        "wall": {"wall_segments": [{"x1": 2, "y1": 0, "x2": 2, "y2": 1}]},
        "dark": {"lighting": {"2,0": "dark"}},
        "total_cover": {"cover_cells": {"1,0": "total"}},
    }[occlusion]
    changed_grid(data, **fields)
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        base = sm._world.grid.base_topology(sid, A, p)
        assert not base.can_see("0,0", "2,0")
        assert base.can_see("0,0", "2,1")
        view = sm.get_object(sid, actor, OBSTACLE, p, context=context)
        assert view in sm.list_scene_objects(sid, actor, p, context=context)
        assert view["occupied_cells"] == ("2,0", "2,1")


def test_all_parts_invisible_stay_hidden(tmp_path, context):
    data = oriented_data(anchor="2,0", footprint=[[0, 0], [0, 1]])
    changed_grid(data, lighting={"2,0": "dark", "2,1": "dark"})
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        with pytest.raises(StateMachineError, match="authorized"):
            sm.get_object(sid, actor, OBSTACLE, p, context=context)
        assert OBSTACLE not in {
            v["object_id"] for v in sm.list_scene_objects(sid, actor, p, context=context)
        }


def test_interaction_uses_reachable_part_and_never_selects_through_wall(tmp_path, context):
    data = oriented_data(anchor="2,0", footprint=[[0, 0], [-1, 0]], openable=True)
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        spatial = sm._objects.spatial
        assert spatial.contact_cell(sid, actor, OBSTACLE, p) == "1,0"
        op = SetObjectState(A, OBSTACLE, ObjectState.OPEN)
        assert submit(sm, sid, p, context, op, "far.anchor").status == "committed"
    data = oriented_data(anchor="1,1", footprint=[[0, 0]], openable=True)
    changed_grid(data, wall_segments=[{"x1": 1, "y1": 1, "x2": 1, "y2": 2}])
    wall_path = tmp_path / "wall"
    wall_path.mkdir()
    sm, sid, p, db = setup(wall_path, context, data)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        # Diagonal contact touches the wall endpoint. Upstream visibility/reach refuses it.
        before = facts_without_receipts(db)
        result = submit(sm, sid, p, context, SetObjectState(A, OBSTACLE, ObjectState.OPEN), "wall")
        assert result.error_code in ("invalid_target", "object_out_of_reach")
        assert facts_without_receipts(db) == before


@pytest.mark.parametrize("policy", ["disable_spatial_policy", "disable_preserve_obstruction"])
def test_open_and_destroyed_properties_are_independent_of_movement(tmp_path, context, policy):
    data = oriented_data(anchor="2,0", footprint=[[0, 0], [-1, 0]], openable=True)
    c = data["world_objects"][-1]["capabilities"][0]
    c.update(
        open_blocks_movement=False,
        closed_properties={"vision_occlusion": True, "cover": "three_quarters"},
        open_properties={"difficult_terrain": True, "cover": "half"},
        destroyed_properties={
            "difficult_terrain": True,
            "vision_occlusion": True,
            "cover": "total",
        },
    )
    durability = deepcopy(durability_data()["world_objects"][0]["capabilities"][-1])
    durability["destruction_policy_ref"] = policy
    data["world_objects"][-1]["capabilities"].append(durability)
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        spatial = sm._objects.spatial
        closed = spatial.project(sid, OBSTACLE, p)
        assert closed.movement_blocking_cells == closed.vision_occlusion_cells == ("2,0", "1,0")
        assert closed.difficult_terrain_cells == ()
        assert (
            submit(
                sm, sid, p, context, SetObjectState(A, OBSTACLE, ObjectState.OPEN), "open"
            ).status
            == "committed"
        )
        opened = spatial.project(sid, OBSTACLE, p)
        assert opened.movement_blocking_cells == opened.vision_occlusion_cells == ()
        assert opened.difficult_terrain_cells == ("2,0", "1,0")
        assert opened.cover_cells == (("2,0", "half"), ("1,0", "half"))
        adopt(sm, sid, p, context, _ObjectHPSettlement(sid, OBSTACLE, "destroy", 1, 20, 0))
        destroyed = spatial.project(sid, OBSTACLE, p)
        if policy == "disable_preserve_obstruction":
            assert destroyed == opened
        else:
            assert destroyed.occupied_cells == opened.occupied_cells
            assert destroyed.movement_blocking_cells == ()
            assert destroyed.vision_occlusion_cells == ("2,0", "1,0")
            assert destroyed.cover_cells == (("2,0", "total"), ("1,0", "total"))


def test_nonblocking_overlap_allowed_but_close_and_destroy_that_block_roll_back(tmp_path, context):
    data = oriented_data(anchor="1,0", footprint=[[0, 0]], blocking=False, openable=True)
    spatial = data["world_objects"][-1]["capabilities"][0]
    spatial.update(closed_blocks_movement=True, destroyed_blocks_movement=True)
    data["scenes"][0]["objects"][-1]["state"] = "open"
    durability = deepcopy(durability_data()["world_objects"][0]["capabilities"][-1])
    durability["destruction_policy_ref"] = "disable_spatial_policy"
    data["world_objects"][-1]["capabilities"].append(durability)
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        assert (
            submit(sm, sid, p, context, StepActor(A, "0,0", "1,0"), "overlap").status == "committed"
        )
        before = facts_without_receipts(db)
        op = SetObjectState(A, OBSTACLE, ObjectState.CLOSED)
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        assert (
            sm.query_world_action(sid, actor, op, p, context=context).reason
            == "invalid_spatial_state"
        )
        assert submit(sm, sid, p, context, op, "close").error_code == "invalid_spatial_state"
        assert facts_without_receipts(db) == before
        with pytest.raises(StateMachineError, match="overlap"):
            adopt(sm, sid, p, context, _ObjectHPSettlement(sid, OBSTACLE, "destroy", 1, 20, 0))
        assert facts_without_receipts(db) == before
        assert sm._world_facts.object_hp(sid, OBSTACLE) == 20


def test_rotation_evidence_fault_rolls_back_orientation_version_and_event(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, oriented_data())
    gm = replace(context, can_place_world_objects=True)
    with sm:
        before = stored_rows(db)
        sm._conn.execute(
            "CREATE TEMP TRIGGER fail_evidence BEFORE INSERT ON event_evidence "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
        op = RepositionObject(A, OBSTACLE, "1,1", "1,1", "north", "east")
        cmd = command(sm, sid, p, gm, op, "fault")
        with pytest.raises(StateMachineError):
            sm.submit_command(cmd, p, context=gm)
        assert stored_rows(db) == before
        sm._conn.execute("DROP TRIGGER fail_evidence")
        assert sm.submit_command(cmd, p, context=gm).status == "committed"


@pytest.mark.parametrize(
    "mutation",
    [
        "old.schema",
        "disconnected",
        "direction",
        "initial",
        "property",
        "complex",
        "untyped.properties",
    ],
)
def test_unsupported_package_geometry_and_orientation_fail_explicitly(mutation):
    data = oriented_data()
    c = data["world_objects"][-1]["capabilities"][0]
    if mutation == "old.schema":
        data["manifest"]["schema_version"] = "state-machine-foundation/7"
    elif mutation == "disconnected":
        c["footprint"] = [[0, 0], [2, 0]]
    elif mutation == "direction":
        c["allowed_orientations"] = ["northeast"]
    elif mutation == "initial":
        data["scenes"][0]["objects"][-1]["initial_orientation"] = "east"
        c["allowed_orientations"] = ["north"]
    elif mutation == "property":
        c["open_properties"] = {"cover": "ac+2"}
    elif mutation == "complex":
        c["mesh"] = "polygon"
    else:
        c["open_properties"] = True
    with pytest.raises(StateMachineError):
        _validate(data)


def test_legacy_package_digest_and_defaults_unchanged():
    data = spatial_world_data()
    digest = "sha256:" + hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
    p = _validate(data)
    assert p.package_digest == digest
    assert p.world_objects[-1].capability("spatial").allowed_orientations == ("north",)
    assert p.scenes[0].objects[-1].initial_orientation == "north"


@pytest.mark.parametrize(
    "direction,extra", [("north", "4,3"), ("east", "3,4"), ("south", "2,3"), ("west", "3,2")]
)
def test_initial_instance_orientation_and_real_rotated_projection(
    tmp_path, context, direction, extra
):
    data = oriented_data(anchor="3,3")
    changed_grid(data, width=5, height=5)
    data["scenes"][0]["objects"][-1]["initial_orientation"] = direction
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        assert sm._world_facts.object(sid, OBSTACLE)["orientation"] == direction
        assert sm._objects.spatial.project(sid, OBSTACLE, p).occupied_cells == ("3,3", extra)
        assert {"3,3", extra} <= sm._objects.spatial.movement_cells(sid, A, p)


def test_blocking_object_conflict_static_obstacle_and_complex_walls(tmp_path, context):
    data = oriented_data(anchor="1,1")
    extra = deepcopy(data["scenes"][0]["objects"][-1])
    extra.update(object_id="object.other", initial_cell="1,0")
    data["scenes"][0]["objects"].append(extra)
    definition = deepcopy(data["world_objects"][-1])
    definition.update(object_id="object.other", definition_ref="definition.other")
    definition["capabilities"][0]["footprint"] = [[0, 0]]
    data["world_objects"].append(definition)
    changed_grid(data, blocked_cells=["0,2"])
    sm, sid, p, db = setup(tmp_path, context, data)
    gm = replace(context, can_place_world_objects=True)
    with sm:
        before = facts_without_receipts(db)
        for key, target in (("object", "1,0"), ("static", "0,2")):
            op = RepositionObject(A, OBSTACLE, "1,1", target)
            assert submit(sm, sid, p, gm, op, key).error_code == "object_placement_blocked"
            assert facts_without_receipts(db) == before
    data = oriented_data()
    changed_grid(data, wall_segments=[{"x1": 0.25, "y1": 2, "x2": 0.75, "y2": 2}])
    unsupported = tmp_path / "complex"
    unsupported.mkdir()
    with pytest.raises(StateMachineError, match="grid-edge walls"):
        sm, _, _, _ = setup(unsupported, context, data)


def test_container_contents_access_uses_shell_footprint(tmp_path, context):
    from test_lockable_passages import acquire
    from test_world_containers import BOX, container_data

    from trpg_state_machine import PutItem, TakeItem

    data = container_data()
    data["manifest"]["schema_version"] = "state-machine-foundation/8"
    data["item_transfers"] = [v for v in data["item_transfers"] if v["instance_id"] != BOX]
    shell = next(v for v in data["scenes"][0]["objects"] if v["object_id"] == BOX)
    shell["initial_cell"] = "2,0"
    definition = next(v for v in data["world_objects"] if v["object_id"] == BOX)
    definition["capabilities"].append(oriented_data()["world_objects"][-1]["capabilities"][0])
    definition["capabilities"][-1].update(footprint=[[0, 0], [-1, 0]], open_blocks_movement=False)
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        actor = sm.read_session(sid, p, context=context).pc_actor_id
        acquire(sm, sid, p, context)
        # Shell anchor is two cells away; its accessible end is adjacent.
        assert submit(sm, sid, p, context, PutItem(A, CRED, BOX, "0,0"), "in").status == "committed"
        assert (
            sm.get_object(sid, actor, CRED, p, context=context)["location"]["container_id"] == BOX
        )
        assert (
            submit(sm, sid, p, context, TakeItem(A, CRED, BOX, "0,0"), "out").status == "committed"
        )


def test_mechanism_sequential_spatial_conflict_rolls_back_prior_link_and_event(tmp_path, context):
    data = oriented_data(anchor="1,0", footprint=[[0, 0]], blocking=False, openable=True)
    spatial = data["world_objects"][-1]["capabilities"][0]
    spatial["open_blocks_movement"] = True
    # Both initially nonblocking shells overlap legally. Each isolated candidate
    # passes preflight; after the first link blocks, the second must fail atomically.
    second = deepcopy(data["scenes"][0]["objects"][-1])
    second["object_id"] = "object.second"
    data["scenes"][0]["objects"].append(second)
    second_definition = deepcopy(data["world_objects"][-1])
    second_definition.update(object_id="object.second", definition_ref="definition.second")
    data["world_objects"].append(second_definition)
    lever = deepcopy(data["scenes"][0]["objects"][-1])
    lever.update(object_id="object.lever", state=None, initial_cell="0,0")
    data["scenes"][0]["objects"].append(lever)
    definition = deepcopy(data["world_objects"][-1])
    definition.update(
        object_id="object.lever",
        definition_ref="definition.lever",
        capabilities=[
            {
                "kind": "mechanism",
                "trigger_kind": "manual",
                "trigger_conditions": "actor_adjacent",
                "initial_status": "armed",
                "reset_policy": "none",
                "actions": [
                    {"kind": "set_open_state", "target_object_id": OBSTACLE, "value": "open"},
                    {
                        "kind": "set_open_state",
                        "target_object_id": "object.second",
                        "value": "open",
                    },
                ],
            }
        ],
    )
    data["world_objects"].append(definition)
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        before = facts_without_receipts(db)
        op = ActivateMechanism(A, "object.lever", "0,0")
        assert submit(sm, sid, p, context, op, "linked").error_code == "invalid_spatial_state"
        assert facts_without_receipts(db) == before
        assert sm._world_facts.mechanism_status(sid, "object.lever") == "armed"


def test_rotation_requires_orientation_precondition_and_allowed_direction(tmp_path, context):
    data = oriented_data()
    data["world_objects"][-1]["capabilities"][0]["allowed_orientations"] = ["north", "east"]
    sm, sid, p, db = setup(tmp_path, context, data)
    gm = replace(context, can_place_world_objects=True)
    with sm:
        before = facts_without_receipts(db)
        op = RepositionObject(A, OBSTACLE, "1,1", "1,1", to_orientation="east")
        assert submit(sm, sid, p, gm, op, "no.precondition").error_code == "invalid_operation"
        op = replace(op, expected_orientation="north", to_orientation="west")
        assert (
            submit(sm, sid, p, gm, op, "not.allowed").error_code == "unsupported_object_orientation"
        )
        assert facts_without_receipts(db) == before


def test_scene_contribution_seam_and_combat_property_guard(tmp_path, context):
    from lock_support import DOOR
    from spatial_support import B

    data = oriented_data(blocking=False)
    data["world_objects"][-1]["capabilities"][0]["closed_properties"] = {
        "difficult_terrain": True,
        "vision_occlusion": True,
        "cover": "half",
    }
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        spatial = sm._objects.spatial
        local = spatial.scene_contributions(sid, A, p)[OBSTACLE]
        assert local.movement_blocking_cells == ()
        assert local.difficult_terrain_cells == local.vision_occlusion_cells == ("1,1", "2,1")
        assert local.cover_cells == (("1,1", "half"), ("2,1", "half"))
        # Same shared object, each local endpoint even before the foreign scene initializes.
        assert spatial.scene_contributions(sid, B, p)[DOOR].occupied_cells == ("0,1",)
        assert spatial.scene_contributions(sid, B, p)[DOOR].movement_blocking_cells == ("0,1",)
        with pytest.raises(StateMachineError, match="properties need RE"):
            sm._world._spatial_combat_guard(sid, A, p)


def test_real_schema32_save_receipt_replay_and_all_old_columns_preserved(
    tmp_path, context, monkeypatch
):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "old"
    archive = subprocess.run(
        ["git", "archive", "--format=zip", "a897ae5", "src"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    with zipfile.ZipFile(io.BytesIO(archive)) as contents:
        contents.extractall(source)
    db = tmp_path / "legacy.db"
    code = (
        "import sys,json; from test_world_spatial import spatial_world_data,OBSTACLE; "
        "from world_support import setup,command; "
        "from trpg_state_machine import TrustedContext,RepositionObject; "
        "from dataclasses import replace,asdict; from pathlib import Path; "
        "c=replace(TrustedContext('authenticated:alice','player.alice'),"
        "can_place_world_objects=True); "
        "s,sid,p,db=setup(Path(sys.argv[1]),c,spatial_world_data()); "
        "cmd=command(s,sid,p,c,RepositionObject('scene.platform',OBSTACLE,'2,0','1,0'),'old'); "
        "r=s.submit_command(cmd,p,context=c); d=asdict(cmd); d.pop('check_world_effects'); "
        "v=s._conn.execute('PRAGMA user_version').fetchone()[0]; "
        "print(json.dumps({'sid':sid,'command':d,'receipt':asdict(r),'schema':v})); s.close()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(source / "src"), str(root / "tests")]),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    old = json.loads(result.stdout)
    assert old["schema"] == 32 and old["receipt"]["status"] == "committed"
    db = tmp_path / "world.db"
    before = stored_rows(db)
    import sqlite3

    read_text = Path.read_text

    def interrupted(path, *args, **kwargs):
        content = read_text(path, *args, **kwargs)
        return (
            content.replace("PRAGMA user_version = 33;", "INVALID MIGRATION;")
            if path.name == "_migration_33.sql"
            else content
        )

    monkeypatch.setattr(Path, "read_text", interrupted)
    with pytest.raises(StateMachineError):
        StateMachine(db, dnd_loader=lock_loader())
    assert stored_rows(db) == before
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
    monkeypatch.undo()
    gm = replace(context, can_place_world_objects=True)
    p = _validate(spatial_world_data())
    with StateMachine(db, dnd_loader=lock_loader()) as sm:
        sid = old["sid"]
        sm.resume_session(sid, p, context=context)
        assert_original_rows_equal(before, stored_rows(db))
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == 35
        assert {r[0] for r in sm._conn.execute("SELECT orientation FROM objects")} == {"north"}
        replay = sm.submit_command(world_command_from_data(old["command"]), p, context=gm)
        assert json.loads(json.dumps(asdict(replay))) == old["receipt"]
        assert_original_rows_equal(before, stored_rows(db))
        assert sm._conn.execute("PRAGMA foreign_key_check").fetchall() == []
