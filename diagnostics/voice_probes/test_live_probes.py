"""Live voice-probe smoke suite (design WS6 — replaces the voice-blind test_grok.sh).

NOT collected by the default run (pytest.ini: testpaths = Orchestrator/tests).
Run explicitly, from the repo root, whenever touching voice code or before any
catalog change:

    Orchestrator/venv/bin/python -m pytest diagnostics/voice_probes/test_live_probes.py -m probe_live -v

Makes real, paid API calls. Skips per-provider when the key is absent
(fresh-box gate: graceful degradation, no hard failure on an unconfigured box).
"""
import asyncio

import pytest

from diagnostics.voice_probes.env import get_key
from diagnostics.voice_probes.probes import probe_gemini, probe_openai, probe_xai

pytestmark = pytest.mark.probe_live


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.skipif(not get_key("OPENAI_API_KEY"), reason="OPENAI_API_KEY not in service env")
def test_openai_flagship_handshake():
    r = _run(probe_openai("gpt-realtime-2.1"))
    assert r.ok, r.summary()


@pytest.mark.skipif(not get_key("XAI_API_KEY"), reason="XAI_API_KEY not in service env")
def test_xai_default_handshake_resolves_model():
    r = _run(probe_xai(""))
    assert r.ok, r.summary()
    assert r.resolved_model, "session.created did not carry a model id"


@pytest.mark.skipif(not get_key("GOOGLE_API_KEY"), reason="GOOGLE_API_KEY not in service env")
def test_gemini_bare_setup_completes():
    r = _run(probe_gemini("gemini-3.1-flash-live-preview"))
    assert r.ok, r.summary()


@pytest.mark.skipif(not get_key("GOOGLE_API_KEY"), reason="GOOGLE_API_KEY not in service env")
@pytest.mark.xfail(
    reason="known 1007: update_sheet_values inner array lacks items — "
    "P1.1 fixes the schema; P1.10's verification step removes this xfail",
    strict=False,
)
def test_gemini_full_toolgroup_setup_completes():
    """The full gemini_live tool group must be accepted by setup (WS1 guard)."""
    from Orchestrator.tools.tool_registry import get_gemini_live_tools
    r = _run(probe_gemini(
        "gemini-3.1-flash-live-preview",
        tools=get_gemini_live_tools("gemini_live"),
    ))
    assert r.ok, r.summary()
