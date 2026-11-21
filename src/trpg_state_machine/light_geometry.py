"""Clip continuous point-light visibility against straight blockers in grid units.

The caller determines which segments are opaque and supplies the region of interest.
Clipping produces immutable convex pieces with disjoint interiors for both map
rendering and positive-area cell queries.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from math import fsum, ulp

type Point = tuple[float, float]
type Segment = tuple[Point, Point]
type Bounds = tuple[float, float, float, float]
type Polygon = tuple[Point, ...]

_CARDINAL_VECTORS: dict[str, Point] = {
    "north": (0, -1),
    "east": (1, 0),
    "south": (0, 1),
    "west": (-1, 0),
}


@dataclass(frozen=True, slots=True)
class _HalfPlane:
    """An anchored line with its inward normal, used to clip a convex piece."""

    anchor: Point
    normal: Point

    def distance(self, point: Point) -> float:
        """Return a signed, unnormalized distance for the clipping test."""
        x, y = self.anchor
        nx, ny = self.normal
        return fsum(((point[0] - x) * nx, (point[1] - y) * ny))

    def intersection(self, start: Point, end: Point, first: float, second: float) -> Point:
        """Interpolate the intersection, preserving an exact axis-aligned boundary."""
        weight = first / (first - second)
        nx, ny = self.normal
        x = self.anchor[0] if ny == 0 else start[0] + (end[0] - start[0]) * weight
        y = self.anchor[1] if nx == 0 else start[1] + (end[1] - start[1]) * weight
        return x, y


def _twice_area(polygon: Polygon) -> float:
    """Sum oriented triangle areas in coordinates relative to the first vertex."""
    x, y = polygon[0]
    vectors = tuple((px - x, py - y) for px, py in polygon[1:])
    return abs(fsum(a * d - b * c for (a, b), (c, d) in pairwise(vectors)))


def _has_area(polygon: Polygon) -> bool:
    """Reject zero-area pieces and slivers at the scale of vertex roundoff."""
    if len(polygon) < 3:
        return False
    xs, ys = zip(*polygon, strict=True)
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    tolerance = 8 * (
        max(ulp(left), ulp(right)) * (bottom - top) + max(ulp(top), ulp(bottom)) * (right - left)
    )
    return _twice_area(polygon) > tolerance


def _split(polygon: Polygon, plane: _HalfPlane) -> tuple[Polygon, Polygon]:
    """Split into pieces on the inward and outward sides of the clipping line."""
    distances = tuple(plane.distance(vertex) for vertex in polygon)
    if min(distances) >= 0:
        return polygon, ()
    if max(distances) <= 0:
        return (), polygon

    inside, outside = [], []
    previous, old_distance = polygon[-1], distances[-1]
    for point, distance in zip(polygon, distances, strict=True):
        if (old_distance < 0 < distance) or (distance < 0 < old_distance):
            intersection = plane.intersection(previous, point, old_distance, distance)
            inside.append(intersection)
            outside.append(intersection)
        if distance >= 0:
            inside.append(point)
        if distance <= 0:
            outside.append(point)
        previous, old_distance = point, distance
    return tuple(inside), tuple(outside)


def _shadow(origin: Point, blocker: Segment) -> tuple[_HalfPlane, ...]:
    """Form the three inward half-planes of a segment's unbounded shadow."""
    start, end = blocker
    ox, oy = origin
    ax, ay = start[0] - ox, start[1] - oy
    bx, by = end[0] - ox, end[1] - oy
    winding = fsum((ax * by, -ay * bx))
    if winding == 0:
        return ()  # A radial segment can affect only a ray through the origin.
    if winding < 0:
        start, end = end, start
        ax, ay, bx, by = bx, by, ax, ay
    return (
        _HalfPlane(start, (end[1] - start[1], start[0] - end[0])),
        _HalfPlane(origin, (-ay, ax)),
        _HalfPlane(origin, (by, -bx)),
    )


def _subtract(polygon: Polygon, shadow: tuple[_HalfPlane, ...]) -> tuple[Polygon, ...]:
    """Keep the disjoint pieces lying outside at least one shadow boundary."""
    outside = []
    candidate = polygon
    for plane in shadow:
        candidate, remainder = _split(candidate, plane)
        if not candidate:
            return (polygon,)
        if remainder and _has_area(remainder):
            outside.append(remainder)
    # A shadow that only touches the piece need not fragment it.
    return tuple(outside) if _has_area(candidate) else (polygon,)


def lit_polygons(origin: Point, bounds: Bounds, blockers: Iterable[Segment]) -> tuple[Polygon, ...]:
    """Return visible pieces within ``(left, top, right, bottom)`` in grid units.

    The point source can lie outside the output bounds. Each finite segment
    blocks the rays passing through it beyond its position. Radial or zero-length
    segments have no effect on visible area. Inputs must contain finite coordinates.
    """
    left, top, right, bottom = bounds
    if left >= right or top >= bottom:
        return ()
    pieces = (((left, top), (right, top), (right, bottom), (left, bottom)),)
    for blocker in blockers:
        shadow = _shadow(origin, blocker)
        if shadow:
            pieces = tuple(remaining for piece in pieces for remaining in _subtract(piece, shadow))
        if not pieces:
            break
    return pieces


def _clip_to_planes(polygon: Polygon, planes: tuple[_HalfPlane, ...]) -> Polygon:
    """Keep the part of a convex piece inside every clipping plane."""
    for plane in planes:
        polygon, _ = _split(polygon, plane)
        if not polygon:
            break
    return polygon


def _clip_polygon(polygon: Polygon, bounds: Bounds) -> Polygon:
    """Clip one convex piece to an axis-aligned rectangle."""
    left, top, right, bottom = bounds
    return _clip_to_planes(
        polygon,
        (
            _HalfPlane((left, top), (1, 0)),
            _HalfPlane((left, top), (0, 1)),
            _HalfPlane((right, bottom), (-1, 0)),
            _HalfPlane((right, bottom), (0, -1)),
        ),
    )


def clip_to_bounds(polygons: Iterable[Polygon], bounds: Bounds) -> tuple[Polygon, ...]:
    """Return the positive-area parts of visible pieces inside the requested rectangle."""
    return tuple(
        clipped
        for polygon in polygons
        if polygon and _has_area(clipped := _clip_polygon(polygon, bounds))
    )


def clip_to_cone(
    polygons: Iterable[Polygon],
    origin: Point,
    direction: str,
    length: float,
    *,
    start: float = 0.0,
) -> tuple[Polygon, ...]:
    """Clip visible pieces to a cardinal cone band in continuous grid units.

    Width equals the axial distance from the apex. The axial limits are measured
    from that same apex. Merely touching a boundary does not illuminate an area.
    """
    if length <= start:
        return ()
    x, y = origin
    dx, dy = _CARDINAL_VECTORS[direction]
    planes = (
        _HalfPlane(origin, (dx + 2 * dy, dy - 2 * dx)),
        _HalfPlane(origin, (dx - 2 * dy, dy + 2 * dx)),
        _HalfPlane((x + start * dx, y + start * dy), (dx, dy)),
        _HalfPlane((x + length * dx, y + length * dy), (-dx, -dy)),
    )
    return tuple(
        clipped
        for polygon in polygons
        if polygon and _has_area(clipped := _clip_to_planes(polygon, planes))
    )


def shadow_intersects_interior(
    origin: Point, polygons: tuple[Polygon, ...], blockers: Iterable[Segment]
) -> bool:
    """Check whether a blocker shadow covers positive area in previously lit pieces.

    Measure intersections directly to retain small shadow areas beside large lit areas.
    """
    for blocker in blockers:
        shadow = _shadow(origin, blocker)
        if shadow and any(
            piece and _has_area(_clip_to_planes(piece, shadow)) for piece in polygons
        ):
            return True
    return False
