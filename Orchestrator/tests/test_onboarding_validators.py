"""Hermetic tests for onboarding key validators.

Network is mocked at requests.get so no live call is ever made. The live
curls in the wizard/plan are the separate manual verification step.
"""
import pytest

from Orchestrator.onboarding import validators


class _FakeResponse:
    """Minimal stand-in for requests.Response covering what validators touch."""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")


# --- validate_elevenlabs ------------------------------------------------------

def test_validate_elevenlabs_valid_key_surfaces_tier_and_cloning(monkeypatch):
    """200 + Starter subscription -> ok, tier, credits, cloning-available string."""
    payload = {
        "subscription": {
            "tier": "starter",
            "can_use_instant_voice_cloning": True,
            "character_limit": 90000,
            "character_count": 0,
        }
    }

    def _fake_get(url, headers=None, timeout=None, **kwargs):
        assert url == "https://api.elevenlabs.io/v1/user"
        assert headers == {"xi-api-key": "xi-good"}
        return _FakeResponse(200, payload)

    monkeypatch.setattr("requests.get", _fake_get)

    result = validators.validate_elevenlabs("xi-good")
    assert result.ok is True
    assert result.error is None
    assert result.detail is not None
    assert result.detail["tier"] == "starter"
    assert result.detail["credits_remaining"] == 90000
    assert "cloning available" in result.detail["features"]


def test_validate_elevenlabs_no_cloning_on_plan(monkeypatch):
    """can_use_instant_voice_cloning False -> 'not available' string (explicit boolean,
    not tier-name inference)."""
    payload = {
        "subscription": {
            "tier": "free",
            "can_use_instant_voice_cloning": False,
            "character_limit": 10000,
            "character_count": 2500,
        }
    }
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _FakeResponse(200, payload),
    )

    result = validators.validate_elevenlabs("xi-free")
    assert result.ok is True
    assert result.detail["tier"] == "free"
    assert result.detail["credits_remaining"] == 7500
    assert "not available" in result.detail["features"]


def test_validate_elevenlabs_invalid_key_401(monkeypatch):
    """401 -> ok False with an error message mentioning the bad key."""
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _FakeResponse(401, {"detail": "unauthorized"}),
    )

    result = validators.validate_elevenlabs("xi-bad")
    assert result.ok is False
    assert result.error is not None
    assert "Invalid ElevenLabs API key" in result.error
    assert result.detail is None


def test_validate_elevenlabs_missing_subscription_defaults(monkeypatch):
    """200 with no subscription block -> defaults (tier free, 0 credits) without raising."""
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _FakeResponse(200, {}),
    )

    result = validators.validate_elevenlabs("xi-empty")
    assert result.ok is True
    assert result.detail["tier"] == "free"
    assert result.detail["credits_remaining"] == 0
