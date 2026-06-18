from unittest import mock

from Orchestrator import context_builder as cb


def _blk(sid: str) -> str:
    """A realistic snapshot block whose START marker matches config.START_RX
    (em-dash separators, real SNAP-ID), so extract_snap_ids parses the ID the
    same way the production snapshot parser does."""
    return f"=== START SNAPSHOT — UTC 2026-06-17T00:00:00Z — {sid} ===\nbody text\n"


def test_local_profile_is_lean_and_capped():
    # semantic_retrieve over-returns (5) though semantic_k=3 — the defensive
    # [:SF] slice must cap it to 3 for the lean budget.
    sem_blocks = [_blk(f"SNAP-20260617-000{i}") for i in range(1, 6)]
    with mock.patch.object(cb, "semantic_retrieve", return_value=sem_blocks) as sem, \
         mock.patch.object(cb, "get_recent_checkpoints_for_operator",
                           return_value=[_blk("SNAP-20260617-0099")]) as cp, \
         mock.patch.object(cb, "get_recent_fossils_for_operator",
                           return_value=[_blk("SNAP-20260617-0050")]) as rec, \
         mock.patch.object(cb, "keyword_retrieve_for_operator",
                           return_value=[_blk("SNAP-20260617-0060")]) as kw, \
         mock.patch.object(cb, "read_text_safe", return_value=""), \
         mock.patch.object(cb, "get_recent_media_artifacts", return_value=[]):
        text, prov = cb.build_fossil_context(
            "roll dice", "Brandon", provider="local",
            semantic_k=3, checkpoint_count=1, include_recent=False, include_keyword=False,
        )
    sem.assert_called_once()
    assert sem.call_args.kwargs["k"] == 3
    cp.assert_called_once()
    assert cp.call_args.kwargs["count"] == 1
    rec.assert_not_called()                       # recent skipped for local
    kw.assert_not_called()                        # keyword skipped for local
    assert prov["recent"] == [] and prov["keyword"] == []
    # semantic capped to 3 (real SNAP-IDs extracted from the marked blocks)
    assert prov["semantic"] == [
        "SNAP-20260617-0001", "SNAP-20260617-0002", "SNAP-20260617-0003",
    ]
    assert prov["checkpoint"] == ["SNAP-20260617-0099"]


def test_local_provider_cap_reserves_loop_headroom():
    assert cb.PROVIDER_CAPS["local"] <= 16000
