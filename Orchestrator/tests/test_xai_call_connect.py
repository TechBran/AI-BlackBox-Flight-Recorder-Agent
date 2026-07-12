"""connect_to_grok URL parameterization: ?call_id= (SIP attach) XOR
?model=[&conversation_id=] (P2.8/P2.13), with a session.call_id fallback so
reconnects rejoin the live call instead of demoting it to a non-call dial."""
import pytest

import Orchestrator.routes.grok_live_routes as glr
from Orchestrator.models import GrokLiveSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS  # P1b shared double


@pytest.fixture
def capture(monkeypatch):
    urls = []

    async def fake_connect(url, **kwargs):
        urls.append(url)
        return FakeUpstreamWS()

    monkeypatch.setattr(glr, "XAI_API_KEY", "test-key")
    monkeypatch.setattr(glr, "WEBSOCKETS_AVAILABLE", True)
    monkeypatch.setattr(glr.websockets, "connect", fake_connect)
    return urls


@pytest.mark.asyncio
async def test_connect_with_call_id(capture):
    session = GrokLiveSession(session_id="phone-xai-c1")
    ok = await glr.connect_to_grok(session, call_id="c1")
    assert ok is True
    assert capture == [f"{glr.GROK_LIVE_URL}?call_id=c1"]
    assert session.call_id == "c1"
    assert session.status == "connected"


@pytest.mark.asyncio
async def test_connect_with_model(capture):
    session = GrokLiveSession(session_id="s1")
    ok = await glr.connect_to_grok(session, model="grok-voice-latest")
    assert ok is True
    assert capture == [f"{glr.GROK_LIVE_URL}?model=grok-voice-latest"]
    assert session.call_id == ""


@pytest.mark.asyncio
async def test_explicit_call_id_excludes_model_and_conversation_id(capture):
    session = GrokLiveSession(session_id="s1")
    with pytest.raises(ValueError):
        await glr.connect_to_grok(session, call_id="c1", model="grok-voice-latest")
    with pytest.raises(ValueError):
        await glr.connect_to_grok(session, call_id="c1", conversation_id="conv_1")
    assert capture == []                             # never dialed


@pytest.mark.asyncio
async def test_reconnect_falls_back_to_session_call_id(capture):
    session = GrokLiveSession(session_id="phone-xai-c9", call_id="c9")
    ok = await glr.connect_to_grok(session)          # bare reconnect shape
    assert ok is True
    assert capture == [f"{glr.GROK_LIVE_URL}?call_id=c9"]


@pytest.mark.asyncio
async def test_call_id_fallback_beats_model_and_conversation_id(capture):
    # grok_reconnect (post-P2.13) passes model=/conversation_id= from the
    # session; on a phone-xai session the call_id fallback must WIN — the
    # args are swallowed (logged, not raised) and the SAME call is rejoined.
    session = GrokLiveSession(session_id="phone-xai-c9", call_id="c9")
    ok = await glr.connect_to_grok(session, model="grok-voice-latest",
                                   conversation_id="conv_stale")
    assert ok is True
    assert capture == [f"{glr.GROK_LIVE_URL}?call_id=c9"]


@pytest.mark.asyncio
async def test_plain_connect_resolves_default_model(capture):
    # Post-P2.8 a plain connect is NOT a bare URL: it resolves to the default
    # allowlisted model bound at the WS URL.
    session = GrokLiveSession(session_id="s1")
    ok = await glr.connect_to_grok(session)
    assert ok is True
    assert capture == [f"{glr.GROK_LIVE_URL}?model={glr.GROK_LIVE_MODEL}"]
    assert session.model == glr.GROK_LIVE_MODEL
