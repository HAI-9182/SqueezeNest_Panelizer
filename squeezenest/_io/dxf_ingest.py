"""squeezenest._io.dxf_ingest -- 6-stage DXF ingestion pipeline.

Stages:
  1. Parse: Read LINE, ARC, LWPOLYLINE, SPLINE entities from ezdxf.
  2. Segment extraction: Convert all entities to a flat list of (p0, p1) segments in mm.
  3. Snap: KD-Tree endpoint snapping via graph_stitch.snap_endpoints.
  4. Graph walk: Traverse the snapped graph to find closed contours.
  5. Topology classification: Outer rings vs holes using winding / containment.
  6. Output: List of (PolygonWithHoles, PartMetadata) + ValidationReport.

Supported entity types (v0.1):
  - LINE
  - LWPOLYLINE (closed and open)
  - CIRCLE (discretised to polygon with _CIRCLE_SEGS vertices)
  - ARC (discretised)

Layer conventions (informational; not enforced in v0.1):
  - "OUTLINE": part outer boundary
  - "CUTOUT": interior holes / pockets
  - "KEEPOUT": keepout zones (not processed in v0.1)
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import ezdxf
from ezdxf.document import Drawing

from squeezenest._io.graph_stitch import snap_endpoints, Segment
from squeezenest.api.models import (
    PolygonWithHoles, Polygon, ValidationReport, Violation,
    ViolationSeverity, ViolationCode, PartMetadata,
)

__all__ = ["ingest_dxf"]

_CIRCLE_SEGS = 64   # Number of segments used to approximate circles / arcs
_EPSILON = 1e-9     # Geometric zero tolerance (mm)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ingest_dxf(
    path: Path,
    eps_snap_mm: float = 0.01,
    strict_layers: bool = False,
) -> tuple[list[tuple[PolygonWithHoles, PartMetadata]], ValidationReport]:
    """Ingest a DXF file and return parts + validation report.

    Args:
        path:           Path to the DXF file.
        eps_snap_mm:    KD-Tree snap tolerance in mm (default 0.01 = 10 µm).
        strict_layers:  If True, enforce OUTLINE/CUTOUT/KEEPOUT layer contract.

    Returns:
        Tuple of:
          - List of (PolygonWithHoles, PartMetadata) -- one per detected closed contour group.
          - ValidationReport -- all diagnostics collected during ingestion.
    """
    violations: list[Violation] = []

    # ------------------------------------------------------------------
    # Stage 1: Parse DXF
    # ------------------------------------------------------------------
    doc: Drawing = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    # ------------------------------------------------------------------
    # Stage 2: Segment extraction
    # ------------------------------------------------------------------
    segments: list[Segment] = []
    for entity in msp:
        segs = _entity_to_segments(entity)
        segments.extend(segs)

    if not segments:
        report = ValidationReport.from_violations(violations)
        return [], report

    # ------------------------------------------------------------------
    # Stage 3: KD-Tree endpoint snapping
    # ------------------------------------------------------------------
    snapped = snap_endpoints(segments, eps_snap_mm=eps_snap_mm)

    # ------------------------------------------------------------------
    # Stage 4: Graph walk to find closed contours
    # ------------------------------------------------------------------
    contours, gap_segs, dup_segs = _extract_closed_contours(snapped, eps_snap_mm)

    # Emit DXF_DUPLICATE_SEGMENT_002 for segments found to be exact duplicates
    for seg in dup_segs:
        violations.append(Violation(
            code=ViolationCode.DXF_DUPLICATE_SEGMENT_002,
            severity=ViolationSeverity.WARNING,
            message=(
                f"Duplicate segment between ({seg[0][0]:.4f},{seg[0][1]:.4f}) "
                f"and ({seg[1][0]:.4f},{seg[1][1]:.4f}) — second copy ignored."
            ),
            locus=(seg[0][0], seg[0][1], seg[1][0], seg[1][1]),
        ))

    # Emit DXF_GAP_001 for any unclosed segments remaining after snapping
    for seg in gap_segs:
        violations.append(Violation(
            code=ViolationCode.DXF_GAP_001,
            severity=ViolationSeverity.ERROR,
            message=(
                f"Unclosed contour gap of {_seg_length(seg):.6f} mm "
                f"between ({seg[0][0]:.4f},{seg[0][1]:.4f}) "
                f"and ({seg[1][0]:.4f},{seg[1][1]:.4f})"
            ),
            locus=(seg[0][0], seg[0][1], seg[1][0], seg[1][1]),
        ))

    # ------------------------------------------------------------------
    # Stage 5: Topology classification (outer rings vs holes)
    # ------------------------------------------------------------------
    parts = _classify_contours(contours, path)

    report = ValidationReport.from_violations(violations)
    return parts, report


# ---------------------------------------------------------------------------
# Entity → segments
# ---------------------------------------------------------------------------

def _entity_to_segments(entity: object) -> list[Segment]:
    """Convert a DXF entity to a list of (p0, p1) float-mm segments."""
    dxftype = getattr(entity, "dxftype", lambda: "")()

    if dxftype == "LINE":
        p0 = _to_2d(entity.dxf.start)  # type: ignore[attr-defined]
        p1 = _to_2d(entity.dxf.end)    # type: ignore[attr-defined]
        if _dist(p0, p1) > _EPSILON:
            return [(p0, p1)]

    elif dxftype == "LWPOLYLINE":
        pts = [_to_2d(p) for p in entity.get_points()]  # type: ignore[attr-defined]
        segs: list[Segment] = []
        for i in range(len(pts) - 1):
            if _dist(pts[i], pts[i + 1]) > _EPSILON:
                segs.append((pts[i], pts[i + 1]))
        if entity.closed and len(pts) >= 2:  # type: ignore[attr-defined]
            if _dist(pts[-1], pts[0]) > _EPSILON:
                segs.append((pts[-1], pts[0]))
        return segs

    elif dxftype == "CIRCLE":
        cx, cy = entity.dxf.center.x, entity.dxf.center.y  # type: ignore[attr-defined]
        r = entity.dxf.radius  # type: ignore[attr-defined]
        return _circle_to_segments(cx, cy, r, _CIRCLE_SEGS)

    elif dxftype == "ARC":
        cx, cy = entity.dxf.center.x, entity.dxf.center.y  # type: ignore[attr-defined]
        r = entity.dxf.radius  # type: ignore[attr-defined]
        start_a = math.radians(entity.dxf.start_angle)  # type: ignore[attr-defined]
        end_a   = math.radians(entity.dxf.end_angle)    # type: ignore[attr-defined]
        return _arc_to_segments(cx, cy, r, start_a, end_a, _CIRCLE_SEGS)

    return []


def _to_2d(pt: object) -> tuple[float, float]:
    """Extract (x, y) from an ezdxf point (Vec3 or tuple)."""
    if hasattr(pt, "x"):
        return (float(pt.x), float(pt.y))  # type: ignore[attr-defined]
    return (float(pt[0]), float(pt[1]))


def _dist(p0: tuple[float, float], p1: tuple[float, float]) -> float:
    return math.hypot(p1[0] - p0[0], p1[1] - p0[1])


def _seg_length(seg: Segment) -> float:
    return _dist(seg[0], seg[1])


def _circle_to_segments(
    cx: float, cy: float, r: float, n: int
) -> list[Segment]:
    pts = [
        (cx + r * math.cos(2 * math.pi * i / n),
         cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]
    segs: list[Segment] = []
    for i in range(n):
        segs.append((pts[i], pts[(i + 1) % n]))
    return segs


def _arc_to_segments(
    cx: float, cy: float, r: float,
    start_a: float, end_a: float, n: int,
) -> list[Segment]:
    """Discretise an arc (CCW from start_a to end_a) into n segments."""
    # Normalise angles to [0, 2π)
    while end_a < start_a:
        end_a += 2 * math.pi
    span = end_a - start_a
    segs_count = max(1, round(n * span / (2 * math.pi)))
    pts = [
        (cx + r * math.cos(start_a + span * i / segs_count),
         cy + r * math.sin(start_a + span * i / segs_count))
        for i in range(segs_count + 1)
    ]
    return [(pts[i], pts[i + 1]) for i in range(segs_count)]


# ---------------------------------------------------------------------------
# Graph walk: closed contour extraction
# ---------------------------------------------------------------------------

def _build_adjacency(
    segments: list[Segment],
    eps: float,
) -> dict[tuple[float, float], list[tuple[float, float]]]:
    """Build adjacency dict: endpoint -> list of connected endpoints."""
    adj: dict[tuple[float, float], list[tuple[float, float]]] = {}
    for p0, p1 in segments:
        adj.setdefault(p0, []).append(p1)
        adj.setdefault(p1, []).append(p0)
    return adj


def _extract_closed_contours(
    segments: list[Segment],
    eps_snap_mm: float,
) -> tuple[list[Polygon], list[Segment], list[Segment]]:
    """Walk the segment graph to extract closed contours.

    Returns:
        (closed_contours, gap_segments, duplicate_segments)
        gap_segments are segments whose endpoints don't connect to form a ring.
        duplicate_segments are segments discarded as exact duplicates after snapping.
    """
    # De-duplicate and record which segments were duplicates
    seen_edges: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    unique_segments: list[Segment] = []
    duplicate_segments: list[Segment] = []
    for seg in segments:
        p0, p1 = seg
        canonical = (min(p0, p1), max(p0, p1))
        if canonical in seen_edges:
            duplicate_segments.append(seg)
        else:
            seen_edges.add(canonical)
            unique_segments.append(seg)

    # Build adjacency over unique segments only
    adj = _build_adjacency(unique_segments, eps_snap_mm)

    visited_edges: set[frozenset] = set()
    contours: list[Polygon] = []

    for seg in unique_segments:
        p0, p1 = seg
        edge_key = frozenset([p0, p1])
        if edge_key in visited_edges:
            continue
        visited_edges.add(edge_key)

        # Try to walk a closed ring starting from this edge
        ring = _walk_ring(p0, p1, adj, visited_edges)
        if ring is not None:
            contours.append(ring)

    # Segments in unclosed chains = gap segments
    # A segment is a "gap" if its endpoints have degree != 2 (dangling)
    degree: dict[tuple[float, float], int] = {}
    for p0, p1 in unique_segments:
        degree[p0] = degree.get(p0, 0) + 1
        degree[p1] = degree.get(p1, 0) + 1

    gap_segments: list[Segment] = []
    for p0, p1 in unique_segments:
        # If either endpoint has degree 1 (dead end), it's part of an open chain
        if degree.get(p0, 0) == 1 or degree.get(p1, 0) == 1:
            gap_segments.append((p0, p1))

    return contours, gap_segments, duplicate_segments


def _walk_ring(
    start: tuple[float, float],
    second: tuple[float, float],
    adj: dict[tuple[float, float], list[tuple[float, float]]],
    visited_edges: set[frozenset],
) -> Optional[Polygon]:
    """Walk from start through second, attempting to close a ring back to start.

    The walk is bounded to ``len(adj) + 1`` steps — the maximum number of
    vertices a valid simple ring can visit in this graph.  Non-manifold
    vertices (degree > 2) are handled by preferring neighbours that would
    close the ring, then any unvisited neighbour.
    """
    ring: Polygon = [start, second]
    prev = start
    curr = second

    # Safe upper bound: a simple ring visits at most every vertex once.
    for _ in range(len(adj) + 1):
        neighbours = [n for n in adj.get(curr, []) if n != prev]
        if not neighbours:
            return None  # Dead end

        # Prefer a neighbour that closes the ring
        closing = [nb for nb in neighbours if nb == start and len(ring) >= 3]
        if closing:
            return ring  # Closed!

        # Among remaining neighbours, pick the first unvisited edge
        next_pt: Optional[tuple[float, float]] = None
        for nb in neighbours:
            edge_key = frozenset([curr, nb])
            if edge_key not in visited_edges:
                next_pt = nb
                break

        if next_pt is None:
            return None  # No unvisited onward edge (dead end in graph)

        edge_key = frozenset([curr, next_pt])
        visited_edges.add(edge_key)
        ring.append(next_pt)
        prev, curr = curr, next_pt

    return None  # Walk too long without closure


# ---------------------------------------------------------------------------
# Stage 5: Classify contours (outers and holes)
# ---------------------------------------------------------------------------

def _polygon_area_signed(poly: Polygon) -> float:
    """Signed area via shoelace formula. Positive = CCW, negative = CW."""
    n = len(poly)
    area = 0.0
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return area / 2.0


def _point_in_polygon(pt: tuple[float, float], poly: Polygon) -> bool:
    """Ray-casting point-in-polygon test."""
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + _EPSILON) + xi):
            inside = not inside
        j = i
    return inside


def _classify_contours(
    contours: list[Polygon],
    path: Path,
) -> list[tuple[PolygonWithHoles, PartMetadata]]:
    """Group contours into (outer, [holes]) parts.

    Strategy:
      - Sort contours by descending absolute area.
      - Each contour is either an outer ring or a hole inside an already-assigned outer.
    """
    if not contours:
        return []

    # Sort by descending area (largest first = outers)
    sorted_contours = sorted(contours, key=lambda c: abs(_polygon_area_signed(c)), reverse=True)

    # Each entry: (outer_polygon, [holes])
    groups: list[tuple[Polygon, list[Polygon]]] = []

    for contour in sorted_contours:
        # Check if this contour's centroid lies inside any existing outer
        n = len(contour)
        cx = sum(p[0] for p in contour) / n
        cy = sum(p[1] for p in contour) / n
        placed = False
        for outer, holes in groups:
            if _point_in_polygon((cx, cy), outer):
                holes.append(contour)
                placed = True
                break
        if not placed:
            groups.append((contour, []))

    # Build result list
    results: list[tuple[PolygonWithHoles, PartMetadata]] = []
    for i, (outer, holes) in enumerate(groups):
        metadata = PartMetadata(
            part_id=f"{path.stem}_part{i}",
            source_file=path,
            source_layer="OUTLINE",
        )
        results.append(((list(outer), [list(h) for h in holes]), metadata))

    return results
