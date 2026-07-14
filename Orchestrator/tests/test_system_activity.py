"""Tests for the pure, UI-only `system_activity` event builder ("The Signal").

These assert HONEST degradation: only stages with a real metric are emitted,
`seq` is monotonic, and a non-retrieval turn (no memory searched) yields nothing.
The builder is presentation-only and never persisted to prompt/ledger.
"""
from Orchestrator.telemetry_events import build_retrieval_activity


def test_full_retrieval_turn():
    m = {"provider": "anthropic", "model": "claude-sonnet-4-5", "window_tokens": 840000,
         "embed_model": "gemini-embedding-2", "embed_dims": 3072, "corpus_count": 8005,
         "candidates": 22, "rrf_c": 60, "mmr_topk": 8, "rerank_enabled": False,
         "memories": 8, "context_tokens": 42000, "dropped": 0}
    ev = build_retrieval_activity(m)
    stages = [e["data"]["stage"] for e in ev]
    assert stages == ["resolve_model", "embed", "search", "mmr", "context"]
    assert all(e["type"] == "system_activity" for e in ev)
    assert [e["data"]["seq"] for e in ev] == list(range(len(ev)))
    assert "3072" in ev[1]["data"]["label"]
    assert "rerank" not in stages


def test_non_retrieval_turn_emits_nothing():
    assert build_retrieval_activity({"provider": "gpt-5.1", "model": "gpt-5.1"}) == []


def test_window_guard_drop_is_honest():
    m = {"provider": "gpt-5.1", "model": "gpt-5.1", "embed_model": "gemini-embedding-2", "embed_dims": 3072,
         "corpus_count": 8005, "candidates": 19, "mmr_topk": 8, "memories": 8, "context_tokens": 38000, "dropped": 3}
    labels = " ".join(e["data"]["label"] for e in build_retrieval_activity(m))
    assert "trimmed 3" in labels


def test_rerank_shown_only_when_enabled():
    m = {"provider": "anthropic", "model": "x", "embed_model": "e", "embed_dims": 10, "corpus_count": 100,
         "candidates": 20, "mmr_topk": 8, "memories": 8, "rerank_enabled": True, "rerank_model": "cohere-rerank-4"}
    stages = [e["data"]["stage"] for e in build_retrieval_activity(m)]
    assert "rerank" in stages
