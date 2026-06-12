"""Pluggable embeddings — one-time inline-JSON → binary store transcode (Task 4).

Per docs/plans/2026-06-11-pluggable-embeddings.md Task 4: the production
408MB Manifest/snapshot_index.json carries 3072-float inline "embedding"
lists; transcode_inline_embeddings() moves them into the gemini-embedding-001
VectorStore and rewrites a slim index. The migration must be idempotent,
atomic, backup-keeping, and disk-space-gated.

ALL tests run against tmp_path fixtures — never the real Manifest/.
"""
import json
import logging
import os
from pathlib import Path

import numpy as np
import pytest

from Orchestrator import config
from Orchestrator.embeddings import transcode as transcode_mod
from Orchestrator.embeddings.store import get_store
from Orchestrator.embeddings.transcode import transcode_inline_embeddings
from Orchestrator.monitoring import cosine_similarity

DIMS = 3072
SLUG = "gemini-embedding-001"
BACKUP_NAME = "snapshot_index.json.bak.pre-embeddings-v2"


# ── fixture builders ─────────────────────────────────────────────────────────

def _rng():
    return np.random.default_rng(42)  # fixed seed: parity ordering must be stable


def _entry(i, embedding):
    return {
        "byte_start": 1000 * i,
        "byte_end": 1000 * i + 999,
        "operator": "Brandon" if i % 2 == 0 else "system",
        "timestamp": f"2026-01-{i + 1:02d}T00:00:00Z",
        "type": "normal",
        "embedding": embedding,
    }


def _build_index(n=10, rng=None):
    """n entries across 2 operators with random 3072-dim inline vectors."""
    rng = rng or _rng()
    return {
        f"SNAP-20260101-{i:04d}": _entry(i, [float(x) for x in rng.standard_normal(DIMS)])
        for i in range(n)
    }


def _write_index(tmp_path, index):
    index_path = tmp_path / "snapshot_index.json"
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index_path


def _old_path_topk(index, query, k=5):
    """Replicates monitoring.semantic_search's legacy index loop: pure-python
    cosine over every inline embedding, sorted desc, top-k."""
    scored = []
    for sid, entry in index.items():
        emb = entry.get("embedding")
        if not emb:
            continue
        scored.append((sid, cosine_similarity(query, emb)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


# ── golden parity ────────────────────────────────────────────────────────────

def test_golden_parity_old_cosine_vs_store_search(tmp_path):
    rng = _rng()
    index = _build_index(rng=rng)
    query = [float(x) for x in rng.standard_normal(DIMS)]
    index_path = _write_index(tmp_path, index)
    base_dir = tmp_path / "embeddings"

    old_top5 = _old_path_topk(index, query, k=5)  # BEFORE transcode

    result = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)
    assert result["skipped"] is False
    assert result["migrated"] == 10

    new_top5 = get_store(SLUG, base_dir=base_dir).search(query, k=5)
    assert [sid for sid, _ in new_top5] == [sid for sid, _ in old_top5]
    for (_, new_score), (_, old_score) in zip(new_top5, old_top5):
        assert new_score == pytest.approx(old_score, abs=1e-5)


# ── idempotency ──────────────────────────────────────────────────────────────

def test_second_run_is_noop(tmp_path):
    index_path = _write_index(tmp_path, _build_index())
    base_dir = tmp_path / "embeddings"

    first = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)
    assert first["skipped"] is False

    backup = tmp_path / BACKUP_NAME
    backup_bytes = backup.read_bytes()
    backup_mtime = backup.stat().st_mtime_ns
    slim_bytes = index_path.read_bytes()
    store = get_store(SLUG, base_dir=base_dir)
    count_after_first = store.count

    second = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)
    assert second["skipped"] is True
    assert second["migrated"] == 0
    assert second["dropped"] == 0
    assert store.count == count_after_first
    assert backup.read_bytes() == backup_bytes
    assert backup.stat().st_mtime_ns == backup_mtime
    assert index_path.read_bytes() == slim_bytes


def test_already_slim_index_is_noop(tmp_path):
    index = _build_index(n=3)
    for entry in index.values():
        del entry["embedding"]
    index_path = _write_index(tmp_path, index)
    original = index_path.read_bytes()
    base_dir = tmp_path / "embeddings"

    result = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)
    assert result == {"migrated": 0, "dropped": 0, "skipped": True}
    assert index_path.read_bytes() == original
    assert not (tmp_path / BACKUP_NAME).exists()
    assert not base_dir.exists()


def test_missing_index_is_noop(tmp_path):
    result = transcode_inline_embeddings(
        index_path=tmp_path / "snapshot_index.json",
        base_dir=tmp_path / "embeddings",
    )
    assert result["skipped"] is True
    assert result["migrated"] == 0
    assert not (tmp_path / "embeddings").exists()


# ── backup ───────────────────────────────────────────────────────────────────

def test_backup_contains_original_bytes(tmp_path):
    index_path = _write_index(tmp_path, _build_index())
    original = index_path.read_bytes()
    base_dir = tmp_path / "embeddings"

    transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)

    backup = tmp_path / BACKUP_NAME
    assert backup.exists()
    assert backup.read_bytes() == original


def test_backup_write_is_atomic_crash_never_leaves_torn_backup(tmp_path, monkeypatch):
    """A crash while finalizing the backup must leave the fat index untouched
    and NO .bak at all (at most a *.tmp leftover). A torn .bak would be
    preserved forever by the rerun's exists() skip-guard and then trusted as
    the only copy of the original after the slim swap. Inject one failure into
    the backup's os.replace, then rerun clean: byte-complete backup required."""
    index = _build_index()
    index_path = _write_index(tmp_path, index)
    original = index_path.read_bytes()
    base_dir = tmp_path / "embeddings"
    backup = tmp_path / BACKUP_NAME

    real_replace = os.replace
    failed = {"once": False}

    def replace_failing_once_for_backup(src, dst, *args, **kwargs):
        if not failed["once"] and Path(dst) == backup:
            failed["once"] = True
            raise OSError("simulated crash finalizing backup")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(transcode_mod.os, "replace", replace_failing_once_for_backup)

    with pytest.raises(OSError, match="simulated crash"):
        transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)

    assert index_path.read_bytes() == original  # fat index untouched
    assert not backup.exists()  # never a torn .bak — atomic rename or nothing

    # rerun without the failure: completes and the backup is byte-complete
    result = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)
    assert result["skipped"] is False
    assert result["migrated"] == 0  # store writes from run 1 already landed
    assert backup.read_bytes() == original
    assert not backup.with_name(backup.name + ".tmp").exists()  # tmp consumed
    assert get_store(SLUG, base_dir=base_dir).count == 10


# ── slim index ───────────────────────────────────────────────────────────────

def test_slim_index_strips_embeddings_keeps_fields(tmp_path):
    index = _build_index()
    index_path = _write_index(tmp_path, index)
    bytes_before = index_path.stat().st_size
    base_dir = tmp_path / "embeddings"

    result = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)

    slim = json.loads(index_path.read_text(encoding="utf-8"))
    assert set(slim) == set(index)
    for sid, entry in index.items():
        assert "embedding" not in slim[sid]
        assert slim[sid] == {k: v for k, v in entry.items() if k != "embedding"}

    bytes_after = index_path.stat().st_size
    assert bytes_after < bytes_before / 10  # dramatically smaller
    assert result["index_bytes_before"] == bytes_before
    assert result["index_bytes_after"] == bytes_after


# ── dropped / vector-less entries ────────────────────────────────────────────

def test_wrong_dims_dropped_none_and_empty_not_counted(tmp_path):
    rng = _rng()
    index = _build_index(n=8, rng=rng)
    index["SNAP-STALE-0768"] = _entry(90, [float(x) for x in rng.standard_normal(768)])
    index["SNAP-NONE"] = _entry(91, None)
    index["SNAP-EMPTY"] = _entry(92, [])
    index_path = _write_index(tmp_path, index)
    base_dir = tmp_path / "embeddings"

    result = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)
    assert result["migrated"] == 8
    assert result["dropped"] == 1  # only the 768-dim leftover

    store = get_store(SLUG, base_dir=base_dir)
    assert store.count == 8
    assert not {"SNAP-STALE-0768", "SNAP-NONE", "SNAP-EMPTY"} & store.ids()

    # all 11 entries survive in the slim index, none with an embedding key
    slim = json.loads(index_path.read_text(encoding="utf-8"))
    assert set(slim) == set(index)
    assert all("embedding" not in e for e in slim.values())


def test_garbage_vectors_dropped_as_unparseable(tmp_path, capsys):
    """Non-numeric legacy garbage (string elements, dicts) used to make
    np.asarray raise and abort the migration on EVERY boot. It must be dropped
    (counted as 'unparseable') and the run must complete with the clean rows."""
    rng = _rng()
    index = _build_index(n=10, rng=rng)
    index["SNAP-GARBAGE-STRINGS"] = _entry(93, ["not", "a", "vector"])
    index["SNAP-GARBAGE-DICT"] = _entry(94, {"weird": "dict"})
    index_path = _write_index(tmp_path, index)
    base_dir = tmp_path / "embeddings"

    result = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)
    assert result["skipped"] is False
    assert result["migrated"] == 10
    assert result["dropped"] == 2

    store = get_store(SLUG, base_dir=base_dir)
    assert store.count == 10
    assert not {"SNAP-GARBAGE-STRINGS", "SNAP-GARBAGE-DICT"} & store.ids()
    assert "unparseable: 2" in capsys.readouterr().out

    # all 12 entries survive in the slim index, none with an embedding key
    slim = json.loads(index_path.read_text(encoding="utf-8"))
    assert set(slim) == set(index)
    assert all("embedding" not in e for e in slim.values())


# ── batch boundaries ─────────────────────────────────────────────────────────

def test_batch_boundary_mid_loop_flushes(tmp_path, monkeypatch):
    """BATCH_SIZE=4 over the 10-row fixture: two mid-loop flushes + a 2-row
    remainder — the same mid-loop flush branch production hits 13 times at
    BATCH_SIZE=500. Count, index order and search parity must survive the
    multiple append_many calls."""
    rng = _rng()
    index = _build_index(rng=rng)
    query = [float(x) for x in rng.standard_normal(DIMS)]
    index_path = _write_index(tmp_path, index)
    base_dir = tmp_path / "embeddings"

    monkeypatch.setattr(transcode_mod, "BATCH_SIZE", 4)
    old_top5 = _old_path_topk(index, query, k=5)  # BEFORE transcode

    result = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)
    assert result["skipped"] is False
    assert result["migrated"] == 10
    assert result["dropped"] == 0

    store = get_store(SLUG, base_dir=base_dir)
    assert store.count == 10
    # rows land in index order across all three flushes (ids.json is the
    # store's ordered row list: row i <-> vector i)
    ids_on_disk = json.loads((base_dir / SLUG / "ids.json").read_text(encoding="utf-8"))
    assert ids_on_disk == list(index)

    new_top5 = store.search(query, k=5)
    assert [sid for sid, _ in new_top5] == [sid for sid, _ in old_top5]
    for (_, new_score), (_, old_score) in zip(new_top5, old_top5):
        assert new_score == pytest.approx(old_score, abs=1e-5)


# ── disk gate ────────────────────────────────────────────────────────────────

def test_disk_gate_skips_without_touching_anything(tmp_path, monkeypatch, capsys):
    index_path = _write_index(tmp_path, _build_index())
    original = index_path.read_bytes()
    base_dir = tmp_path / "embeddings"

    monkeypatch.setattr(transcode_mod, "_disk_free", lambda path: 10)  # ~no space

    result = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)
    assert result == {"skipped": True, "reason": "disk"}
    assert index_path.read_bytes() == original  # index untouched
    assert not (tmp_path / BACKUP_NAME).exists()  # no backup
    assert not base_dir.exists()  # no store created
    assert "insufficient disk" in capsys.readouterr().out


# ── partial-run resume ───────────────────────────────────────────────────────

def test_partial_run_resume_migrates_only_missing(tmp_path):
    """Simulate a crash mid-store-write on a previous run: 4 of 10 vectors
    already in the store and a good backup already on disk. The rerun must
    append only the missing 6 ('migrated' = newly appended THIS run) and must
    never clobber the existing backup with the current (about-to-be-slimmed)
    index."""
    index = _build_index()
    index_path = _write_index(tmp_path, index)
    base_dir = tmp_path / "embeddings"
    ids = list(index)

    store = get_store(SLUG, base_dir=base_dir)
    store.append_many([(sid, index[sid]["embedding"]) for sid in ids[:4]])

    backup = tmp_path / BACKUP_NAME
    backup.write_bytes(b"SENTINEL-ORIGINAL-BYTES")  # backup from the crashed run

    result = transcode_inline_embeddings(index_path=index_path, base_dir=base_dir)
    assert result["skipped"] is False
    assert result["migrated"] == 6  # only the missing rows count
    assert result["dropped"] == 0
    assert store.count == 10
    assert store.ids() == set(ids)
    assert backup.read_bytes() == b"SENTINEL-ORIGINAL-BYTES"  # never overwritten


# ── startup hook ─────────────────────────────────────────────────────────────

def test_startup_hook_broken_index_logs_and_does_not_raise(tmp_path, monkeypatch, caplog):
    broken = tmp_path / "snapshot_index.json"
    broken.write_text("{this is not json", encoding="utf-8")
    monkeypatch.setattr(config, "SNAPSHOT_INDEX", broken)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(tmp_path / "embeddings"))

    from Orchestrator.startup import startup_embeddings_transcode

    with caplog.at_level(logging.DEBUG, logger="blackbox.startup"):
        startup_embeddings_transcode()  # must not raise

    assert any("TRANSCODE" in r.getMessage() for r in caplog.records)
    assert not (tmp_path / "embeddings").exists()
