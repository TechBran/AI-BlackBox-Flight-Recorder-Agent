"""Regression-guard golden set for the canonical retriever (Phase 5.2).

Two complementary guards against the two opposite failure modes of the
recency tie-break:

  Half A — RECURRING topics: recent active work must NOT be buried. For each
  query, at least one top-5 result must be a 2026-06 snapshot (the recent
  active period). Catches "recency_weight too low / relevance overwhelms recent
  duplicates" regressions.

  Half B — SINGLE-EVENT facts: a specific older snapshot that documents one
  discrete fix must NOT be buried by recency. The gold snap_ids below were
  pinned by INDEPENDENT ground truth — searching the volume text for the topic,
  reading the candidate snapshots, and selecting the one that actually documents
  the fix (commit SHAs cited in the snapshot body). Catches "recency_weight too
  high / over-correction drowns canonical older facts" regressions.

These tests hit the LIVE active store + embedding provider; they skip (never
fake-pass) when the store/provider is unavailable. They assert ONLY ranking
outcomes — they do not change retrieval behavior or the locked [retrieval]
defaults.
"""
import pytest

from Orchestrator.retrieval import retrieve


# -- Half-A: recurring topics -- recent work must surface (>=1 of top-5 is 2026-06)
RECURRING_QUERIES = [
    "pluggable embeddings model migration reembed",
    "control_phone delegate device task tailscale",
    "on-device gemma phone agent native tool loop",
    "streaming speech to text multi provider",
]

# -- Half-B: single-event facts -- the canonical older snapshot must stay in top-10.
# Each gold id was verified by reading the snapshot body in the volume:
#
# SNAP-20260606-6930: the ONLY snapshot containing both "AudioRecord"/"releaseBuffer"
#   and the merge commit a194134; its body documents commit e8e6ce9 "native
#   AudioRecord SIGABRT crash" fix and even carries the search hint "AudioRecord
#   releaseBuffer SIGABRT read release race". This is THE AudioRecord-fix snapshot.
#   (The Phase-5 candidate SNAP-20260604-6893 was WRONG -- an unrelated 'Anna'
#   cross-file-beacon snapshot with no AudioRecord content.)
#
# SNAP-20260427-6316: the "IMU ZUPT preprocessor landed" snapshot -- body records
#   the /odom yaw rate 0.08->0.0000 deg/s result, the new imu_zupt_node, and
#   commits 476b33d (node) + 5fcbd3f (EKF imu0 re-point). MEMORY's
#   snapshot_zupt_landed.md cites exactly this id. (Candidate confirmed.)
#
# SNAP-20260427-6313: the snapshot where the e-stop cmd_vel fan-out fix LANDED --
#   Raw Session Log line 1: "e-stop fan-out -- system_emergency_stop now cancels
#   Nav2 goal + triggers /explore/stop + pins zero cmd_vel". Body documents the
#   4-branch fan-out and the "controller_server overwrites /cmd_vel within ~50ms"
#   root cause (commit 3830828 / Plan A). The later 6315 is only the session
#   wrap-up/bench-plan, not the fix -- so 6313 is the canonical fix snapshot.
SINGLE_EVENT_GOLD = [
    ("android AudioRecord release race SIGABRT fix",            "SNAP-20260606-6930"),
    ("UGV ZUPT zero-velocity IMU drift preprocessor",           "SNAP-20260427-6316"),
    ("E-stop cmd_vel overwrite fan-out controller reassert",    "SNAP-20260427-6313"),
]


def _require_live_store():
    """Skip (don't fake-pass) when the active store / embedding provider is down."""
    try:
        from Orchestrator.embeddings.search import get_active_store
        store = get_active_store()
    except Exception as e:  # noqa: BLE001 - provider/store unavailable in test env
        pytest.skip(f"active store/provider unavailable: {e}")
    if store.count == 0:
        pytest.skip("active store empty")


@pytest.mark.parametrize("query", RECURRING_QUERIES)
def test_recurring_topic_surfaces_recent_snapshot(query):
    """Half A: >=1 of the top-5 results is a 2026-06 snapshot (recency not buried)."""
    _require_live_store()
    results = retrieve(query, "system", k=5)
    if not results:
        pytest.skip(f"retrieve returned nothing for {query!r} (query embed unavailable)")
    top_ids = [sid for sid, _ in results]
    assert any(sid.startswith("SNAP-202606") for sid in top_ids), (
        f"no 2026-06 snapshot in top-5 for {query!r}: {top_ids}"
    )


@pytest.mark.parametrize("query,gold_id", SINGLE_EVENT_GOLD)
def test_single_event_gold_in_top10(query, gold_id):
    """Half B: the canonical older single-event snapshot stays in top-10 (recency
    must not over-correct and bury a specific fact)."""
    _require_live_store()
    results = retrieve(query, "system", k=10)
    if not results:
        pytest.skip(f"retrieve returned nothing for {query!r} (query embed unavailable)")
    top_ids = [sid for sid, _ in results]
    assert gold_id in top_ids, (
        f"canonical gold {gold_id} not in top-10 for {query!r}; "
        f"recency over-correction suspected. top-10 = {top_ids}"
    )
