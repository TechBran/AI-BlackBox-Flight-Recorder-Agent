"""Binary per-model vector stores — replaces inline-JSON embeddings.

Layout under {base_dir}/{slug}/ (get_store() defaults base_dir to
config.EMBEDDINGS_STORES_DIR):
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
    """Append-only float32 vector store for one embedding model.

    One live instance per (base_dir, slug) — always obtain stores via
    get_store(); constructing VectorStore directly is for tests only.
    """

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
        # Dims guard MUST precede self-heal: healing across a dims mismatch
        # reinterprets row boundaries (rows = bytes // wrong_row_bytes),
        # rewrites meta to the wrong dims, and silently corrupts searches.
        if self.meta_path.exists():
            try:
                stored_dims = json.loads(
                    self.meta_path.read_text(encoding="utf-8")
                )["dims"]
            except (json.JSONDecodeError, KeyError, TypeError, OSError,
                    UnicodeDecodeError):
                stored_dims = None  # unreadable meta: heal below rebuilds it
            if stored_dims is not None and stored_dims != self.dims:
                raise ValueError(
                    f"{self.slug}: store dims {stored_dims} != requested {self.dims}"
                )

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
        self.append_many([(snap_id, vector)])

    def append_many(self, items: list[tuple[str, "np.ndarray | list"]]) -> int:
        """Append a batch of (snap_id, vector) rows; returns rows written.

        Validation is all-or-nothing: every item is checked (dims, finiteness)
        BEFORE any byte is written, so one bad item aborts the whole batch.
        Duplicate snap_ids (against the store or earlier in the batch — first
        wins) are skipped silently, matching append()'s idempotency. The whole
        batch costs ONE fsync set: one vectors.f32 append, one ids.json
        rewrite, one meta rewrite, one matrix invalidation.
        """
        prepared = []
        for snap_id, vector in items:
            vec = np.asarray(vector, dtype=np.float32)
            if vec.shape != (self.dims,):
                raise ValueError(
                    f"{self.slug}: vector has {len(vector)} dims, "
                    f"store expects {self.dims}"
                )
            # NaN/Inf would rank #1 in every search forever (NaN compares
            # poison argsort) — reject before normalize.
            if not np.isfinite(vec).all():
                raise ValueError(f"{snap_id}: non-finite vector")
            prepared.append((snap_id, vec))

        with self._lock:
            self._ensure_open_locked()
            new_ids: list[str] = []
            new_rows: list[bytes] = []
            seen: set[str] = set()
            for snap_id, vec in prepared:
                if snap_id in self._id_set or snap_id in seen:
                    continue  # idempotent: first write wins
                seen.add(snap_id)
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    vec = vec / norm
                new_ids.append(snap_id)
                new_rows.append(vec.astype("<f4").tobytes())
            if not new_ids:
                return 0

            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.vectors_path, "ab") as f:
                f.write(b"".join(new_rows))
                f.flush()
                os.fsync(f.fileno())
            # Vector rows are durable before ids.json names them — a crash
            # between the two leaves orphan rows that open() heals away.
            self._ids.extend(new_ids)
            self._id_set.update(new_ids)
            _atomic_write_json(self.ids_path, self._ids)
            self._write_meta_locked()
            self._matrix = None  # re-read lazily on next search
            return len(new_ids)

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

_STORES: dict[tuple[str, str], VectorStore] = {}
_STORES_LOCK = threading.Lock()


def get_store(slug: str, dims: int = None, base_dir=None) -> VectorStore:
    """Canonical-instance factory: ONE live VectorStore per (base_dir, slug).

    Two instances on the same directory would race each other's files, so all
    production code must come through here. dims defaults from
    EMBEDDING_MODELS[slug]; base_dir defaults to config.EMBEDDINGS_STORES_DIR.
    The key uses the realpath of base_dir so aliased paths share an instance.
    """
    if dims is None:
        try:
            dims = EMBEDDING_MODELS[slug]["dims"]
        except KeyError:
            raise ValueError(
                f"unknown embedding model slug {slug!r}; "
                f"known: {sorted(EMBEDDING_MODELS)}"
            ) from None
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    key = (os.path.realpath(base), slug)
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            # open() before caching: a dims-mismatch refusal must not leave a
            # poisoned entry behind.
            store = VectorStore(slug, dims, base).open()
            _STORES[key] = store
        return store


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


# ── Per-box keep_alive override (local Ollama models) ────────────────────────
# How long Ollama keeps a model resident after the last embed. WARM pins it in
# RAM for instant embeds; COLD frees the RAM and reloads on demand (the first
# embed after idle pays the cold-load cost). The registry ships a conservative
# default per model (small boxes can't afford to pin a 6GB model); this is the
# per-box override the wizard toggle writes, stored as runtime state in Manifest
# (NOT config.ini, which code never rewrites). Only meaningful for ollama models.
KEEP_ALIVE_FILE = "keep_alive.json"
KEEP_ALIVE_WARM = "-1m"   # negative duration = stay resident indefinitely
KEEP_ALIVE_COLD = "5m"    # unload 5 minutes after the last embed


def get_keep_alive(slug: str, base_dir=None, fallback=None) -> "str | None":
    """Effective keep_alive for slug: per-box override, else the registry
    default, else `fallback` (for synthetic entries not in the registry)."""
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    try:
        overrides = json.loads((base / KEEP_ALIVE_FILE).read_text(encoding="utf-8"))
        if isinstance(overrides, dict) and slug in overrides:
            return overrides[slug]
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        pass
    entry = EMBEDDING_MODELS.get(slug)
    if entry is not None:
        return entry.get("keep_alive")
    return fallback


def set_keep_alive(slug: str, warm: bool, base_dir=None) -> str:
    """Write the per-box keep_alive override for a LOCAL model; returns the
    value written. WARM → resident forever; cold → idle-unload."""
    entry = EMBEDDING_MODELS.get(slug)
    if entry is None:
        raise ValueError(f"unknown embedding model slug {slug!r}")
    if entry["provider"] != "ollama":
        raise ValueError(f"{slug!r} is not a local model; keep_alive is Ollama-only")
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    base.mkdir(parents=True, exist_ok=True)
    path = base / KEEP_ALIVE_FILE
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            overrides = {}
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        overrides = {}
    value = KEEP_ALIVE_WARM if warm else KEEP_ALIVE_COLD
    overrides[slug] = value
    _atomic_write_json(path, overrides)
    return value


def is_warm(value) -> bool:
    """True when a keep_alive value means 'stay resident' (negative duration)."""
    return isinstance(value, str) and value.strip().startswith("-")
