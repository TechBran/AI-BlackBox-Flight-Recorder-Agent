"""P1.9 — Gemini Live default model = gemini-3.1-flash-live-preview.

Research 2026-07-11: 3.1-flash-live-preview is THE recommended Live model
(probe-confirmed alive); the 2.5 native-audio line is deprecated. Android
already defaults to 3.1 — this aligns config.py and /gemini-live/status
(one canonical default). GEMINI_LIVE_MODEL env override must still win.
"""
import os

import pytest

from Orchestrator.config import GEMINI_LIVE_MODEL, GEMINI_LIVE_MODELS


def test_catalog_default_is_31_preview():
    defaults = [m["id"] for m in GEMINI_LIVE_MODELS if m.get("default")]
    assert defaults == ["gemini-3.1-flash-live-preview"]


def test_module_default_resolves_env_then_31_preview():
    # Same expression as config.py — pins the fallback literal when the env
    # var is unset, and stays true when an operator overrides it.
    assert GEMINI_LIVE_MODEL == os.getenv(
        "GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview"
    )


@pytest.mark.asyncio
async def test_status_endpoint_reflects_default():
    from Orchestrator.routes.gemini_live_routes import gemini_live_status

    data = await gemini_live_status()
    assert data["model_default"] == GEMINI_LIVE_MODEL
    assert [m["id"] for m in data["models"] if m.get("default")] == [
        "gemini-3.1-flash-live-preview"
    ]
