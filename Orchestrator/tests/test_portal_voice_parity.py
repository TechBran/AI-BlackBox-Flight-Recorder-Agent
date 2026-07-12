"""Portal voice panels <-> voice-bridge contract guard (P3c Portal voice parity).

The three Portal voice modules (gpt-realtime.js / gemini-live.js /
grok-live.js) are hand-bound to the Orchestrator voice-bridge WS event
vocabulary and the P2/P4 status/preset contracts. There is no JS test
infra, so -- mirroring test_portal_embeddings_card_parity.py -- this is a
deliberate source-text test: it asserts each file still references the
real event names / endpoints / connect-message fields.

NOTE: keep these literals greppable in the JS (no string concatenation)
or update this test alongside.
"""
from pathlib import Path

import pytest

PORTAL = Path(__file__).resolve().parents[2] / "Portal"

# Grown task-by-task through Phase 3c. Each entry: relative path -> literals.
FILE_LITERALS = {
    "modules/gemini-live.js": [
        "case 'text_delta':",
        "case 'user_transcript_delta':",
        "case 'speech_started':",
        "case 'speech_stopped':",
        "appendBubble",
    ],
    "modules/grok-live.js": [
        "populateGrokModelDropdown",
        "bb_grok_live_catalog",
        "model_default",
        "connectMsg.model",
        "reconnectMsg.model",
        "connectMsg.reasoning_effort",
        "reconnectMsg.reasoning_effort",
    ],
    "modules/gpt-realtime.js": [
        "connectMsg.noise_reduction",
        "reconnectMsg.noise_reduction",
        "voice-presets.js",
        "connectMsg.agent",
        "reconnectMsg.agent",
    ],
    "modules/voice-presets.js": [
        "/voice-agents",
        "filterPresetsByProvider",
        "populatePresetDropdown",
        "None (manual config)",
    ],
    "index.html": [
        "vaGrokModelSelect",
        "vaGrokReasoningSelect",
        "vaRealtimeNoiseSelect",
        "vaRealtimePresetSelect",
    ],
    "modules/voice-agents-modal.js": [
        "vaGrokModelSelect",
        "vaGrokReasoningSelect",
        "vaRealtimeNoiseSelect",
        "vaRealtimePresetSelect",
    ],
}

CASES = [(f, lit) for f, lits in FILE_LITERALS.items() for lit in lits]


@pytest.mark.parametrize(
    "relpath,literal", CASES, ids=[f"{f}::{lit}" for f, lit in CASES]
)
def test_portal_voice_contract_literals(relpath, literal):
    src = (PORTAL / relpath).read_text(encoding="utf-8")
    assert literal in src, (
        f"Portal/{relpath} no longer references {literal!r} -- the voice "
        "panel has drifted from the voice-bridge contract (P3c parity)."
    )
