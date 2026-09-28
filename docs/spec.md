# SqueezeNest -- Technical Specification

**Version:** 0.1.0-draft  
**Status:** Pre-implementation  
**Module identity:** General-purpose 2D irregular nesting & dimensional sensitivity engine

---

## 1. Scope & Goals

SqueezeNest is a Python library (with a CLI surface) that solves two coupled problems:

| Problem | Description |
|---|---|
| **2D Irregular Nesting** | Place a set of arbitrarily shaped flat parts onto one or more rectangular stock sheets, minimising waste (maximising yield). |
| **Squeeze-to-Fit Sensitivity** | Given a set of parts that fail to nest into a target stock size, compute a Pareto frontier over two relaxation axes -- affine part scaling (sx, sy) and inter-part clearance (d_clearance) -- identifying the minimal deformation that achieves a target yield. |

The engine is intentionally domain-agnostic. Downstream fabrication-domain extensions (CNC routing, sheet-stock singulation) attach via typed Protocol contracts -- not monkey-patching.

---

## 2. Mathematical Model

### 2.1 Coordinate System & Units

All internal geometry operates on a **64-bit signed integer grid** (Clipper2 `int64` coordinates). Physical coordinates (millimetres) are mapped to integer sub-micron units at ingestion:

```
int_coord = round(mm_value x SCALE_FACTOR)
SCALE_FACTOR = 1_000_000   # 1 mm = 1,000,000 units (sub-micron resolution)
```

The maximum representable coordinate is +/-9,223,372,036 mm -- far beyond any physical stock size. This eliminates floating-point drift in all boolean clipping and Minkowski operations.

> **Invariant:** No floating-point coordinate may enter the Clipper2 kernel.
> All fp<->int conversions are isolated to the `_core.scale` module.

### 2.2 Part Geometry Model

A `Part` is modelled as a **Polygon with Holes**:

```
Part = {
    outer_boundary : SimplePolygon       # CCW winding (Clipper2 convention)
    holes          : List[SimplePolygon] # CW winding, each strictly inside outer_boundary
    metadata       : PartMetadata
}
```

`SimplePolygon` = a closed, non-self-intersecting sequence of int64 (x, y) vertices. Arcs, splines, and bulged LWPOLYLINE segments from DXF sources are **tessellated** to polylines before storage (sagitta/chord-error delta <= 0.01 mm by default, configurable).

Multi-contour parts (disconnected outer boundaries) are supported as `MultiPart = List[Part]`. The nesting engine treats each contour independently for collision and groups them for placement.

### 2.3 Affine Squeeze-to-Fit Model

When baseline nesting fails to achieve target yield `Y_target`, the engine enters sensitivity mode. Two relaxation axes are defined:

| Axis | Symbol | Domain |
|---|---|---|
| Horizontal scale factor | sx | [1 - dsx_max, 1.0] |
| Vertical scale factor | sy | [1 - dsy_max, 1.0] |
| Inter-part clearance | d | [d_min, d_baseline] |

Constraints may be specified as:
- **Relative**: `dsx_max = 0.02` (max 2% width reduction)
- **Absolute**: `dW_max = -1.5 mm` (converted to relative via part bounding box at ingestion)

The scaled geometry of part *i* under parameters (sx, sy) is computed about its centroid (cx, cy):

```
P_i(sx, sy) = { (sx*x - sx*cx + cx,  sy*y - sy*cy + cy) | (x,y) in P_i }
```

The engine sweeps a discrete parameter grid:

```
S = { (sx_k, sy_l, d_m) | k in [0..N_x], l in [0..N_y], m in [0..N_d] }
```

Each grid point `(sx, sy, d)` is an independent nesting run, yielding count `Y(sx, sy, d)`.

### 2.4 Pareto Frontier

The Pareto-optimal set is computed over:

```
Minimise:  deformation_cost(sx, sy, d) = w1*(1-sx) + w2*(1-sy) + w3*(d_baseline - d)
Maximise:  yield Y(sx, sy, d)
```

Weights (w1, w2, w3) are user-configurable (default: equal). Output is a `SensitivityResult` listing all Pareto-non-dominated points with human-readable bottleneck annotations, e.g. *"Reducing d_clearance by 0.2 mm alone increases yield from 12 to 15 (+25%)"*.

### 2.5 Minimum Feature Thickness Constraint

Before any scaling or offset operation, every part must satisfy:

```
for all feature_neck in Part:  width(feature_neck) >= 2 * kerf_radius + DFM_MIN_WEB
```

`DFM_MIN_WEB` defaults to 0.5 mm and is configurable per fabrication context. If a scaling step would violate this, that grid point is marked `TOPOLOGY_INVALID` and excluded from the Pareto sweep rather than silently splitting the part.

---

## 3. DXF Ingestion Pipeline

### 3.1 Stage Overview

```
DXF file
   |
   v
[Stage 1] Entity extraction (ezdxf)
   |  -> LINE, ARC, SPLINE, LWPOLYLINE, CIRCLE on target layers
   v
[Stage 2] Spatial graph construction
   |  -> KD-Tree endpoint index (scipy.spatial.KDTree)
   |  -> Snap endpoints within eps_snap (default 0.01 mm) -> bridge micro-gaps
   |  -> De-duplicate coincident segments (exact int-hash comparison)
   v
[Stage 3] Curve tessellation
   |  -> Arc -> polyline (sagitta delta <= 0.01 mm)
   |  -> B-Spline -> polyline (ezdxf adaptive sampling)
   |  -> Bulged LWPOLYLINE -> arc segments -> polyline
   v
[Stage 4] Chain assembly
   |  -> Walk adjacency graph -> closed chains
   |  -> Flag unclosed chains as DXF_GAP violations (not silent discard)
   v
[Stage 5] Clipper2 PolyTree reconstruction
   |  -> Union all chains -> resolve winding
   |  -> Classify outer boundary (CCW) vs holes (CW)
   |  -> Produce List[Part]
   v
[Stage 6] Pre-flight validation
      -> Feature thickness check
      -> Minimum hole diameter check (DFM_MIN_HOLE_DIA, default 0.8 mm)
      -> Emit structured ValidationReport
```

### 3.2 Layer Contract

By default, SqueezeNest reads entities from all layers (auto-detection mode). For deterministic CI ingestion, a strict layer contract can be enforced:

| Layer name (configurable) | Semantics |
|---|---|
| `OUTLINE` | Outer part boundary |
| `CUTOUT` | Interior holes / pockets |
| `KEEPOUT` | Clearance exclusion zones (spatial buffer only, not cut) |

Entities on unlisted layers: silently ignored in strict mode; `DXF_LAYER_UNKNOWN` INFO in permissive mode.

---

## 4. Clipper2 Geometric Pipeline

### 4.1 Scale Conversion

```python
# squeezenest/_core/scale.py
SCALE = 1_000_000  # mm -> int64

def to_int(mm: float) -> int:
    return round(mm * SCALE)

def to_mm(i: int) -> float:
    return i / SCALE
```

### 4.2 Kerf / Clearance Offset

All morphological offsets use **Clipper2 InflatePaths**:

- `JoinType = Miter` with `MiterLimit = 2.0` (prevents spike explosion on acute corners)
- `EndType = Polygon` (closed contours only)
- Negative offsets (erosion for kerf compensation) applied before NFP computation
- Post-offset validity check: polygon count change -> `DFM_NECK_PINCH` violation -> reject grid point

### 4.3 No-Fit Polygon (NFP) Computation

NFPs are computed via Minkowski difference approximation using Clipper2 offset + boolean difference:

```
NFP(A, B) = Boundary of { t in R^2 | (B translated by t) intersect A != empty }
```

Steps:
1. Reflect B around its reference point -> B'
2. Compute Minkowski sum A (+) B' via Clipper2 polygon offset cascade
3. NFP inner frontier = inner boundary of the resulting union

The placement engine uses NFPs to enumerate all valid non-overlapping positions for B relative to A in O(1) per query.

### 4.4 Collision Detection Hierarchy

```
Stage 1: AABB overlap check          O(1) scalar   - rejects ~70% of candidates
Stage 2: OBB (rotated bbox) check    O(1) scalar   - rejects ~85% cumulatively
Stage 3: Convex hull intersection    O(n+m)        - rejects ~97% cumulatively
Stage 4: Exact Clipper2 boolean      O(n*m*log)    - applied to remaining ~3%
```

The R-Tree (Shapely STRtree) indexes all placed part bounding boxes, reducing Stage 1 from O(n) scan to O(log n + k) query.

---

## 5. Two-Tier NFP Cache

### 5.1 Cache Key

```python
CacheKey = tuple[
    bytes,   # SHA-256 of polygon_A vertices (normalised: centroid->origin, lexsort)
    bytes,   # SHA-256 of polygon_B vertices
    int,     # rotation_A in 0.001-degree units  (e.g. 90000 = 90 deg)
    int,     # rotation_B in 0.001-degree units
    int,     # d_clearance in int64 units (quantised to SCALE)
]
```

Polygon hash normalisation: translate centroid to origin, lexicographically sort vertices to produce a canonical form independent of starting vertex and translation.

### 5.2 Tier 1 -- In-Process LRU Cache

- `cachetools.LRUCache`, default capacity: 4096 entries (~256 MB for complex polygons)
- Lifetime: single process / single `NestingJob.run()` call
- Partial invalidation: drop keys where the modified parameter dimension intersects

### 5.3 Tier 2 -- On-Disk Content-Addressable Store

- SQLite at `~/.squeezenest/nfp_cache.db` (XDG-compliant path)
- Schema: `(hash_key TEXT PRIMARY KEY, data BLOB, last_accessed_at REAL)`
- Features an LRU eviction policy limited to 100 entries to prevent disk bloat.
- `data`: Clipper2 paths serialised as msgpack
- Workers open DB in read-only WAL mode; writes serialised through a main-process cache-writer thread

### 5.4 Invalidation Rules

| Change | Cache behaviour |
|---|---|
| (sx, sy) changes | Polygon hashes change -> full recompute |
| d_clearance changes only | Base polygon hashes unchanged -> partial recompute |
| New rotation added | Only new rotation keys miss |
| Cache capacity exceeded | LRU eviction (Tier 1 only; Tier 2 never auto-evicts) |

---

## 6. Placement Engine

### 6.1 Rotation Model

Discrete orthogonal rotations by default: {0 deg, 90 deg, 180 deg, 270 deg}.
Configurable discrete step (e.g. every 45 deg). Continuous rotation is out of scope for v0.x.

### 6.2 Bottom-Left-Fill with Beam Search

```
1. Sort part sequence by (area DESC, perimeter DESC) -- largest-first heuristic
2. For each beam candidate (B parallel sequences, default B=5):
   a. Take next unplaced part P with rotation r in Rotations
   b. Enumerate candidate placements along NFP frontier (touching-pair positions)
   c. Score by leftmost-then-bottommost packed centroid
   d. Select top-K placements -> expand beam
3. Return highest-yield beam
```

B=1 degrades to pure BLF greedy (fast). B=infinity is exhaustive (impractical for large sets).

### 6.3 Lattice Tiling (Homogeneous Panelization)

For homogeneous jobs (many copies of a single part), SqueezeNest implements a `Lattice` strategy.
1. Computes the optimal 2-part interlocking cluster (minimizing convex-hull area over all valid rotations).
2. Computes 2D axis-aligned and brick-wall tiling patterns of the cluster across the stock sheet. It evaluates all global allowed rotations (e.g., 0, 90, 180, 270) of the paired cluster to maximize the placement count.
3. Falls back to BLF for any remaining quantities that don't fit in the lattice grid.
This bypasses the BLF heuristic limitations and achieves near-optimal density.

### 6.4 Genetic Algorithm (GA)

A GA optimises the **part sequence** passed to BLF, treating BLF as the fitness evaluator.
Genome = permutation of part indices. Crossover = Order Crossover (OX). Mutation = adjacent swap.
Fitness = yield count + compactness bonus.

---

## 7. DFM Diagnostic Contract

### 7.1 Severity Levels

| Severity | Meaning | Job effect |
|---|---|---|
| `ERROR` | Part physically invalid; cannot be processed | Part excluded; job fails unless `strict=False` |
| `WARNING` | Manufacturability concern | Part included with warning flag |
| `INFO` | Advisory; no action needed | Logged only |

### 7.2 Error Code Catalogue (v0.1)

| Code | Severity | Description |
|---|---|---|
| `DXF_GAP_001` | ERROR | Unclosed contour: endpoint gap > eps_snap |
| `DXF_DUPLICATE_SEGMENT_002` | INFO | Coincident segment removed during graph construction |
| `DXF_LAYER_UNKNOWN_003` | INFO | Entity on unlisted layer ignored in strict mode |
| `DXF_SELF_INTERSECT_004` | WARNING | Self-intersecting contour -- Clipper2 will attempt repair |
| `DFM_NECK_PINCH_010` | ERROR | Feature neck width < 2*kerf + DFM_MIN_WEB after offset |
| `DFM_HOLE_COLLAPSE_011` | ERROR | Interior hole diameter < DFM_MIN_HOLE_DIA after scaling |
| `DFM_CLEARANCE_BREACH_012` | WARNING | Inter-part clearance below minimum router bit diameter |
| `DFM_TOPOLOGY_SPLIT_013` | ERROR | Offset/scaling caused single boundary to split into multiple polygons |
| `NFP_CACHE_COLLISION_020` | WARNING | SHA-256 cache key collision detected (extremely rare) |
| `SWEEP_POINT_INVALID_030` | INFO | Sweep grid point rejected -- topology constraint violated |

### 7.3 Output Formats

- **CLI**: JSON Lines to stdout (one object per violation) + human summary table to stderr
- **Python API**: `ValidationReport` typed dataclass (see arch_contracts.md)
- **SARIF**: Optional `--format=sarif` flag emitting SARIF 2.1.0-compatible JSON for IDE / GitHub Actions

---

## 8. Concurrency Model

```
Main process:
  +-- Ingestion (single-threaded, IO-bound)
  +-- Pre-flight validation (single-threaded)
  +-- Sweep grid generation
  +-- ProcessPoolExecutor (N = cpu_count - 1 workers)
  |     Worker 1: sweep slice (sx_0..sx_k, sy_0, d_0)
  |     Worker 2: sweep slice (sx_0..sx_k, sy_1, d_0)
  |     ...
  +-- Pareto aggregation + Tier-2 cache writes (single-threaded)
```

No Ray, no Dask. The tool must be runnable in CI with zero cluster infrastructure.

---

## 9. Module Structure

```
squeezenest/
+-- api/                      # Stable public surface (SemVer-protected)
|   +-- __init__.py
|   +-- models.py             # NestingJob, SensitivityResult, PlacementLayout, ValidationReport
|   +-- protocols.py          # FabricationEntity, HoldingTabStrategy, PartingCutStrategy, IngestAdapter
+-- _core/                    # Private geometric kernel
|   +-- scale.py              # mm <-> int64 conversion, SCALE_FACTOR
|   +-- clipper.py            # Clipper2 wrappers (offset, boolean, NFP)
|   +-- spatial.py            # R-Tree / STRtree spatial index
|   +-- cache.py              # Two-tier NFP cache (LRU + SQLite)
+-- _io/                      # Private ingestion & export
|   +-- dxf_ingest.py         # ezdxf pipeline (Stages 1-6)
|   +-- graph_stitch.py       # KD-Tree endpoint snapping & chain assembly
|   +-- dxf_export.py         # Multi-layer CAM DXF writer
+-- _nesting/                 # Private placement engine
|   +-- _geometry.py          # Shared polygon/rotation primitives
|   +-- bounding.py           # AABB / OBB / convex hull filter cascade
|   +-- nfp.py                # NFP computation & cache integration
|   +-- blf.py                # Bottom-Left-Fill + Beam Search
|   +-- ga.py                 # Optional GA part-sequence optimiser (v0.2)
+-- _sensitivity/             # Private parametric sweep
|   +-- sweep.py              # Grid generation & ProcessPool dispatch
|   +-- pareto.py             # Pareto frontier extraction & bottleneck annotation
|   +-- affine.py             # Affine scale transform + topology validity guard
+-- _fabrication/             # Private fabrication strategy implementations
|   +-- holding_tab.py        # HoldingTabStrategy: bridge/tab anchor generation
|   +-- parting_cut.py        # PartingCutStrategy: continuous separation pass optimiser
|   +-- stock_envelope.py     # StockEnvelope: clamping margin & keepout geometry
+-- cli.py                    # Typer CLI entry point
+-- py.typed                  # PEP 561 marker
```

---

## 10. Dependency Table

| Category | Library | Rationale |
|---|---|---|
| Geometry kernel | clipper2-python | 64-bit int, robust booleans, Minkowski |
| Spatial index | shapely (STRtree) | Fast broad-phase bounding index |
| DXF I/O | ezdxf | Full DXF spec, active maintenance |
| KD-Tree snapping | scipy | scipy.spatial.KDTree |
| Public API models | pydantic v2 | Typed, validated public schemas |
| CLI | typer | Type-annotated Click wrapper |
| Serialisation | msgpack | Compact NFP cache payloads |
| Cache | cachetools | LRUCache with configurable capacity |
| Testing | pytest, hypothesis | Property-based + unit testing |

**Explicitly excluded:** Ray, Dask, networkx (replaced by custom graph stitcher).

---

## 11. Versioning & Stability Policy

SqueezeNest follows SemVer 2.0.0:

| Surface | Stability | Policy |
|---|---|---|
| squeezenest.api.* | Stable | Breaking changes require MAJOR bump |
| squeezenest._* | Private | May change any release; no guarantees |
| squeezenest.api.protocols.* | Stable Protocol interfaces | New optional methods in MINOR only if default-implemented |

Extension authors MUST import exclusively from `squeezenest.api`.
Importing from `squeezenest._*` is explicitly unsupported.
