"""Tests for ToolVault v2 store-based search (Task 2.2).

``semantic_search_store`` and ``hybrid_search_store`` read vectors from the
``embeddings.json`` store shape (``{name: {"hash","model","vector":[...]}}``)
built by ``sync_embeddings``, blending keyword (40%) + semantic (60%) scores.

Tests are hermetic: ``embeddings.embed_query`` is monkeypatched to a fixed
vector (never hits the network).
"""

import pytest

from Orchestrator.toolvault import embeddings
from Orchestrator.toolvault.config import (
    KEYWORD_WEIGHT,
    SEMANTIC_WEIGHT,
)


# A 2-D vector space makes cosine ordering trivial to reason about.
# query_vec points along +x. Tools aligned with +x score higher.
QUERY_VEC = [1.0, 0.0]


@pytest.fixture
def patched_query(monkeypatch):
    """Monkeypatch embed_query to return a fixed query vector (no network)."""
    def fake_embed_query(query):
        return list(QUERY_VEC)

    monkeypatch.setattr(embeddings, "embed_query", fake_embed_query)
    return QUERY_VEC


def _store(vectors):
    """Build a store dict {name: {"vector": vec, ...}} from {name: vec}."""
    return {
        name: {"hash": "h", "model": "m", "vector": vec}
        for name, vec in vectors.items()
    }


# ---------------------------------------------------------------------------
# semantic_search_store
# ---------------------------------------------------------------------------

def test_semantic_search_store_orders_by_cosine():
    store = _store({
        "aligned": [1.0, 0.0],        # cosine 1.0 with query
        "diagonal": [1.0, 1.0],       # cosine ~0.707
        "orthogonal": [0.0, 1.0],     # cosine 0.0
    })
    results = embeddings.semantic_search_store(QUERY_VEC, store, limit=10)
    names = [n for n, _ in results]
    assert names == ["aligned", "diagonal", "orthogonal"]
    # scores descending
    scores = [s for _, s in results]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(1.0)


def test_semantic_search_store_respects_limit():
    store = _store({
        "aligned": [1.0, 0.0],
        "diagonal": [1.0, 1.0],
        "orthogonal": [0.0, 1.0],
    })
    results = embeddings.semantic_search_store(QUERY_VEC, store, limit=2)
    assert len(results) == 2
    assert [n for n, _ in results] == ["aligned", "diagonal"]


def test_semantic_search_store_skips_missing_and_empty_vectors():
    store = {
        "aligned": {"vector": [1.0, 0.0]},
        "empty": {"vector": []},      # empty vector → skipped
        "novec": {"hash": "h"},        # missing vector key → skipped
    }
    results = embeddings.semantic_search_store(QUERY_VEC, store, limit=10)
    names = [n for n, _ in results]
    assert names == ["aligned"]


def test_semantic_search_store_deterministic():
    store = _store({
        "a": [1.0, 0.0],
        "b": [1.0, 1.0],
        "c": [0.0, 1.0],
    })
    r1 = embeddings.semantic_search_store(QUERY_VEC, store, limit=10)
    r2 = embeddings.semantic_search_store(QUERY_VEC, store, limit=10)
    assert r1 == r2


# ---------------------------------------------------------------------------
# hybrid_search_store
# ---------------------------------------------------------------------------

def test_hybrid_search_store_keyword_match_ranks_high(patched_query):
    # All vectors orthogonal to query (semantic 0) so keyword decides ordering.
    store = _store({
        "send_sms": [0.0, 1.0],
        "send_email": [0.0, 1.0],
    })
    descriptions = {
        "send_sms": "send a text message via sms",
        "send_email": "compose and send an email",
    }
    results = embeddings.hybrid_search_store(
        "send sms text", descriptions, store, limit=10, threshold=0.0,
    )
    names = [n for n, _ in results]
    assert names[0] == "send_sms"


def test_hybrid_search_store_drops_below_threshold(patched_query):
    store = _store({
        "match": [1.0, 0.0],       # semantic 1.0
        "nomatch": [0.0, 1.0],     # semantic 0.0, no keyword overlap either
    })
    descriptions = {
        "match": "the query word here",
        "nomatch": "completely different content",
    }
    # nomatch: kw 0, sem 0 → combined 0, dropped by any positive threshold.
    results = embeddings.hybrid_search_store(
        "query", descriptions, store, limit=10, threshold=0.01,
    )
    names = [n for n, _ in results]
    assert "match" in names
    assert "nomatch" not in names


def test_hybrid_search_store_tool_missing_from_store_still_keyword_reachable(patched_query):
    # "orphan" has a description but no vector in the store; it must still be
    # reachable via keyword score (semantic treated as 0, no crash).
    store = _store({
        "has_vec": [0.0, 1.0],     # orthogonal → semantic 0
    })
    descriptions = {
        "has_vec": "unrelated words",
        "orphan": "orphan keyword match target",
    }
    results = embeddings.hybrid_search_store(
        "orphan keyword", descriptions, store, limit=10, threshold=0.0,
    )
    names = [n for n, _ in results]
    assert "orphan" in names
    assert names[0] == "orphan"


def test_hybrid_search_store_blend_matches_existing_math(patched_query):
    # One tool aligned with query (semantic 1.0) and a strong keyword match.
    store = _store({"tool_a": [1.0, 0.0]})
    descriptions = {"tool_a": "tool_a"}  # exact name + desc match

    combined = embeddings.hybrid_search_store(
        "tool_a", descriptions, store, limit=10, threshold=0.0,
    )
    # Recompute expected blend from the component functions directly.
    sem = dict(embeddings.semantic_search_store(QUERY_VEC, store, limit=30))
    kw = dict(embeddings.keyword_search("tool_a", {}, descriptions, limit=30))
    expected = (KEYWORD_WEIGHT * kw.get("tool_a", 0.0)) + (
        SEMANTIC_WEIGHT * sem.get("tool_a", 0.0)
    )
    got = dict(combined)["tool_a"]
    assert got == pytest.approx(expected)


def test_hybrid_search_store_deterministic(patched_query):
    store = _store({
        "a": [1.0, 0.0],
        "b": [0.0, 1.0],
    })
    descriptions = {"a": "alpha word", "b": "beta word"}
    r1 = embeddings.hybrid_search_store("alpha", descriptions, store, limit=10, threshold=0.0)
    r2 = embeddings.hybrid_search_store("alpha", descriptions, store, limit=10, threshold=0.0)
    assert r1 == r2

