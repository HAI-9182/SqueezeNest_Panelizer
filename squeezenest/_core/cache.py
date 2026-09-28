"""squeezenest._core.cache -- NFP (No-Fit Polygon) two-tier cache.

Tier 1: In-process LRU cache (cachetools.LRUCache) for hot NFP lookups.
Tier 2: SQLite on-disk persistence (planned for v0.2; stub here).

Cache Key design (Invariant I-06: symmetry):
    make_cache_key(A, B, rA, rB, d) == make_cache_key(B, A, rB, rA, d)

    The key is a SHA-256 hex digest of a canonical representation obtained by:
      1. Computing a stable fingerprint for each polygon (SHA-256 of its
         canonically-ordered vertex bytes).
      2. Forming a sorted pair of (fingerprint, rotation) tuples so that
         swapping (A, rA) <-> (B, rB) produces the same ordering.
      3. Hashing (sorted_pair, clearance_int) to produce the final key.

All coordinates are int64 scale units (see squeezenest._core.scale).
"""
from __future__ import annotations

import hashlib
import sqlite3
import struct
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import msgpack

__all__ = ["make_cache_key", "NFPCache", "SQLiteNFPCache", "TieredNFPCache"]

# Type aliases
Polygon = list[tuple[int, int]]


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------

def _poly_fingerprint(poly: Polygon) -> bytes:
    """Stable SHA-256 fingerprint of a polygon's vertex sequence.

    Normalised to the lexicographically-smallest rotation of the vertex list
    so that the same shape in different starting-vertex representations still
    produces the same fingerprint.
    """
    n = len(poly)
    # Canonicalise: choose the rotation that starts at the lexicographically
    # smallest vertex (handles different start-vertex representations).
    min_idx = min(range(n), key=lambda i: poly[i])
    rotated = poly[min_idx:] + poly[:min_idx]
    raw = b"".join(struct.pack("<qq", x, y) for x, y in rotated)
    return hashlib.sha256(raw).digest()


def make_cache_key(
    poly_a: Polygon,
    poly_b: Polygon,
    rot_a_mdeg: int,
    rot_b_mdeg: int,
    clearance_int: int,
) -> str:
    """Generate a symmetric SHA-256 cache key for an NFP entry.

    The key is invariant under swapping (poly_a, rot_a_mdeg) <-> (poly_b, rot_b_mdeg),
    satisfying Invariant I-06.

    Args:
        poly_a:        First polygon (int64 scale units).
        poly_b:        Second polygon (int64 scale units).
        rot_a_mdeg:    Rotation of poly_a in milli-degrees (0 = unrotated).
        rot_b_mdeg:    Rotation of poly_b in milli-degrees.
        clearance_int: Inter-part clearance in int64 scale units.

    Returns:
        Hex-encoded SHA-256 digest string (64 characters).
    """
    fp_a = _poly_fingerprint(poly_a)
    fp_b = _poly_fingerprint(poly_b)

    # Build (fingerprint, rotation) token for each polygon
    token_a = fp_a + struct.pack("<q", rot_a_mdeg)
    token_b = fp_b + struct.pack("<q", rot_b_mdeg)

    # Sort tokens to achieve symmetry invariant I-06
    ordered = sorted([token_a, token_b])

    # Final hash = SHA-256(sorted_token_a || sorted_token_b || clearance)
    h = hashlib.sha256()
    h.update(ordered[0])
    h.update(ordered[1])
    h.update(struct.pack("<q", clearance_int))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# LRU Cache (Tier 1)
# ---------------------------------------------------------------------------

class NFPCache:
    """In-process LRU cache for No-Fit Polygon results.

    Backed by collections.OrderedDict for O(1) get/put/evict.

    Args:
        max_size: Maximum number of NFP entries to hold in memory.
    """

    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._max_size = max_size
        self._store: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str) -> Any | None:
        """Retrieve an NFP by key.  Returns None on cache miss.

        Moves the accessed entry to the end (most-recently-used).
        """
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def put(self, key: str, value: Any) -> None:
        """Store an NFP entry. Evicts the least-recently-used entry on overflow."""
        if key in self._store:
            self._store.move_to_end(key)
        else:
            if len(self._store) >= self._max_size:
                self._store.popitem(last=False)  # evict LRU (front)
        self._store[key] = value

    def __len__(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# SQLite Cache (Tier 2)
# ---------------------------------------------------------------------------

class SQLiteNFPCache:
    """On-disk SQLite cache for No-Fit Polygon results.

    Implements an LRU eviction policy using a ``last_accessed_at`` timestamp.
    The connection is opened with ``check_same_thread=False`` so the cache
    can be passed across threads (callers are responsible for external locking
    in concurrent scenarios).  Always close the cache via :meth:`close` or
    use it as a context manager to release the file handle.

    Args:
        db_path:  Path to the SQLite database file (created if absent).
        max_size: Maximum number of NFP entries to retain (default: 100).
    """

    def __init__(self, db_path: Path, max_size: int = 100) -> None:
        self.db_path = db_path
        self._max_size = max_size
        # check_same_thread=False allows cross-thread use; callers must
        # ensure external serialisation when sharing across threads.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                '''CREATE TABLE IF NOT EXISTS nfp_cache (
                    hash_key TEXT PRIMARY KEY,
                    data BLOB,
                    last_accessed_at REAL
                )'''
            )
            # Index on last_accessed_at for fast eviction queries
            self._conn.execute(
                '''CREATE INDEX IF NOT EXISTS idx_last_accessed
                   ON nfp_cache(last_accessed_at)'''
            )

    def get(self, key: str) -> Any | None:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT data FROM nfp_cache WHERE hash_key = ?", (key,)
        )
        row = cur.fetchone()
        if row is None:
            return None

        # Update last_accessed_at on cache hit
        with self._conn:
            self._conn.execute(
                "UPDATE nfp_cache SET last_accessed_at = ? WHERE hash_key = ?",
                (time.time(), key)
            )

        return msgpack.unpackb(row[0])

    def put(self, key: str, value: Any) -> None:
        packed = msgpack.packb(value)
        with self._conn:
            # Upsert
            self._conn.execute(
                '''INSERT INTO nfp_cache (hash_key, data, last_accessed_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(hash_key) DO UPDATE SET
                   data=excluded.data, last_accessed_at=excluded.last_accessed_at''',
                (key, packed, time.time())
            )
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        """Enforce the LRU limit by deleting the oldest records if exceeding max_size."""
        with self._conn:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) FROM nfp_cache")
            count = cur.fetchone()[0]

            if count > self._max_size:
                excess = count - self._max_size
                self._conn.execute(
                    '''DELETE FROM nfp_cache
                       WHERE hash_key IN (
                           SELECT hash_key FROM nfp_cache
                           ORDER BY last_accessed_at ASC
                           LIMIT ?
                       )''', (excess,)
                )

    def __len__(self) -> int:
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM nfp_cache")
        return cur.fetchone()[0]

    def close(self) -> None:
        """Close the underlying SQLite connection and release the file handle."""
        self._conn.close()

    def __enter__(self) -> "SQLiteNFPCache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Tiered Cache (Tier 1 + Tier 2)
# ---------------------------------------------------------------------------

class TieredNFPCache:
    """Wrapper that combines Tier 1 (in-process LRU) and Tier 2 (SQLite) caches.

    A cache hit in Tier 1 avoids the SQLite round-trip.  A hit in Tier 2
    back-fills Tier 1 so subsequent lookups are served from memory.

    Args:
        mem_size: Maximum entries for the in-process LRU cache (Tier 1).
        db_path:  Path to the SQLite database file (Tier 2).
        db_size:  Maximum entries for the SQLite cache (Tier 2, default: 100).
    """

    def __init__(self, mem_size: int, db_path: Path, db_size: int = 100) -> None:
        self.t1 = NFPCache(mem_size)
        self.t2 = SQLiteNFPCache(db_path, max_size=db_size)

    def get(self, key: str) -> Any | None:
        val = self.t1.get(key)
        if val is not None:
            return val

        val = self.t2.get(key)
        if val is not None:
            self.t1.put(key, val)
        return val

    def put(self, key: str, value: Any) -> None:
        self.t1.put(key, value)
        self.t2.put(key, value)

    def close(self) -> None:
        """Close the Tier 2 SQLite connection."""
        self.t2.close()

    def __enter__(self) -> "TieredNFPCache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
