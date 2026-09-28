"""squeezenest._sensitivity.sweep -- Parametric sensitivity sweep engine.

Evaluates the full (sx, sy, clearance) parameter grid via bottom-left-fill
nesting and extracts the Pareto frontier.

Grid construction:
    n_scale_x   points linearly spaced in scale_x_range
    n_scale_y   points linearly spaced in scale_y_range
    n_clearance points linearly spaced in clearance_range
    Total grid size = n_scale_x * n_scale_y * n_clearance

Baseline point: (sx=1.0, sy=1.0, d=max(clearance_range)) -- the unmodified job.

Invariant I-08: SensitivityResult.baseline_yield <= max(p.yield_count for p in pareto_frontier)

Cost function (lower = less deformation from baseline):
    cost = w_sx * (1 - sx) + w_sy * (1 - sy) + w_d * (d_baseline - d)

Workers: Sequential execution only in v0.1 (max_workers parameter is accepted
but ignored; ProcessPool parallel execution is planned for v0.2).
"""
from __future__ import annotations

import itertools
import math
from typing import Iterator

from squeezenest.api.models import (
    NestingJob, SensitivityConfig, SensitivityResult,
    ParetoPoint, PlacementLayout, PartMetadata,
    PolygonWithHoles,
)
from squeezenest._sensitivity.affine import scale_polygon
from squeezenest._sensitivity.pareto import extract_pareto
from squeezenest._nesting.blf import bottom_left_fill
from squeezenest._core.scale import to_int, SCALE

__all__ = ["run_sensitivity_sweep"]


def _linspace(start: float, stop: float, n: int) -> list[float]:
    """n evenly-spaced values from start to stop inclusive."""
    if n == 1:
        return [stop]
    return [start + (stop - start) * i / (n - 1) for i in range(n)]


def _scale_parts(
    job: NestingJob,
    sx: float,
    sy: float,
) -> dict[str, tuple[PolygonWithHoles, PartMetadata]]:
    """Return a new parts dict with all outer rings scaled by (sx, sy)."""
    scaled: dict[str, tuple[PolygonWithHoles, PartMetadata]] = {}
    for part_id, (geom, meta) in job.parts.items():
        outer, holes = geom
        # Convert to int64, scale, convert back to float mm
        outer_int = [(to_int(x), to_int(y)) for x, y in outer]
        scaled_int = scale_polygon(outer_int, sx=sx, sy=sy)
        scaled_outer = [(x / SCALE, y / SCALE) for x, y in scaled_int]
        holes_scaled = []
        for hole in holes:
            hole_int = [(to_int(x), to_int(y)) for x, y in hole]
            hole_scaled_int = scale_polygon(hole_int, sx=sx, sy=sy)
            holes_scaled.append([(x / SCALE, y / SCALE) for x, y in hole_scaled_int])
        scaled[part_id] = ((scaled_outer, holes_scaled), meta)
    return scaled


def _run_single_point(
    job: NestingJob,
    sx: float,
    sy: float,
    clearance_mm: float,
    cost_weights: tuple[float, float, float],
    d_baseline: float,
) -> ParetoPoint:
    """Evaluate one (sx, sy, clearance) point and return a ParetoPoint."""
    scaled_parts = _scale_parts(job, sx, sy)
    parts_list = [(pid, geom_meta[0]) for pid, geom_meta in scaled_parts.items()]

    # Use first stock sheet
    sheet = job.stock[0] if job.stock else None
    if sheet is None:
        layout = PlacementLayout(placements=(), unplaced_parts=(), yield_count=0,
                                 utilisation=0.0, sheets_used=0)
    else:
        layout = bottom_left_fill(
            parts_list, sheet,
            clearance_mm=clearance_mm,
            rotation_set=job.rotation_set,
        )

    w_sx, w_sy, w_d = cost_weights
    cost = w_sx * (1.0 - sx) + w_sy * (1.0 - sy) + w_d * max(0.0, d_baseline - clearance_mm)

    return ParetoPoint(
        scale_x=sx,
        scale_y=sy,
        clearance_mm=clearance_mm,
        yield_count=layout.yield_count,
        deformation_cost=cost,
        layout=layout,
    )


def run_sensitivity_sweep(
    job: NestingJob,
    config: SensitivityConfig,
) -> SensitivityResult:
    """Run the full parametric sensitivity sweep.

    Args:
        job:    The base NestingJob (sx=1.0, sy=1.0, nominal clearance).
        config: Sweep grid configuration.

    Returns:
        SensitivityResult with pareto_frontier, all_points, config, baseline_yield.
    """
    sx_vals = _linspace(*config.scale_x_range, config.n_scale_x)
    sy_vals = _linspace(*config.scale_y_range, config.n_scale_y)
    d_vals  = _linspace(*config.clearance_range, config.n_clearance)
    d_baseline = config.clearance_range[1]  # max clearance = baseline

    all_points: list[ParetoPoint] = []

    for sx, sy, d in itertools.product(sx_vals, sy_vals, d_vals):
        pt = _run_single_point(job, sx, sy, d, config.cost_weights, d_baseline)
        all_points.append(pt)

    # Baseline: sx=1.0, sy=1.0, d=d_baseline
    baseline_pt = next(
        (p for p in all_points
         if math.isclose(p.scale_x, 1.0, rel_tol=1e-6)
         and math.isclose(p.scale_y, 1.0, rel_tol=1e-6)
         and math.isclose(p.clearance_mm, d_baseline, rel_tol=1e-6)),
        all_points[0] if all_points else None,
    )
    baseline_yield = baseline_pt.yield_count if baseline_pt else 0

    frontier = extract_pareto(all_points)

    return SensitivityResult(
        pareto_frontier=tuple(frontier),
        all_points=tuple(all_points),
        config=config,
        baseline_yield=baseline_yield,
    )
