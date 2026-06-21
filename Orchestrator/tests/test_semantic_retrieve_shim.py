"""Behavioral tests for semantic_retrieve (Phase 3b-2).

Phase 3b-2 rewrite: semantic_retrieve is now a THIN SHIM over the canonical
Orchestrator.retrieval.retrieve() in semantic-only mode (include_keyword=False) —
RRF over the semantic channel, a mild recency tie-break, MMR diversity, and a
junk-floor instead of the old hard 0.60 threshold. This INTENTIONALLY makes the
per-turn semantic context recency-aware (the point of Phase 3b-2). The `threshold`
arg is retained for signature compatibility but is now VESTIGIAL.

Because the ranking changed BY DESIGN (no more hard-threshold filter), the old
threshold-based assertions no longer apply. Instead we assert the BEHAVIORS the new
pipeline must exhibit: the SAME return type (list of decoded snapshot texts, each
carrying a SNAP- START marker), bounded by k, no duplicate ids, and recency
surfacing recent work. Hits the LIVE store/provider; skips (never fake-passes)
when unavailable so the suite stays green offline.
"""
import pytest

from Orchestrator.config import VOL_PATH
from Orchestrator.fossils import (
    extract_snap_ids,
    read_volume_bytes,
    semantic_retrieve,
)


def _require_live_store():
    """Skip (not fail) when the embeddings store/provider is unavailable or empty —
    semantic_retrieve is semantic-only, so without an active store it returns []."""
    try:
        from Orchestrator.embeddings.search import get_active_store
        store = get_active_store()
    except Exception as e:  # noqa: BLE001 - provider/store unavailable in test env
        pytest.skip(f"active store/provider unavailable: {e}")
    if store.count == 0:
        pytest.skip("active store empty")


# Behavioral queries (no pinned id baseline — ranking is recency-aware by design).
QUERIES = [
    "pluggable embeddings model migration reembed",
    "control phone on-device gemma delegate device task",
    "streaming speech to text multi provider",
]


@pytest.mark.parametrize("query", QUERIES)
def test_semantic_retrieve_returns_bounded_real_snapshots(query):
    """SAME return type: <=k real, non-empty, START-marked, de-duplicated texts."""
    _require_live_store()
    k = 8
    results = semantic_retrieve(query, operator="system", k=k)
    # results should be non-empty for these well-represented recent topics
    assert results, f"{query!r}: expected at least one result"
    # bounded by k
    assert len(results) <= k, f"{query!r}: got {len(results)} results, expected <= {k}"
    # each result is a non-empty text carrying a SNAP- START marker (return type)
    for r in results:
        assert isinstance(r, str) and len(r) > 0
        assert "=== START SNAPSHOT" in r and "SNAP-" in r, (
            f"{query!r}: a result is missing its START marker / SNAP- id"
        )
    # no duplicate snap_ids in the output
    ids = extract_snap_ids(results)
    assert len(ids) == len(set(ids)), f"{query!r}: duplicate snap_ids: {ids}"


def test_semantic_retrieve_surfaces_recent_work():
    """Recency tie-break: a recent 2026-06 snapshot appears in the top-k for a topic
    with recent activity (the recency-aware per-turn context is the point of 3b-2)."""
    _require_live_store()
    results = semantic_retrieve(
        "pluggable embeddings model migration", operator="system", k=8
    )
    ids = extract_snap_ids(results)
    assert any(sid.startswith("SNAP-202606") for sid in ids), (
        f"expected a recent 2026-06 snapshot in the top-k, got: {ids}"
    )


def test_semantic_retrieve_threshold_arg_is_vestigial():
    """The threshold arg is retained for signature compat but is now UNUSED:
    passing an absurdly high threshold no longer empties the result set (retrieve()
    uses its own junk_floor, not this arg)."""
    _require_live_store()
    q = "pluggable embeddings model migration reembed"
    with_high = semantic_retrieve(q, operator="system", k=5, threshold=0.99)
    default = semantic_retrieve(q, operator="system", k=5)
    # threshold=0.99 would have emptied the OLD body; now it's ignored, so both
    # return the SAME (non-empty) ranked ids.
    assert with_high, "threshold arg should be vestigial — high threshold must not empty results"
    assert extract_snap_ids(with_high) == extract_snap_ids(default)


def test_semantic_retrieve_empty_query_returns_empty():
    """retrieve() short-circuits a blank query to [] — the shim must propagate that
    (no decode, no crash). No live store needed for this contract."""
    assert semantic_retrieve("", operator="system", k=5) == []
    assert semantic_retrieve("   ", operator="system", k=5) == []
