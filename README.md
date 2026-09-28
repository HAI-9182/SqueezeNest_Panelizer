# SqueezeNest

General-purpose 2D irregular nesting and dimensional sensitivity ("squeeze-to-fit") engine.

## Overview

SqueezeNest is a Python library for packing 2D irregular polygons onto a rectangular stock sheet (nesting). It features DXF ingestion, collision-free placement using exact integer arithmetic, and a unique **Dimensional Sensitivity Sweep** that explores whether minor part deformations (e.g., scaling down by 1-5%) can unlock higher material yields.

## Quick Start

### 1. Basic 2D Irregular Nesting (Bottom-Left Fill)

```python
from squeezenest.api import NestingStrategy
from squeezenest.api.models import StockSheet, RotationSet
from squeezenest._nesting.blf import run_nesting_job

# Define stock sheet (100x100 mm)
stock = StockSheet(width_mm=100.0, height_mm=100.0)

# Define a part: (outer_boundary_pts, [hole_pts_list])
l_shape = (
    [(0.0, 0.0), (30.0, 0.0), (30.0, 15.0), (15.0, 15.0), (15.0, 30.0), (0.0, 30.0)],
    [] 
)
parts_to_pack = [("L1", l_shape), ("L2", l_shape), ("L3", l_shape)]

# Run nesting
layout = bottom_left_fill(
    parts=parts_to_pack,
    stock=stock,
    clearance_mm=1.0,
    rotation_set=RotationSet.ORTHO,
)
print(f"Placed {layout.yield_count} parts.")
```

### 2. High-Yield Panelization (Lattice Tiling)

For homogeneous parts (e.g., mass-producing the exact same L-shape bracket), SqueezeNest provides a `Lattice Tiling` engine. It pairs parts into tightest interlocking clusters (e.g. Yin-Yang patterns) and mathematically tiles them to maximize density. The algorithm automatically evaluates all allowed global rotations of the cluster to ensure the tiling grid optimally aligns with the stock sheet dimensions.

```python
from squeezenest.api import NestingStrategy
from squeezenest.api.models import NestingJob, PartMetadata

job = NestingJob(
    parts={"L": (l_shape, PartMetadata(part_id="L", quantity=40))},
    stock=[stock],
    clearance_mm=1.0,
    rotation_set=RotationSet.ORTHO,
    strategy=NestingStrategy.LATTICE
)
result = job.run()
print(f"Lattice tiled {result.layout.yield_count} parts.")
```

### 3. Ingesting DXF Files

```python
from pathlib import Path
from squeezenest._io.dxf_ingest import ingest_dxf

# Ingest DXF, healing micro-gaps (e.g. up to 0.01mm)
extracted_parts, report = ingest_dxf(Path("parts.dxf"), eps_snap_mm=0.01)

if not report.is_fatal:
    print(f"Extracted {len(extracted_parts)} valid closed parts from DXF.")
```

### 3. Dimensional Sensitivity Sweep ("Squeeze-to-Fit")

```python
from squeezenest.api.models import NestingJob, SensitivityConfig, StockSheet, PartMetadata, RotationSet
from squeezenest._sensitivity.sweep import run_sensitivity_sweep

# Setup job
job = NestingJob(
    parts={"PART_A": (my_polygon, PartMetadata(part_id="PART_A", quantity=10))},
    stock=[StockSheet(width_mm=25.0, height_mm=25.0)],
    clearance_mm=0.5,
    rotation_set=RotationSet.ORTHO,
)

# Configure the "squeeze" search grid
config = SensitivityConfig(
    scale_x_range=(0.95, 1.0),   # Allow scaling X down to 95%
    scale_y_range=(0.95, 1.0),   # Allow scaling Y down to 95%
    clearance_range=(0.3, 0.5),  # Explore clearance between 0.3mm and 0.5mm
    n_scale_x=3, n_scale_y=3, n_clearance=2,
)

sweep_result = run_sensitivity_sweep(job, config)
best = sweep_result.best_yield()
print(f"Max Yield achieved: {best.yield_count} (scale_x={best.scale_x:.2f}, scale_y={best.scale_y:.2f})")
```

### 4. Interactive CNC Panelization Web Demo

SqueezeNest includes an interactive local web studio to test panelization with custom `.dxf` CAD files, live 2D vector preview (SVG), stop controls, timeout protection, and direct DXF panel export. The "Maximize Yield" mode scales dynamically based on the theoretical maximum part count matching your sheet size without artificial caps.

```bash
# Launch the web demo server via CLI
squeezenest serve --port 8080
```

Then open your browser at: **http://localhost:8080**

### 5. Command Line Interface (CLI)

SqueezeNest provides a robust CLI via `typer`. You can run nesting jobs and sensitivity sweeps directly from your terminal:

```bash
# Basic Nesting
squeezenest nest parts.dxf --width 100.0 --height 100.0 --clearance 0.5 --strategy blf

# Genetic Algorithm (GA) Nesting Optimization
squeezenest nest parts.dxf --width 100.0 --height 100.0 --strategy ga

# Dimensional Sensitivity Sweep
squeezenest sweep parts.dxf --width 100.0 --height 100.0
```

*Note: SqueezeNest v0.2 features an SQLite-backed Tier-2 NFP cache with LRU eviction (limit 100 entries) to speed up repeated runs.*

For full instructions, user controls, and empirical nesting benchmark analysis, see [`demo/Readme_Demo.md`](demo/Readme_Demo.md).

---

## Appendix A: How It Works (Explained Simply)

Think of SqueezeNest like an ultra-smart version of Tetris for a laser cutter or a CNC machine.

### Expected Input
1. **The Shapes to Cut**: Either a CAD drawing file (`.dxf`) or a list of `(x, y)` coordinate points describing the shape outline (and any holes).
2. **The Sheet Size**: The dimensions of your raw board (e.g., 100x100 mm).
3. **The Rules & "Squeeze" Tolerance**: Clearance gap between pieces, allowed rotations (e.g., 90°), and whether the computer is allowed to shrink pieces slightly (e.g., 1-5%) to fit more.

### Expected Output
1. **The Cutting Layout (Blueprint)**: Exactly where each part goes on the sheet `(X, Y)` position and rotation angle.
2. **The Score (Yield)**: How many parts successfully fit.
3. **The "Squeeze" Trade-off (Pareto Frontier)**: A menu showing trade-offs, like: *"At 100% size, 4 parts fit. Shrink by 2%, and 6 parts fit."*

### The Pipeline Step-by-Step
1. **Clean & Fix (Ingestion)**: Reads the DXF. Heals tiny open gaps by snapping lines together so every shape is a solid, closed loop.
2. **Precision Guard (Integer Kernel)**: Converts millimeter floating-point numbers into nanometer integers (multiplies by 1,000,000). This prevents computer rounding errors from causing false overlapping or crashing the geometry engine.
3. **Greedy Nesting (Bottom-Left Fill)**: Slides shapes as far down and left as possible into the sheet without colliding.
4. **The Squeeze Tester (Sensitivity Sweep)**: Tests micro-adjustments to scale or clearance across a grid to discover if tiny deformations unlock higher yields.

---

## Appendix B: The Integer Math Scale Guard

Computers are notoriously bad at decimal math (e.g. `0.1 + 0.2 = 0.30000000000000004`). In geometry, this causes "false overlaps" or topological crashes. SqueezeNest solves this by using exact integer arithmetic.

All coordinates are multiplied by `1,000,000` and rounded to the nearest integer using round-half-up. 
- `1.0 mm` becomes `1,000,000`
- `0.001 mm` (1 micron) becomes `1,000`
- `0.000001 mm` (1 nanometer) becomes `1`

This provides perfect fixed-point math and guarantees geometry is immune to rounding drift, while maintaining a supported scale envelope of `[-9000 mm, 9000 mm]` with no risk of `int64` overflow.
