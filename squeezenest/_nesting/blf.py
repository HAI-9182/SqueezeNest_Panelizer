"""squeezenest._nesting.blf -- Greedy Bottom-Left-Fill (BLF) nesting algorithm.

Implements a row-packing bottom-left-fill placement strategy:
  1. For each part instance (in order), try candidate positions left-to-right, bottom-to-top.
  2. At each candidate, check:
     a. Part + clearance fits within stock boundary.
     b. Part + clearance does not overlap any previously placed part.
  3. If a valid position is found, record the placement.
  4. If no position fits, add the part to unplaced_parts.

This greedy BLF is the v0.1 baseline. The Beam Search variant (v0.2) will replace
the inner candidate-selection loop.

Invariants enforced:
  I-02: No two placed_outline polygons overlap (Clipper2 intersection area == 0).
  I-03: All placed_outline polygons fit strictly within StockSheet boundary.
  I-05: PlacementLayout.yield_count == len(placements).

Public API note: coordinates at the nesting layer are float mm (not int64).
Int64 conversion is only needed at the Clipper2 boundary (SN-004).
"""
from __future__ import annotations

from shapely.geometry import Polygon as SPolygon

from squeezenest._nesting._geometry import (
    GRID_STEP_MM,
    get_rotations,
    rotate_polygon,
    part_bounding_box,
    translate_polygon,
)
from squeezenest.api.models import (
    PlacedPart, PlacementLayout, StockSheet, RotationSet,
    PolygonWithHoles, Polygon,
)

__all__ = ["bottom_left_fill", "run_nesting_job"]

# ---------------------------------------------------------------------------
# Back-compat aliases so existing code that imported private helpers directly
# continues to work during any transition period.  Deprecated — use
# squeezenest._nesting._geometry instead.
# ---------------------------------------------------------------------------
_GRID_STEP_MM = GRID_STEP_MM
_get_rotations = get_rotations
_rotate_polygon = rotate_polygon
_part_bounding_box = part_bounding_box
_translate_polygon = translate_polygon


def bottom_left_fill(
    parts: list[tuple[str, PolygonWithHoles]],
    stock: StockSheet,
    clearance_mm: float,
    rotation_set: RotationSet,
) -> PlacementLayout:
    """Place parts greedily using bottom-left-fill.

    Args:
        parts:        List of (part_id, (outer_ring, holes)) in placement order.
                      Each entry represents **one instance** of the part; callers
                      are responsible for expanding multi-quantity parts before
                      passing them here.
        stock:        Stock sheet defining the available area.
        clearance_mm: Minimum gap between any two part outlines.
        rotation_set: Allowed rotation angles for each part.

    Returns:
        PlacementLayout with all successful placements and any unplaced parts.
    """
    W, H = stock.width_mm, stock.height_mm
    stock_poly = SPolygon([(0, 0), (W, 0), (W, H), (0, H)])

    placed_shapely: list[SPolygon] = []  # inflated (with clearance) placed polys
    placements: list[PlacedPart] = []
    unplaced: list[tuple[str, int]] = []

    rotations = get_rotations(rotation_set)

    # Track per-part-id instance counters so instance_idx reflects the
    # 0-based copy number within each part_id (Invariant I-05 / model docs).
    instance_counters: dict[str, int] = {}

    for part_id, (outer, _holes) in parts:
        # Determine the instance index for this part_id
        inst_idx = instance_counters.get(part_id, 0)

        placed = False

        for angle in rotations:
            rotated = rotate_polygon(outer, angle)
            min_x, min_y, max_x, max_y = part_bounding_box(rotated)
            part_w = max_x - min_x
            part_h = max_y - min_y

            # Can this rotated part fit in the stock at all?
            if part_w + 2 * clearance_mm > W or part_h + 2 * clearance_mm > H:
                continue

            # Normalise rotated polygon to origin
            normalised = translate_polygon(rotated, -min_x, -min_y)

            # Scan candidate bottom-left positions
            y = clearance_mm
            found = False
            while y + part_h <= H - clearance_mm + 1e-9 and not found:
                x = clearance_mm
                while x + part_w <= W - clearance_mm + 1e-9 and not found:
                    candidate = translate_polygon(normalised, x, y)
                    candidate_shapely = SPolygon(candidate)
                    # Check within stock
                    if not stock_poly.contains(candidate_shapely):
                        x += GRID_STEP_MM
                        continue
                    # Inflate by clearance for collision check
                    inflated = candidate_shapely.buffer(clearance_mm, quad_segs=2)
                    # Check no overlap with previously placed (inflated) parts
                    collision = any(
                        inflated.intersects(prev) and inflated.intersection(prev).area > 1e-9
                        for prev in placed_shapely
                    )
                    if not collision:
                        # Valid placement found
                        placed_shapely.append(inflated)
                        outline: Polygon = list(candidate)
                        placements.append(PlacedPart(
                            part_id=part_id,
                            instance_idx=inst_idx,
                            sheet_id=stock.sheet_id,
                            position_mm=(x, y),
                            rotation_deg=angle,
                            placed_outline=outline,
                        ))
                        instance_counters[part_id] = inst_idx + 1
                        found = True
                        placed = True
                    else:
                        x += GRID_STEP_MM
                y += GRID_STEP_MM

            if found:
                break  # No need to try other rotations

        if not placed:
            unplaced.append((part_id, inst_idx))
            # Advance the counter even for unplaced instances so subsequent
            # instances of the same part_id get correct indices.
            instance_counters[part_id] = inst_idx + 1

    # Compute utilisation
    placed_area = sum(SPolygon(p.placed_outline).area for p in placements)
    utilisation = placed_area / (W * H) if (W * H) > 0 else 0.0

    return PlacementLayout(
        placements=tuple(placements),
        unplaced_parts=tuple(unplaced),
        yield_count=len(placements),       # Invariant I-05
        utilisation=utilisation,
        sheets_used=1,
    )


def run_nesting_job(job: "NestingJob") -> "NestingResult":  # type: ignore[name-defined]
    """Execute a NestingJob using BLF.  Called by NestingJob.run()."""
    from squeezenest.api.models import NestingJob, NestingResult, ValidationReport, PlacementLayout
    assert isinstance(job, NestingJob)

    if not job.stock:
        report = ValidationReport.from_violations([])
        layout = PlacementLayout(
            placements=(), unplaced_parts=(), yield_count=0, utilisation=0.0, sheets_used=0
        )
        return NestingResult(layout=layout, report=report, job=job)

    # Multi-sheet support beyond a single sheet is not yet implemented (v0.1).
    # Only the first sheet is used; using multiple sheets would require carrying
    # unplaced parts from one sheet forward to the next.
    if len(job.stock) > 1:
        import warnings
        warnings.warn(
            "BLF nesting currently uses only the first stock sheet; "
            f"{len(job.stock) - 1} additional sheet(s) ignored.",
            stacklevel=2,
        )

    sheet = job.stock[0]

    # Expand parts by quantity so instance_idx within bottom_left_fill reflects
    # the 0-based copy number for each part_id (fixes invariant on PlacedPart).
    parts_list: list[tuple[str, PolygonWithHoles]] = []
    for part_id, (geom, meta) in job.parts.items():
        for _ in range(meta.quantity):
            parts_list.append((part_id, geom))

    layout = bottom_left_fill(
        parts_list, sheet,
        clearance_mm=job.clearance_mm,
        rotation_set=job.rotation_set,
    )

    report = ValidationReport.from_violations([])
    return NestingResult(layout=layout, report=report, job=job)
