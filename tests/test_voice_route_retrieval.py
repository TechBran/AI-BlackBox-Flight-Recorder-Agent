"""Integration tests for the voice routes' shared `build_context_for_operator` helpers.

These verify the three voice-WebSocket routes (Gemini Live, OpenAI Realtime,
xAI Grok Live) all delegate to `build_fossil_context` and therefore now
populate all four retrieval sources (recent, keyword, semantic, checkpoint)
when given a non-empty user_text.

This protects against the original regression where each voice route only
fetched 1 checkpoint + 2 recent snapshots and silently dropped keyword/semantic.
"""
import pytest

from Orchestrator.routes.gemini_live_routes import build_context_for_operator as build_gemini_ctx
from Orchestrator.routes.realtime_routes import build_context_for_operator as build_realtime_ctx
from Orchestrator.routes.grok_live_routes import build_context_for_operator as build_grok_ctx

# Retrieval quality over this box's real ledger.
pytestmark = pytest.mark.live_store


def _assert_context_quality(fn, name):
    text, prov = fn(operator="Brandon", user_text="tool vault recent work")
    assert "recent" in prov and "semantic" in prov and "keyword" in prov and "checkpoint" in prov, \
        f"{name} missing provenance keys"
    assert len(prov["recent"]) >= 1, f"{name} missing recent snapshots"
    assert len(prov["keyword"]) + len(prov["semantic"]) >= 1, \
        f"{name} returned zero keyword+semantic — retrieval regression"


def test_gemini_live_context_populated():
    _assert_context_quality(build_gemini_ctx, "gemini-live")


def test_realtime_context_populated():
    _assert_context_quality(build_realtime_ctx, "realtime")


def test_grok_live_context_populated():
    _assert_context_quality(build_grok_ctx, "grok-live")
