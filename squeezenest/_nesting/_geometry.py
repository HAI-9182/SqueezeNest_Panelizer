"""squeezenest._nesting._geometry -- Shared low-level polygon helpers.

This module collects geometry primitives that are used by more than one
nesting algorithm (BLF, Lattice, GA).  Centralising them here avoids the
fragile pattern of importing private ``_``-prefixed symbols across module
boundaries.

All coordinates are float mm (not int64 scale units).  int64 conversion
only happens at the Clipper2 boundary (squeezenest._core.scale).
"""
from __future__ import annotations

import math

from squeezenest.api.models import Polygon, RotationSet

__all__ = [
    "GRID_STEP_MM",
    "get_rotations",
    "rotate_polygon",
    "part_bounding_box",
    "translate_polygon",
]

# Candidate grid step size (mm).  Smaller = tighter packing, slower.
GRID_STEP_MM: float = 0.5


def get_rotations(rotation_set: RotationSet) -> list[float]:
    """Return the list of rotation angles (degrees) for the given RotationSet."""
    mapping: dict[RotationSet, list[float]] = {
        RotationSet.NONE:    [0.0],
        RotationSet.HALF:    [0.0, 180.0],
        RotationSet.ORTHO:   [0.0, 90.0, 180.0, 270.0],
        RotationSet.FREE_45: [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0],
    }
    return mapping[rotation_set]


def rotate_polygon(poly: Polygon, angle_deg: float) -> Polygon:
    """Rotate a polygon around its own centroid by angle_deg degrees."""
    if angle_deg == 0.0:
        return list(poly)
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    n = len(poly)
    cx = sum(p[0] for p in poly) / n
    cy = sum(p[1] for p in poly) / n
    rotated: Polygon = []
    for x, y in poly:
        dx, dy = x - cx, y - cy
        rotated.append((cx + dx * cos_a - dy * sin_a,
                        cy + dx * sin_a + dy * cos_a))
    return rotated


def part_bounding_box(poly: Polygon) -> tuple[float, float, float, float]:
    """Return (min_x, min_y, max_x, max_y) of a polygon."""
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def translate_polygon(poly: Polygon, dx: float, dy: float) -> Polygon:
    """Translate every vertex of poly by (dx, dy)."""
    return [(x + dx, y + dy) for x, y in poly]
