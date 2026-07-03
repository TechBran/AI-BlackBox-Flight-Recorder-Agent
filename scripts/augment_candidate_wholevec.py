#!/usr/bin/env python3
"""Augment the M6f chunk candidate with whole-doc vectors (iteration 2).

Transforms the READ-ONLY candidate at {old-dir}/{slug} (schema 2, chunk-only
groups from the 6d rebuild) into a NEW candidate at {new-dir}/{slug}
(schema 2, explicit) under the iteration-2 group policy:

  * group size == 1  → copied as-is (the one chunk IS the whole text —
    identity chunking; no new embed, no new row);
  * group size  > 1  → the WHOLE clamped snapshot body is embedded once via
    the production provider path (purpose="document" — the provider's M5
    clamp bounds the length, exactly the v1 whole-snapshot embedding) and
    prepended, so the new group is [whole_vec] + old_chunk_vecs with the
    whole-doc vector at ordinal 0. The store's max-cosine collapse then
    scores max(whole, chunks), strictly dominating both v1 whole-doc and
    pure-chunk scoring;
  * snapshots in the live index but NOT in the old candidate (minted after
    the build finished) → embedded fully under the new policy
    (chunk_snapshot + whole-body prepend when multi-chunk).

Bodies are sliced from the live volume exactly as the engine does
(migrate.slice_snapshot_text against the snapshot index byte offsets).

SAFETY: the old candidate is opened RAW (ids.json/ordinals.json/vectors.f32
read directly — never through VectorStore, whose open() could self-heal =
WRITE). Writes go ONLY to {new-dir}; the live service never opens it (a
different base_dir realpath is a different canonical store instance), and
active.json / the live stores are never touched.

RESUMABLE: snapshots already complete in the new store are skipped (and
append_many's whole-group idempotency makes a crash-rerun safe anyway).
Provider failures quarantine their batch for the run (logged; the sids stay
missing in the new store — re-run to retry).

Run (from the repo root; the live service may keep running — this process
only READS the shared files):
    Orchestrator/venv/bin/python scripts/augment_candidate_wholevec.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)  # Orchestrator.config reads config.ini relative to CWD
sys.path.insert(0, str(REPO))

from Orchestrator import config  # noqa: E402
from Orchestrator.embeddings.chunker import chunk_snapshot  # noqa: E402
from Orchestrator.embeddings.migrate import slice_snapshot_text  # noqa: E402
from Orchestrator.embeddings.providers import (  # noqa: E402
    EmbeddingProviderError, get_provider,
)
from Orchestrator.embeddings.registry import EMBEDDING_MODELS  # noqa: E402
from Orchestrator.embeddings.store import get_store  # noqa: E402
from Orchestrator.fossils import load_snapshot_index  # noqa: E402
from Orchestrator.volume import read_volume_bytes  # noqa: E402

EMBED_BATCH = 16       # whole bodies per provider.embed call (blast radius)
BATCH_SLEEP_S = 0.2    # engine pacing between provider calls
FLUSH_ROWS = 512       # staged rows per append_many (one fsync set each)


def read_old_store_raw(old_store_dir: Path, dims: int):
    """(ids, ordinals, matrix) read directly from disk — NEVER via
    VectorStore (open() self-heals = writes; the old candidate is read-only).
    Refuses on any inconsistency instead of healing."""
    ids = json.loads((old_store_dir / "ids.json").read_text(encoding="utf-8"))
    ordinals = json.loads(
        (old_store_dir / "ordinals.json").read_text(encoding="utf-8"))
    meta = json.loads((old_store_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("schema") != 2:
        raise SystemExit(f"old candidate {old_store_dir} is not schema 2")
    if meta.get("dims") != dims:
        raise SystemExit(
            f"old candidate dims {meta.get('dims')} != registry {dims}")
    matrix = np.fromfile(old_store_dir / "vectors.f32", dtype="<f4")
    if matrix.size % dims:
        raise SystemExit("vectors.f32 size is not a multiple of dims — refusing")
    matrix = matrix.reshape(-1, dims)
    if not (len(ids) == len(ordinals) == matrix.shape[0]):
        raise SystemExit(
            f"old candidate inconsistent: ids={len(ids)} "
            f"ordinals={len(ordinals)} rows={matrix.shape[0]} — refusing "
            f"(this script never heals the read-only input)")
    return ids, ordinals, matrix


def groups_in_disk_order(ids: list) -> list:
    """[(snap_id, start_row, size)] — contiguous same-id runs, disk order."""
    out, i = [], 0
    while i < len(ids):
        j = i
        while j < len(ids) and ids[j] == ids[i]:
            j += 1
        out.append((ids[i], i, j - i))
        i = j
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="gemini-embedding-2")
    ap.add_argument("--old-dir", default=str(REPO / "Manifest/embeddings/_build"))
    ap.add_argument("--new-dir", default=str(REPO / "Manifest/embeddings/_build2"))
    ap.add_argument("--embed-batch", type=int, default=EMBED_BATCH)
    args = ap.parse_args(argv)

    slug = args.slug
    dims = EMBEDDING_MODELS[slug]["dims"]
    old_store_dir = Path(args.old_dir) / slug
    t0 = time.time()

    old_ids, _old_ordinals, old_matrix = read_old_store_raw(old_store_dir, dims)
    groups = groups_in_disk_order(old_ids)
    print(f"[augment] old candidate: {len(groups)} snapshots, "
          f"{len(old_ids)} rows at {old_store_dir}")

    # Explicit schema=2 (F1: autodetect on a fresh dir would cement v1).
    new_store = get_store(slug, base_dir=args.new_dir, schema=2)
    done = new_store.ids()
    print(f"[augment] new store: {new_store.snapshots} snapshots already "
          f"present at {Path(args.new_dir) / slug} (resume skips them)")

    index = load_snapshot_index()
    vol_bytes = read_volume_bytes(Path(config.VOL_PATH))
    provider = get_provider(slug)

    stats = {"copied_single": 0, "whole_vec_added": 0, "late_full_embed": 0,
             "skipped_done": 0, "body_missing": 0, "quarantined": 0,
             "embed_calls": 0, "embedded_texts": 0, "embedded_chars": 0}
    quarantined: list[str] = []
    staged_rows: list = []      # [(sid, vec)] whole groups, append_many-ready
    embed_jobs: list = []       # [(sid, body, chunk_rows_np)] awaiting whole-vec

    def flush_staged(force: bool = False) -> None:
        if staged_rows and (force or len(staged_rows) >= FLUSH_ROWS):
            new_store.append_many(staged_rows)
            staged_rows.clear()

    def run_embed_jobs(force: bool = False) -> None:
        """Embed pending whole bodies (one provider call), stage the groups."""
        while embed_jobs and (force or len(embed_jobs) >= args.embed_batch):
            batch = embed_jobs[:args.embed_batch]
            del embed_jobs[:args.embed_batch]
            texts = [body for _sid, body, _rows in batch]
            try:
                vectors = asyncio.run(provider.embed(texts, "document"))
            except EmbeddingProviderError as e:
                sids = [sid for sid, _b, _r in batch]
                print(f"[augment] provider failed after retries — "
                      f"quarantining {len(sids)} snapshot(s) this run: "
                      f"{sids}: {e}")
                quarantined.extend(sids)
                stats["quarantined"] += len(sids)
                time.sleep(BATCH_SLEEP_S)
                continue
            if len(vectors) != len(texts):
                raise SystemExit(
                    f"provider returned {len(vectors)} vectors for "
                    f"{len(texts)} texts — refusing to misalign groups")
            stats["embed_calls"] += 1
            stats["embedded_texts"] += len(texts)
            stats["embedded_chars"] += sum(len(t) for t in texts)
            for (sid, _body, chunk_rows), whole_vec in zip(batch, vectors):
                staged_rows.append((sid, np.asarray(whole_vec, dtype=np.float32)))
                staged_rows.extend((sid, row) for row in chunk_rows)
                stats["whole_vec_added"] += 1
            flush_staged()
            time.sleep(BATCH_SLEEP_S)

    # ── phase A: transform every old-candidate group ─────────────────────────
    for n, (sid, start, size) in enumerate(groups, 1):
        if sid in done:
            stats["skipped_done"] += 1
            continue
        chunk_rows = [old_matrix[start + r] for r in range(size)]
        if size == 1:
            staged_rows.append((sid, chunk_rows[0]))   # identity chunk: as-is
            stats["copied_single"] += 1
            flush_staged()
        else:
            body = slice_snapshot_text(sid, index, vol_bytes)
            if body is None:
                # Should not happen (the rebuild sliced this body); keep the
                # valid chunk vectors rather than dropping the snapshot, and
                # say so loudly — this group stays chunk-only.
                print(f"[augment] WARNING {sid}: body unavailable from the "
                      f"volume — copying chunk-only group (no whole vec)")
                staged_rows.extend((sid, row) for row in chunk_rows)
                stats["body_missing"] += 1
                flush_staged()
            else:
                embed_jobs.append((sid, body, chunk_rows))
                run_embed_jobs()
        if n % 500 == 0 or n == len(groups):
            print(f"[augment] phase A {n}/{len(groups)} groups "
                  f"(whole_vec_added={stats['whole_vec_added']} "
                  f"copied_single={stats['copied_single']} "
                  f"skipped={stats['skipped_done']}) "
                  f"[{time.time() - t0:.0f}s]")
    run_embed_jobs(force=True)
    flush_staged(force=True)

    # ── phase B: late mints (in the index, absent from the old candidate) ────
    old_id_set = set(old_ids)
    late = [sid for sid in sorted(index) if sid not in old_id_set
            and sid not in done and sid not in quarantined]
    print(f"[augment] phase B: {len(late)} late-minted snapshot(s) to embed fully")
    for sid in late:
        body = slice_snapshot_text(sid, index, vol_bytes)
        if body is None:
            print(f"[augment] WARNING {sid}: invalid byte range — skipping")
            stats["body_missing"] += 1
            continue
        chunks = chunk_snapshot(body, model_key=slug)
        if not chunks:
            print(f"[augment] WARNING {sid}: empty body — skipping")
            continue
        texts = ([body] + chunks) if len(chunks) > 1 else chunks
        try:
            vectors = asyncio.run(provider.embed(texts, "document"))
        except EmbeddingProviderError as e:
            print(f"[augment] provider failed for late mint {sid}: {e}")
            quarantined.append(sid)
            stats["quarantined"] += 1
            time.sleep(BATCH_SLEEP_S)
            continue
        stats["embed_calls"] += 1
        stats["embedded_texts"] += len(texts)
        stats["embedded_chars"] += sum(len(t) for t in texts)
        new_store.append_group(sid, vectors)
        stats["late_full_embed"] += 1
        if len(chunks) > 1:
            stats["whole_vec_added"] += 1
        time.sleep(BATCH_SLEEP_S)

    # ── final report ─────────────────────────────────────────────────────────
    print(f"[augment] DONE in {time.time() - t0:.0f}s — new store: "
          f"snapshots={new_store.snapshots} rows={new_store.rows}")
    print(f"[augment] stats: {json.dumps(stats)}")
    if quarantined:
        print(f"[augment] QUARANTINED this run (re-run to retry): {quarantined}")
        return 1
    print(f"[augment] verify: old_rows({len(old_ids)}) + "
          f"whole_vec_added({stats['whole_vec_added']}) vs "
          f"new rows({new_store.rows}) "
          f"[late mints add their own chunk rows on top]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
