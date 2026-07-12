"""P6 workstream 5 — Gemini affective dialog + proactive audio (2.5-native-audio family, v1alpha only).

Pins:
1. gemini_live_url() derives v1beta/v1alpha endpoints; GEMINI_LIVE_URL back-compat exact.
2. GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS = the 2.5-native-audio family EXACTLY (3.1 rejects the fields).
Later tasks (P6.11-P6.13) append session-persistence, flag-resolution, and setup-emission tests here.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from Orchestrator.config import (
    GEMINI_LIVE_URL,
    GEMINI_LIVE_URL_TEMPLATE,
    gemini_live_url,
    GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS,
)


def test_gemini_live_url_versions():
    assert gemini_live_url() == GEMINI_LIVE_URL
    assert ".v1beta." in gemini_live_url()
    assert gemini_live_url("v1alpha") == GEMINI_LIVE_URL.replace("v1beta", "v1alpha")
    with pytest.raises(ValueError):
        gemini_live_url("v2wrong")


def test_gemini_live_url_backcompat_exact():
    # Byte-exact guard: routes + phone bridge import this constant today.
    assert GEMINI_LIVE_URL == (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    )


def test_affective_capable_models_exact():
    # 2.5-native-audio family ONLY — 3.1 rejects enableAffectiveDialog/proactivity in setup.
    assert GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS == frozenset({
        "gemini-2.5-flash-native-audio-latest",
        "gemini-2.5-flash-native-audio-preview-12-2025",
    })
    assert "gemini-3.1-flash-live-preview" not in GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS
