"""P1.3 — the gemini_live tool list is read FRESH at configure time.

GEMINI_LIVE_TOOLS was an import-time snapshot (old line 98): /toolvault/reload
never reached live voice sessions, so the 2026-06-20 schema regression (and its
fix!) required a full service restart to even take effect. Pin: each
configure_gemini_session call pulls get_gemini_live_tools("gemini_live") anew,
and the frozen module constant is gone.

grok/openai routes get the same change in the hardening phase — this file
intentionally covers ONLY gemini.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import Orchestrator.routes.gemini_live_routes as glr


@pytest.fixture
def no_fossils(monkeypatch):
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(glr, "build_fossil_context", _stub)


def _make_session():
    session = MagicMock()
    session.gemini_ws = MagicMock()
    session.gemini_ws.send = AsyncMock()
    session.resumption_handle = None
    session.provenance = {}
    session.voice = ""
    # Inert values for the P1.4 session-persisted config (MagicMock attrs are
    # truthy and would hijack the None-fallbacks once P1.4 lands).
    session.model = None
    session.vad_sensitivity_start = None
    session.vad_sensitivity_end = None
    session.thinking_level = None
    session.custom_role = ""
    session.phone_mode = False
    return session


def test_frozen_snapshot_is_gone():
    assert not hasattr(glr, "GEMINI_LIVE_TOOLS"), (
        "GEMINI_LIVE_TOOLS import-time snapshot must not come back — it is why "
        "the 2026-06-20 schema regression needed a restart to even diagnose"
    )


@pytest.mark.asyncio
async def test_configure_reads_tools_fresh_each_call(no_fossils, monkeypatch):
    calls = []

    def fake_get_tools(group):
        calls.append(group)
        return [{"functionDeclarations": [{"name": f"tool_v{len(calls)}"}]}]

    monkeypatch.setattr(glr, "get_gemini_live_tools", fake_get_tools)

    for expected in ("tool_v1", "tool_v2"):
        session = _make_session()
        await glr.configure_gemini_session(session, "test_operator", "Orus")
        payload = json.loads(session.gemini_ws.send.await_args.args[0])
        names = [
            fd["name"]
            for t in payload["setup"]["tools"]
            for fd in t["functionDeclarations"]
        ]
        assert names == [expected]

    assert calls == ["gemini_live", "gemini_live"]
