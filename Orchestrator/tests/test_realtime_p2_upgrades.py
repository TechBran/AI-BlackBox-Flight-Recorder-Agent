"""P2 — OpenAI Realtime GA session upgrades: noise_reduction + transcription delay.

Follows the fixtures/conventions of test_live_models.py (stubbed fossil
context, MagicMock session, single-send payload extraction).
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from Orchestrator.routes.realtime_routes import configure_openai_session


@pytest.fixture
def stub_fossil_context(monkeypatch):
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(
        "Orchestrator.routes.realtime_routes.build_fossil_context", _stub
    )


def _make_openai_session(session_id="test-session"):
    session = MagicMock()
    session.session_id = session_id
    session.openai_ws = MagicMock()
    session.openai_ws.send = AsyncMock()
    session.provenance = {}
    session.context_injected = False
    return session


def _extract_payload(send_mock):
    assert send_mock.await_count == 1
    return json.loads(send_mock.await_args.args[0])


# ---------------------------------------------------------------------------
# noise_reduction (GA schema: audio.input.noise_reduction = {type} | null)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_noise_reduction_emitted_when_requested(stub_fossil_context):
    session = _make_openai_session()
    await configure_openai_session(
        session=session, operator="test_operator", voice="ash",
        noise_reduction="far_field",
    )
    audio_input = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]
    assert audio_input["noise_reduction"] == {"type": "far_field"}


@pytest.mark.asyncio
async def test_noise_reduction_absent_by_default_for_portal(stub_fossil_context):
    session = _make_openai_session(session_id="portal-uuid-1234")
    await configure_openai_session(session=session, operator="test_operator", voice="ash")
    audio_input = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]
    assert "noise_reduction" not in audio_input


@pytest.mark.asyncio
async def test_noise_reduction_defaults_near_field_on_phone_bridge(stub_fossil_context):
    """phone/bridge.py sessions are keyed 'phone-<sid>' and call
    configure_openai_session positionally — the near_field default must apply
    with NO new argument at the call sites (signature stays backward-compatible)."""
    session = _make_openai_session(session_id="phone-CA1234567890")
    await configure_openai_session(session=session, operator="system", voice="ash")
    audio_input = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]
    assert audio_input["noise_reduction"] == {"type": "near_field"}


@pytest.mark.asyncio
async def test_noise_reduction_off_sends_explicit_null(stub_fossil_context):
    session = _make_openai_session(session_id="phone-CA999")
    await configure_openai_session(
        session=session, operator="system", voice="ash", noise_reduction="off",
    )
    audio_input = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]
    assert "noise_reduction" in audio_input
    assert audio_input["noise_reduction"] is None


@pytest.mark.asyncio
async def test_noise_reduction_invalid_ignored_with_warning(stub_fossil_context, capsys):
    session = _make_openai_session()
    await configure_openai_session(
        session=session, operator="test_operator", voice="ash",
        noise_reduction="ultra_field",
    )
    audio_input = _extract_payload(session.openai_ws.send)["session"]["audio"]["input"]
    assert "noise_reduction" not in audio_input
    out = capsys.readouterr().out
    assert "noise_reduction" in out and "WARNING" in out
