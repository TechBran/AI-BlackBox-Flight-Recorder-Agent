"""Binary per-model vector stores — replaces inline-JSON embeddings.

Layout under {base_dir}/{slug}/ (base_dir defaults to config.EMBEDDINGS_STORES_DIR):
  vectors.f32 — raw little-endian float32, row-major N×dims
  ids.json    — ordered snap_id list; row i ↔ vector i
  meta.json   — {slug, dims, normalized: True, count, last_updated} (ISO-8601 UTC)

The active-model pointer lives at {base_dir}/active.json ({"active": slug}).

Invariants:
- Stored rows are L2-normalized at append time, so cosine similarity is a
  single mat-vec (scores = M @ q) — never a python loop over rows.
- ids.json and vectors.f32 are kept consistent by open()'s self-heal:
  whichever is longer is truncated to match the shorter (torn writes only
  ever lose the trailing rows, never corrupt earlier ones).
- One process owns the store (the orchestrator); a threading.Lock guards
  append/search state mutation across its worker threads.
"""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from Orchestrator import config
from Orchestrator.embeddings.registry import EMBEDDING_MODELS

VECTORS_FILE = "vectors.f32"
IDS_FILE = "ids.json"
META_FILE = "meta.json"
ACTIVE_FILE = "active.json"


def _atomic_write_json(path: Path, obj) -> None:
    """Write JSON durably: tmp file + fsync + os.replace (never a torn read)."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class VectorStore:
    """Append-only float32 vector store for one embedding model."""

    def __init__(self, slug: str, dims: int, base_dir):
        self.slug = slug
        self.dims = dims
        self.dir = Path(base_dir) / slug
        self._row_bytes = 4 * dims  # float32
        self._ids: list[str] = []
        self._id_set: set[str] = set()
        self._matrix = None  # lazily loaded; invalidated on append
        self._opened = False
        self._lock = threading.Lock()

    # ── paths ────────────────────────────────────────────────────────────────

    @property
    def vectors_path(self) -> Path:
        return self.dir / VECTORS_FILE

    @property
    def ids_path(self) -> Path:
        return self.dir / IDS_FILE

    @property
    def meta_path(self) -> Path:
        return self.dir / META_FILE

    # ── open / self-heal ─────────────────────────────────────────────────────

    def open(self) -> "VectorStore":
        """Load ids + vector matrix; heal any ids↔rows mismatch to min(...).

        On a nonexistent dir this creates nothing — files appear on first
        append, so probing a store is side-effect free.
        """
        with self._lock:
            self._load_locked()
        return self

    def _load_locked(self) -> None:
        self._matrix = None
        self._ids = []
        if self.ids_path.exists():
            self._ids = json.loads(self.ids_path.read_text(encoding="utf-8"))

        rows = 0
        if self.vectors_path.exists():
            rows = self.vectors_path.stat().st_size // self._row_bytes

        target = min(len(self._ids), rows)
        if len(self._ids) != rows or (
            self.vectors_path.exists()
            and self.vectors_path.stat().st_size != target * self._row_bytes
        ):
            # Self-heal: truncate BOTH sides to the shorter (drops only
            # trailing rows, including any partial row from a torn write).
            print(
                f"[VECSTORE] {self.slug}: healing ids={len(self._ids)} "
                f"rows={rows} -> {target}"
            )
            if self.vectors_path.exists():
                with open(self.vectors_path, "r+b") as f:
                    f.truncate(target * self._row_bytes)
            if len(self._ids) != target:
                self._ids = self._ids[:target]
                _atomic_write_json(self.ids_path, self._ids)
            self._write_meta_locked()

        self._id_set = set(self._ids)
        self._opened = True

    def _ensure_open_locked(self) -> None:
        if not self._opened:
            self._load_locked()

    # ── write path ───────────────────────────────────────────────────────────

    def append(self, snap_id: str, vector) -> None:
        """L2-normalize and append one row; idempotent on snap_id."""
        if len(vector) != self.dims:
            raise ValueError(
                f"{self.slug}: vector has {len(vector)} dims, store expects {self.dims}"
            )
        with self._lock:
            self._ensure_open_locked()
            if snap_id in self._id_set:
                return  # idempotent: first write wins
            vec = np.asarray(vector, dtype=np.float32)
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm

            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.vectors_path, "ab") as f:
                f.write(vec.astype("<f4").tobytes())
                f.flush()
                os.fsync(f.fileno())
            # Vector row is durable before ids.json names it — a crash between
            # the two leaves an orphan row that open() heals away.
            self._ids.append(snap_id)
            self._id_set.add(snap_id)
            _atomic_write_json(self.ids_path, self._ids)
            self._write_meta_locked()
            self._matrix = None  # re-read lazily on next search

    def _write_meta_locked(self) -> None:
        _atomic_write_json(self.meta_path, {
            "slug": self.slug,
            "dims": self.dims,
            "normalized": True,
            "count": len(self._ids),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        })

    # ── read path ────────────────────────────────────────────────────────────

    @property
    def count(self) -> int:
        with self._lock:
            self._ensure_open_locked()
            return len(self._ids)

    def ids(self) -> set:
        with self._lock:
            self._ensure_open_locked()
            return set(self._id_set)

    def missing(self, all_snap_ids) -> list:
        """snap_ids not yet in the store, preserving input order."""
        with self._lock:
            self._ensure_open_locked()
            present = self._id_set
            return [sid for sid in all_snap_ids if sid not in present]

    def _get_matrix_locked(self):
        if self._matrix is None and self.vectors_path.exists():
            self._matrix = np.fromfile(self.vectors_path, dtype="<f4").reshape(
                -1, self.dims
            )
        return self._matrix

    def search(self, query_vec, k: int, allowed_ids=None) -> list:
        """Top-k cosine matches as [(snap_id, score), ...].

        Performance constraint: scoring is ONE numpy mat-vec over the whole
        matrix (rows are pre-normalized), never a python loop over rows.
        """
        with self._lock:
            self._ensure_open_locked()
            matrix = self._get_matrix_locked()
            ids = list(self._ids)
        if matrix is None or matrix.shape[0] == 0:
            return []

        q = np.asarray(query_vec, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if norm > 0:
            q = q / norm
        scores = matrix @ q

        results = []
        for i in np.argsort(scores)[::-1]:
            sid = ids[i]
            if allowed_ids is not None and sid not in allowed_ids:
                continue
            results.append((sid, float(scores[i])))
            if len(results) >= k:
                break
        return results


# ── module-level helpers ─────────────────────────────────────────────────────

def list_stores(base_dir) -> list:
    """[{slug, dims, count, last_updated}] from meta.json files; skips malformed dirs."""
    base = Path(base_dir)
    if not base.is_dir():
        return []
    stores = []
    for child in sorted(base.iterdir()):
        meta_path = child / META_FILE
        if not child.is_dir() or not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            stores.append({
                "slug": meta["slug"],
                "dims": meta["dims"],
                "count": meta["count"],
                "last_updated": meta["last_updated"],
            })
        except (json.JSONDecodeError, KeyError, OSError, UnicodeDecodeError) as e:
            print(f"[VECSTORE] skipping malformed store dir {child}: {e}")
    return stores


def get_active_slug(base_dir=None) -> str:
    """Active model slug from {base_dir}/active.json, else the config default."""
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    try:
        return json.loads((base / ACTIVE_FILE).read_text(encoding="utf-8"))["active"]
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError, KeyError):
        return config.EMBEDDINGS_ACTIVE_DEFAULT


def set_active_slug(slug: str, base_dir=None) -> None:
    """Atomically point active.json at a registry slug (cutover seam)."""
    if slug not in EMBEDDING_MODELS:
        raise ValueError(
            f"unknown embedding model slug {slug!r}; known: {sorted(EMBEDDING_MODELS)}"
        )
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    base.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(base / ACTIVE_FILE, {"active": slug})
