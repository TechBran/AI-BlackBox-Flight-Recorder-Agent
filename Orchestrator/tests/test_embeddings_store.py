"""Pluggable embeddings — binary VectorStore + active pointer (Task 2).

Per docs/plans/2026-06-11-pluggable-embeddings.md Task 2: per-model stores
under {base_dir}/{slug}/ holding raw little-endian float32 vectors with an
ordered id list, self-healing on open, plus the active-model pointer file.
All tests run against tmp_path — never the real Manifest/.
"""
import json

import numpy as np
import pytest

from Orchestrator import config
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import (
    VectorStore,
    get_active_slug,
    list_stores,
    set_active_slug,
)

DIMS = 4
ROW_BYTES = 4 * DIMS  # float32


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _make_store(tmp_path, slug="unit-test-model", dims=DIMS):
    return VectorStore(slug, dims, tmp_path).open()


THREE_VECTORS = {
    "snap-a": [1.0, 0.0, 0.0, 0.0],
    "snap-b": [0.0, 1.0, 0.0, 0.0],
    "snap-c": [1.0, 1.0, 0.0, 0.0],
}
QUERY = [1.0, 0.1, 0.0, 0.0]


def _populated_store(tmp_path):
    store = _make_store(tmp_path)
    for sid, vec in THREE_VECTORS.items():
        store.append(sid, vec)
    return store


# ── append / search roundtrip ────────────────────────────────────────────────

def test_append_search_roundtrip(tmp_path):
    store = _populated_store(tmp_path)
    assert store.count == 3

    results = store.search(QUERY, k=3)
    assert [sid for sid, _ in results] == ["snap-a", "snap-c", "snap-b"]

    q = _unit(QUERY)
    for sid, score in results:
        expected = float(_unit(THREE_VECTORS[sid]) @ q)
        assert score == pytest.approx(expected, abs=1e-5)


def test_search_k_limits_results(tmp_path):
    store = _populated_store(tmp_path)
    results = store.search(QUERY, k=2)
    assert [sid for sid, _ in results] == ["snap-a", "snap-c"]


def test_stored_rows_are_unit_norm(tmp_path):
    store = _make_store(tmp_path)
    store.append("snap-x", [3.0, 4.0, 0.0, 0.0])
    raw = np.fromfile(tmp_path / "unit-test-model" / "vectors.f32", dtype="<f4")
    assert raw.shape == (DIMS,)
    assert float(np.linalg.norm(raw)) == pytest.approx(1.0, abs=1e-6)


def test_dims_mismatch_raises(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(ValueError):
        store.append("snap-bad", [1.0, 2.0, 3.0])  # 3 != DIMS


def test_idempotent_reappend(tmp_path):
    store = _make_store(tmp_path)
    store.append("snap-a", [1.0, 0.0, 0.0, 0.0])
    store.append("snap-a", [0.0, 1.0, 0.0, 0.0])  # silently skipped
    assert store.count == 1
    vec_file = tmp_path / "unit-test-model" / "vectors.f32"
    assert vec_file.stat().st_size == ROW_BYTES  # vector not duplicated
    results = store.search([1.0, 0.0, 0.0, 0.0], k=1)
    assert results[0][0] == "snap-a"
    assert results[0][1] == pytest.approx(1.0, abs=1e-5)  # first vector kept


# ── self-heal on open ────────────────────────────────────────────────────────

def test_self_heal_ids_fewer_than_rows(tmp_path):
    _populated_store(tmp_path)
    store_dir = tmp_path / "unit-test-model"
    ids = json.loads((store_dir / "ids.json").read_text())
    (store_dir / "ids.json").write_text(json.dumps(ids[:2]))  # hand-truncate ids

    healed = _make_store(tmp_path)
    assert healed.count == 2
    assert (store_dir / "vectors.f32").stat().st_size == 2 * ROW_BYTES
    assert healed.ids() == {"snap-a", "snap-b"}


def test_self_heal_vectors_truncated_mid_row(tmp_path):
    _populated_store(tmp_path)
    store_dir = tmp_path / "unit-test-model"
    with open(store_dir / "vectors.f32", "r+b") as f:
        f.truncate(2 * ROW_BYTES + ROW_BYTES // 2)  # torn write: 2.5 rows

    healed = _make_store(tmp_path)
    assert healed.count == 2
    assert (store_dir / "vectors.f32").stat().st_size == 2 * ROW_BYTES
    assert json.loads((store_dir / "ids.json").read_text()) == ["snap-a", "snap-b"]
    # store still searchable after heal
    results = healed.search(QUERY, k=3)
    assert [sid for sid, _ in results] == ["snap-a", "snap-b"]


# ── ids / missing ────────────────────────────────────────────────────────────

def test_missing_preserves_input_order(tmp_path):
    store = _make_store(tmp_path)
    store.append("snap-b", [0.0, 1.0, 0.0, 0.0])
    assert store.missing(["snap-c", "snap-a", "snap-b", "snap-d"]) == [
        "snap-c", "snap-a", "snap-d",
    ]


def test_ids_returns_set(tmp_path):
    store = _populated_store(tmp_path)
    assert store.ids() == {"snap-a", "snap-b", "snap-c"}


# ── allowed_ids filter ───────────────────────────────────────────────────────

def test_search_allowed_ids_filter(tmp_path):
    store = _populated_store(tmp_path)
    results = store.search(QUERY, k=3, allowed_ids={"snap-b"})
    assert results == [(("snap-b"), pytest.approx(float(_unit(THREE_VECTORS["snap-b"]) @ _unit(QUERY)), abs=1e-5))]


# ── empty store ──────────────────────────────────────────────────────────────

def test_empty_store_search_returns_empty(tmp_path):
    store = _make_store(tmp_path)
    assert store.count == 0
    assert store.ids() == set()
    assert store.search(QUERY, k=5) == []
    # open() on a nonexistent dir creates nothing until first append
    assert not (tmp_path / "unit-test-model").exists()


# ── active pointer ───────────────────────────────────────────────────────────

def test_active_slug_roundtrip(tmp_path):
    set_active_slug("qwen3-embedding-0.6b", base_dir=tmp_path)
    assert get_active_slug(base_dir=tmp_path) == "qwen3-embedding-0.6b"
    assert json.loads((tmp_path / "active.json").read_text()) == {
        "active": "qwen3-embedding-0.6b",
    }


def test_active_slug_absent_file_falls_back_to_default(tmp_path):
    assert get_active_slug(base_dir=tmp_path / "nonexistent") == config.EMBEDDINGS_ACTIVE_DEFAULT
    assert config.EMBEDDINGS_ACTIVE_DEFAULT in EMBEDDING_MODELS


def test_set_active_slug_rejects_unknown(tmp_path):
    with pytest.raises(ValueError):
        set_active_slug("not-a-registered-model", base_dir=tmp_path)
    assert not (tmp_path / "active.json").exists()


# ── list_stores ──────────────────────────────────────────────────────────────

def test_list_stores_reads_metas_and_skips_malformed(tmp_path):
    store_a = VectorStore("model-a", 4, tmp_path).open()
    store_a.append("snap-1", [1.0, 0.0, 0.0, 0.0])
    store_a.append("snap-2", [0.0, 1.0, 0.0, 0.0])
    store_b = VectorStore("model-b", 2, tmp_path).open()
    store_b.append("snap-1", [1.0, 0.0])

    # malformed: meta.json is not JSON
    junk = tmp_path / "junk-store"
    junk.mkdir()
    (junk / "meta.json").write_text("{not json")
    # dir without meta.json is skipped too
    (tmp_path / "no-meta").mkdir()

    stores = {s["slug"]: s for s in list_stores(tmp_path)}
    assert set(stores) == {"model-a", "model-b"}
    assert stores["model-a"]["dims"] == 4
    assert stores["model-a"]["count"] == 2
    assert stores["model-b"]["count"] == 1
    assert stores["model-a"]["last_updated"]  # ISO timestamp present


def test_list_stores_empty_base_dir(tmp_path):
    assert list_stores(tmp_path / "nonexistent") == []
