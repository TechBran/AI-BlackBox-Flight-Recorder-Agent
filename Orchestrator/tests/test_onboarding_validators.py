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


# --- validate_cohere (M10.0) --------------------------------------------------

def test_validate_cohere_valid_key_surfaces_org(monkeypatch):
    """200 from the zero-cost check-api-key endpoint -> ok + organization detail."""
    def _fake_post(url, headers=None, timeout=None, **kwargs):
        assert url == "https://api.cohere.ai/v1/check-api-key"
        assert headers == {"Authorization": "Bearer co-good"}
        return _FakeResponse(200, {"valid": True, "organization_name": "Acme Corp"})

    monkeypatch.setattr("requests.post", _fake_post)

    result = validators.validate_cohere("co-good")
    assert result.ok is True
    assert result.error is None
    assert result.detail is not None
    assert result.detail["organization"] == "Acme Corp"


def test_validate_cohere_invalid_key_401(monkeypatch):
    """401 -> ok False with a clear error, no detail, never raises."""
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: _FakeResponse(401, {"message": "invalid api token"}),
    )
    result = validators.validate_cohere("co-bad")
    assert result.ok is False
    assert result.error is not None
    assert "Invalid Cohere API key" in result.error
    assert result.detail is None


def test_validate_cohere_never_raises_on_transport_error(monkeypatch):
    """A transport exception is captured as ok False, never propagated."""
    def _boom(*a, **k):
        raise ConnectionError("dns failure")

    monkeypatch.setattr("requests.post", _boom)
    result = validators.validate_cohere("co-x")
    assert result.ok is False
    assert result.error is not None


# --- validate_voyage (M10.0) --------------------------------------------------

def test_validate_voyage_valid_key_uses_one_document_probe(monkeypatch):
    """200 from a tiny 1-document rerank -> ok. The probe MUST send exactly one
    document (stays under the free-tier 10K-TPM cap a 40-doc call exceeds)."""
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        assert url == "https://api.voyageai.com/v1/rerank"
        assert headers["Authorization"] == "Bearer pa-good"
        captured["json"] = json
        return _FakeResponse(200, {"model": "rerank-2.5",
                                   "data": [{"index": 0, "relevance_score": 0.9}]})

    monkeypatch.setattr("requests.post", _fake_post)

    result = validators.validate_voyage("pa-good")
    assert result.ok is True
    assert result.error is None
    assert result.detail is not None
    assert result.detail["model"] == "rerank-2.5"
    # 1-document discipline (the M8 free-tier finding).
    assert len(captured["json"]["documents"]) == 1


def test_validate_voyage_invalid_key_401(monkeypatch):
    """401 -> ok False with a clear error, no detail, never raises."""
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: _FakeResponse(401, {"detail": "Provided API key is invalid."}),
    )
    result = validators.validate_voyage("pa-bad")
    assert result.ok is False
    assert result.error is not None
    assert "Invalid Voyage API key" in result.error
    assert result.detail is None


def test_validate_voyage_never_raises_on_transport_error(monkeypatch):
    """A transport exception is captured as ok False, never propagated."""
    def _boom(*a, **k):
        raise ConnectionError("timeout")

    monkeypatch.setattr("requests.post", _boom)
    result = validators.validate_voyage("pa-x")
    assert result.ok is False
    assert result.error is not None
