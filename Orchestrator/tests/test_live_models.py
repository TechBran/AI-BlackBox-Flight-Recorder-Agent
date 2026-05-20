"""T4 — Allowlist + filter + casing invariants for Live Models Upgrade (Phase A).

Pins the audit-critical invariants of T1-T3:

1. Invalid OpenAI vad_type falls back to server_vad (does not raise, warns).
2. /realtime/status filters category=="chat" (4 entries; whisper/translate hidden).
3. idle_timeout_ms suppressed under semantic_vad (per OpenAI SDK).
4. Gemini thinkingLevel emitted ONLY for thinking-capable models.
5. Casing precision on the allowlist constants (guard against future
   "normalize casing" refactors that would silently break upstream APIs).

Per docs/plans/2026-05-19-live-models-upgrade.md → Phase A → T4.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from Orchestrator.config import (
    OPENAI_REALTIME_VAD_TYPES,
    OPENAI_REALTIME_VAD_EAGERNESS,
    GEMINI_LIVE_VAD_SENSITIVITIES,
    GEMINI_LIVE_THINKING_LEVELS,
    GEMINI_LIVE_THINKING_CAPABLE_MODELS,
    OPENAI_REALTIME_MODELS,
    GEMINI_LIVE_MODELS,
)
from Orchestrator.routes.realtime_routes import (
    configure_openai_session,
    realtime_status,
)
from Orchestrator.routes.gemini_live_routes import configure_gemini_session


# -----------------------------------------------------------------------------
# Shared fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def stub_fossil_context(monkeypatch):
    """Stub build_fossil_context to avoid hitting real snapshots during tests.

    Both configure_openai_session and configure_gemini_session call
    build_context_for_operator() which in turn calls build_fossil_context().
    We replace it at the *module-level imported name* in each route module.
    """
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})

    monkeypatch.setattr(
        "Orchestrator.routes.realtime_routes.build_fossil_context", _stub
    )
    monkeypatch.setattr(
        "Orchestrator.routes.gemini_live_routes.build_fossil_context", _stub
    )


def _make_openai_session():
    """RealtimeSession-shaped MagicMock with AsyncMock send."""
    session = MagicMock()
    session.openai_ws = MagicMock()
    session.openai_ws.send = AsyncMock()
    session.provenance = {}
    session.context_injected = False
    return session


def _make_gemini_session():
    """GeminiLiveSession-shaped MagicMock with AsyncMock send."""
    session = MagicMock()
    session.gemini_ws = MagicMock()
    session.gemini_ws.send = AsyncMock()
    session.resumption_handle = None
    session.provenance = {}
    session.context_injected = False
    session.voice = ""
    return session


def _extract_payload(send_mock):
    """Unwrap the JSON string passed to session.*_ws.send() back to dict."""
    assert send_mock.await_count == 1, (
        f"expected exactly one upstream send, got {send_mock.await_count}"
    )
    raw = send_mock.await_args.args[0]
    return json.loads(raw)


# -----------------------------------------------------------------------------
# Test 1: Invalid vad_type falls back to server_vad (does not raise, warns)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_vad_type_falls_back_to_server_vad(stub_fossil_context, capsys):
    session = _make_openai_session()

    # Must not raise
    await configure_openai_session(
        session=session,
        operator="test_operator",
        voice="ash",
        vad_type="evil_attacker_input",
    )

    payload = _extract_payload(session.openai_ws.send)
    turn_detection = payload["session"]["turn_detection"]

    # Bad value rejected -> default server_vad shape preserved
    assert turn_detection["type"] == "server_vad"
    assert "threshold" in turn_detection  # existing server_vad-only field

    # Warning logged via print() (route uses print, not logging)
    captured = capsys.readouterr()
    assert "vad_type" in captured.out
    assert "evil_attacker_input" in captured.out
    assert "WARNING" in captured.out


# -----------------------------------------------------------------------------
# Test 2: /realtime/status filters category=="chat" only
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_realtime_status_filters_non_chat_categories():
    resp = await realtime_status()

    models = resp["models"]
    model_ids = {m["id"] for m in models}

    # Exactly the 4 chat-category models, no whisper/translate
    assert len(models) == 4, f"expected 4 chat models, got {len(models)}: {model_ids}"

    # Every emitted model is category=="chat"
    assert all(m.get("category") == "chat" for m in models), (
        f"non-chat category leaked into dropdown: {[m for m in models if m.get('category') != 'chat']}"
    )

    # Specialized variants explicitly hidden (audit I4)
    assert "gpt-realtime-whisper" not in model_ids, "STT-only model leaked into voice dropdown"
    assert "gpt-realtime-translate" not in model_ids, "translate-only model leaked into voice dropdown"

    # gpt-realtime-2 present + flagged default
    default_models = [m for m in models if m.get("default") is True]
    assert len(default_models) == 1, f"expected exactly one default model, got {default_models}"
    assert default_models[0]["id"] == "gpt-realtime-2"


# -----------------------------------------------------------------------------
# Test 3: idle_timeout_ms suppressed under semantic_vad (per OpenAI SDK)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_timeout_ignored_under_semantic_vad(stub_fossil_context):
    session = _make_openai_session()

    await configure_openai_session(
        session=session,
        operator="test_operator",
        voice="ash",
        vad_type="semantic_vad",
        vad_eagerness="medium",
        idle_timeout_ms=30000,
    )

    payload = _extract_payload(session.openai_ws.send)
    turn_detection = payload["session"]["turn_detection"]

    assert turn_detection["type"] == "semantic_vad"
    assert turn_detection.get("eagerness") == "medium"
    # idle_timeout_ms is server_vad-only per SDK — must NOT leak into semantic_vad payload
    assert "idle_timeout_ms" not in turn_detection, (
        f"idle_timeout_ms leaked into semantic_vad payload: {turn_detection}"
    )


@pytest.mark.asyncio
async def test_idle_timeout_honored_under_server_vad(stub_fossil_context):
    """Positive companion to test_idle_timeout_ignored_under_semantic_vad."""
    session = _make_openai_session()

    await configure_openai_session(
        session=session,
        operator="test_operator",
        voice="ash",
        vad_type="server_vad",
        idle_timeout_ms=45000,
    )

    payload = _extract_payload(session.openai_ws.send)
    turn_detection = payload["session"]["turn_detection"]

    assert turn_detection["type"] == "server_vad"
    assert turn_detection.get("idle_timeout_ms") == 45000


@pytest.mark.asyncio
async def test_idle_timeout_out_of_range_rejected(stub_fossil_context, capsys):
    """Out-of-range idle_timeout values are rejected with a warning, not passed to OpenAI.

    HTML enforces min=5000 max=300000 in the dropdown, but JS parseInt strips
    that constraint, so a stale or hostile client could send idle_timeout_ms=1
    straight through. T14 F2 added a server-side clamp matching the HTML range.
    """
    session = _make_openai_session()

    await configure_openai_session(
        session=session,
        operator="test_operator",
        voice="ash",
        vad_type="server_vad",
        idle_timeout_ms=1,  # way below 5000 minimum
    )

    payload = _extract_payload(session.openai_ws.send)
    turn_detection = payload["session"]["turn_detection"]

    # Out-of-range value must not leak into upstream payload
    assert "idle_timeout_ms" not in turn_detection, (
        f"out-of-range idle_timeout leaked into payload: {turn_detection}"
    )

    # And the route should have logged the ignore reason
    captured = capsys.readouterr()
    assert "idle_timeout_ms" in captured.out
    assert "out of range" in captured.out


# -----------------------------------------------------------------------------
# Test 4: Gemini thinkingLevel emitted ONLY for thinking-capable models
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gemini_thinking_level_suppressed_for_25(stub_fossil_context, capsys):
    session = _make_gemini_session()

    await configure_gemini_session(
        session=session,
        operator="test_operator",
        voice="Charon",
        model="gemini-2.5-flash-native-audio-latest",
        thinking_level="medium",
    )

    payload = _extract_payload(session.gemini_ws.send)
    setup = payload["setup"]

    # generationConfig exists (carries speechConfig + responseModalities)...
    assert "generationConfig" in setup
    # ...but thinkingConfig must NOT be set on a non-thinking-capable model
    assert "thinkingConfig" not in setup["generationConfig"], (
        f"thinkingConfig leaked onto 2.5 model: {setup['generationConfig']}"
    )

    # And the route should have logged the ignore reason
    captured = capsys.readouterr()
    assert "ignored" in captured.out and "thinking" in captured.out.lower()


@pytest.mark.asyncio
async def test_gemini_thinking_level_emitted_for_31(stub_fossil_context):
    session = _make_gemini_session()

    await configure_gemini_session(
        session=session,
        operator="test_operator",
        voice="Charon",
        model="gemini-3.1-flash-live-preview",
        thinking_level="medium",
    )

    payload = _extract_payload(session.gemini_ws.send)
    setup = payload["setup"]

    assert setup["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "medium"


# -----------------------------------------------------------------------------
# Test 5: Casing precision on the allowlists (regression guard for upstream APIs)
# -----------------------------------------------------------------------------

def test_allowlist_casing_precision():
    """Guard against future "normalize casing" refactors silently breaking
    upstream APIs. Each provider mandates a specific case:

    - OpenAI: vad type + eagerness are lowercase
    - Gemini: VAD sensitivities are UPPERCASE (LOW/MEDIUM/HIGH)
    - Gemini: thinking levels are LOWERCASE (per google-genai SDK 1.64.0)
    """
    # OpenAI VAD types: lowercase only
    assert "server_vad" in OPENAI_REALTIME_VAD_TYPES
    assert "semantic_vad" in OPENAI_REALTIME_VAD_TYPES
    assert "SERVER_VAD" not in OPENAI_REALTIME_VAD_TYPES
    assert "Semantic_VAD" not in OPENAI_REALTIME_VAD_TYPES

    # OpenAI VAD eagerness: lowercase only
    assert "medium" in OPENAI_REALTIME_VAD_EAGERNESS
    assert "MEDIUM" not in OPENAI_REALTIME_VAD_EAGERNESS

    # Gemini VAD sensitivities: UPPERCASE only
    assert "LOW" in GEMINI_LIVE_VAD_SENSITIVITIES
    assert "MEDIUM" in GEMINI_LIVE_VAD_SENSITIVITIES
    assert "HIGH" in GEMINI_LIVE_VAD_SENSITIVITIES
    assert "low" not in GEMINI_LIVE_VAD_SENSITIVITIES
    assert "medium" not in GEMINI_LIVE_VAD_SENSITIVITIES

    # Gemini thinking levels: LOWERCASE only (google-genai SDK enum)
    assert "minimal" in GEMINI_LIVE_THINKING_LEVELS
    assert "medium" in GEMINI_LIVE_THINKING_LEVELS
    assert "MINIMAL" not in GEMINI_LIVE_THINKING_LEVELS
    assert "MEDIUM" not in GEMINI_LIVE_THINKING_LEVELS

    # Thinking-capable model set holds the 3.1 preview (T3 invariant)
    assert "gemini-3.1-flash-live-preview" in GEMINI_LIVE_THINKING_CAPABLE_MODELS
    assert "gemini-2.5-flash-native-audio-latest" not in GEMINI_LIVE_THINKING_CAPABLE_MODELS

    # Catalogs non-empty + contain expected anchors
    assert any(m["id"] == "gpt-realtime-2" for m in OPENAI_REALTIME_MODELS)
    assert any(m["id"] == "gemini-3.1-flash-live-preview" for m in GEMINI_LIVE_MODELS)
