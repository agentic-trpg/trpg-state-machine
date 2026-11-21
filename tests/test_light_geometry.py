from math import fsum

import pytest

from trpg_state_machine.light_geometry import (
    clip_to_bounds,
    clip_to_cone,
    lit_polygons,
    shadow_intersects_interior,
)


def coverage_area(polygons):
    areas = []
    for polygon in polygons:
        x, y = polygon[0]
        relative = tuple((px - x, py - y) for px, py in polygon)
        area = fsum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(relative[:-1], relative[1:], strict=True)
        )
        areas.append(abs(area) / 2)
    return fsum(areas)


def overlaps_cell(polygons, x, y):
    return bool(clip_to_bounds(polygons, (x, y, x + 1, y + 1)))


@pytest.mark.parametrize("offset", (0, 1_000_000))
def test_diagonal_shadow_preserves_partial_area_and_rejects_tangency(offset):
    origin = (offset + 0.5, offset + 0.5)
    bounds = (offset + 1, offset + 1, offset + 3, offset + 3)
    blockers = (((offset + 0.5, offset + 2.5), (offset + 2.5, offset + 0.5)),)
    polygons = lit_polygons(origin, bounds, blockers)

    assert coverage_area(polygons) == pytest.approx(0.5)
    assert overlaps_cell(polygons, offset + 1, offset + 1)
    assert not overlaps_cell(polygons, offset + 2, offset + 1)
    assert not overlaps_cell(polygons, offset + 1, offset + 2)


def test_finite_wall_blocks_the_center_ray_but_illuminates_both_sides_of_cell():
    origin, bounds = (0.5, 0.5), (2, 0, 3, 1)
    wall = ((1.5, 0.4), (1.5, 0.6))
    polygons = lit_polygons(origin, bounds, (wall,))

    assert len(polygons) == 2
    assert coverage_area(polygons) == pytest.approx(0.6)
    assert overlaps_cell(polygons, 2, 0)
    assert coverage_area(clip_to_bounds(polygons, (2, 0.45, 3, 0.55))) == 0
    assert lit_polygons(origin, bounds, (wall[::-1],)) == polygons


def test_overlapping_shadows_can_cover_a_cell_that_either_wall_alone_would_light():
    origin, bounds = (0.5, 0.5), (2, 0, 3, 1)
    upper_wall = ((1.5, -0.5), (1.5, 0.6))
    lower_wall = ((1.5, 0.4), (1.5, 1.5))

    assert overlaps_cell(lit_polygons(origin, bounds, (upper_wall,)), 2, 0)
    assert overlaps_cell(lit_polygons(origin, bounds, (lower_wall,)), 2, 0)
    assert lit_polygons(origin, bounds, (upper_wall, lower_wall)) == ()
    assert lit_polygons(origin, bounds, (lower_wall, upper_wall)) == ()


def test_closed_opaque_footprint_blocks_its_interior_and_the_area_beyond():
    corners = ((1, 0), (2, 0), (2, 1), (1, 1))
    edges = tuple(zip(corners, corners[1:] + corners[:1], strict=True))
    polygons = lit_polygons((0.5, 0.5), (0, 0, 3, 1), edges)

    assert overlaps_cell(polygons, 0, 0)
    assert not overlaps_cell(polygons, 1, 0)
    assert not overlaps_cell(polygons, 2, 0)


def test_non_occluding_walls_keep_visible_boundary_intact():
    origin, bounds = (0.5, 0.5), (2, 0, 3, 1)
    blockers = (
        ((3.5, 0), (3.5, 1)),  # Beyond the bounds.
        ((0, 0), (0, 1)),  # Behind the source.
        ((1, 1), (2, 2)),  # Radial, including the extension to the source.
        ((1, 0.1), (1, 0.1)),  # Zero length.
        ((0.5, 0.5), (2, 0)),  # Touches the source.
        ((1.25, 0.75), (1.25, 1)),  # Shadow only touches the bottom-left corner.
    )

    assert lit_polygons(origin, bounds, blockers) == lit_polygons(origin, bounds, ())


def test_cell_boundary_contact_does_not_count_as_illumination():
    polygons = lit_polygons((0.5, 0.5), (0, 0, 1, 1), ())

    assert overlaps_cell(polygons, 0, 0)
    assert not overlaps_cell(polygons, 1, 0)
    assert not overlaps_cell(polygons, 1, 1)
    assert clip_to_bounds(polygons, (1, 0, 2, 1)) == ()


def test_small_positive_area_at_a_cell_corner_is_still_lit():
    origin, bounds = (0.5, 0.5), (2, 0, 3, 1)
    sliver = 2**-24
    wall = ((1.25, 0), (1.25, 0.75 - sliver))
    polygons = lit_polygons(origin, bounds, (wall,))

    assert overlaps_cell(polygons, 2, 0)
    assert 0 < coverage_area(polygons) < 1e-12


def test_possible_shadow_distinguishes_tiny_intersection_from_tangency():
    origin, bounds = (0.5, 0.5), (2, 0, 3, 1)
    polygons = lit_polygons(origin, bounds, ())
    tangent = ((1.25, 0.75), (1.25, 1))
    tiny = ((1.25, 0.75 - 2**-24), (1.25, 1))

    assert not shadow_intersects_interior(origin, polygons, (tangent,))
    assert shadow_intersects_interior(origin, polygons, (tangent, tiny))
    assert not shadow_intersects_interior(origin, lit_polygons(origin, bounds, (tiny,)), (tiny,))


@pytest.mark.parametrize(
    ("direction", "ahead", "behind"),
    [
        ("north", (0, -2), (0, 2)),
        ("east", (2, 0), (-2, 0)),
        ("south", (0, 2), (0, -2)),
        ("west", (-2, 0), (2, 0)),
    ],
)
def test_cone_rotates_with_facing_and_uses_disjoint_axial_bands(direction, ahead, behind):
    origin = (0.5, 0.5)
    visible = lit_polygons(origin, (-5, -5, 6, 6), ())

    bright = clip_to_cone(visible, origin, direction, 2.5)
    dim = clip_to_cone(visible, origin, direction, 4.5, start=2.5)
    assert coverage_area(bright) == pytest.approx(0.5 * 2.5**2)
    assert coverage_area(dim) == pytest.approx(0.5 * (4.5**2 - 2.5**2))
    assert overlaps_cell(bright, *ahead)
    assert not overlaps_cell(dim, *ahead)
    assert not overlaps_cell(bright, *behind)
    assert not overlaps_cell(dim, *behind)
    assert clip_to_cone(visible, origin, direction, 2.5, start=2.5) == ()


def test_cone_clips_existing_wall_shadows_and_ignores_edge_contact():
    origin, bounds = (0.5, 0.5), (-1, -3, 5, 4)
    wall = ((1.5, -0.1), (1.5, 0.6))
    visible = lit_polygons(origin, bounds, (wall,))

    cone = clip_to_cone(visible, origin, "east", 3.5)
    assert not overlaps_cell(cone, 3, -1)
    assert overlaps_cell(cone, 3, 1)
    assert not overlaps_cell(cone, 4, 0)
    assert not overlaps_cell(cone, -1, 0)
    assert not overlaps_cell(cone, 1, 2)
