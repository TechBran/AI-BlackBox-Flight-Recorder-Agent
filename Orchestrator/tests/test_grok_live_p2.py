"""P2 — Grok Voice Agent modernization: catalog, model addressing, session params.

Conventions mirror test_live_models.py (stubbed fossil context, MagicMock
sessions, single-send payload extraction)."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from Orchestrator.config import (
    GROK_LIVE_MODEL,
    GROK_LIVE_MODELS,
    GROK_LIVE_VOICES,
)


def test_grok_catalog_contents():
    """P0 probe (diagnostics/voice_probes/results/) verified both ids at
    wss://api.x.ai/v1/realtime?model=. grok-voice-fast-1.0 is deprecated
    upstream; 'grok-voice-agent' was never a real model id — neither belongs."""
    ids = {m["id"] for m in GROK_LIVE_MODELS}
    assert ids == {"grok-voice-latest", "grok-voice-think-fast-1.0"}

    defaults = [m for m in GROK_LIVE_MODELS if m.get("default") is True]
    assert len(defaults) == 1
    assert defaults[0]["id"] == "grok-voice-latest"
    assert GROK_LIVE_MODEL == "grok-voice-latest"

    assert "grok-voice-agent" not in ids
    assert "grok-voice-fast-1.0" not in ids


def test_grok_voices_unchanged():
    """Voice list is a separate contract (Portal/Android hydrate from
    /grok-live/status) — the catalog addition must not disturb it."""
    assert GROK_LIVE_VOICES == ["Ara", "Rex", "Sal", "Eve", "Leo"]


# ---------------------------------------------------------------------------
# connect_to_grok — model bound at the WS URL (mirrors OpenAI/Gemini patterns)
# ---------------------------------------------------------------------------

from Orchestrator.models import GrokLiveSession


@pytest.fixture
def fake_grok_dial(monkeypatch):
    """Capture the websockets.connect URL without any network."""
    import Orchestrator.routes.grok_live_routes as gl
    monkeypatch.setattr(gl, "XAI_API_KEY", "test-key")
    monkeypatch.setattr(gl, "WEBSOCKETS_AVAILABLE", True)
    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        return MagicMock()

    monkeypatch.setattr(gl.websockets, "connect", fake_connect)
    return captured


@pytest.mark.asyncio
async def test_connect_to_grok_default_model_in_url(fake_grok_dial):
    from Orchestrator.routes.grok_live_routes import connect_to_grok
    session = GrokLiveSession(session_id="t1", created_at="")
    assert await connect_to_grok(session) is True
    assert fake_grok_dial["url"] == "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
    assert session.model == "grok-voice-latest"


@pytest.mark.asyncio
async def test_connect_to_grok_pinned_model(fake_grok_dial):
    from Orchestrator.routes.grok_live_routes import connect_to_grok
    session = GrokLiveSession(session_id="t2", created_at="")
    assert await connect_to_grok(session, model="grok-voice-think-fast-1.0") is True
    assert fake_grok_dial["url"].endswith("?model=grok-voice-think-fast-1.0")
    assert session.model == "grok-voice-think-fast-1.0"


@pytest.mark.asyncio
async def test_connect_to_grok_invalid_model_falls_back(fake_grok_dial, capsys):
    from Orchestrator.routes.grok_live_routes import connect_to_grok
    session = GrokLiveSession(session_id="t3", created_at="")
    assert await connect_to_grok(session, model="grok-voice-agent") is True
    assert fake_grok_dial["url"].endswith("?model=grok-voice-latest")
    assert session.model == "grok-voice-latest"
    out = capsys.readouterr().out
    assert "WARNING" in out and "grok-voice-agent" in out


# ---------------------------------------------------------------------------
# Endpoint plumbing + /grok-live/status catalog surface
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient

from Orchestrator.checkpoint import app
from Orchestrator.routes.grok_live_routes import grok_live_status


@pytest.mark.asyncio
async def test_grok_status_serves_models_and_default():
    resp = await grok_live_status()
    assert resp["model_default"] == "grok-voice-latest"
    assert {m["id"] for m in resp["models"]} == {
        "grok-voice-latest", "grok-voice-think-fast-1.0",
    }
    # Existing contract stays additive (3-surface rule)
    assert resp["voices"] == GROK_LIVE_VOICES
    assert "default_voice" in resp and "sample_rate" in resp


@pytest.fixture
def grok_relay_stubs(monkeypatch):
    import Orchestrator.routes.grok_live_routes as gl
    monkeypatch.setattr(gl, "XAI_API_KEY", "test-key")
    monkeypatch.setattr(gl, "WEBSOCKETS_AVAILABLE", True)

    connect_mock = AsyncMock()

    async def fake_connect(session, model=None):
        session.model = model or gl.GROK_LIVE_MODEL
        session.status = "connected"
        connect_mock(session, model=model)
        return True

    configure_mock = AsyncMock()
    monkeypatch.setattr(gl, "connect_to_grok", fake_connect)
    monkeypatch.setattr(gl, "configure_grok_session", configure_mock)
    monkeypatch.setattr(gl, "save_grok_session_to_blackbox", AsyncMock())

    async def _noop(session):
        return None

    monkeypatch.setattr(gl, "grok_listener", _noop)
    monkeypatch.setattr(gl, "grok_keepalive_loop", _noop)
    return connect_mock, configure_mock


def test_connected_event_reports_resolved_model(grok_relay_stubs):
    connect_mock, _ = grok_relay_stubs
    client = TestClient(app)
    with client.websocket_connect("/ws/grok-live/p2-grok-ep-1") as ws:
        ws.send_text(json.dumps({
            "type": "connect", "operator": "test_operator",
            "model": "grok-voice-think-fast-1.0",
        }))
        assert ws.receive_json()["type"] == "status"
        connected = ws.receive_json()
        ws.send_text(json.dumps({"type": "disconnect"}))

    assert connected["type"] == "connected"
    # The cosmetic "grok-voice-agent" label is dead — real resolved model only.
    assert connected["data"]["model"] == "grok-voice-think-fast-1.0"
    assert connect_mock.call_args.kwargs["model"] == "grok-voice-think-fast-1.0"


def test_model_query_param_fallback(grok_relay_stubs):
    connect_mock, _ = grok_relay_stubs
    client = TestClient(app)
    with client.websocket_connect("/ws/grok-live/p2-grok-ep-2?model=grok-voice-think-fast-1.0") as ws:
        ws.send_text(json.dumps({"type": "connect", "operator": "test_operator"}))
        ws.receive_json()
        connected = ws.receive_json()
        ws.send_text(json.dumps({"type": "disconnect"}))

    assert connected["data"]["model"] == "grok-voice-think-fast-1.0"


# ---------------------------------------------------------------------------
# reasoning.effort — think-fast models only (mirror Gemini thinkingLevel gate)
# ---------------------------------------------------------------------------

from Orchestrator.routes.grok_live_routes import configure_grok_session


@pytest.fixture
def stub_grok_fossil_context(monkeypatch):
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(
        "Orchestrator.routes.grok_live_routes.build_fossil_context", _stub
    )


def _make_grok_session(model="grok-voice-latest"):
    session = MagicMock()
    session.session_id = "test-grok"
    session.grok_ws = MagicMock()
    session.grok_ws.send = AsyncMock()
    session.model = model
    session.provenance = {}
    session.context_injected = False
    return session


def _extract_grok_payload(send_mock):
    assert send_mock.await_count == 1
    return json.loads(send_mock.await_args.args[0])


@pytest.mark.asyncio
async def test_reasoning_effort_emitted_for_capable_model(stub_grok_fossil_context):
    session = _make_grok_session(model="grok-voice-think-fast-1.0")
    await configure_grok_session(session, "test_operator", voice="Ara",
                                 reasoning_effort="high")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert payload["session"]["reasoning"] == {"effort": "high"}


@pytest.mark.asyncio
async def test_reasoning_effort_suppressed_for_unknown_model(stub_grok_fossil_context, capsys):
    session = _make_grok_session(model="")  # legacy session with no resolved model
    await configure_grok_session(session, "test_operator", voice="Ara",
                                 reasoning_effort="high")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert "reasoning" not in payload["session"]
    assert "reasoning" in capsys.readouterr().out.lower()


@pytest.mark.asyncio
async def test_reasoning_effort_invalid_value_ignored(stub_grok_fossil_context, capsys):
    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara",
                                 reasoning_effort="maximum_overdrive")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert "reasoning" not in payload["session"]
    assert "WARNING" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_reasoning_absent_by_default(stub_grok_fossil_context):
    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert "reasoning" not in payload["session"]


# ---------------------------------------------------------------------------
# Input transcription — explicit opt-in (user turns must reach saved transcripts)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_input_transcription_explicitly_configured(stub_grok_fossil_context):
    """Recon 2026-07-11: session.update never configured input transcription —
    user turns silently relied on an undocumented xAI default. Mirror
    realtime_routes.py:531 (audio.input.transcription) explicitly."""
    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara")
    audio_input = _extract_grok_payload(session.grok_ws.send)["session"]["audio"]["input"]
    assert isinstance(audio_input.get("transcription"), dict)


# ---------------------------------------------------------------------------
# Session resumption — resumption.enabled + conversation.id capture
# ---------------------------------------------------------------------------

from Orchestrator.routes.grok_live_routes import handle_grok_message


@pytest.mark.asyncio
async def test_resumption_enabled_in_session_update(stub_grok_fossil_context):
    session = _make_grok_session()
    await configure_grok_session(session, "test_operator", voice="Ara")
    payload = _extract_grok_payload(session.grok_ws.send)
    assert payload["session"]["resumption"] == {"enabled": True}


@pytest.mark.asyncio
async def test_conversation_created_captures_id():
    session = GrokLiveSession(session_id="t-resume", created_at="")
    await handle_grok_message(session, {
        "type": "conversation.created",
        "conversation": {"id": "conv_abc123"},
    })
    assert session.conversation_id == "conv_abc123"


@pytest.mark.asyncio
async def test_conversation_created_without_id_is_harmless():
    session = GrokLiveSession(session_id="t-resume-2", created_at="")
    await handle_grok_message(session, {"type": "conversation.created"})
    assert session.conversation_id is None


# ---------------------------------------------------------------------------
# Reconnect resumes the server-side conversation (no context rebuild)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_to_grok_appends_conversation_id(fake_grok_dial):
    from Orchestrator.routes.grok_live_routes import connect_to_grok
    session = GrokLiveSession(session_id="t-resume-url", created_at="")
    assert await connect_to_grok(session, model="grok-voice-latest",
                                 conversation_id="conv_xyz") is True
    assert fake_grok_dial["url"] == (
        "wss://api.x.ai/v1/realtime?model=grok-voice-latest&conversation_id=conv_xyz"
    )


@pytest.mark.asyncio
async def test_reconnect_with_conversation_id_skips_rebuild(monkeypatch):
    import Orchestrator.routes.grok_live_routes as gl
    session = GrokLiveSession(session_id="t-rc-1", created_at="")
    session.model = "grok-voice-latest"
    session.conversation_id = "conv_resume_me"
    session.operator = "test_operator"

    connect_mock = AsyncMock(return_value=True)
    configure_mock = AsyncMock()
    monkeypatch.setattr(gl, "connect_to_grok", connect_mock)
    monkeypatch.setattr(gl, "configure_grok_session", configure_mock)

    await gl.grok_reconnect(session)

    assert connect_mock.await_args.kwargs["conversation_id"] == "conv_resume_me"
    assert connect_mock.await_args.kwargs["model"] == "grok-voice-latest"
    configure_mock.assert_not_awaited()  # resumed — no context rebuild
    assert session.status == "connected"


@pytest.mark.asyncio
async def test_reconnect_without_conversation_id_rebuilds(monkeypatch):
    import Orchestrator.routes.grok_live_routes as gl
    session = GrokLiveSession(session_id="t-rc-2", created_at="")
    session.operator = "test_operator"

    connect_mock = AsyncMock(return_value=True)
    configure_mock = AsyncMock()
    monkeypatch.setattr(gl, "connect_to_grok", connect_mock)
    monkeypatch.setattr(gl, "configure_grok_session", configure_mock)

    await gl.grok_reconnect(session)

    configure_mock.assert_awaited_once()  # legacy full-rebuild path preserved
