"""OSS protection through real Session facts, explicit current WO inputs, no writes."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest
from lock_support import CRED, DOOR, A, B
from test_migrations import stored_rows
from test_object_spatial_service import changed_grid, oriented_data
from test_world_spatial import OBSTACLE
from world_support import setup, submit

from trpg_state_machine import ObjectState, PickupItem, PutItem, RepositionObject, SetObjectState
from trpg_state_machine.object_protection import (
    ORIENTATIONS,
    CurrentProtectionProperties,
    RainProtection,
    WindBoundary,
    WindProtection,
)
from trpg_state_machine.world_spatial import ObjectSpatialService


def protection(*, direction="north", n=3, base=10, lower=1, upper=5, cell=(0, 0)):
    return CurrentProtectionProperties(
        RainProtection(((0, 0), (1, 0), (1, 0))),
        WindProtection((WindBoundary(cell, direction, n, lower, upper),)),
        base,
    )


def large_data():
    data = oriented_data(anchor="3,3", footprint=[[0, 0]], blocking=False)
    changed_grid(data, width=9, height=9)
    return data


@pytest.mark.parametrize(
    "orientation,rain,wind_from,wind",
    [
        ("north", {"3,3", "4,3"}, "north", {"3,4", "3,5", "3,6"}),
        ("east", {"3,3", "3,4"}, "east", {"2,3", "1,3", "0,3"}),
        ("south", {"3,3", "2,3"}, "south", {"3,2", "3,1", "3,0"}),
        ("west", {"3,3", "3,2"}, "west", {"4,3", "5,3", "6,3"}),
    ],
)
def test_four_rotations_and_directions_use_same_anchor_transform(
    tmp_path, context, orientation, rain, wind_from, wind
):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm:
        oss = sm._objects.spatial
        before, changes = stored_rows(db), sm._conn.total_changes
        candidate = replace(oss.state(sid, OBSTACLE, p), orientation=orientation)
        sm._conn.execute("PRAGMA query_only=ON")
        try:
            result = oss.project_protection(
                sid, OBSTACLE, p, candidate=candidate, properties=protection(), wind_from=wind_from
            )
            assert {c.cell for c in result.rain_cells} == rain
            assert {c.cell for c in result.wind_cells} == wind
            assert result.rain_status == result.wind_status == "ready"
            assert {
                (c.object_id, c.lower_height_ft, c.upper_height_ft) for c in result.wind_cells
            } == {(OBSTACLE, 11, 15)}
            assert result.wind_at(next(iter(wind)), 11)
            assert not result.wind_at(next(iter(wind)), 15)  # upper exclusive
            assert oss.project(sid, OBSTACLE, p).movement_blocking_cells == ()
            assert oss.project(sid, OBSTACLE, p).occupied_cells == ("3,3",)
        finally:
            sm._conn.execute("PRAGMA query_only=OFF")
        assert sm._conn.total_changes == changes
        assert stored_rows(db) == before  # every row, event, receipt, RNG and version


@pytest.mark.parametrize(
    "direction,step", [("north", (0, 1)), ("east", (-1, 0)), ("south", (0, -1)), ("west", (1, 0))]
)
@pytest.mark.parametrize("n", [1, 2, 3, 256])
def test_n_starts_after_explicit_thickness_face_and_clips_scene(
    tmp_path, context, direction, step, n
):
    data = large_data()
    # A 2-cell-thick wall: local (0,0) and the downstream (dx,dy) cell.
    dx, dy = step
    data["world_objects"][-1]["capabilities"][0]["footprint"] = [[0, 0], [dx, dy]]
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        oss = sm._objects.spatial
        result = oss.project_protection(
            sid,
            OBSTACLE,
            p,
            wind_from=direction,
            properties=protection(direction=direction, n=n, cell=step),
        )
        expected = {
            f"{3 + dx * (k + 1)},{3 + dy * (k + 1)}"
            for k in range(1, n + 1)
            if 0 <= 3 + dx * (k + 1) < 9 and 0 <= 3 + dy * (k + 1) < 9
        }
        assert {c.cell for c in result.wind_cells} == expected
        assert not expected.intersection(oss.project(sid, OBSTACLE, p).occupied_cells)


def test_movement_uses_live_pose_and_preserves_legacy_consumers(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        oss = sm._objects.spatial
        args = dict(wind_from="north", properties=protection())
        old = oss.project_protection(sid, OBSTACLE, p, **args)
        gm = replace(context, can_place_world_objects=True)
        op = RepositionObject(A, OBSTACLE, "3,3", "4,3", "north", "east")
        assert submit(sm, sid, p, gm, op, "move.rotate.protection").status == "committed"
        new = oss.project_protection(sid, OBSTACLE, p, **args)
        assert {c.cell for c in old.rain_cells} == {"3,3", "4,3"}
        assert {c.cell for c in new.rain_cells} == {"4,3", "4,4"}
        assert new.wind_status == "none"  # boundary now applies to incoming east
        east = oss.project_protection(sid, OBSTACLE, p, **{**args, "wind_from": "east"})
        assert {c.cell for c in east.wind_cells} == {"3,3", "2,3", "1,3"}
        assert oss.scene_contributions(sid, A, p)[OBSTACLE] == oss.project(sid, OBSTACLE, p)


def test_rain_is_independent_of_footprint_wind_height_walls_and_blocked_cells(tmp_path, context):
    data = large_data()
    changed_grid(data, blocked_cells=["4,3"], wall_segments=[{"x1": 3, "y1": 4, "x2": 4, "y2": 4}])
    sm, sid, p, _ = setup(tmp_path, context, data)
    with sm:
        oss = sm._objects.spatial
        props = protection(base=None)
        for direction in ORIENTATIONS:
            result = oss.project_protection(sid, OBSTACLE, p, properties=props, wind_from=direction)
            assert {c.cell for c in result.rain_cells} == {"3,3", "4,3"}
        assert oss.project(sid, OBSTACLE, p).occupied_cells == ("3,3",)
        # Existing walls/obstacles neither become protection sources nor terminate rays.
        result = oss.project_protection(
            sid, OBSTACLE, p, properties=protection(), wind_from="north"
        )
        assert {c.cell for c in result.wind_cells} == {"3,4", "3,5", "3,6"}


@pytest.mark.parametrize("field", ["base", "lower", "upper"])
def test_missing_height_is_explicit_and_rain_still_projects(tmp_path, context, field):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        result = sm._objects.spatial.project_protection(
            sid, OBSTACLE, p, properties=protection(**{field: None}), wind_from="north"
        )
        assert result.wind_status == "missing_input"
        assert result.wind_reason == "wind_height_required" and result.wind_cells == ()
        assert result.rain_status == "ready"


def test_statuses_clipping_negative_base_and_unconfigured_legacy(tmp_path, context):
    sm, sid, p, _ = setup(tmp_path, context, large_data())
    with sm:
        oss = sm._objects.spatial
        assert (
            oss.project_protection(sid, OBSTACLE, p, wind_from="north").rain_status
            == "unconfigured"
        )
        empty = CurrentProtectionProperties(RainProtection(()), WindProtection(()))
        result = oss.project_protection(sid, OBSTACLE, p, wind_from="north", properties=empty)
        assert result.rain_status == result.wind_status == "none"
        candidate = replace(oss.state(sid, OBSTACLE, p), anchor="8,8")
        result = oss.project_protection(
            sid, OBSTACLE, p, candidate=candidate, properties=protection(), wind_from="north"
        )
        assert [c.cell for c in result.rain_cells] == ["8,8"]
        assert result.wind_status == "none"
        result = oss.project_protection(
            sid, OBSTACLE, p, properties=protection(base=-5), wind_from="north"
        )
        assert result.wind_at("3,4", -3) and not result.wind_at("3,4", 0)
        with pytest.raises(FrozenInstanceError):
            result.wind_status = "none"
        with pytest.raises(FrozenInstanceError):
            result.wind_cells[0].upper_height_ft = 100


def test_scene_keeps_source_intervals_and_gaps_including_one_object_multiple_intervals(
    tmp_path, context
):
    data = large_data()
    other = "object.second_shelter"
    obj = deepcopy(data["scenes"][0]["objects"][-1])
    obj["object_id"] = other
    data["scenes"][0]["objects"].append(obj)
    definition = deepcopy(data["world_objects"][-1])
    definition.update(object_id=other, definition_ref="definition.second_shelter")
    data["world_objects"].append(definition)
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        oss = sm._objects.spatial
        first = protection(base=0, lower=0, upper=2)
        # Duplicate identical boundary deduplicates, separated intervals survive.
        b = first.wind.boundaries[0]
        first = replace(
            first, wind=WindProtection((b, b, replace(b, lower_height_ft=6, upper_height_ft=8)))
        )
        second = protection(base=0, lower=4, upper=5)
        before = stored_rows(db)
        scene = oss.scene_protection_contributions(
            sid, A, p, wind_from="north", properties={OBSTACLE: first, other: second}
        )
        assert [
            (c.object_id, c.lower_height_ft, c.upper_height_ft) for c in scene.wind_at("3,4", 1)
        ] == [(OBSTACLE, 0, 2)]
        assert not scene.wind_at("3,4", 3)
        assert [c.object_id for c in scene.wind_at("3,4", 4.5)] == [other]
        assert not scene.wind_at("3,4", 5.5)
        assert [c.object_id for c in scene.wind_at("3,4", 7)] == [OBSTACLE]
        assert len(next(p for p in scene.objects if p.object_id == OBSTACLE).wind_cells) == 6
        assert stored_rows(db) == before


class TestWOProvider:
    __test__ = False

    def __init__(self, value):
        self.value = value
        self.calls = []

    def current_protection(self, sid, oid, package, *, candidate):
        self.calls.append((sid, oid, package.package_digest, candidate))
        return self.value if oid == OBSTACLE else None


def test_current_provider_changes_without_guessing_open_or_destroyed(tmp_path, context):
    data = oriented_data(anchor="1,0", footprint=[[0, 0]], blocking=False, openable=True)
    sm, sid, p, db = setup(tmp_path, context, data)
    with sm:
        provider = TestWOProvider(protection(base=0, n=1))
        oss = ObjectSpatialService(sm._world, protection_provider=provider)
        before = oss.project_protection(sid, OBSTACLE, p, wind_from="north")
        assert (
            submit(
                sm, sid, p, context, SetObjectState(A, OBSTACLE, ObjectState.OPEN), "open"
            ).status
            == "committed"
        )
        # Opening does not imply rain/wind change while WO supplies the same values.
        opened = oss.project_protection(sid, OBSTACLE, p, wind_from="north")
        assert opened == before
        state = replace(oss.state(sid, OBSTACLE, p), destroyed=True)
        assert (
            oss.project_protection(sid, OBSTACLE, p, candidate=state, wind_from="north") == before
        )
        assert provider.calls[-1][-1] == state
        rows, changes = stored_rows(db), sm._conn.total_changes
        provider.value = CurrentProtectionProperties(RainProtection(()), WindProtection(()))
        after = oss.project_protection(sid, OBSTACLE, p, wind_from="north")
        assert after.rain_status == after.wind_status == "none"
        assert before.rain_cells and before.wind_cells  # old immutable projection remains intact
        # Explicit input overrides provider; no retained protection state/cache.
        assert (
            oss.project_protection(
                sid, OBSTACLE, p, properties=protection(base=0, n=1), wind_from="north"
            )
            == before
        )
        provider.value = None
        assert (
            oss.project_protection(sid, OBSTACLE, p, wind_from="north").wind_status
            == "unconfigured"
        )
        assert stored_rows(db) == rows and sm._conn.total_changes == changes


def test_shared_doors_not_copied_and_unvisited_scene_query_does_not_initialize(tmp_path, context):
    sm, sid, p, db = setup(tmp_path, context, large_data())
    with sm:
        oss = sm._objects.spatial
        before = stored_rows(db)
        for scene_id in (A, B):
            scene = oss.scene_protection_contributions(
                sid, scene_id, p, wind_from="north", properties={DOOR: protection()}
            )
            door = next(p for p in scene.objects if p.object_id == DOOR)
            assert door.scene_id == scene_id
            assert door.rain_status == door.wind_status == "unsupported"
            assert not door.rain_cells and not door.wind_cells
        assert stored_rows(db) == before


def test_carried_and_contained_objects_do_not_gain_ground_protection(tmp_path, context):
    from test_lockable_passages import acquire
    from test_world_containers import BOX, container_data

    sm, sid, p, db = setup(tmp_path, context, container_data())
    with sm:
        oss = sm._objects.spatial
        acquire(sm, sid, p, context)
        assert submit(sm, sid, p, context, PickupItem(BOX, A, "1,0"), "carry").status == "committed"
        assert (
            submit(sm, sid, p, context, PutItem(A, CRED, BOX, "0,0"), "contain").status
            == "committed"
        )
        before = stored_rows(db)
        for oid in (BOX, CRED):
            result = oss.project_protection(sid, oid, p, wind_from="north", properties=protection())
            assert result.rain_status == result.wind_status == "unsupported"
            assert result.wind_reason == "nonground_protection_pose_unsupported"
            assert result.rain_cells == result.wind_cells == ()
        scene = oss.scene_protection_contributions(sid, A, p, wind_from="north")
        assert {BOX, CRED}.isdisjoint({p.object_id for p in scene.objects})
        assert stored_rows(db) == before


@pytest.mark.parametrize("n", [0, -1, 257, True, 1.5, None])
def test_invalid_n_rejected(n):
    with pytest.raises(ValueError):
        protection(n=n)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RainProtection([(0, 0)]),
        lambda: RainProtection(((0.5, 0),)),
        lambda: RainProtection(((33, 0),)),
        lambda: RainProtection(((0, 0),) * 65),
        lambda: WindProtection((WindBoundary((0, 0), "north", 256, 0, 1),) * 17),
        lambda: WindProtection((WindBoundary((0, 0), "north", 1, 0, 1),) * 65),
        lambda: WindProtection([WindBoundary((0, 0), "north", 1, 0, 1)]),
        lambda: protection(direction="northeast"),
        lambda: protection(lower=5, upper=5),
        lambda: protection(lower=6, upper=5),
        lambda: protection(base=float("nan")),
        lambda: protection(upper=float("inf")),
        lambda: protection(lower=True),
        lambda: CurrentProtectionProperties(rain={}),
    ],
)
def test_invalid_or_mutable_current_inputs_rejected(factory):
    with pytest.raises(ValueError):
        factory()
