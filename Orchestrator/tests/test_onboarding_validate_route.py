"""POST /onboarding/validate dispatch for the M10 reranker keys (Voyage, Cohere).

Direct-call tests against the route function (no TestClient / app construction):
the validators themselves are unit-tested in test_onboarding_validators.py, so
here we only prove the route ACCEPTS the two new provider ids (the Literal) and
dispatches to the right validator, stamping validated_at on success.
"""
import pytest

from Orchestrator.onboarding.validators import ValidationResult
from Orchestrator.routes import onboarding_routes as ob


@pytest.mark.parametrize("provider,fn_name", [
    ("voyage", "validate_voyage"),
    ("cohere", "validate_cohere"),
])
def test_validate_route_dispatches_reranker_providers(provider, fn_name, monkeypatch):
    called = {}

    def _fake(api_key):
        called["api_key"] = api_key
        return ValidationResult(ok=True, latency_ms=7, detail={"probe": "ok"})

    monkeypatch.setattr(ob.validators, fn_name, _fake)
    stamped = []
    monkeypatch.setattr(ob._state, "record_validation", lambda p: stamped.append(p))

    req = ob.ValidateRequest(provider=provider, credentials={"api_key": "k-123"})
    resp = ob.validate(req)

    assert resp.ok is True
    assert resp.detail == {"probe": "ok"}
    assert called["api_key"] == "k-123"      # dispatched to the right validator
    assert stamped == [provider]             # validated_at stamped on success


@pytest.mark.parametrize("provider", ["voyage", "cohere"])
def test_validate_route_reports_bad_key_without_raising(provider, monkeypatch):
    fn = "validate_voyage" if provider == "voyage" else "validate_cohere"
    monkeypatch.setattr(
        ob.validators, fn,
        lambda k: ValidationResult(ok=False, latency_ms=3, error="RuntimeError: bad key"),
    )
    stamped = []
    monkeypatch.setattr(ob._state, "record_validation", lambda p: stamped.append(p))

    resp = ob.validate(ob.ValidateRequest(provider=provider, credentials={"api_key": "nope"}))
    assert resp.ok is False
    assert "bad key" in resp.error
    assert stamped == []  # a failed validation must NOT stamp validated_at
