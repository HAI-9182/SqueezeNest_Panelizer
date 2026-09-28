"""Genetic Algorithm (GA) Nesting strategy for global placement optimization."""
from __future__ import annotations

import dataclasses
import random
from typing import TYPE_CHECKING

from squeezenest.api.models import NestingStrategy
from squeezenest._nesting.blf import run_nesting_job

if TYPE_CHECKING:
    from squeezenest.api.models import NestingJob, NestingResult


def run_ga_job(job: "NestingJob") -> "NestingResult":
    """
    Execute a Genetic Algorithm nesting job.

    This is a v0.2 stub implementation that currently just delegates to
    the Bottom-Left Fill engine with shuffled part orderings as a rudimentary
    stochastic search.
    """
    # In a full implementation, we would maintain a population of part orderings
    # and rotation mappings, apply crossover, and decode via the BLF heuristic.
    # For now, we perform a simple multi-start random search (beam search equivalent)
    # and return the best layout found.

    best_result = None
    best_yield = -1

    # We do a few random shuffles based on beam_width
    original_parts = list(job.parts.items())

    for _ in range(job.beam_width):
        shuffled = original_parts.copy()
        random.shuffle(shuffled)

        # dataclasses.replace produces a shallow copy of the dataclass with the
        # named fields overridden.  NestingJob.stock is not mutated by BLF so
        # sharing the list reference across iterations is safe here.
        proxy_job = dataclasses.replace(
            job,
            parts=dict(shuffled),
            strategy=NestingStrategy.BLF,
        )

        result = run_nesting_job(proxy_job)
        if result.layout.yield_count > best_yield:
            best_yield = result.layout.yield_count
            best_result = result

    if best_result is None:
        # Fallback if beam_width is 0 or parts empty
        best_result = run_nesting_job(job)

    return best_result
