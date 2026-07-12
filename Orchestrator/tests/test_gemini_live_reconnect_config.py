"""P1.4 — session config survives the reconnect reconfigure.

gemini_reconnect calls configure_gemini_session(session, operator, voice) with
no model/VAD/thinking/custom_role/phone_mode kwargs. Before P1.4 that reverted
the session to the default model with default VAD/thinking and dropped any
outbound-call custom role (recon finding #5). Pin: (a) configure persists the
validated config onto the session; (b) the EXACT bare reconnect call shape
re-emits the same setup payload extensions.
"""
import json

import pytest

from Orchestrator.routes.gemini_live_routes import configure_gemini_session
from Orchestrator.tests.gemini_live_fakes import (
    FakeGeminiWS,
    make_session,
    stub_fossil_context,
)


@pytest.fixture
def no_fossils(monkeypatch):
    stub_fossil_context(monkeypatch)


def _last_setup(ws: FakeGeminiWS) -> dict:
    return json.loads(ws.send.await_args.args[0])["setup"]


@pytest.mark.asyncio
async def test_configure_persists_config_on_session(no_fossils):
    session = make_session(gemini_ws=FakeGeminiWS())

    await configure_gemini_session(
        session,
        "test_operator",
        "Orus",
        model="gemini-3.1-flash-live-preview",
        vad_sensitivity_start="LOW",
        vad_sensitivity_end="HIGH",
        thinking_level="low",
        phone_mode=False,
    )

    assert session.model == "gemini-3.1-flash-live-preview"
    assert session.vad_sensitivity_start == "LOW"
    assert session.vad_sensitivity_end == "HIGH"
    assert session.thinking_level == "low"
    assert session.custom_role == ""
    assert session.phone_mode is False


@pytest.mark.asyncio
async def test_bare_reconfigure_reuses_persisted_config(no_fossils):
    session = make_session(gemini_ws=FakeGeminiWS())

    await configure_gemini_session(
        session,
        "test_operator",
        "Orus",
        model="gemini-3.1-flash-live-preview",
        vad_sensitivity_start="LOW",
        vad_sensitivity_end="HIGH",
        thinking_level="low",
    )

    # Second configure: the EXACT call shape gemini_reconnect uses.
    session.gemini_ws = FakeGeminiWS()
    await configure_gemini_session(session, session.operator, session.voice)

    setup = _last_setup(session.gemini_ws)
    assert setup["model"] == "models/gemini-3.1-flash-live-preview"
    aad = setup["realtimeInputConfig"]["automaticActivityDetection"]
    assert aad["startOfSpeechSensitivity"] == "START_SENSITIVITY_LOW"
    assert aad["endOfSpeechSensitivity"] == "END_SENSITIVITY_HIGH"
    assert setup["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"
