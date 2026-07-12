"""P1b: voice routes must read ToolVault tools at session-configure time, not import time."""
import asyncio

import Orchestrator.routes.grok_live_routes as gk
import Orchestrator.routes.realtime_routes as rt
from Orchestrator.models import GrokLiveSession, RealtimeSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS


def _patch_light(monkeypatch, module):
    """Avoid heavy retrieval/persona work inside configure_* during the test."""
    monkeypatch.setattr(module, "build_context_for_operator",
                        lambda operator, user_text="": ("", {}))
    monkeypatch.setattr(module, "get_persona",
                        lambda operator, modality: "Test persona.")


def test_no_import_time_tool_snapshots():
    assert not hasattr(rt, "REALTIME_TOOLS"), \
        "REALTIME_TOOLS import-time freeze must be removed (toolvault/reload blind spot)"
    assert not hasattr(gk, "GROK_LIVE_TOOLS"), \
        "GROK_LIVE_TOOLS import-time freeze must be removed (toolvault/reload blind spot)"


def test_grok_configure_reads_tools_fresh_each_time(monkeypatch):
    _patch_light(monkeypatch, gk)
    calls = []

    def fake_get(group):
        calls.append(group)
        return [{"type": "function", "name": f"tool_v{len(calls)}", "parameters": {}}]

    monkeypatch.setattr(gk, "get_openai_realtime_tools", fake_get)

    async def run():
        session = GrokLiveSession(session_id="t-gk-tools", operator="system")
        ws = FakeUpstreamWS()
        session.grok_ws = ws
        await gk.configure_grok_session(session, "system", "Ara")
        await gk.configure_grok_session(session, "system", "Ara")
        assert calls == ["grok_live", "grok_live"]
        assert ws.sent[-1]["session"]["tools"][0]["name"] == "tool_v2", \
            "second configure must carry the FRESH tool list"
    asyncio.run(run())


def test_realtime_configure_reads_tools_fresh_each_time(monkeypatch):
    _patch_light(monkeypatch, rt)
    calls = []

    def fake_get(group):
        calls.append(group)
        return [{"type": "function", "name": f"tool_v{len(calls)}", "parameters": {}}]

    monkeypatch.setattr(rt, "get_openai_realtime_tools", fake_get)

    async def run():
        session = RealtimeSession(session_id="t-rt-tools", operator="system")
        ws = FakeUpstreamWS()
        session.openai_ws = ws
        await rt.configure_openai_session(session, "system", "ash")
        await rt.configure_openai_session(session, "system", "ash")
        assert calls == ["realtime", "realtime"]
        assert ws.sent[-1]["session"]["tools"][0]["name"] == "tool_v2"
    asyncio.run(run())
