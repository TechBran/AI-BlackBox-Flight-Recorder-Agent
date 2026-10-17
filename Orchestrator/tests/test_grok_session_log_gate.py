"""Grok realtime keeps the operator's memory out of the journal (audit
O09-live-voice-providers-12).

The session.update carries the system instructions built from the operator's
recent snapshots/checkpoint (up to 50,000 chars), the custom role and every
tool schema; it used to be dumped to journald on every configure, and the
echoed session.updated again. Now only tool names and the instructions length
are logged; the full dump is opt-in via VOICE_DEBUG.

Hermetic: fake upstream WebSocket, stubbed context/persona/tools.
"""
import asyncio

import pytest

import Orchestrator.routes.grok_live_routes as gk
from Orchestrator.models import GrokLiveSession
from Orchestrator.routes.voice_ws_shared import voice_debug_enabled
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS

MEMORY = "PRIVATE-MEMORY-SENTINEL-snapshot-text"


@pytest.fixture
def light_grok(monkeypatch):
    monkeypatch.setattr(gk, "build_context_for_operator",
                        lambda operator, user_text="": (MEMORY, {}))
    monkeypatch.setattr(gk, "get_persona", lambda operator, modality: "Test persona.")
    monkeypatch.setattr(gk, "get_openai_realtime_tools",
                        lambda group: [{"type": "function", "name": "tool_a", "parameters": {}}])
    return gk


def _configure():
    session = GrokLiveSession(session_id="t-gk-log", operator="system")
    ws = FakeUpstreamWS()
    session.grok_ws = ws
    asyncio.run(gk.configure_grok_session(session, "system", "Ara"))
    return ws


def test_grok_configure_does_not_dump_session_by_default(light_grok, monkeypatch, capsys):
    monkeypatch.delenv("VOICE_DEBUG", raising=False)
    ws = _configure()
    out = capsys.readouterr().out
    assert any(MEMORY in str(f) for f in ws.sent), "the session itself still carries the context"
    assert MEMORY not in out
    assert "Full config" not in out
    assert "tool_a" in out, "tool names are still logged"


def test_grok_configure_full_dump_is_opt_in(light_grok, monkeypatch, capsys):
    monkeypatch.setenv("VOICE_DEBUG", "1")
    _configure()
    assert MEMORY in capsys.readouterr().out


def test_grok_session_updated_echo_is_not_dumped_by_default(monkeypatch, capsys):
    monkeypatch.delenv("VOICE_DEBUG", raising=False)
    session = GrokLiveSession(session_id="t-gk-upd", operator="system")
    event = {"type": "session.updated",
             "session": {"instructions": MEMORY, "tools": [{"name": "tool_a"}]}}
    asyncio.run(gk.handle_grok_message(session, event))
    out = capsys.readouterr().out
    assert MEMORY not in out
    assert "tool_a" in out


@pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
def test_voice_debug_off_values(monkeypatch, value):
    monkeypatch.setenv("VOICE_DEBUG", value)
    assert voice_debug_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_voice_debug_on_values(monkeypatch, value):
    monkeypatch.setenv("VOICE_DEBUG", value)
    assert voice_debug_enabled() is True
