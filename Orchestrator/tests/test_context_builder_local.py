from unittest import mock
from Orchestrator import context_builder as cb

def test_local_profile_is_lean_and_capped():
    with mock.patch.object(cb, "semantic_retrieve", return_value=["S1","S2","S3","S4","S5"]) as sem, \
         mock.patch.object(cb, "get_recent_checkpoints_for_operator", return_value=["CP1"]) as cp, \
         mock.patch.object(cb, "get_recent_fossils_for_operator", return_value=["R1"]) as rec, \
         mock.patch.object(cb, "keyword_retrieve_for_operator", return_value=["K1"]) as kw, \
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
    rec.assert_not_called()
    kw.assert_not_called()
    assert prov["recent"] == [] and prov["keyword"] == []
    assert len(prov["semantic"]) <= 3 and prov["checkpoint"] == ["CP1"]

def test_local_provider_cap_reserves_loop_headroom():
    assert cb.PROVIDER_CAPS["local"] <= 16000
