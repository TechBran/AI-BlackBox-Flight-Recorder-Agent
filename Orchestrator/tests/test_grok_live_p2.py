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
