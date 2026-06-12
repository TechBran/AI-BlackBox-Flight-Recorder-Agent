"""Pluggable embeddings — live search layer tests (Task 5 cutover).

Per docs/plans/2026-06-11-pluggable-embeddings.md Task 5: monitoring's
generate_embedding / semantic_search become delegates onto
Orchestrator.embeddings.search, which serves searches from the active binary
VectorStore (with a temporary inline-JSON fallback for the pre-transcode
window). Behavior preservation is the whole point — these tests pin the
legacy contracts: None on embed failure, [] on query-embed failure, operator
"" / "system" sees ALL, [(snap_id, score)] top-k sorted desc.

ALL providers are fakes injected via providers._instances; all stores live in
tmp_path via monkeypatched config.EMBEDDINGS_STORES_DIR — zero network, zero
real Manifest/ access.
"""
import asyncio
import math
import threading
from unittest.mock import patch

import numpy as np
import pytest

from Orchestrator import config, monitoring
from Orchestrator.embeddings import providers, search
from Orchestrator.embeddings.providers import EmbeddingProviderError
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_store, set_active_slug

GEMINI_SLUG = "gemini-embedding-001"          # config default → initial active
OPENAI_SLUG = "openai-text-embedding-3-large"  # same dims (3072) → swap target
DIMS = EMBEDDING_MODELS[GEMINI_SLUG]["dims"]


# ── fakes & fixtures ─────────────────────────────────────────────────────────

class FakeProvider:
    """Deterministic provider stand-in, injected via providers._instances."""

    def __init__(self, vector, fail=False):
        self.vector = list(vector)
        self.fail = fail
        self.calls = []  # (texts, purpose) per embed() call

    async def embed(self, texts, purpose):
        self.calls.append((list(texts), purpose))
        if self.fail:
            raise EmbeddingProviderError("fake provider: down")
        return [list(self.vector) for _ in texts]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Fresh stores dir + clean provider cache + reset active-store state."""
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(tmp_path / "embeddings"))
    providers._instances.clear()
    search._active_store = None
    yield
    providers._instances.clear()
    search._active_store = None


def _rng():
    return np.random.default_rng(7)  # fixed seed: ordering must be stable


def _vectors_by_id(n, rng, prefix="SNAP-20260101"):
    return {
        f"{prefix}-{i:04d}": [float(x) for x in rng.standard_normal(DIMS)]
        for i in range(n)
    }


def _install_fake(slug, query_vec, fail=False):
    fake = FakeProvider(query_vec, fail=fail)
    providers._instances[slug] = fake
    return fake


def _naive_topk(vec_by_id, query, k, allowed=None):
    """Independent cosine ranking — NOT via store or monitoring code."""

    def cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    scored = [
        (sid, cos(query, vec))
        for sid, vec in vec_by_id.items()
        if allowed is None or sid in allowed
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


def _index_for(vec_by_id, operator_of, with_embeddings=False):
    """Snapshot-index fixture; optionally carries inline embeddings."""
    index = {}
    for i, (sid, vec) in enumerate(vec_by_id.items()):
        entry = {
            "byte_start": 1000 * i,
            "byte_end": 1000 * i + 999,
            "operator": operator_of(i),
            "timestamp": f"2026-01-{i + 1:02d}T00:00:00Z",
            "type": "normal",
        }
        if with_embeddings:
            entry["embedding"] = vec
        index[sid] = entry
    return index


# ── parity: store-backed search vs naive cosine ──────────────────────────────

def test_semantic_search_parity_with_naive_cosine():
    rng = _rng()
    vec_by_id = _vectors_by_id(10, rng)
    query = [float(x) for x in rng.standard_normal(DIMS)]
    _install_fake(GEMINI_SLUG, query)

    store = get_store(GEMINI_SLUG)
    for sid, vec in vec_by_id.items():
        store.append(sid, vec)

    results = search.semantic_search("anything", k=5)
    expected = _naive_topk(vec_by_id, query, k=5)

    assert [sid for sid, _ in results] == [sid for sid, _ in expected]
    for (_, got), (_, want) in zip(results, expected):
        assert got == pytest.approx(want, abs=1e-5)


# ── operator filtering ───────────────────────────────────────────────────────

def test_operator_filter_store_path(monkeypatch):
    rng = _rng()
    vec_by_id = _vectors_by_id(6, rng)
    query = [float(x) for x in rng.standard_normal(DIMS)]
    _install_fake(GEMINI_SLUG, query)

    operator_of = lambda i: "alice" if i % 2 == 0 else "bob"
    index = _index_for(vec_by_id, operator_of)
    monkeypatch.setattr("Orchestrator.fossils.load_snapshot_index", lambda: index)

    store = get_store(GEMINI_SLUG)
    for sid, vec in vec_by_id.items():
        store.append(sid, vec)

    alice_ids = {sid for sid, e in index.items() if e["operator"] == "alice"}

    alice_results = search.semantic_search("q", operator="alice", k=10)
    assert {sid for sid, _ in alice_results} == alice_ids
    expected = _naive_topk(vec_by_id, query, k=10, allowed=alice_ids)
    assert [sid for sid, _ in alice_results] == [sid for sid, _ in expected]
    for (_, got), (_, want) in zip(alice_results, expected):
        assert got == pytest.approx(want, abs=1e-5)

    # "system" and "" both see ALL operators (legacy special case)
    for op in ("system", ""):
        results = search.semantic_search("q", operator=op, k=10)
        assert {sid for sid, _ in results} == set(vec_by_id)


# ── purpose plumbed through to the provider ──────────────────────────────────

def test_query_purpose_from_semantic_search_and_document_from_generate():
    rng = _rng()
    query = [float(x) for x in rng.standard_normal(DIMS)]
    fake = _install_fake(GEMINI_SLUG, query)
    get_store(GEMINI_SLUG).append("SNAP-X", [1.0] * DIMS)

    search.semantic_search("find my stuff", k=3)
    assert fake.calls[-1] == (["find my stuff"], "query")

    vec = search.generate_embedding_sync("snapshot body text")
    assert vec == query
    assert fake.calls[-1] == (["snapshot body text"], "document")


# ── failure contracts ────────────────────────────────────────────────────────

def test_generate_embedding_sync_failure_returns_none(capsys):
    _install_fake(GEMINI_SLUG, [0.0] * DIMS, fail=True)
    assert search.generate_embedding_sync("text") is None
    assert "[EMBEDDING]" in capsys.readouterr().out


def test_semantic_search_failure_returns_empty_list(capsys):
    _install_fake(GEMINI_SLUG, [0.0] * DIMS, fail=True)
    assert search.semantic_search("query") == []
    out = capsys.readouterr().out
    assert "[SEMANTIC] Query embedding failed" in out


# ── store-empty inline fallback (pre-transcode window; removed in Task 16) ──

def test_store_empty_falls_back_to_inline_index_embeddings(monkeypatch):
    rng = _rng()
    vec_by_id = _vectors_by_id(8, rng)
    query = [float(x) for x in rng.standard_normal(DIMS)]
    _install_fake(GEMINI_SLUG, query)

    operator_of = lambda i: "alice" if i < 4 else "bob"
    index = _index_for(vec_by_id, operator_of, with_embeddings=True)
    # One entry without an embedding must be skipped (legacy behavior)
    no_vec_sid = "SNAP-20260101-0000"
    del index[no_vec_sid]["embedding"]
    monkeypatch.setattr("Orchestrator.fossils.load_snapshot_index", lambda: index)

    assert search.get_active_store().count == 0  # nothing transcoded yet

    embedded = {sid: v for sid, v in vec_by_id.items() if sid != no_vec_sid}
    results = search.semantic_search("q", k=5)
    expected = _naive_topk(embedded, query, k=5)
    assert [sid for sid, _ in results] == [sid for sid, _ in expected]
    for (_, got), (_, want) in zip(results, expected):
        assert got == pytest.approx(want, abs=1e-9)

    # operator filter applies on the fallback path too
    alice_ids = {sid for sid, e in index.items() if e["operator"] == "alice"}
    alice_results = search.semantic_search("q", operator="alice", k=10)
    assert {sid for sid, _ in alice_results} == alice_ids & set(embedded)


# ── swap_active cutover seam ─────────────────────────────────────────────────

def test_swap_active_searches_hit_the_new_store():
    rng = _rng()
    gemini_vecs = _vectors_by_id(3, rng, prefix="SNAP-GEM")
    openai_vecs = _vectors_by_id(3, rng, prefix="SNAP-OAI")
    query = [float(x) for x in rng.standard_normal(DIMS)]
    _install_fake(GEMINI_SLUG, query)
    _install_fake(OPENAI_SLUG, query)

    for sid, vec in gemini_vecs.items():
        get_store(GEMINI_SLUG).append(sid, vec)
    for sid, vec in openai_vecs.items():
        get_store(OPENAI_SLUG).append(sid, vec)

    before = search.semantic_search("q", k=10)
    assert {sid for sid, _ in before} == set(gemini_vecs)

    # cutover exactly as Task 8 will do it: persist pointer + in-memory swap
    set_active_slug(OPENAI_SLUG)
    swapped = search.swap_active(OPENAI_SLUG)
    assert swapped is get_store(OPENAI_SLUG)
    assert search.get_active_store() is swapped

    after = search.semantic_search("q", k=10)
    assert {sid for sid, _ in after} == set(openai_vecs)


def test_swap_active_rejects_unknown_slug():
    with pytest.raises(ValueError, match="unknown embedding model slug"):
        search.swap_active("not-a-model")
    assert search._active_store is None  # refusal must not poison state


# ── sync bridge: works with and without a running event loop ────────────────

def test_generate_embedding_sync_inside_running_event_loop():
    query = [0.5] * DIMS
    _install_fake(GEMINI_SLUG, query)

    async def call_from_loop():
        # sync call made FROM the event-loop thread (e.g. a route handler)
        return search.generate_embedding_sync("hello")

    assert asyncio.run(call_from_loop()) == query


def test_generate_embedding_sync_from_plain_thread():
    query = [0.25] * DIMS
    _install_fake(GEMINI_SLUG, query)

    out = {}
    t = threading.Thread(  # e.g. checkpoint mint path / APScheduler executor
        target=lambda: out.setdefault("vec", search.generate_embedding_sync("hi"))
    )
    t.start()
    t.join(timeout=30)
    assert not t.is_alive()
    assert out["vec"] == query


# ── monitoring delegates ─────────────────────────────────────────────────────

def test_monitoring_generate_embedding_delegates():
    with patch(
        "Orchestrator.embeddings.search.generate_embedding_sync",
        return_value=[0.1, 0.2],
    ) as mock_gen:
        assert monitoring.generate_embedding("some text") == [0.1, 0.2]
    mock_gen.assert_called_once_with("some text", purpose="document")


def test_monitoring_semantic_search_delegates():
    with patch(
        "Orchestrator.embeddings.search.semantic_search",
        return_value=[("SNAP-1", 0.9)],
    ) as mock_search:
        assert monitoring.semantic_search("q", operator="alice", k=4) == [("SNAP-1", 0.9)]
    mock_search.assert_called_once_with("q", operator="alice", k=4)


def test_monitoring_keeps_cosine_similarity():
    # kept until Task 16 (fallback + tests depend on it)
    assert monitoring.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert monitoring.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
