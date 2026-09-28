"""squeezenest._nesting.lattice -- Pair Clustering & Lattice Tiling algorithm.

Optimized for mass panelization of homogeneous parts.
"""
from __future__ import annotations

import math
from typing import Callable
from shapely.geometry import Polygon as SPolygon, MultiPoint

from squeezenest.api.models import (
    PlacedPart, PlacementLayout, StockSheet, RotationSet,
    PolygonWithHoles, Point2D, Polygon, NestingJob,
)
from squeezenest._nesting._geometry import (
    GRID_STEP_MM,
    get_rotations,
    rotate_polygon,
    part_bounding_box,
    translate_polygon,
)
from squeezenest._nesting.blf import bottom_left_fill

__all__ = ["run_lattice_job"]


def _convex_hull_area(poly_a: Polygon, poly_b: Polygon) -> float:
    """Return the convex hull area of two polygons combined."""
    return MultiPoint(poly_a + poly_b).convex_hull.area


def _combined_bounding_box(poly_a: Polygon, poly_b: Polygon) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly_a] + [p[0] for p in poly_b]
    ys = [p[1] for p in poly_a] + [p[1] for p in poly_b]
    return min(xs), min(ys), max(xs), max(ys)


def find_best_cluster(
    outer: Polygon,
    rotations: list[float],
    clearance_mm: float,
    stop_check: Callable[[], bool] | None = None
) -> tuple[Polygon, float, float, float, float, float, float, float]:
    """Finds the tightest 2-part cluster.

    Returns:
        (best_b_poly, b_dx, b_dy, b_angle, cluster_min_x, cluster_min_y, cluster_max_x, cluster_max_y)
    """
    min_x, min_y, max_x, max_y = part_bounding_box(outer)
    # Normalize A to origin for clustering search
    poly_a = translate_polygon(outer, -min_x, -min_y)

    w_a = max_x - min_x
    h_a = max_y - min_y

    a_shape = SPolygon(poly_a)
    # Inflate A by clearance to check collisions easily
    a_inflated = a_shape.buffer(clearance_mm, quad_segs=2)

    best_area = float('inf')
    best_b_poly: Polygon = []
    best_dx = 0.0
    best_dy = 0.0
    best_angle = 0.0
    best_bbox = (0.0, 0.0, 0.0, 0.0)

    step = GRID_STEP_MM

    for angle in rotations:
        rotated_b = rotate_polygon(poly_a, angle)
        b_min_x, b_min_y, b_max_x, b_max_y = part_bounding_box(rotated_b)
        b_norm = translate_polygon(rotated_b, -b_min_x, -b_min_y)
        b_w = b_max_x - b_min_x
        b_h = b_max_y - b_min_y

        # Scan around A to find the best non-overlapping position
        start_x = -math.ceil(b_w / step) * step - step
        end_x = math.ceil(w_a / step) * step + step
        start_y = -math.ceil(b_h / step) * step - step
        end_y = math.ceil(h_a / step) * step + step

        y = start_y
        while y <= end_y:
            x = start_x
            while x <= end_x:
                if stop_check and stop_check():
                    if best_area != float('inf'):
                        return (best_b_poly, best_dx, best_dy, best_angle,
                                best_bbox[0], best_bbox[1], best_bbox[2], best_bbox[3])
                    else:
                        break  # Stop X loop

                candidate_b = translate_polygon(b_norm, x, y)
                c_min_x, c_min_y, c_max_x, c_max_y = part_bounding_box(candidate_b)

                # Fast bounding box overlap check with A's inflated bbox
                if (c_min_x > w_a + clearance_mm + 1e-9 or
                        c_max_x < -clearance_mm - 1e-9 or
                        c_min_y > h_a + clearance_mm + 1e-9 or
                        c_max_y < -clearance_mm - 1e-9):
                    x += step
                    continue

                candidate_shape = SPolygon(candidate_b)

                if not a_inflated.intersects(candidate_shape) or a_inflated.intersection(candidate_shape).area < 1e-9:
                    area = _convex_hull_area(poly_a, candidate_b)
                    if area < best_area:
                        best_area = area
                        best_b_poly = candidate_b
                        best_dx = x
                        best_dy = y
                        best_angle = angle
                        best_bbox = _combined_bounding_box(poly_a, candidate_b)
                x += step

            if stop_check and stop_check() and best_area == float('inf'):
                break  # Stop Y loop
            y += step

        if stop_check and stop_check() and best_area == float('inf'):
            break  # Stop rotation loop

    if best_area == float('inf'):
        best_angle = rotations[0]
        rotated_b = rotate_polygon(poly_a, best_angle)
        b_min_x, b_min_y, b_max_x, b_max_y = part_bounding_box(rotated_b)
        b_norm = translate_polygon(rotated_b, -b_min_x, -b_min_y)
        best_dx = w_a + clearance_mm
        best_dy = 0.0
        best_b_poly = translate_polygon(b_norm, best_dx, best_dy)
        best_bbox = _combined_bounding_box(poly_a, best_b_poly)

    return (best_b_poly, best_dx, best_dy, best_angle,
            best_bbox[0], best_bbox[1], best_bbox[2], best_bbox[3])


def tile_cluster(
    cluster_w: float,
    cluster_h: float,
    stock: StockSheet,
    clearance_mm: float
) -> list[tuple[float, float]]:
    """Generates tiling grid positions for the cluster bounding box."""
    stride_x = cluster_w + clearance_mm
    stride_y = cluster_h + clearance_mm

    W = stock.width_mm
    H = stock.height_mm

    # 1. Axis-aligned grid
    aligned_positions = []
    y = clearance_mm
    while y + cluster_h <= H - clearance_mm + 1e-9:
        x = clearance_mm
        while x + cluster_w <= W - clearance_mm + 1e-9:
            aligned_positions.append((x, y))
            x += stride_x
        y += stride_y

    # 2. Brick-wall grid
    brick_positions = []
    y = clearance_mm
    row = 0
    while y + cluster_h <= H - clearance_mm + 1e-9:
        x = clearance_mm
        if row % 2 == 1:
            x += stride_x / 2.0

        while x + cluster_w <= W - clearance_mm + 1e-9:
            brick_positions.append((x, y))
            x += stride_x
        y += stride_y
        row += 1

    return aligned_positions if len(aligned_positions) >= len(brick_positions) else brick_positions


def run_lattice_job(job: NestingJob, stop_check: Callable[[], bool] | None = None) -> "NestingResult":  # type: ignore[name-defined]
    from squeezenest.api.models import NestingResult, ValidationReport, PlacementLayout, PlacedPart

    if not job.parts:
        return NestingResult(
            layout=PlacementLayout((), (), 0, 0.0, 0),
            report=ValidationReport.from_violations([]),
            job=job
        )

    primary_part_id = max(job.parts.keys(), key=lambda k: job.parts[k][1].quantity)
    geom, meta = job.parts[primary_part_id]
    outer, holes = geom
    total_qty = meta.quantity

    stock = job.stock[0]
    rotations = get_rotations(job.rotation_set)
    clearance_mm = job.clearance_mm

    # 1. Cluster
    best_b_poly, b_dx, b_dy, b_angle, c_min_x, c_min_y, c_max_x, c_max_y = find_best_cluster(
        outer, rotations, clearance_mm, stop_check
    )

    c_w = c_max_x - c_min_x
    c_h = c_max_y - c_min_y

    # 2. Tile - Try all allowed global rotations of the cluster to maximize yield
    best_placements: list[PlacedPart] = []

    # We will try rotating the found cluster globally
    a_min_x, a_min_y, _, _ = part_bounding_box(outer)
    poly_a = translate_polygon(outer, -a_min_x, -a_min_y)

    for global_angle in rotations:
        # Rotate the cluster polygons globally around origin
        rot_a = rotate_polygon(poly_a, global_angle)
        rot_b = rotate_polygon(best_b_poly, global_angle)

        c_min_x, c_min_y, c_max_x, c_max_y = _combined_bounding_box(rot_a, rot_b)
        c_w = c_max_x - c_min_x
        c_h = c_max_y - c_min_y

        tile_positions = tile_cluster(c_w, c_h, stock, clearance_mm)

        current_placements: list[PlacedPart] = []
        placed_count = 0

        # Calculate bounding box mins for the rotated A and B
        ra_min_x, ra_min_y, _, _ = part_bounding_box(rot_a)
        rb_min_x, rb_min_y, _, _ = part_bounding_box(rot_b)

        # Calculate effective rotations
        eff_a_angle = global_angle
        eff_b_angle = (b_angle + global_angle) % 360.0

        for tx, ty in tile_positions:
            if placed_count >= total_qty or (stop_check and stop_check()):
                break

            shift_x = tx - c_min_x
            shift_y = ty - c_min_y

            final_a_outline = translate_polygon(rot_a, shift_x, shift_y)
            current_placements.append(PlacedPart(
                part_id=primary_part_id,
                instance_idx=placed_count,
                sheet_id=stock.sheet_id,
                position_mm=(ra_min_x + shift_x, ra_min_y + shift_y),
                rotation_deg=eff_a_angle,
                placed_outline=final_a_outline
            ))
            placed_count += 1

            if placed_count >= total_qty or (stop_check and stop_check()):
                break

            final_b_outline = translate_polygon(rot_b, shift_x, shift_y)
            current_placements.append(PlacedPart(
                part_id=primary_part_id,
                instance_idx=placed_count,
                sheet_id=stock.sheet_id,
                position_mm=(rb_min_x + shift_x, rb_min_y + shift_y),
                rotation_deg=eff_b_angle,
                placed_outline=final_b_outline
            ))
            placed_count += 1

        if len(current_placements) > len(best_placements):
            best_placements = current_placements

        if stop_check and stop_check():
            break

    placements = best_placements
    placed_count = len(placements)
    unplaced: list[tuple[str, int]] = []

    # 3. Fallback for remainder
    remainder = total_qty - placed_count
    leftover_parts: list[tuple[str, PolygonWithHoles]] = []

    if remainder > 0:
        for _ in range(remainder):
            leftover_parts.append((primary_part_id, job.parts[primary_part_id][0]))

    for p_id, (p_geom, p_meta) in job.parts.items():
        if p_id != primary_part_id:
            for _ in range(p_meta.quantity):
                leftover_parts.append((p_id, p_geom))

    if leftover_parts:
        stock_poly = SPolygon([
            (0, 0), (stock.width_mm, 0),
            (stock.width_mm, stock.height_mm), (0, stock.height_mm)
        ])
        placed_shapely = [SPolygon(p.placed_outline).buffer(clearance_mm, quad_segs=2) for p in placements]

        # Track per-part-id instance counters for leftover placement (instance_idx correctness)
        leftover_counters: dict[str, int] = {p.part_id: 0 for p in placements}
        for p in placements:
            leftover_counters[p.part_id] = max(leftover_counters[p.part_id], p.instance_idx + 1)

        for p_id, (p_outer, _) in leftover_parts:
            inst_idx = leftover_counters.get(p_id, 0)
            placed = False

            for angle in rotations:
                rotated = rotate_polygon(p_outer, angle)
                min_x, min_y, max_x, max_y = part_bounding_box(rotated)
                part_w = max_x - min_x
                part_h = max_y - min_y

                if part_w + 2 * clearance_mm > stock.width_mm or part_h + 2 * clearance_mm > stock.height_mm:
                    continue

                normalised = translate_polygon(rotated, -min_x, -min_y)

                y_pos = clearance_mm
                found = False
                while y_pos + part_h <= stock.height_mm - clearance_mm + 1e-9 and not found:
                    if stop_check and stop_check():
                        break
                    x_pos = clearance_mm
                    while x_pos + part_w <= stock.width_mm - clearance_mm + 1e-9 and not found:
                        candidate = translate_polygon(normalised, x_pos, y_pos)
                        candidate_shapely = SPolygon(candidate)

                        if not stock_poly.contains(candidate_shapely):
                            x_pos += GRID_STEP_MM
                            continue

                        inflated = candidate_shapely.buffer(clearance_mm, quad_segs=2)
                        collision = any(
                            inflated.intersects(prev) and inflated.intersection(prev).area > 1e-9
                            for prev in placed_shapely
                        )
                        if not collision:
                            placed_shapely.append(inflated)
                            placements.append(PlacedPart(
                                part_id=p_id,
                                instance_idx=inst_idx,
                                sheet_id=stock.sheet_id,
                                position_mm=(x_pos, y_pos),
                                rotation_deg=angle,
                                placed_outline=list(candidate)
                            ))
                            found = True
                            placed = True
                        else:
                            x_pos += GRID_STEP_MM
                    y_pos += GRID_STEP_MM
                if found:
                    leftover_counters[p_id] = inst_idx + 1
                    break
            if not placed:
                unplaced.append((p_id, inst_idx))
                leftover_counters[p_id] = inst_idx + 1

    placed_area = sum(SPolygon(p.placed_outline).area for p in placements)
    utilisation = (
        placed_area / (stock.width_mm * stock.height_mm)
        if (stock.width_mm * stock.height_mm) > 0 else 0.0
    )

    return NestingResult(
        layout=PlacementLayout(
            placements=tuple(placements),
            unplaced_parts=tuple(unplaced),
            yield_count=len(placements),
            utilisation=utilisation,
            sheets_used=1
        ),
        report=ValidationReport.from_violations([]),
        job=job
    )
