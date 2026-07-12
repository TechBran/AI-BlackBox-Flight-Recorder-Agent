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
