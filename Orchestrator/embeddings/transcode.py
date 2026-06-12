"""One-time migration: inline-JSON embeddings → binary VectorStore (layout v2).

The legacy snapshot index (Manifest/snapshot_index.json, ~408MB in production)
stores a 3072-float "embedding" list inline on every embedded entry. This
module moves those vectors into the registry.LEGACY_INLINE_SLUG binary store
and rewrites the index slim (same entries, every "embedding" key removed). It runs
from a startup hook on merge day and must be paranoid: idempotent, atomic,
backup-keeping, and disk-space-gated.

Crash-safety analysis (ordering is deliberate — do not reorder):
  1. Store writes happen BEFORE the index is slimmed. A crash mid-store-write
     leaves the original (fat) index intact; the rerun is idempotent because
     VectorStore.append_many dedupes by snap_id (and open() self-heals any
     torn trailing row), so only the missing vectors are appended.
  2. The backup is written tmp + fsync + os.replace after store writes,
     before the index swap. A crash mid-copy leaves only a *.tmp leftover,
     never a torn .bak, so a .bak that exists is always a complete copy —
     which is what makes the rerun's exists() skip-guard safe (a good backup
     is never overwritten; re-copying would be harmless while the index is
     still fat, but on the post-replace rerun it would clobber the backup
     with the slimmed file).
  3. os.replace is atomic. A crash after it is simply the completed state:
     the rerun finds no "embedding" keys and no-ops.
"""
import json
import os
import shutil
from pathlib import Path

import numpy as np

from Orchestrator import config
from Orchestrator.embeddings.registry import LEGACY_INLINE_SLUG
from Orchestrator.embeddings.store import get_store

# Every inline vector ever written to the index came from the legacy Gemini
# default model — this migration targets the registry's LEGACY_INLINE_SLUG
# store unconditionally (NOT the active slug, which an operator may already
# have pointed elsewhere).
BACKUP_SUFFIX = ".bak.pre-embeddings-v2"
BATCH_SIZE = 500          # bounds peak numpy copy, one fsync set per batch
DISK_HEADROOM = 1.5       # need free >= 1.5x index size before doing anything


def _disk_free(path: Path) -> int:
    """Free bytes on the filesystem holding `path` (seam for tests)."""
    return shutil.disk_usage(path).free


def _fmt_bytes(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}MB"
    if n >= 1_000:
        return f"{n / 1_000:.0f}KB"
    return f"{n}B"


def transcode_inline_embeddings(index_path=None, base_dir=None) -> dict:
    """Move inline index embeddings into the binary store; slim the index.

    Returns one of:
      {"migrated": 0, "dropped": 0, "skipped": True}            — nothing to do
      {"skipped": True, "reason": "disk"}                        — gated, untouched
      {"migrated": N, "dropped": M, "skipped": False,
       "index_bytes_before": B0, "index_bytes_after": B1}        — completed

    "migrated" counts vectors newly appended THIS run (a resumed run after a
    crash reports only the rows it actually added — append_many dedupes the
    rest). "dropped" counts vectors with stale dims (pre-2026 768-dim
    leftovers), non-finite values, or unparseable garbage (non-numeric
    elements); entries whose "embedding" is None/empty were vector-less
    already and count as neither.
    """
    index_path = Path(index_path if index_path is not None else config.SNAPSHOT_INDEX)
    base_dir = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)

    if not index_path.exists():
        print(f"[TRANSCODE] no snapshot index at {index_path} — nothing to do")
        return {"migrated": 0, "dropped": 0, "skipped": True}

    index_bytes_before = index_path.stat().st_size
    with open(index_path, "r", encoding="utf-8") as f:
        index = json.load(f)

    if not any(isinstance(e, dict) and "embedding" in e for e in index.values()):
        print(
            f"[TRANSCODE] index already slim ({len(index)} entries, "
            f"no inline embeddings) — no-op"
        )
        return {"migrated": 0, "dropped": 0, "skipped": True}

    # Disk gate FIRST when work is needed: the slim rewrite needs a tmp copy
    # and the backup needs a full copy; 1.5x the index size is the floor.
    # On insufficient space we touch NOTHING — the system keeps working
    # because search falls back to the inline vectors (Task 5) until a later
    # boot finds enough room.
    free = _disk_free(index_path.parent)
    needed = int(index_bytes_before * DISK_HEADROOM)
    if free < needed:
        print(
            f"[TRANSCODE] insufficient disk: free={_fmt_bytes(free)} "
            f"need>={_fmt_bytes(needed)} — SKIPPING migration (index untouched, "
            f"inline-embedding fallback stays active; free up space and restart)"
        )
        return {"skipped": True, "reason": "disk"}

    # ── 1. vectors → binary store (idempotent by snap_id) ────────────────────
    store = get_store(LEGACY_INLINE_SLUG, base_dir=base_dir)
    migrated = 0
    dropped = 0
    dropped_dims: dict[int, int] = {}
    dropped_nonfinite = 0
    dropped_unparseable = 0
    batch: list[tuple[str, np.ndarray]] = []
    for snap_id, entry in index.items():
        if not isinstance(entry, dict):
            continue
        vec = entry.get("embedding")
        if not vec:
            continue  # None/empty: vector-less already — neither migrated nor dropped
        try:
            arr = np.asarray(vec, dtype=np.float32)
        except (TypeError, ValueError):
            # Non-numeric legacy garbage (string elements, dicts, ragged
            # nests) raising here would abort the migration on EVERY boot —
            # a permanent stall. Drop it like the other stale vectors.
            dropped += 1
            dropped_unparseable += 1
            continue
        if arr.ndim != 1 or arr.shape[0] != store.dims:
            dropped += 1
            dropped_dims[int(arr.size)] = dropped_dims.get(int(arr.size), 0) + 1
            continue
        if not np.isfinite(arr).all():
            # append_many is all-or-nothing per batch; one NaN row must not
            # abort 499 good neighbours. Pre-filter and count it as dropped.
            dropped += 1
            dropped_nonfinite += 1
            continue
        batch.append((snap_id, arr))
        if len(batch) >= BATCH_SIZE:
            migrated += store.append_many(batch)
            batch = []
    if batch:
        migrated += store.append_many(batch)

    if dropped:
        print(
            f"[TRANSCODE] dropped {dropped} stale vectors "
            f"(dims histogram: {dropped_dims}, non-finite: {dropped_nonfinite}, "
            f"unparseable: {dropped_unparseable}) "
            f"— pre-2026 leftovers are stale by design, not migrated"
        )

    # ── 2. backup the original index (never overwrite a good backup) ─────────
    # Written tmp + fsync + os.replace (same pattern as the slim write below)
    # so a torn .bak cannot exist: the .bak appears complete via atomic rename
    # or not at all. That is what makes the exists() skip-guard safe.
    backup_path = index_path.with_name(index_path.name + BACKUP_SUFFIX)
    if backup_path.exists():
        print(f"[TRANSCODE] backup {backup_path.name} already exists — keeping it")
    else:
        backup_tmp = backup_path.with_name(backup_path.name + ".tmp")
        with open(index_path, "rb") as src, open(backup_tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(backup_tmp, backup_path)
        print(f"[TRANSCODE] backed up original index to {backup_path.name}")

    # ── 3. atomic slim-index swap (tmp + fsync + os.replace) ─────────────────
    slim = {
        sid: ({k: v for k, v in e.items() if k != "embedding"}
              if isinstance(e, dict) else e)
        for sid, e in index.items()
    }
    tmp_path = index_path.with_name(index_path.name + ".transcode.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(slim, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, index_path)
    index_bytes_after = index_path.stat().st_size

    print(
        f"[TRANSCODE] migrated={migrated} dropped={dropped} index "
        f"{_fmt_bytes(index_bytes_before)}->{_fmt_bytes(index_bytes_after)} "
        f"({len(index)} entries, {LEGACY_INLINE_SLUG} store now {store.count} rows)"
    )
    return {
        "migrated": migrated,
        "dropped": dropped,
        "skipped": False,
        "index_bytes_before": index_bytes_before,
        "index_bytes_after": index_bytes_after,
    }
