"""squeezenest._sensitivity.pareto -- Pareto frontier extraction.

Extracts the non-dominated frontier from a list of ParetoPoint objects.

Objectives (both to maximise yield, minimise cost):
  - yield_count:      Higher is better (maximise).
  - deformation_cost: Lower is better (minimise).

Dominance definition (strict): point B dominates point A if:
  B.yield_count >= A.yield_count  AND  B.deformation_cost <= A.deformation_cost
  AND at least one inequality is strict.

Invariant I-07: The returned frontier contains no dominated points.
"""
from __future__ import annotations

import logging

from squeezenest.api.models import ParetoPoint

__all__ = ["extract_pareto"]

_log = logging.getLogger(__name__)
_LARGE_GRID_THRESHOLD = 5_000


def extract_pareto(points: list[ParetoPoint]) -> list[ParetoPoint]:
    """Return all Pareto-non-dominated points from the input list.

    Complexity: O(n²) — acceptable for the expected grid sizes (≤ 1000 points).

    Args:
        points: All evaluated grid points from the sensitivity sweep.

    Returns:
        List of non-dominated points (Invariant I-07).  Order is not guaranteed.
    """
    if not points:
        return []

    if len(points) > _LARGE_GRID_THRESHOLD:
        _log.warning(
            "extract_pareto received %d points — O(n²) dominance check may be slow. "
            "Consider reducing n_scale_x / n_scale_y / n_clearance in SensitivityConfig.",
            len(points),
        )

    frontier: list[ParetoPoint] = []

    for candidate in points:
        # Check if candidate is dominated by any other point
        dominated = False
        for other in points:
            if other is candidate:
                continue
            # 'other' dominates 'candidate' if it is at least as good on both
            # objectives and strictly better on at least one
            if (
                other.yield_count >= candidate.yield_count
                and other.deformation_cost <= candidate.deformation_cost
                and (
                    other.yield_count > candidate.yield_count
                    or other.deformation_cost < candidate.deformation_cost
                )
            ):
                dominated = True
                break

        if not dominated:
            frontier.append(candidate)

    return frontier
