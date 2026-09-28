"""squeezenest.api -- Public API surface (SemVer-protected).

All symbols exported from this module are stable across minor versions.
Do NOT import from squeezenest._* directly in application code.
"""
from squeezenest.api.models import (
    NestingJob,
    NestingResult,
    NestingStrategy,
    PlacementLayout,
    PlacedPart,
    SensitivityConfig,
    SensitivityResult,
    ParetoPoint,
    ValidationReport,
    Violation,
    ViolationSeverity,
    ViolationCode,
    PartMetadata,
    StockSheet,
    RotationSet,
)
from squeezenest.api.protocols import (
    FabricationEntity,
    HoldingTabStrategy,
    PartingCutStrategy,
    IngestAdapter,
)

__all__ = [
    "NestingJob", "NestingResult", "NestingStrategy", "PlacementLayout", "PlacedPart",
    "SensitivityConfig", "SensitivityResult", "ParetoPoint",
    "ValidationReport", "Violation", "ViolationSeverity", "ViolationCode",
    "PartMetadata", "StockSheet", "RotationSet",
    "FabricationEntity", "HoldingTabStrategy", "PartingCutStrategy", "IngestAdapter",
]
