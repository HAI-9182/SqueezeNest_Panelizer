# SqueezeNest CNC Panelization Demo & Benchmark Analysis

This document provides both the complete **user guide** for running the interactive 2D web panelizer and the **empirical benchmark analysis** comparing greedy Bottom-Left Fill (BLF) against optimal nesting.

---

# Part 1: How to Use the Demo Program

The demo is a zero-dependency local web application that runs directly on top of the SqueezeNest geometry kernel.

## 1. Features
- **CAD DXF Ingestion**: Upload single or multiple AutoCAD `.dxf` board designs.
- **Micro-Gap Healing & Entity Normalization**: Automatically explodes blocks (`INSERT`), converts old-style `POLYLINE` to `LWPOLYLINE`, and snaps endpoint micro-gaps.
- **Configurable Panel Dimensions**: Width and height inputs with presets (100x100mm, Eurocard 100x160mm, 150x150mm, 200x200mm, etc.).
- **Clearance & Router Kerf**: Custom tool diameter spacing (0.5mm, 1.5mm, 2.0mm, 2.4mm).
- **Interactive 2D Vector Preview (SVG)**:
  - High-contrast stock material CNC rendering with internal cutouts/holes.
  - Coordinate grid (10mm / 50mm).
  - Toggleable clearance halos and part ID labels.
  - Mouse pan & scroll wheel zoom.
- **Stop Button & Upper Limit Timer**:
  - Live progress counter (`Optimizing 5s / 30s...`).
  - `⏹ Stop` button to halt execution at any second and immediately inspect the best layout placed so far.
  - Configurable upper limit timer (e.g. 10s, 30s, 60s) to prevent runaway execution.
- **Security & Safety Guardrails (80/20 Controls)**:
  - Strict payload limit ($25\text{ MB}$ max request body) to prevent memory DoS.
  - File constraints: max $20$ DXF files per request and $10\text{ MB}$ max per individual DXF.
  - Automatic entity complexity checks ($50{,}000$ entities max) to stop malicious DXF bombs.
  - Rigid path sanitization preventing directory traversal on file uploads and sample downloads.
  - Server bounds on numeric parameters (panel dimensions, clearance, timeout capped at $120\text{s}$).
  - CORS restricted to localhost origins, and server strictly bound to local loopback interface (`127.0.0.1`).
- **Panel Metrics & Direct DXF Export**: Real-time panel area utilization percentage, placed board counts, and 1-click download of the resulting panelized `.dxf` CAD file.

---

## 2. How to Run the Demo

From the repository root, start the server using the virtual environment:

```bash
./.venv/bin/python demo/server.py --port 8080
```

*(If port `8080` is occupied by another process on your machine, simply choose another port such as `--port 8090` or `--port 5000`).*

Then open your browser at:
👉 **http://localhost:8080** (or your selected port).

---

## 3. Step-by-Step Usage Guide

1. **Set Panel Size**: Enter Width and Height in mm (or click a preset chip like `100x100mm` or `Eurocard`).
2. **Configure Spacing**:
   - **Router Kerf / Clearance**: Set spacing between adjacent boards (e.g., `2.0mm` for standard CNC routing).
   - **Endpoint Gap Snapping**: Set snap tolerance (`0.05mm` default, or `0.1mm` / `0.5mm` if CAD files have loose corners).
   - **Rotation Freedom**: Select `Orthogonal (0°, 90°, 180°, 270°)` or `Fixed Upright (0° only)`.
   - **Upper Limit Timer**: Select maximum allowable runtime (e.g., `30s`).
3. **Load CNC Geometries**:
   - Drag and drop your `.dxf` file into the drop zone, OR
   - Click one of the quick sample buttons: `⚡ Load Washer (with Cutout)` or `⚡ 3-Part Set`.
4. **Click "⚡ Optimize Panel Layout"**:
   - The engine begins packing boards onto the sheet.
   - You can click **`⏹ Stop`** at any time to freeze search and display the best layout found so far.
   - If the timer hits the limit (e.g. 30s), it wraps up and shows the best panelization found.
5. **Inspect & Export**:
   - Review the **Panel Utilization %** and **Placed Boards** KPI cards.
   - Pan and zoom on the vector preview to verify clearances and cutouts.
   - Click **"⬇ Download Panelized DXF"** to export the ready-to-cut CAD layout.

---

# Part 2: Benchmark Test & Geometric Analysis

This section records empirical benchmark results, visual observations, and geometric analysis of testing SqueezeNest v0.1.0 on a real-world irregular CNC panelization run.

---

## 1. Test Setup & Configuration

| Parameter | Value | Notes |
|---|---|---|
| **Board Geometry** | Custom irregular tapered CNC | Wedge / teardrop profile with connector tab at top |
| **Panel Dimensions** | $100.0 \times 100.0\text{ mm}$ | Total panel area: $10{,}000\text{ mm}^2$ |
| **Router Kerf / Clearance** | $2.0\text{ mm}$ | Standard mechanical CNC routing tool diameter |
| **Rotation Freedom** | Orthogonal ($0^\circ, 90^\circ, 180^\circ, 270^\circ$) | 4 discrete $90^\circ$ rotational steps |
| **Packing Mode** | Maximize Yield (Fill Panel) | Greedy auto-fill until sheet saturation |
| **Endpoint Snapping** | $0.05\text{ mm}$ | Automatic micro-gap healing |
| **Engine Kernel** | SqueezeNest v0.1.0 (Greedy BLF) | Integer coordinates (1 unit = 1 nm) |

---

## 2. Benchmark Results

| Metric | Result | Assessment |
|---|---|---|
| **Placed Boards** | **22 boards** | (Out of 60 candidate instances) |
| **Panel Utilization** | **35.95% (~36.0%)** | Area of placed copper / total panel area |
| **Execution Time** | **107.1 seconds** | Raw un-capped BLF scanning |
| **Orientation Distribution** | **21 boards at $0^\circ$**, **1 board at $90^\circ$** | Heavy monoculture orientation |

---

## 3. Visual & Geometric Observations

1. **Monoculture Orientation (The Upright Grid)**:
   - 21 out of the 22 boards were packed in the exact same upright ($0^\circ$) orientation in staggered vertical columns.
   - Only a single board (the 22nd, in the bottom-right corner) was placed at $90^\circ$ when no upright position remained.
2. **Missing Interlocking (The "Yin-Yang" Opportunity)**:
   - The tested board features a pronounced tapered profile: narrow at the bottom (chin) and wide at the top (ears/tab).
   - In 2D irregular nesting, tapered geometries are prime candidates for **$180^\circ$ interlocking (head-to-tail nesting)**: flipping alternating boards upside-down allows the narrow bottom of one board to nestle between the wide ears of its neighbors.
   - Because all boards were kept upright, the wide tops collided with adjacent tops, leaving large triangular "valleys" of empty, wasted CNC stock material around the lower half of each board.

---

## 4. Root-Cause Analysis: Why v0.1.0 BLF Did This

### A. The "First-Fit" Rotation Trap
In `squeezenest/_nesting/blf.py`, the rotation evaluation loop is ordered:
```python
rotations = [0.0, 90.0, 180.0, 270.0]
for angle in rotations:
    ...
    if placed:
        break  # Immediately exits on the first legal candidate
```
Because $0^\circ$ is evaluated first:
- As long as an upright board finds **any** legal space on the sheet (even if it leaves a large, loose gap), the algorithm immediately accepts that position.
- It **never tests whether $180^\circ$ would have tucked into a tighter crevice**.
- It only ever considers $90^\circ$ or $180^\circ$ once the upright orientation can no longer physically fit anywhere on the entire panel.

### B. Purely Local Greedy Placement
Greedy Bottom-Left-Fill evaluates parts one by one with no global lookahead:
- It asks: *"What is the lowest, leftmost valid point for this single piece?"*
- It does not ask: *"Does this placement leave a usable pocket for future pieces?"*

### C. Search Complexity in Saturated Sheets
When a panel nears saturation:
- Each failing part must exhaustively scan $(100 / 0.5) \times (100 / 0.5) \times 4 = 160{,}000$ candidate positions across the entire board.
- Testing dozens of failing candidate copies in pure Python caused the runtime to stretch to $107\text{ seconds}$. This is why the Stop button and 30s Upper Limit Timer were introduced.

---

## 5. Potential Yield Comparison

| Strategy | Est. Utilization | Est. Board Count | Notes |
|---|---|---|---|
| **v0.1.0 Greedy BLF (Observed)** | **36.0%** | **22 boards** | Baseline integer validator; strict clearance guarantee |
| **Yin-Yang Pre-Clustering ($0^\circ / 180^\circ$ pairs)** | **55% – 65%** | **34 – 39 boards** | Pre-pairs two boards into an interlocking rectangular brick |
| **True NFP Contour Sliding + Beam Search (v0.2 Target)** | **65% – 75%** | **40 – 46 boards** | Global optimization with continuous boundary docking |

---

## 6. Recommended Roadmap for Future SqueezeNest Iterations

1. **Cluster / Pre-Pairing Strategy (IMPLEMENTED)**:
   - For single-board panelization, SqueezeNest now provides `NestingStrategy.LATTICE` which automatically pairs boards (e.g., $0^\circ$ and $180^\circ$ interlocks) and tiles them to bypass the "First-Fit" rotation trap. You can test this live in the Web Demo by selecting "Lattice Tiling" under the Nesting Strategy dropdown.
2. **Compactness-Score Rotation Selection**:
   - Instead of breaking on the first legal rotation, evaluate all allowed rotations and pick the one that minimizes the resulting bounding envelope or maximizes local contact area.
3. **Genetic Algorithm / Beam Search (v0.2)**:
   - Activate `_nesting/ga.py` and `NestingJob(beam_width > 1)` to explore multiple placement permutations.
4. **SqueezeNest Sensitivity Integration ("Squeeze-to-Fit")**:
   - Use `run_sensitivity_sweep()`: if the CNC's mechanical outline allows a slight non-critical dimensional tolerance (e.g. $98.5\%$ scale), an extra column of interlocking parts could easily squeeze onto the $100\text{ mm}$ panel edge.
