"""Pluggable embeddings — binary VectorStore + active pointer (Task 2).

Per docs/plans/2026-06-11-pluggable-embeddings.md Task 2: per-model stores
under {base_dir}/{slug}/ holding raw little-endian float32 vectors with an
ordered id list, self-healing on open, plus the active-model pointer file.
All tests run against tmp_path — never the real Manifest/.
"""
import json
import threading

import numpy as np
import pytest

from Orchestrator import config
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import (
    VectorStore,
    get_active_slug,
    get_store,
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


# ── dims guard on open (C1) ──────────────────────────────────────────────────

def test_reopen_with_wrong_dims_raises_and_leaves_files_untouched(tmp_path):
    _populated_store(tmp_path)  # written with dims=4
    store_dir = tmp_path / "unit-test-model"
    vectors_before = (store_dir / "vectors.f32").read_bytes()
    ids_before = (store_dir / "ids.json").read_bytes()
    meta_before = (store_dir / "meta.json").read_bytes()

    # Reopening with the wrong dims must refuse — the self-heal would
    # otherwise reinterpret row boundaries and silently corrupt the store.
    with pytest.raises(ValueError):
        VectorStore("unit-test-model", 8, tmp_path).open()

    assert (store_dir / "vectors.f32").read_bytes() == vectors_before
    assert (store_dir / "ids.json").read_bytes() == ids_before
    assert (store_dir / "meta.json").read_bytes() == meta_before


# ── non-finite vectors rejected (I1) ─────────────────────────────────────────

def test_append_nonfinite_vector_raises_store_unchanged(tmp_path):
    store = _make_store(tmp_path)
    store.append("snap-a", [1.0, 0.0, 0.0, 0.0])
    with pytest.raises(ValueError):
        store.append("snap-nan", [float("nan"), 0.0, 0.0, 0.0])
    with pytest.raises(ValueError):
        store.append("snap-inf", [0.0, float("inf"), 0.0, 0.0])
    assert store.count == 1
    assert store.ids() == {"snap-a"}
    vec_file = tmp_path / "unit-test-model" / "vectors.f32"
    assert vec_file.stat().st_size == ROW_BYTES


# ── append_many (I2) ─────────────────────────────────────────────────────────

def test_append_many_in_order_and_searchable(tmp_path):
    store = _make_store(tmp_path)
    appended = store.append_many(list(THREE_VECTORS.items()))
    assert appended == 3
    assert store.count == 3
    # rows land in input order
    assert json.loads(
        (tmp_path / "unit-test-model" / "ids.json").read_text()
    ) == list(THREE_VECTORS)
    results = store.search(QUERY, k=3)
    assert [sid for sid, _ in results] == ["snap-a", "snap-c", "snap-b"]


def test_append_many_skips_existing_and_intra_batch_duplicates(tmp_path):
    store = _make_store(tmp_path)
    store.append("snap-a", THREE_VECTORS["snap-a"])
    appended = store.append_many([
        ("snap-a", [0.0, 0.0, 1.0, 0.0]),        # dup of existing — skipped
        ("snap-b", THREE_VECTORS["snap-b"]),
        ("snap-b", [0.0, 0.0, 0.0, 1.0]),        # intra-batch dup — first wins
        ("snap-c", THREE_VECTORS["snap-c"]),
    ])
    assert appended == 2
    assert store.count == 3
    vec_file = tmp_path / "unit-test-model" / "vectors.f32"
    assert vec_file.stat().st_size == 3 * ROW_BYTES
    # first write won for both duplicates
    a_hit = store.search([1.0, 0.0, 0.0, 0.0], k=1)[0]
    assert a_hit[0] == "snap-a" and a_hit[1] == pytest.approx(1.0, abs=1e-5)
    b_hit = store.search([0.0, 1.0, 0.0, 0.0], k=1)[0]
    assert b_hit[0] == "snap-b" and b_hit[1] == pytest.approx(1.0, abs=1e-5)


def test_append_many_bad_dims_appends_nothing(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(ValueError):
        store.append_many([
            ("snap-a", THREE_VECTORS["snap-a"]),
            ("snap-bad", [1.0, 2.0]),  # wrong dims aborts the WHOLE batch
            ("snap-c", THREE_VECTORS["snap-c"]),
        ])
    assert store.count == 0
    # all-or-nothing: validation failed before any write, so no files appeared
    assert not (tmp_path / "unit-test-model").exists()


# ── get_store canonical-instance factory (I3) ────────────────────────────────

def test_get_store_returns_same_instance_for_same_key(tmp_path):
    a = get_store("unit-test-model", dims=DIMS, base_dir=tmp_path)
    b = get_store("unit-test-model", dims=DIMS, base_dir=tmp_path)
    assert a is b


def test_get_store_different_base_dir_different_instance(tmp_path):
    a = get_store("unit-test-model", dims=DIMS, base_dir=tmp_path / "one")
    b = get_store("unit-test-model", dims=DIMS, base_dir=tmp_path / "two")
    assert a is not b


def test_get_store_dims_default_from_registry(tmp_path):
    slug = "qwen3-embedding-0.6b"
    store = get_store(slug, base_dir=tmp_path)
    assert store.dims == EMBEDDING_MODELS[slug]["dims"]


def test_get_store_unknown_slug_without_dims_raises(tmp_path):
    with pytest.raises(ValueError):
        get_store("not-a-registered-model", base_dir=tmp_path)


# ── cache invalidation + lock regressions (I4) ───────────────────────────────

def test_search_sees_rows_appended_after_cache_warm(tmp_path):
    # Guards the `self._matrix = None` invalidation on append: without it the
    # first search pins a stale matrix and later rows never rank.
    store = _make_store(tmp_path)
    store.append("snap-a", [1.0, 0.0, 0.0, 0.0])
    assert store.search([1.0, 0.0, 0.0, 0.0], k=2)[0][0] == "snap-a"  # warm cache
    store.append("snap-b", [0.0, 1.0, 0.0, 0.0])
    results = store.search([0.0, 1.0, 0.0, 0.0], k=2)
    assert [sid for sid, _ in results] == ["snap-b", "snap-a"]


def test_concurrent_appends_and_searches_stay_consistent(tmp_path):
    store = _make_store(tmp_path)
    n_threads, n_ops = 4, 25
    errors = []

    def worker(t):
        try:
            for i in range(n_ops):
                vec = np.zeros(DIMS, dtype=np.float32)
                vec[(t + i) % DIMS] = 1.0
                store.append(f"snap-{t}-{i}", vec)
                store.search(vec, k=3)
        except Exception as e:  # surfaced via assert below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == []
    total = n_threads * n_ops
    assert store.count == total
    # on-disk state consistent: a fresh instance reopens without healing
    store_dir = tmp_path / "unit-test-model"
    assert (store_dir / "vectors.f32").stat().st_size == total * ROW_BYTES
    reopened = VectorStore("unit-test-model", DIMS, tmp_path).open()
    assert reopened.count == total
    assert reopened.ids() == {
        f"snap-{t}-{i}" for t in range(n_threads) for i in range(n_ops)
    }


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


# ── evict_store + store_dir (re-embed activation seam, Task 0.1) ──────────────

def test_evict_store_drops_cached_instance(tmp_path, monkeypatch):
    from Orchestrator.embeddings import store as st
    monkeypatch.setattr(st.config, "EMBEDDINGS_STORES_DIR", str(tmp_path))
    a = st.get_store("qwen3-embedding-0.6b")
    assert st.get_store("qwen3-embedding-0.6b") is a          # cached
    assert st.evict_store("qwen3-embedding-0.6b") is True
    assert st.get_store("qwen3-embedding-0.6b") is not a      # fresh instance
    assert st.evict_store("qwen3-embedding-0.6b") is True     # re-cached by prev line
    assert st.evict_store("nonexistent-never-cached") is False


def test_store_dir_resolves_under_base(tmp_path):
    from Orchestrator.embeddings import store as st
    assert st.store_dir("s", base_dir=tmp_path) == tmp_path / "s"
