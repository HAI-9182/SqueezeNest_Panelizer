#!/usr/bin/env python3
"""SqueezeNest CNC Panelization Demo Server.

Provides a lightweight, zero-external-dependency web server for testing
SqueezeNest with real DXF files, interactive 2D graphical preview (SVG),
and DXF panel export. Includes job cancellation and runtime timeout limits.

Usage:
    python demo/server.py [--port 8080]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import sys
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ezdxf
from shapely.geometry import Polygon as SPolygon

from squeezenest._io.dxf_ingest import ingest_dxf
from squeezenest._nesting.blf import (
    _GRID_STEP_MM,
    _get_rotations,
    _part_bounding_box,
    _rotate_polygon,
    _translate_polygon,
)
from squeezenest.api.models import PlacedPart, RotationSet, StockSheet

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "dxf"
DEMO_DIR = REPO_ROOT / "demo"
CACHE_DIR = DEMO_DIR / ".dxf_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# In-memory store for generated DXF downloads: download_id -> (filename, bytes)
_DXF_CACHE: dict[str, tuple[str, bytes]] = {}

# Active jobs tracking: job_id -> {"stop": bool, "status": str}
_ACTIVE_JOBS: dict[str, dict[str, Any]] = {}

# Security & sanity limits (80/20 risk mitigations)
MAX_REQUEST_BODY_BYTES = 25 * 1024 * 1024   # 25 MB max total HTTP request body
MAX_FILES_PER_REQUEST = 20                  # Max DXF files per optimization request
MAX_DXF_FILE_BYTES = 10 * 1024 * 1024       # 10 MB max per individual DXF file
MAX_DXF_ENTITIES = 50_000                   # Max entities in a single DXF to prevent DoS
MIN_PANEL_DIM_MM = 10.0                     # Min panel dimension (mm)
MAX_PANEL_DIM_MM = 5000.0                   # Max panel dimension (mm)
MAX_CLEARANCE_MM = 100.0                    # Max clearance (mm)
MIN_TIMEOUT_SEC = 1.0                       # Min timeout (sec)
MAX_TIMEOUT_SEC = 120.0                     # Max timeout (sec)
MAX_TARGET_QTY = 500                        # Max target pieces per part


def _sanitize_filename(name: str, fallback: str = "part.dxf") -> str:
    """Sanitize filename to prevent directory traversal and path injection."""
    clean = Path(name).name
    clean = clean.replace("\x00", "").strip()
    clean = "".join(c for c in clean if c.isalnum() or c in "._- ")
    clean = clean.lstrip(".")
    if not clean:
        clean = fallback
    if not clean.lower().endswith(".dxf"):
        clean += ".dxf"
    return clean


def _normalize_dxf_document(doc: ezdxf.document.Drawing) -> dict[str, Any]:
    """Preprocess DXF to ensure compatibility with squeezenest._io.dxf_ingest.
    
    1. Explodes INSERT block references (common in CNC exports).
    2. Converts old-style 2D POLYLINE entities to modern LWPOLYLINE.
    3. Flattens SPLINE curves into discrete LWPOLYLINE segments.
    4. Gathers entity & layer statistics for diagnostics.
    """
    msp = doc.modelspace()
    entity_count = len(msp)
    if entity_count > MAX_DXF_ENTITIES:
        raise ValueError(f"DXF entity count ({entity_count}) exceeds maximum safety limit ({MAX_DXF_ENTITIES}).")

    stats: dict[str, int] = {}
    layers: set[str] = set()

    for entity in msp:
        t = entity.dxftype()
        stats[t] = stats.get(t, 0) + 1
        if hasattr(entity.dxf, "layer"):
            layers.add(entity.dxf.layer)

    # 1. Explode INSERT block references
    for insert in list(msp.query("INSERT")):
        try:
            insert.explode()
        except Exception:
            pass

    # 2. Convert old-style POLYLINE (2D) to LWPOLYLINE
    for poly in list(msp.query("POLYLINE")):
        try:
            pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in poly.vertices]
            if len(pts) >= 2:
                msp.add_lwpolyline(
                    pts,
                    close=getattr(poly, "is_closed", False),
                    dxfattribs={"layer": getattr(poly.dxf, "layer", "0")},
                )
            msp.delete_entity(poly)
        except Exception:
            pass

    # 3. Flatten SPLINE curves to LWPOLYLINE
    for spline in list(msp.query("SPLINE")):
        try:
            pts = [(float(p.x), float(p.y)) for p in spline.flattening(distance=0.02)]
            if len(pts) >= 2:
                msp.add_lwpolyline(
                    pts,
                    close=getattr(spline, "closed", False),
                    dxfattribs={"layer": getattr(spline.dxf, "layer", "0")},
                )
            msp.delete_entity(spline)
        except Exception:
            pass

    return {
        "original_entities": stats,
        "layers": sorted(layers),
    }


def _transform_contour(
    contour: list[tuple[float, float]],
    angle_deg: float,
    cx_ref: float,
    cy_ref: float,
    offset_x: float,
    offset_y: float,
) -> list[tuple[float, float]]:
    """Rotate contour around (cx_ref, cy_ref) by angle_deg, then translate by (offset_x, offset_y)."""
    if angle_deg == 0:
        rotated = list(contour)
    else:
        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        rotated = []
        for x, y in contour:
            dx, dy = x - cx_ref, y - cy_ref
            rotated.append((
                cx_ref + dx * cos_a - dy * sin_a,
                cy_ref + dx * sin_a + dy * cos_a,
            ))
    return [(round(x + offset_x, 4), round(y + offset_y, 4)) for x, y in rotated]


def _build_panel_dxf(
    panel_w: float,
    panel_h: float,
    placements_data: list[dict[str, Any]],
) -> bytes:
    """Generate a clean DXF R2010 file with panel outline, CNC outlines, and cutouts/holes."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    # Create distinct CAD layers with standard colors
    doc.layers.add("PANEL_BORDER", color=7)   # White / Gray
    doc.layers.add("CNC_OUTLINE", color=3)    # Green (stock material edge)
    doc.layers.add("CNC_HOLES", color=2)      # Yellow (drill holes / slots)
    doc.layers.add("TEXT_LABELS", color=4)    # Cyan

    # 1. Panel outer boundary
    panel_pts = [(0.0, 0.0), (panel_w, 0.0), (panel_w, panel_h), (0.0, panel_h)]
    msp.add_lwpolyline(panel_pts, close=True, dxfattribs={"layer": "PANEL_BORDER"})

    # 2. Placed CNC parts
    text_height = max(1.5, min(panel_w, panel_h) * 0.02)
    for p in placements_data:
        # Outer outline
        msp.add_lwpolyline(p["outline"], close=True, dxfattribs={"layer": "CNC_OUTLINE"})
        # Internal holes / cutouts
        for hole in p.get("holes", []):
            msp.add_lwpolyline(hole, close=True, dxfattribs={"layer": "CNC_HOLES"})
        # Text label at part centroid
        if p["outline"]:
            cx = sum(pt[0] for pt in p["outline"]) / len(p["outline"])
            cy = sum(pt[1] for pt in p["outline"]) / len(p["outline"])
            txt = msp.add_text(
                f"{p['part_id']}",
                dxfattribs={"layer": "TEXT_LABELS", "height": text_height},
            )
            txt.dxf.insert = (round(cx, 2), round(cy, 2))

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def _timed_bottom_left_fill(
    parts: list[tuple[str, Any]],
    stock: StockSheet,
    clearance_mm: float,
    rotation_set: RotationSet,
    fill_mode: str,
    timeout_sec: float,
    job_id: str,
) -> tuple[list[PlacedPart], list[tuple[str, int]], str]:
    """Execute greedy Bottom-Left-Fill with cooperative cancellation and runtime timeout."""
    W, H = stock.width_mm, stock.height_mm
    stock_poly = SPolygon([(0, 0), (W, 0), (W, H), (0, H)])

    placed_shapely: list[SPolygon] = []
    placements: list[PlacedPart] = []
    unplaced: list[tuple[str, int]] = []

    rotations = _get_rotations(rotation_set)
    t_start = time.perf_counter()
    status = "completed"

    for idx, (part_id, (outer, _holes)) in enumerate(parts):
        # 1. Check cooperative user cancellation
        if _ACTIVE_JOBS.get(job_id, {}).get("stop", False):
            status = "stopped_by_user"
            break

        # 2. Check upper limit timer
        elapsed = time.perf_counter() - t_start
        if elapsed >= timeout_sec:
            status = "timeout_reached"
            break

        placed = False

        for angle in rotations:
            rotated = _rotate_polygon(outer, angle)
            min_x, min_y, max_x, max_y = _part_bounding_box(rotated)
            part_w = max_x - min_x
            part_h = max_y - min_y

            # Check if rotated part fits in raw stock bounds
            if part_w + 2 * clearance_mm > W or part_h + 2 * clearance_mm > H:
                continue

            normalised = _translate_polygon(rotated, -min_x, -min_y)

            # Scan bottom-left candidate coordinates
            y = clearance_mm
            while y + part_h <= H - clearance_mm + 1e-9 and not placed:
                # Periodic cooperative stop/timeout check during long scans
                if (
                    _ACTIVE_JOBS.get(job_id, {}).get("stop", False)
                    or (time.perf_counter() - t_start >= timeout_sec)
                ):
                    break

                x = clearance_mm
                while x + part_w <= W - clearance_mm + 1e-9 and not placed:
                    candidate = _translate_polygon(normalised, x, y)
                    candidate_shapely = SPolygon(candidate)

                    if not stock_poly.contains(candidate_shapely):
                        x += _GRID_STEP_MM
                        continue

                    inflated = candidate_shapely.buffer(clearance_mm, quad_segs=2)
                    collision = any(
                        inflated.intersects(prev) and inflated.intersection(prev).area > 1e-9
                        for prev in placed_shapely
                    )
                    if not collision:
                        placed_shapely.append(inflated)
                        placements.append(PlacedPart(
                            part_id=part_id,
                            instance_idx=idx,
                            sheet_id=stock.sheet_id,
                            position_mm=(x, y),
                            rotation_deg=angle,
                            placed_outline=list(candidate),
                        ))
                        placed = True
                    else:
                        x += _GRID_STEP_MM
                y += _GRID_STEP_MM

            if placed:
                break

        if not placed:
            unplaced.append((part_id, idx))
            # In Maximize Yield mode, if a part cannot fit, the panel is full!
            # Stop immediately to avoid scanning remaining identical parts.
            if fill_mode == "max_fill":
                break

    return placements, unplaced, status


class PanelizerHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for SqueezeNest CNC Panelization Demo."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(DEMO_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(DEMO_DIR / "index.html", "rb") as f:
                self.wfile.write(f.read())
            return

        if path == "/api/samples":
            self._handle_get_samples()
            return

        if path == "/api/sample-dxf":
            self._handle_get_sample_dxf(query)
            return

        if path == "/api/download-dxf":
            self._handle_download_dxf(query)
            return

        # Fallback to serving static files from demo/
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/optimize":
            self._handle_optimize()
            return

        if parsed.path == "/api/stop":
            self._handle_stop()
            return

        self.send_error(404, "Endpoint not found")

    def _send_json(self, status_code: int, data: dict[str, Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        origin = self.headers.get("Origin", "")
        if origin and ("://localhost" in origin or "://127.0.0.1" in origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(payload)

    def _handle_get_samples(self) -> None:
        """List available DXF test fixtures from tests/fixtures/dxf."""
        samples = []
        if FIXTURES_DIR.exists():
            for p in sorted(FIXTURES_DIR.glob("*.dxf")):
                desc = "Clean square contour" if "clean" in p.name else (
                    "Washer with inner hole cutout" if "washer" in p.name else (
                        "Set of 3 distinct rectangular parts" if "three" in p.name else p.name
                    )
                )
                samples.append({
                    "id": p.name,
                    "filename": p.name,
                    "description": desc,
                    "size_bytes": p.stat().st_size,
                })
        self._send_json(200, {"samples": samples})

    def _handle_get_sample_dxf(self, query: dict[str, list[str]]) -> None:
        name = query.get("name", [""])[0]
        safe_name = _sanitize_filename(name)
        target = (FIXTURES_DIR / safe_name).resolve()
        if not target.is_file() or not str(target).startswith(str(FIXTURES_DIR.resolve())):
            self._send_json(404, {"error": "Sample file not found"})
            return

        content = target.read_bytes()
        self._send_json(200, {
            "filename": safe_name,
            "content_base64": base64.b64encode(content).decode("ascii"),
        })

    def _handle_download_dxf(self, query: dict[str, list[str]]) -> None:
        download_id = query.get("id", [""])[0]
        entry = _DXF_CACHE.get(download_id)
        if not entry:
            self.send_error(404, "Download expired or not found")
            return

        filename, data = entry
        safe_filename = _sanitize_filename(filename, fallback="squeezenest_panel.dxf")
        self.send_response(200)
        self.send_header("Content-Type", "application/dxf")
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_stop(self) -> None:
        """Signal an active optimization job to stop immediately and return best layout so far."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_REQUEST_BODY_BYTES:
                self._send_json(413, {"error": "Payload too large."})
                return
            body = self.rfile.read(length) if length > 0 else b"{}"
            req = json.loads(body.decode("utf-8")) if body else {}
            job_id = req.get("job_id", "")

            if job_id and job_id in _ACTIVE_JOBS:
                _ACTIVE_JOBS[job_id]["stop"] = True
                self._send_json(200, {"success": True, "message": "Stop signal registered"})
            else:
                # If no specific job_id, signal all active jobs
                for j in _ACTIVE_JOBS.values():
                    j["stop"] = True
                self._send_json(200, {"success": True, "message": "Stop signal broadcasted"})
        except Exception as ex:  # noqa: BLE001
            self._send_json(500, {"error": f"Failed to stop: {ex}"})

    def _handle_optimize(self) -> None:
        """Parse uploaded DXFs, run SqueezeNest BLF nesting, and return layout + DXF download."""
        job_id = ""
        try:
            t_total_start = time.perf_counter()
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_REQUEST_BODY_BYTES:
                self._send_json(
                    413,
                    {"error": f"Payload too large ({length} bytes). Maximum allowed is {MAX_REQUEST_BODY_BYTES // (1024 * 1024)} MB."},
                )
                return
            if length <= 0:
                self._send_json(400, {"error": "Empty request body."})
                return

            body = self.rfile.read(length)
            try:
                req = json.loads(body.decode("utf-8"))
            except Exception:
                self._send_json(400, {"error": "Malformed JSON payload."})
                return

            job_id = req.get("job_id") or str(uuid.uuid4())
            _ACTIVE_JOBS[job_id] = {"stop": False, "status": "running"}

            raw_panel_w = float(req.get("panel_width_mm", 150.0))
            raw_panel_h = float(req.get("panel_height_mm", 150.0))
            raw_clearance = float(req.get("clearance_mm", 2.0))
            raw_snap = float(req.get("snap_tolerance_mm", 0.05))
            raw_timeout = float(req.get("timeout_sec", 30.0))
            raw_qty = int(req.get("target_quantity", 20))
            rot_str = str(req.get("rotation_mode", "ortho")).lower()
            strategy_str = str(req.get("strategy", "blf")).lower()
            raw_fill_mode = str(req.get("fill_mode", "max_fill"))
            fill_mode = raw_fill_mode if raw_fill_mode in ("max_fill", "exact") else "max_fill"
            files_data = req.get("files", [])

            if raw_panel_w <= 0 or raw_panel_h <= 0:
                self._send_json(400, {"error": "Panel dimensions must be positive."})
                return

            if raw_clearance < 0:
                self._send_json(400, {"error": "Clearance must be non-negative."})
                return

            if not files_data:
                self._send_json(400, {"error": "At least one DXF file must be provided."})
                return

            if len(files_data) > MAX_FILES_PER_REQUEST:
                self._send_json(
                    400,
                    {"error": f"Too many files uploaded ({len(files_data)}). Max allowed is {MAX_FILES_PER_REQUEST}."},
                )
                return

            # Apply bounded safety limits to prevent algorithmic exhaustion
            panel_w = min(max(raw_panel_w, MIN_PANEL_DIM_MM), MAX_PANEL_DIM_MM)
            panel_h = min(max(raw_panel_h, MIN_PANEL_DIM_MM), MAX_PANEL_DIM_MM)
            clearance_mm = min(max(raw_clearance, 0.0), MAX_CLEARANCE_MM)
            eps_snap_mm = min(max(raw_snap, 0.001), 10.0)
            timeout_sec = max(raw_timeout, MIN_TIMEOUT_SEC) # MAX_TIMEOUT_SEC clamp removed
            target_quantity = min(max(raw_qty, 1), MAX_TARGET_QTY)

            # Determine rotation set
            if rot_str == "none":
                rotation_set = RotationSet.NONE
            elif rot_str == "half":
                rotation_set = RotationSet.HALF
            elif rot_str == "free_45":
                rotation_set = RotationSet.FREE_45
            else:
                rotation_set = RotationSet.ORTHO

            stock = StockSheet(width_mm=panel_w, height_mm=panel_h)

            # Ingest all uploaded DXFs
            ingested_parts_meta = []
            dxf_diagnostics = []

            for file_idx, f_info in enumerate(files_data):
                raw_fname = f_info.get("filename", f"board_{file_idx}.dxf")
                fname = _sanitize_filename(raw_fname, fallback=f"board_{file_idx}.dxf")
                b64_content = f_info.get("content_base64", "")

                try:
                    dxf_bytes = base64.b64decode(b64_content)
                except Exception:
                    self._send_json(400, {"error": f"Invalid base64 payload for file '{fname}'."})
                    return

                if len(dxf_bytes) > MAX_DXF_FILE_BYTES:
                    self._send_json(
                        400,
                        {"error": f"File '{fname}' exceeds {MAX_DXF_FILE_BYTES // (1024 * 1024)} MB limit ({len(dxf_bytes)} bytes)."},
                    )
                    return

                # Save to local cache dir inside demo/ with safe validated path
                tmp_path = (CACHE_DIR / f"{uuid.uuid4().hex}_{fname}").resolve()
                if not str(tmp_path).startswith(str(CACHE_DIR.resolve())):
                    self._send_json(400, {"error": f"Invalid file destination for '{fname}'."})
                    return

                tmp_path.write_bytes(dxf_bytes)

                # Normalize DXF (explode blocks, convert POLYLINEs, flatten SPLINEs)
                stats = {"original_entities": {}, "layers": []}
                try:
                    dxf_doc = ezdxf.readfile(tmp_path)
                    stats = _normalize_dxf_document(dxf_doc)
                    dxf_doc.saveas(tmp_path)
                except Exception as ex:
                    print(f"DXF normalization warning: {ex}")

                # Adaptive Progressive Snapping:
                snap_candidates = [eps_snap_mm]
                for s in [0.02, 0.05, 0.1, 0.25, 0.5, 1.0]:
                    if abs(s - eps_snap_mm) > 1e-4 and s not in snap_candidates:
                        snap_candidates.append(s)

                extracted = []
                report = None
                successful_snap = eps_snap_mm

                for snap_val in snap_candidates:
                    extracted, report = ingest_dxf(tmp_path, eps_snap_mm=snap_val)
                    if extracted:
                        successful_snap = snap_val
                        break

                # Clean up temp file
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

                if not extracted:
                    violation_details = []
                    if report and report.violations:
                        for v in report.violations:
                            violation_details.append({
                                "code": str(v.code),
                                "message": v.message,
                                "locus": v.locus,
                            })

                    dxf_diagnostics.append({
                        "filename": fname,
                        "layers": stats["layers"],
                        "entities": stats["original_entities"],
                        "violations": violation_details,
                    })
                    continue

                qty = int(f_info.get("quantity", target_quantity if fill_mode == "exact" else 0))
                for poly_with_holes, meta in extracted:
                    ingested_parts_meta.append({
                        "poly": poly_with_holes,
                        "part_id": meta.part_id,
                        "filename": fname,
                        "user_qty": qty,
                        "snap_used_mm": successful_snap,
                    })

            if not ingested_parts_meta:
                diag_msg = ["No valid closed CNC polygons could be extracted from the uploaded DXF(s)."]
                for diag in dxf_diagnostics:
                    diag_msg.append(f"\n• File '{diag['filename']}':")
                    diag_msg.append(f"  - Layers: {', '.join(diag['layers']) or 'None'}")
                    diag_msg.append(f"  - Entities: {diag['entities']}")
                    if diag["violations"]:
                        diag_msg.append("  - Gaps detected:")
                        for v in diag["violations"][:3]:
                            diag_msg.append(f"    * {v['message']}")
                    else:
                        diag_msg.append("  - Reason: No closed line or polyline contours found.")

                diag_msg.append(
                    "\nTip: If your DXF has small open gaps, try increasing 'Endpoint Snap Tolerance' "
                    "(e.g., to 0.1 mm or 0.5 mm) in the Spacing settings."
                )

                self._send_json(400, {
                    "error": "\n".join(diag_msg),
                    "diagnostics": dxf_diagnostics,
                })
                return

            # Prepare parts list for nesting
            parts_to_pack: list[tuple[str, Any]] = []
            part_lookup: dict[str, Any] = {}

            if fill_mode == "max_fill":
                panel_area = panel_w * panel_h
                for item in ingested_parts_meta:
                    part_lookup[item['part_id']] = item
                    outer, holes = item["poly"]
                    part_poly = SPolygon(outer)
                    part_area = max(1.0, part_poly.area)
                    # Safe upper bound to fill sheet
                    est_count = max(4, int((panel_area / part_area) * 1.3))
                    for i in range(est_count):
                        inst_id = f"{item['part_id']}_{i + 1}"
                        parts_to_pack.append((inst_id, item["poly"]))
                        part_lookup[inst_id] = item
            else:
                for item in ingested_parts_meta:
                    part_lookup[item['part_id']] = item
                    qty = max(1, item["user_qty"] or target_quantity)
                    for i in range(qty):
                        inst_id = f"{item['part_id']}_{i + 1}"
                        parts_to_pack.append((inst_id, item["poly"]))
                        part_lookup[inst_id] = item

            if strategy_str == "lattice":
                from squeezenest.api.models import NestingJob, NestingStrategy, PartMetadata
                job_parts = {}
                for item in ingested_parts_meta:
                    p_id = item['part_id']
                    if fill_mode == "max_fill":
                        outer, holes = item["poly"]
                        part_area = max(1.0, SPolygon(outer).area)
                        qty = max(4, int(((panel_w * panel_h) / part_area) * 1.3))
                    else:
                        qty = max(1, item["user_qty"] or target_quantity)
                    job_parts[p_id] = (item["poly"], PartMetadata(part_id=p_id, quantity=qty))

                job = NestingJob(
                    parts=job_parts,
                    stock=[stock],
                    clearance_mm=clearance_mm,
                    rotation_set=rotation_set,
                    strategy=NestingStrategy.LATTICE
                )
                from squeezenest._nesting.lattice import run_lattice_job
                
                t_start = time.perf_counter()
                def lattice_stop_check() -> bool:
                    return _ACTIVE_JOBS.get(job_id, {}).get("stop", False) or (time.perf_counter() - t_start >= timeout_sec)

                try:
                    result = run_lattice_job(job, stop_check=lattice_stop_check)
                    placements = list(result.layout.placements)
                    unplaced = list(result.layout.unplaced_parts)
                    
                    if _ACTIVE_JOBS.get(job_id, {}).get("stop", False):
                        run_status = "stopped_by_user"
                    elif time.perf_counter() - t_start >= timeout_sec:
                        run_status = "timeout_reached"
                    else:
                        run_status = "success"
                except Exception as ex:
                    run_status = f"error_lattice: {ex}"
                    placements = []
                    unplaced = []
            else:
                # Execute timed BLF Nesting with cancellation & timeout
                placements, unplaced, run_status = _timed_bottom_left_fill(
                    parts=parts_to_pack,
                    stock=stock,
                    clearance_mm=clearance_mm,
                    rotation_set=rotation_set,
                    fill_mode=fill_mode,
                    timeout_sec=timeout_sec,
                    job_id=job_id,
                )

            # Reconstruct geometry for preview and DXF export
            placements_result = []
            for p in placements:
                item = part_lookup[p.part_id]
                outer, holes = item["poly"]

                cx = sum(pt[0] for pt in outer) / len(outer)
                cy = sum(pt[1] for pt in outer) / len(outer)

                rotated_outer = _rotate_polygon(outer, p.rotation_deg)
                min_x, min_y, _, _ = _part_bounding_box(rotated_outer)
                px, py = p.position_mm
                off_x = -min_x + px
                off_y = -min_y + py

                transformed_outer = _transform_contour(outer, p.rotation_deg, cx, cy, off_x, off_y)
                transformed_holes = [
                    _transform_contour(h, p.rotation_deg, cx, cy, off_x, off_y)
                    for h in holes
                ]

                placements_result.append({
                    "part_id": p.part_id,
                    "source_file": item["filename"],
                    "position_mm": [round(px, 3), round(py, 3)],
                    "rotation_deg": p.rotation_deg,
                    "outline": transformed_outer,
                    "holes": transformed_holes,
                })

            # Calculate total placed area & utilization
            total_placed_area = sum(SPolygon(p["outline"]).area for p in placements_result)
            panel_area = panel_w * panel_h
            utilization_pct = (total_placed_area / panel_area * 100.0) if panel_area > 0 else 0.0

            # Generate export DXF for whatever has been placed so far
            download_id = str(uuid.uuid4())
            if placements_result:
                dxf_bytes = _build_panel_dxf(panel_w, panel_h, placements_result)
                _DXF_CACHE[download_id] = ("squeezenest_panel.dxf", dxf_bytes)

            total_elapsed_sec = round(time.perf_counter() - t_total_start, 2)

            self._send_json(200, {
                "success": True,
                "status": run_status,  # "completed" | "stopped_by_user" | "timeout_reached"
                "elapsed_sec": total_elapsed_sec,
                "timeout_sec": timeout_sec,
                "panel": {
                    "width_mm": panel_w,
                    "height_mm": panel_h,
                    "area_mm2": round(panel_area, 2),
                },
                "clearance_mm": clearance_mm,
                "utilization_pct": round(utilization_pct, 2),
                "placed_count": len(placements_result),
                "total_requested": len(parts_to_pack),
                "unplaced_count": len(unplaced),
                "placed_area_mm2": round(total_placed_area, 2),
                "download_id": download_id if placements_result else None,
                "placements": placements_result,
            })

        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] Optimization error: {exc}", file=sys.stderr)
            err_msg = str(exc)
            if str(REPO_ROOT) in err_msg:
                err_msg = err_msg.replace(str(REPO_ROOT), "[REPO_ROOT]")
            self._send_json(500, {"error": f"Optimization failed: {err_msg}"})
        finally:
            if job_id:
                _ACTIVE_JOBS.pop(job_id, None)


def run_server(port: int = 8080) -> None:
    server = None
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), PanelizerHandler)
            print(f"SqueezeNest CNC Panelizer Demo running at http://localhost:{p}")
            break
        except OSError:
            continue

    if server is None:
        print(f"Could not bind to any port in range {port}-{port + 10}.")
        sys.exit(1)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run SqueezeNest CNC Panelizer Demo")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default 8080)")
    args = parser.parse_args()
    run_server(args.port)
