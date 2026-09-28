# SqueezeNest — Senior Architect Code Review

> Reviewed 2026-09-28 | v0.1 codebase

---

## Summary

The codebase is generally well-structured. The public API layer, invariant documentation, and module boundaries are clear. That said, there are several **real bugs, design gaps, and reliability risks** worth fixing — ranging from critical correctness issues down to maintenance debt.

---

## 🔴 Critical / Correctness Bugs

### 1. `SQLiteNFPCache.get()` — connection not thread-safe + no `close()`

**File:** [`_core/cache.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_core/cache.py#L141-L220)

`SQLiteNFPCache` opens a single `sqlite3.connect()` at `__init__` time with **no thread-safety mode** set (`check_same_thread=True` by default). If `TieredNFPCache` is ever used from the sensitivity sweep's planned `ProcessPool`, each forked process will inherit the same connection object, which is undefined behaviour in SQLite.

Additionally, there is **no `close()` / context manager** (`__enter__`/`__exit__`) on either `SQLiteNFPCache` or `TieredNFPCache`, so the connection is never deterministically closed — this leaks a file handle in tests and production use.

**Fix:**
```python
class SQLiteNFPCache:
    def __init__(self, db_path: Path, max_size: int = 100) -> None:
        self.db_path = db_path
        self._max_size = max_size
        # Use check_same_thread=False only when protected externally,
        # or open per-call.  For simplicity: open fresh per call.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_db()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteNFPCache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
```

---

### 2. `SQLiteNFPCache.__all__` omission — `SQLiteNFPCache` and `TieredNFPCache` not exported

**File:** [`_core/cache.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_core/cache.py#L25)

The `__all__` at the top of the file is `["make_cache_key", "NFPCache"]`. `SQLiteNFPCache` and `TieredNFPCache` were added later but never added to `__all__`. This is a maintenance trap — any `from squeezenest._core.cache import *` will silently miss the Tier 2 classes.

---

### 3. `ga.py` — `__import__("copy").copy(job)` on a mutable `NestingJob`

**File:** [`_nesting/ga.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_nesting/ga.py#L34-L37)

```python
proxy_job = __import__("copy").copy(job)  # ← shallow copy
proxy_job.parts = dict(shuffled)
proxy_job.strategy = __import__("squeezenest").api.models.NestingStrategy.BLF
```

Three issues here:
- `__import__("copy")` and `__import__("squeezenest")` are a code smell — `import copy` and `from squeezenest.api.models import NestingStrategy` should be at the top of the file.
- `copy.copy` is a **shallow** copy, so `proxy_job.stock` is the same list object. Writing to `proxy_job.stock` from one iteration could mutate another. Use `copy.deepcopy` or dataclasses `replace`.
- `NestingJob` is a mutable `@dataclass` (not `frozen=True`), so nothing prevents concurrent writers in a future parallel path.

**Fix:**
```python
import copy
from squeezenest.api.models import NestingStrategy
...
proxy_job = copy.replace(job, parts=dict(shuffled), strategy=NestingStrategy.BLF)
# (Python 3.13+) or dataclasses.replace for 3.11/3.12:
import dataclasses
proxy_job = dataclasses.replace(job, parts=dict(shuffled), strategy=NestingStrategy.BLF)
```

---

### 4. `blf.py` — `instance_idx` is always the *enumeration* index, not the per-part instance index

**File:** [`_nesting/blf.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_nesting/blf.py#L104-L150)

```python
for idx, (part_id, (outer, _holes)) in enumerate(parts):
    ...
    placements.append(PlacedPart(
        part_id=part_id,
        instance_idx=idx,   # ← BUG: this is the index in the flat list, not the per-part copy number
```

`PlacedPart.instance_idx` is documented as *"0-based index for multi-quantity parts (e.g. 0, 1, 2)"*. If the input `parts` list contains the same `part_id` expanded for quantity, `idx=5` for the 6th item (which may be the 2nd instance of part "P3") is wrong. The caller in `run_nesting_job` does not currently expand by quantity either (it just iterates `job.parts.items()`), so the lattice strategy correctly handles `meta.quantity` but BLF silently ignores it.

**Severity:** The invariant is violated silently — callers that rely on `instance_idx` to reconstruct the quantity grouping get wrong data.

---

### 5. `lattice.py` — imports private BLF symbols across module boundary

**File:** [`_nesting/lattice.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_nesting/lattice.py#L15-L18)

```python
from squeezenest._nesting.blf import (
    _get_rotations, _rotate_polygon, _part_bounding_box, _translate_polygon,
    _GRID_STEP_MM, bottom_left_fill
)
```

Five of the six imported symbols are **private** (underscore-prefixed). This creates a fragile coupling — any rename or refactor of BLF internals silently breaks lattice. These helpers should be extracted to a shared `_nesting/_geometry.py` utility module and made part of the internal (but non-underscore-private) interface.

---

## 🟡 Design / Reliability Issues

### 6. `_sensitivity/sweep.py` — hardcoded `1_000_000` magic constant instead of `SCALE`

**File:** [`_sensitivity/sweep.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_sensitivity/sweep.py#L60-L65)

```python
scaled_outer = [(x / 1_000_000, y / 1_000_000) for x, y in scaled_int]
...
holes_scaled.append([(x / 1_000_000, y / 1_000_000) for x, y in hole_scaled_int])
```

`SCALE = 1_000_000` is already defined in `_core/scale.py` and should be used here. If the scale factor ever changes (e.g. to 1e9 for sub-micron precision), this file would produce silently wrong results.

**Fix:**
```python
from squeezenest._core.scale import to_int, to_mm, SCALE
...
scaled_outer = [(x / SCALE, y / SCALE) for x, y in scaled_int]
```

---

### 7. `blf.py` — multi-sheet support silently discarded

**File:** [`_nesting/blf.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_nesting/blf.py#L185-L197)

```python
layouts = []
for sheet in job.stock:
    layout = bottom_left_fill(...)
    layouts.append(layout)

# For v0.1, use the first sheet's layout
final_layout = layouts[0] if layouts else ...
```

The loop runs nesting for every sheet in `job.stock`, but every run independently places **all** parts, and only `layouts[0]` is returned. The computation is wasted and, more importantly, unplaced parts from sheet 0 are never retried on sheet 1. This is a known v0.1 limitation but is silently misleading — the loop gives a false impression of multi-sheet support.

**Minimum fix:** Either remove the loop (use only `job.stock[0]`) or raise `NotImplementedError` for `len(job.stock) > 1`.

---

### 8. `dxf_ingest.py` — `_walk_ring` uses `frozenset` keying on float tuples

**File:** [`_io/dxf_ingest.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_io/dxf_ingest.py#L238-L244)

```python
edge_key = frozenset([p0, p1])
```

`p0` and `p1` are `tuple[float, float]`. Two distinct edges from the same two endpoints (e.g. two parallel overlapping lines) will produce the same `frozenset`, so the second edge is dropped. For real-world DXF files that contain duplicate segments, this silently discards geometry. The `DXF_DUPLICATE_SEGMENT_002` violation code exists but is never emitted.

**Fix:** Emit `DXF_DUPLICATE_SEGMENT_002` when a duplicate is detected (already done in `snap_endpoints` dedup, but not surfaced as a `Violation`).

---

### 9. `cache.py` — imports at module mid-point (style / linting violation)

**File:** [`_core/cache.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_core/cache.py#L136-L139)

```python
# (after NFPCache class definition)
import sqlite3
import msgpack
import time
from pathlib import Path
```

All imports should be at the top of the file per PEP 8. This also means `ruff` (configured with `I` isort rules) will flag this. The current layout suggests `SQLiteNFPCache` was added later in a rush.

---

### 10. `cli.py` — `sweep` command ignores `report.errors()` on fatal ingestion

**File:** [`squeezenest/cli.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/cli.py#L75-L78)

```python
if report.is_fatal:
    typer.echo("Fatal errors during DXF ingestion.", err=True)
    raise typer.Exit(code=1)
```

The `nest` command correctly prints each individual error. The `sweep` command swallows them. Users get no actionable diagnosis. Both commands should share a `_print_report_errors(report)` helper.

---

### 11. `_walk_ring` iteration bound is `len(adj) + 2` — may not be enough

**File:** [`_io/dxf_ingest.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_io/dxf_ingest.py#L280)

```python
for _ in range(len(adj) + 2):  # bounded walk to prevent infinite loops
```

The bound `len(adj)` is the number of **unique vertices**, not edges. A ring can visit at most `len(adj)` vertices, but the `+2` is arbitrary. If a valid ring has exactly `len(adj)` vertices (i.e., the contour touches every vertex), the last step that would close it falls outside the range. The safe bound is `len(adj) + 1`.

More importantly, the algorithm does not protect against **non-manifold vertices** (a vertex with 3+ neighbors). In that case, `_walk_ring` picks the first unvisited neighbor, which may lead into the wrong branch and miss the actual ring — emitting a false `DXF_GAP_001` violation. This is a real-world scenario with shared-edge DXF geometries.

---

## 🟢 Minor / Code Quality

### 12. `NestingStrategy` missing from `api/__init__.py` `__all__`

**File:** [`api/__init__.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/api/__init__.py)

`NestingStrategy` is imported in `api/models.py` and used in `cli.py` and `ga.py`, but it is **not** exported from `squeezenest.api.__init__` — neither the import nor the `__all__` entry. Users doing `from squeezenest.api import NestingStrategy` get an `ImportError`.

---

### 13. `ValidationReport` counts computed by iterating twice

**File:** [`api/models.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/api/models.py#L118-L124)

```python
errors   = sum(1 for v in violations if v.severity == ViolationSeverity.ERROR)
warnings = sum(1 for v in violations if v.severity == ViolationSeverity.WARNING)
```

Two passes over `violations`. For large ingestion jobs this is trivial, but a single-pass `Counter` is cleaner and more idiomatic:

```python
from collections import Counter
counts = Counter(v.severity for v in violations)
errors   = counts[ViolationSeverity.ERROR]
warnings = counts[ViolationSeverity.WARNING]
```

---

### 14. `pareto.py` — O(n²) documented but no guard for large grids

**File:** [`_sensitivity/pareto.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_sensitivity/pareto.py#L25)

The docstring says *"O(n²) — acceptable for the expected grid sizes (≤ 1000 points)"*. With default `SensitivityConfig` of `n_scale_x=6, n_scale_y=6, n_clearance=6`, the grid is 216 points. However, no guard prevents a user from setting `n_scale_x=50, n_scale_y=50, n_clearance=50` (125,000 points → ~15B comparisons). A simple assertion or `logging.warning` at the top of `extract_pareto` for `len(points) > 5000` would save debugging time.

---

### 15. `_core/cache.py` — `NFPCache.__all__` placed before the class it describes, `SQLiteNFPCache` uses a hardcoded `max_size=100` in docstring but the invariant isn't enforced by a guard

**File:** [`_core/cache.py`](file:///Users/ferb/Documents/GoogleAntigravity/SqueezeNest/squeezenest/_core/cache.py#L147)

The docstring says *"Implements an LRU eviction policy with a maximum of 100 entries"*, but `max_size` is a constructor parameter. The docstring is misleading — it should say *"default 100 entries"*.

---

## Summary Table

| # | Severity | File | Issue |
|---|----------|------|-------|
| 1 | 🔴 Critical | `_core/cache.py` | `SQLiteNFPCache`: no thread safety, no `close()` |
| 2 | 🔴 Critical | `_core/cache.py` | `SQLiteNFPCache`/`TieredNFPCache` missing from `__all__` |
| 3 | 🔴 Critical | `_nesting/ga.py` | `__import__` hacks, shallow copy, mutable job shared across iterations |
| 4 | 🔴 Critical | `_nesting/blf.py` | `instance_idx` tracks list-index, not per-part copy number |
| 5 | 🟡 Design | `_nesting/lattice.py` | Imports 5 private BLF symbols; fragile coupling |
| 6 | 🟡 Design | `_sensitivity/sweep.py` | `1_000_000` magic constant should use `SCALE` from `_core/scale` |
| 7 | 🟡 Design | `_nesting/blf.py` | Multi-sheet loop runs but only first sheet returned |
| 8 | 🟡 Design | `_io/dxf_ingest.py` | `frozenset` edge key drops duplicate edges silently |
| 9 | 🟡 Style | `_core/cache.py` | Imports mid-file (PEP 8 / ruff `I` violation) |
| 10 | 🟡 UX | `cli.py` | `sweep` command swallows per-violation error messages |
| 11 | 🟡 Correctness | `_io/dxf_ingest.py` | `_walk_ring` bound off-by-one; non-manifold vertex blind spot |
| 12 | 🟢 Minor | `api/__init__.py` | `NestingStrategy` not exported → `ImportError` for public users |
| 13 | 🟢 Minor | `api/models.py` | Two-pass count; single-pass `Counter` is cleaner |
| 14 | 🟢 Minor | `_sensitivity/pareto.py` | No guard for large grids (O(n²) blowup) |
| 15 | 🟢 Minor | `_core/cache.py` | Misleading docstring on `SQLiteNFPCache.max_size` default |
