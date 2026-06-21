"""On-device lean-profile recall guard (Phase 5.3).

The phone profile calls the canonical retriever in semantic-only mode with a
small k (it has no volume text, so no keyword channel). This guards the F1/F6
failure where "the phone gets zero memories" -- i.e. the calibrated
gemini-embedding-2 query threshold (0.55) plus the retriever junk floor (0.40)
combine to filter EVERYTHING out for representative recent queries.

We assert only that the lean path returns NON-EMPTY results (>=1) for several
representative recent queries -- not specific ids -- because the failure mode
being guarded is "empty", and ranking is validated separately by the golden
set. Hits the LIVE store/provider; skips (never fake-passes) when unavailable.
"""
import pytest

from Orchestrator.retrieval import retrieve


# Representative recent queries the on-device agent would realistically ask.
LEAN_QUERIES = [
    "pluggable embeddings model migration reembed",
    "control_phone delegate device task tailscale",
    "streaming speech to text multi provider",
]


def _require_live_store():
    try:
        from Orchestrator.embeddings.search import get_active_store
        store = get_active_store()
    except Exception as e:  # noqa: BLE001 - provider/store unavailable in test env
        pytest.skip(f"active store/provider unavailable: {e}")
    if store.count == 0:
        pytest.skip("active store empty")


@pytest.mark.parametrize("query", LEAN_QUERIES)
def test_lean_semantic_only_returns_nonempty(query):
    """Lean phone profile: semantic-only, k=3 -> at least one snapshot.

    include_keyword=False mirrors the on-device call (no volume text). A non-empty
    result proves the active-model threshold + junk floor don't starve the phone.
    """
    _require_live_store()
    results = retrieve(query, "system", k=3, include_keyword=False)
    if not results:
        # Distinguish "embed unavailable" (skip) from "starved" (fail) is hard
        # from inside; but query-embed failure for a live store is itself the
        # failure we guard. Surface the empty result as a hard failure here --
        # the store IS live (asserted above), so empty == the F1/F6 bug.
        pytest.fail(
            f"lean semantic-only retrieve returned EMPTY for {query!r} "
            f"(threshold/junk-floor starvation -- the phone gets zero memories)"
        )
    assert 1 <= len(results) <= 3, results
    assert all(sid.startswith("SNAP-") for sid, _ in results), results
