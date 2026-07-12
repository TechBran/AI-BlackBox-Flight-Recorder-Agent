"""P6a — Translation voice mode: helper validation + minimal-session invariants.

Conventions mirror Orchestrator/tests/test_live_models.py (stubbed fossil
context, MagicMock sessions, single-send payload extraction).
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


# -----------------------------------------------------------------------------
# Shared fixtures/helpers (used by P6.3-P6.6 tests appended below)
# -----------------------------------------------------------------------------

@pytest.fixture
def stub_fossil_context(monkeypatch):
    """Stub build_fossil_context in both route modules (no real snapshot I/O)."""
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(
        "Orchestrator.routes.realtime_routes.build_fossil_context", _stub)
    monkeypatch.setattr(
        "Orchestrator.routes.gemini_live_routes.build_fossil_context", _stub)


def _boom(*args, **kwargs):
    raise AssertionError(
        "build_fossil_context must NEVER be called in translate mode "
        "(translate branch must run BEFORE the persona/context build)")


def _make_openai_session():
    session = MagicMock()
    session.openai_ws = MagicMock()
    session.openai_ws.send = AsyncMock()
    session.provenance = {}
    session.context_injected = False
    return session


def _make_gemini_session():
    session = MagicMock()
    session.gemini_ws = MagicMock()
    session.gemini_ws.send = AsyncMock()
    session.resumption_handle = None
    session.provenance = {}
    session.context_injected = False
    session.voice = ""
    return session


def _extract_payload(send_mock):
    assert send_mock.await_count == 1, (
        f"expected exactly one upstream send, got {send_mock.await_count}")
    return json.loads(send_mock.await_args.args[0])


# -----------------------------------------------------------------------------
# P6.2 — resolve_translate_params / build_translate_instructions / constants
# -----------------------------------------------------------------------------

def test_translate_model_constants():
    from Orchestrator.config import (
        OPENAI_REALTIME_TRANSLATE_MODEL, GEMINI_LIVE_TRANSLATE_MODEL,
        OPENAI_REALTIME_MODELS,
    )
    assert OPENAI_REALTIME_TRANSLATE_MODEL == "gpt-realtime-translate"
    assert GEMINI_LIVE_TRANSLATE_MODEL == "gemini-3.5-live-translate-preview"
    # connect_to_openai validates against the allowlist — the translate model
    # MUST be present there or translate sessions silently bind the chat default.
    assert OPENAI_REALTIME_TRANSLATE_MODEL in {m["id"] for m in OPENAI_REALTIME_MODELS}


def test_gemini_translate_model_not_in_chat_dropdown():
    from Orchestrator.config import GEMINI_LIVE_MODELS, GEMINI_LIVE_TRANSLATE_MODEL
    assert GEMINI_LIVE_TRANSLATE_MODEL not in {m["id"] for m in GEMINI_LIVE_MODELS}


def test_mode_translate_recognized():
    from Orchestrator.routes.voice_translate import resolve_translate_params
    assert resolve_translate_params("translate", "es") == (True, "es")


def test_non_translate_modes_pass_through():
    from Orchestrator.routes.voice_translate import resolve_translate_params
    assert resolve_translate_params(None, "es")[0] is False
    assert resolve_translate_params("", "es")[0] is False
    assert resolve_translate_params("normal", "es")[0] is False


def test_bcp47_region_subtags_accepted():
    from Orchestrator.routes.voice_translate import resolve_translate_params
    assert resolve_translate_params("translate", "pt-BR") == (True, "pt-BR")
    assert resolve_translate_params("translate", "zh-CN") == (True, "zh-CN")


def test_invalid_target_language_falls_back_to_en(capsys):
    from Orchestrator.routes.voice_translate import resolve_translate_params
    assert resolve_translate_params("translate", "not a lang!!") == (True, "en")
    out = capsys.readouterr().out
    assert "WARNING" in out and "target_language" in out


def test_missing_target_language_falls_back_to_en(capsys):
    from Orchestrator.routes.voice_translate import resolve_translate_params
    assert resolve_translate_params("translate", None) == (True, "en")


def test_instructions_are_minimal_and_name_the_language():
    from Orchestrator.routes.voice_translate import build_translate_instructions
    text = build_translate_instructions("fr")
    assert "fr" in text
    assert "translat" in text.lower()
    assert len(text) < 1000  # minimal by design — NOT the persona build


# -----------------------------------------------------------------------------
# P6.3 — OpenAI translate session branch
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openai_translate_session_minimal(monkeypatch):
    # If the translate branch runs BEFORE the persona/context build, this
    # booby-trapped context builder is never reached.
    monkeypatch.setattr(
        "Orchestrator.routes.realtime_routes.build_fossil_context", _boom)
    from Orchestrator.routes.realtime_routes import configure_openai_session

    session = _make_openai_session()
    await configure_openai_session(
        session=session, operator="op", voice="marin",
        mode="translate", target_language="es",
    )
    payload = _extract_payload(session.openai_ws.send)
    assert payload["type"] == "session.update"
    s = payload["session"]
    # Tool-free by design (fast setup — no 56-tool ride-along)
    assert "tools" not in s and "tool_choice" not in s
    # Minimal instructions naming the target language, not the persona build
    assert "es" in s["instructions"]
    assert len(s["instructions"]) < 1000
    # GA shape + user voice preserved
    assert s["type"] == "realtime"
    assert s["audio"]["output"]["voice"] == "marin"
    assert s["audio"]["input"]["turn_detection"]["type"] == "server_vad"


@pytest.mark.asyncio
async def test_openai_default_path_unchanged(stub_fossil_context):
    """Regression pin: mode=None must still build the full persona session."""
    from Orchestrator.routes.realtime_routes import configure_openai_session

    session = _make_openai_session()
    await configure_openai_session(session=session, operator="op", voice="ash")
    payload = _extract_payload(session.openai_ws.send)
    s = payload["session"]
    assert "tools" in s               # full tool catalog still declared
    assert len(s["instructions"]) > 1000  # persona/context build still runs


# -----------------------------------------------------------------------------
# P6.4 — RealtimeSession persists translate mode across reconnects
# -----------------------------------------------------------------------------

def test_realtime_session_persists_translate_fields():
    from Orchestrator.models import RealtimeSession
    s = RealtimeSession(session_id="t")
    assert s.mode == "" and s.target_language == ""  # default = normal session
    s.mode = "translate"
    s.target_language = "es"
    assert (s.mode, s.target_language) == ("translate", "es")
