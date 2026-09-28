"""Public API models for SqueezeNest.

All public-facing coordinates are in millimetres (float).
Internal int64 grid conversion is handled transparently by _core.scale.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence


# ---------------------------------------------------------------------------
# Coordinate primitives
# ---------------------------------------------------------------------------

Point2D = tuple[float, float]                          # (x_mm, y_mm)
Polygon = list[Point2D]                                # Closed ring; last vertex != first
PolygonWithHoles = tuple[Polygon, list[Polygon]]       # (outer_ccw, [hole_cw, ...])


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class NestingStrategy(Enum):
    """Algorithm used by NestingJob.run() to place parts."""
    BLF     = "blf"       # Greedy Bottom-Left Fill (default, backwards-compatible)
    LATTICE = "lattice"   # Pair Clustering & Lattice Tiling (high-yield panelization)
    GA      = "ga"        # Genetic Algorithm optimization


class ViolationSeverity(Enum):
    ERROR   = "ERROR"
    WARNING = "WARNING"
    INFO    = "INFO"


class ViolationCode(Enum):
    DXF_GAP_001                = "DXF_GAP_001"
    DXF_DUPLICATE_SEGMENT_002  = "DXF_DUPLICATE_SEGMENT_002"
    DXF_LAYER_UNKNOWN_003      = "DXF_LAYER_UNKNOWN_003"
    DXF_SELF_INTERSECT_004     = "DXF_SELF_INTERSECT_004"
    DFM_NECK_PINCH_010         = "DFM_NECK_PINCH_010"
    DFM_HOLE_COLLAPSE_011      = "DFM_HOLE_COLLAPSE_011"
    DFM_CLEARANCE_BREACH_012   = "DFM_CLEARANCE_BREACH_012"
    DFM_TOPOLOGY_SPLIT_013     = "DFM_TOPOLOGY_SPLIT_013"
    NFP_CACHE_COLLISION_020    = "NFP_CACHE_COLLISION_020"
    SWEEP_POINT_INVALID_030    = "SWEEP_POINT_INVALID_030"


class RotationSet(Enum):
    """Predefined discrete rotation sets."""
    ORTHO   = "ortho"    # {0, 90, 180, 270} degrees -- default
    HALF    = "half"     # {0, 180} degrees
    FREE_45 = "free_45"  # {0, 45, 90, 135, 180, 225, 270, 315} degrees
    NONE    = "none"     # {0} -- no rotation permitted


# ---------------------------------------------------------------------------
# DFM Diagnostic types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Violation:
    """
    A single DFM or ingestion diagnostic event.

    Attributes:
        code:            Machine-readable error code (ViolationCode enum).
        severity:        ERROR / WARNING / INFO.
        message:         Human-readable description with context.
        source_ref:      DXF entity handle or part_id that triggered the violation.
        locus:           Optional bounding box (min_x, min_y, max_x, max_y) in mm.
        suggested_delta: Optional suggested remediation value in mm (e.g. +0.15 for a gap).
    """
    code: ViolationCode
    severity: ViolationSeverity
    message: str
    source_ref: Optional[str] = None
    locus: Optional[tuple[float, float, float, float]] = None
    suggested_delta: Optional[float] = None

    def as_json_record(self) -> dict:  # type: ignore[type-arg]
        """Serialise to a JSON Lines-compatible dict."""
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "source_ref": self.source_ref,
            "locus": self.locus,
            "suggested_delta": self.suggested_delta,
        }


@dataclass(frozen=True)
class ValidationReport:
    """
    The complete DFM validation result for a NestingJob.

    Attributes:
        violations:    All violations raised during ingestion and pre-flight.
        error_count:   Count of ERROR-severity violations.
        warning_count: Count of WARNING-severity violations.
        is_fatal:      True if any ERROR violations are present and strict=True.
    """
    violations: tuple[Violation, ...]
    error_count: int
    warning_count: int
    is_fatal: bool

    @classmethod
    def from_violations(
        cls, violations: Sequence[Violation], strict: bool = True
    ) -> "ValidationReport":
        """Construct a report from a sequence of Violation objects."""
        counts = Counter(v.severity for v in violations)
        errors   = counts[ViolationSeverity.ERROR]
        warnings = counts[ViolationSeverity.WARNING]
        return cls(
            violations=tuple(violations),
            error_count=errors,
            warning_count=warnings,
            is_fatal=strict and errors > 0,
        )

    def errors(self) -> list[Violation]:
        """Return all ERROR-severity violations."""
        return [v for v in self.violations if v.severity == ViolationSeverity.ERROR]

    def warnings(self) -> list[Violation]:
        """Return all WARNING-severity violations."""
        return [v for v in self.violations if v.severity == ViolationSeverity.WARNING]


# ---------------------------------------------------------------------------
# Part & Stock types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PartMetadata:
    """
    Non-geometric metadata attached to each part.

    Attributes:
        part_id:         Unique identifier (e.g. 'BRACKET_LEFT_01').
        quantity:        How many copies of this part are required.
        grain_direction: Optional preferred axis of orientation in degrees (0 = horizontal).
        source_file:     Source DXF path (for traceability).
        source_layer:    DXF layer the boundary was read from.
        user_data:       Arbitrary key-value pairs for downstream consumers.
    """
    part_id: str
    quantity: int = 1
    grain_direction: Optional[float] = None
    source_file: Optional[Path] = None
    source_layer: Optional[str] = None
    user_data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StockSheet:
    """
    A rectangular stock sheet to nest parts onto.

    Attributes:
        width_mm:  Sheet width in millimetres.
        height_mm: Sheet height in millimetres.
        sheet_id:  Optional identifier (e.g. 'SHEET_01').
        material:  Optional material label (informational only).
    """
    width_mm: float
    height_mm: float
    sheet_id: str = "SHEET_01"
    material: Optional[str] = None


# ---------------------------------------------------------------------------
# Nesting Job & Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlacedPart:
    """
    A single part instance placed on a sheet.

    Attributes:
        part_id:       References PartMetadata.part_id.
        instance_idx:  0-based index for multi-quantity parts (e.g. 0, 1, 2).
        sheet_id:      Sheet the part is placed on.
        position_mm:   (x, y) translation applied to the part origin, in mm.
        rotation_deg:  Rotation applied (degrees, counterclockwise).
        placed_outline: Transformed outer boundary in mm (for verification / export).
    """
    part_id: str
    instance_idx: int
    sheet_id: str
    position_mm: Point2D
    rotation_deg: float
    placed_outline: Polygon


@dataclass(frozen=True)
class PlacementLayout:
    """
    The complete placement result for one nesting run.

    Attributes:
        placements:     All successfully placed part instances.
        unplaced_parts: (part_id, instance_idx) pairs that did not fit.
        yield_count:    Total placed part instances (Invariant I-05: == len(placements)).
        utilisation:    Sheet area utilisation ratio (0.0 to 1.0).
        sheets_used:    Number of sheets consumed.
    """
    placements: tuple[PlacedPart, ...]
    unplaced_parts: tuple[tuple[str, int], ...]
    yield_count: int
    utilisation: float
    sheets_used: int


@dataclass
class NestingJob:
    """
    The primary entry point for a nesting run.

    Attributes:
        parts:       Mapping of part_id -> (geometry, metadata).
        stock:       One or more stock sheets available.
        clearance_mm: Minimum inter-part clearance (router bit radius or laser kerf).
        kerf_mm:     Outward kerf compensation applied to each part boundary.
        rotation_set: Permitted discrete rotation angles.
        beam_width:  Beam Search width (1 = greedy BLF, default 5).
        strict:      If True, any ERROR violation aborts the job.
        cache_dir:   Optional override for Tier-2 SQLite cache location.

    Usage:
        job = NestingJob(parts=..., stock=[sheet], clearance_mm=0.5)
        result = job.run()
    """
    parts: dict[str, tuple[PolygonWithHoles, PartMetadata]]
    stock: list[StockSheet]
    clearance_mm: float = 0.5
    kerf_mm: float = 0.0
    rotation_set: RotationSet = RotationSet.ORTHO
    beam_width: int = 5
    strict: bool = True
    cache_dir: Optional[Path] = None
    strategy: NestingStrategy = NestingStrategy.BLF

    def run(self) -> "NestingResult":
        """Execute the nesting job. Raises NestingError if strict=True and fatal violations exist."""
        if self.strategy == NestingStrategy.LATTICE:
            from squeezenest._nesting.lattice import run_lattice_job  # noqa: PLC0415
            return run_lattice_job(self)
        if self.strategy == NestingStrategy.GA:
            from squeezenest._nesting.ga import run_ga_job # noqa: PLC0415
            return run_ga_job(self)
        from squeezenest._nesting.blf import run_nesting_job  # noqa: PLC0415
        return run_nesting_job(self)


@dataclass(frozen=True)
class NestingResult:
    """
    The complete output of a NestingJob.run() call.

    Attributes:
        layout: The final placement layout.
        report: DFM validation report (all violations, severities).
        job:    Reference to the originating NestingJob (for audit / replay).
    """
    layout: PlacementLayout
    report: ValidationReport
    job: NestingJob


# ---------------------------------------------------------------------------
# Sensitivity Sweep types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SensitivityConfig:
    """
    Configuration for the squeeze-to-fit parametric sensitivity sweep.

    Attributes:
        scale_x_range:   (min_sx, max_sx) -- fractional scale factors for part width.
                         max_sx should be <= 1.0 (squeeze only, no enlargement).
        scale_y_range:   (min_sy, max_sy) -- fractional scale factors for part height.
        clearance_range: (min_d_mm, max_d_mm) -- clearance range to sweep in mm.
        n_scale_x:       Number of discrete steps along the sx axis.
        n_scale_y:       Number of discrete steps along the sy axis.
        n_clearance:     Number of discrete steps along the clearance axis.
        cost_weights:    (w_sx, w_sy, w_clearance) for Pareto cost function.
        target_yield:    Optional early-termination yield target.
        max_workers:     ProcessPool worker count (default: -1 = cpu_count - 1).
    """
    scale_x_range: tuple[float, float] = (0.95, 1.0)
    scale_y_range: tuple[float, float] = (0.95, 1.0)
    clearance_range: tuple[float, float] = (0.2, 0.5)
    n_scale_x: int = 6
    n_scale_y: int = 6
    n_clearance: int = 6
    cost_weights: tuple[float, float, float] = (1.0, 1.0, 1.0)
    target_yield: Optional[int] = None
    max_workers: int = -1  # -1 -> cpu_count - 1


@dataclass(frozen=True)
class ParetoPoint:
    """
    A single Pareto-non-dominated point on the sensitivity frontier.

    Attributes:
        scale_x:          Horizontal scale factor applied to all parts.
        scale_y:          Vertical scale factor applied to all parts.
        clearance_mm:     Inter-part clearance at this point.
        yield_count:      Parts placed at this parameter combination.
        deformation_cost: Scalar cost (lower = less deformation from baseline).
        layout:           The PlacementLayout at this point.
        bottleneck_hint:  Human-readable annotation of the limiting constraint.
    """
    scale_x: float
    scale_y: float
    clearance_mm: float
    yield_count: int
    deformation_cost: float
    layout: PlacementLayout
    bottleneck_hint: str = ""


@dataclass(frozen=True)
class SensitivityResult:
    """
    The complete output of a sensitivity sweep.

    Attributes:
        pareto_frontier: All Pareto-non-dominated (yield, cost) points.
        all_points:      Every evaluated grid point (for full export / plotting).
        config:          The SensitivityConfig used to produce this result.
        baseline_yield:  Yield at (sx=1.0, sy=1.0, d=d_baseline) -- the unmodified job.
    """
    pareto_frontier: tuple[ParetoPoint, ...]
    all_points: tuple[ParetoPoint, ...]
    config: SensitivityConfig
    baseline_yield: int

    def best_yield(self) -> ParetoPoint:
        """Return the Pareto point with the highest yield."""
        return max(self.pareto_frontier, key=lambda p: p.yield_count)

    def least_deformation(self) -> ParetoPoint:
        """Return the Pareto point with the lowest deformation cost."""
        return min(self.pareto_frontier, key=lambda p: p.deformation_cost)


__all__ = [
    "Point2D", "Polygon", "PolygonWithHoles",
    "ViolationSeverity", "ViolationCode", "RotationSet", "NestingStrategy",
    "Violation", "ValidationReport",
    "PartMetadata", "StockSheet",
    "PlacedPart", "PlacementLayout",
    "NestingJob", "NestingResult",
    "SensitivityConfig", "ParetoPoint", "SensitivityResult",
]
