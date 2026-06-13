"""Hermetic tests for GET /elevenlabs/status (the single hydration point).

Network/key are mocked: resolve_api_key + catalog.get_user are patched where the
route LOOKS THEM UP (it imports them inside the handler), so no live ElevenLabs
call is ever made. Gating reads the provider's EXPLICIT capability booleans, so
flipping those booleans must flip the corresponding feature gate.
"""
import pytest
from fastapi.testclient import TestClient

from Orchestrator.app import app
from Orchestrator.elevenlabs import client as el_client
from Orchestrator.elevenlabs import catalog as el_catalog


@pytest.fixture
def cli():
    return TestClient(app)


def test_status_no_key_reports_unconfigured(cli, monkeypatch):
    """No key -> {"configured": False} (every ElevenLabs UI hides)."""
    monkeypatch.setattr(el_client, "resolve_api_key", lambda: None)
    # get_user must never be reached; make it explode if it is.
    monkeypatch.setattr(
        el_catalog, "get_user",
        lambda *a, **k: pytest.fail("get_user called despite no key"),
    )

    resp = cli.get("/elevenlabs/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False}


def test_status_starter_tier_gates_off_professional_cloning(cli, monkeypatch):
    """Starter: IVC True, PVC False -> features reflect the explicit booleans."""
    monkeypatch.setattr(el_client, "resolve_api_key", lambda: "xi-key")
    monkeypatch.setattr(
        el_catalog, "get_user",
        lambda *a, **k: {
            "tier": "starter",
            "credits_remaining": 90000,
            "credits_limit": 100000,
            "can_use_instant_voice_cloning": True,
            "can_use_professional_voice_cloning": False,
        },
    )

    resp = cli.get("/elevenlabs/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["tier"] == "starter"
    assert body["credits_remaining"] == 90000
    assert body["credits_limit"] == 100000
    feats = body["features"]
    # Plan-independent features always on.
    for always_on in ("tts", "stt", "music", "sound_effects",
                       "voice_changer", "voice_isolator", "voice_design"):
        assert feats[always_on] is True
    # Gated features track the explicit booleans.
    assert feats["instant_voice_cloning"] is True
    assert feats["professional_voice_cloning"] is False


def test_status_creator_tier_unlocks_both_cloning_gates(cli, monkeypatch):
    """Flip the booleans (creator-like: both True) -> both cloning gates open.

    Proves gating reads the provider booleans, NOT a hardcoded tier-name list.
    """
    monkeypatch.setattr(el_client, "resolve_api_key", lambda: "xi-key")
    monkeypatch.setattr(
        el_catalog, "get_user",
        lambda *a, **k: {
            "tier": "creator",
            "credits_remaining": 250000,
            "credits_limit": 500000,
            "can_use_instant_voice_cloning": True,
            "can_use_professional_voice_cloning": True,
        },
    )

    resp = cli.get("/elevenlabs/status")
    assert resp.status_code == 200
    feats = resp.json()["features"]
    assert feats["instant_voice_cloning"] is True
    assert feats["professional_voice_cloning"] is True


def test_status_get_user_none_falls_back_to_unknown(cli, monkeypatch):
    """Key present but get_user returns None -> still configured, tier 'unknown',
    cloning gates default to False (no crash)."""
    monkeypatch.setattr(el_client, "resolve_api_key", lambda: "xi-key")
    monkeypatch.setattr(el_catalog, "get_user", lambda *a, **k: None)

    resp = cli.get("/elevenlabs/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["tier"] == "unknown"
    assert body["features"]["instant_voice_cloning"] is False
    assert body["features"]["professional_voice_cloning"] is False
