"""Tests for the conditional `local` (on-device Gemma) model catalog entry
(Task 0.5).

The `local` provider's two models (gemma-4-e2b, gemma-4-e4b) run ON the phone;
the Orchestrator has NO server-side inference for them. The catalog entry exists
ONLY so the Android picker can render them, and ONLY for an operator who has a
verified on-device model (the Task 0.1 attestation registry). For everyone else,
`local` returns an empty model list + a reason, so the picker hides/disables it.

Hermetic: the local-provider registry is pointed at a per-test tmp store and the
cached singleton is nulled (same isolation pattern as test_local_routes.py), so
no real `Orchestrator/local_provider/local_devices.json` is touched.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from Orchestrator.local_provider import registry as registry_module
from Orchestrator.routes import local_routes


@pytest.fixture
def client():
    """TestClient with the startup embedding-sync hook mocked (it spawns a
    daemon thread calling sync_embeddings, which would hit the network)."""
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_src:
        m_src.return_value = {"x": {"vector": [0.1]}}
        from Orchestrator.app import app
        with TestClient(app) as c:
            yield c


@pytest.fixture(autouse=True)
def isolate_local_registry(monkeypatch, tmp_path):
    """Point the local-provider registry at a per-test tmp store AND reset the
    cached module-level singleton.

    HAZARD (mirrors test_local_routes.py): ``get_local_registry()`` caches a
    singleton on first call that captures ``STORE_FILE`` at construction.
    Patching ``STORE_FILE`` WITHOUT also nulling ``_registry`` would leave a
    stale singleton bound to the REAL repo file — polluting it. Reset both.
    """
    monkeypatch.setattr(registry_module, "STORE_FILE", tmp_path / "local_devices.json")
    monkeypatch.setattr(registry_module, "_registry", None)


# ---------------------------------------------------------------------------
# Conditional surfacing via GET /models/local
# ---------------------------------------------------------------------------

def test_local_models_hidden_when_no_device(client):
    """Fresh registry (no attestation) → available False, no models, reason."""
    resp = client.get("/models/local", params={"operator": "Brandon"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["models"] == []
    assert body["reason"] == "no verified on-device model"


def test_local_models_shown_when_verified(client):
    """Attest a device for Brandon, then GET /models/local?operator=Brandon →
    available True, both gemma model ids present, each flagged on_device."""
    # Attest via the real registry endpoint (exercises the actual binding path).
    attest = client.post(
        "/local/device/attest",
        json={
            "operator": "Brandon",
            "device_id": "pixel-9",
            "model_slug": "gemma-4-e4b",
            "version": "1.0",
            "sha256": "abc123",
            "delegate": "gpu",
        },
    )
    assert attest.status_code == 200

    resp = client.get("/models/local", params={"operator": "Brandon"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True

    ids = {m["id"] for m in body["models"]}
    assert "gemma-4-e2b" in ids
    assert "gemma-4-e4b" in ids

    for m in body["models"]:
        assert m["on_device"] is True
        # Descriptive-only entries: no server-side inference path/flag.
        assert "server_inference" not in m


def test_local_models_missing_operator(client):
    """No operator query param → available False (consistent with the rest of
    local_routes' status endpoint, which treats missing operator as
    'unavailable'). We return 200 with available False rather than 400 so the
    picker can render the disabled/empty state without special-casing an error
    status; the device-status endpoint 400s, but a catalog read for nobody is a
    legitimately-empty result, not a malformed request."""
    resp = client.get("/models/local")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["models"] == []
    assert body["reason"] == "no verified on-device model"


def test_local_models_response_is_isolated_from_catalog(client):
    """Mutating a model dict from the /models/local response must NOT corrupt the
    module-level LOCAL_MODELS catalog — a second call returns the original ids.

    Proves the builder's per-entry copy actually isolates callers (the entries
    are flat, so a shallow dict() copy is sufficient today)."""
    # Attest a device so models are returned (gated on a verified binding).
    attest = client.post(
        "/local/device/attest",
        json={
            "operator": "Brandon",
            "device_id": "pixel-9",
            "model_slug": "gemma-4-e4b",
            "version": "1.0",
            "sha256": "abc123",
            "delegate": "gpu",
        },
    )
    assert attest.status_code == 200

    first = client.get("/models/local", params={"operator": "Brandon"})
    assert first.status_code == 200
    models = first.json()["models"]
    assert models[0]["id"] == "gemma-4-e2b"
    # Tamper with the returned dict.
    models[0]["id"] = "tampered"

    # A fresh call must still report the original id — the catalog was untouched.
    second = client.get("/models/local", params={"operator": "Brandon"})
    assert second.status_code == 200
    assert second.json()["models"][0]["id"] == "gemma-4-e2b"
    # And the module-level catalog itself is uncorrupted.
    assert {m["id"] for m in local_routes.LOCAL_MODELS} == {"gemma-4-e2b", "gemma-4-e4b"}


def test_local_catalog_entry_shape():
    """The shared catalog structure carries exactly the two on-device gemma
    models, each flagged on_device True with id/name/provider and NO
    server-inference flag (descriptive entries only)."""
    catalog = local_routes.LOCAL_MODELS
    ids = {m["id"] for m in catalog}
    assert ids == {"gemma-4-e2b", "gemma-4-e4b"}
    for m in catalog:
        assert m["on_device"] is True
        assert m["provider"] == "local"
        assert isinstance(m.get("name"), str) and m["name"]
        assert "server_inference" not in m
