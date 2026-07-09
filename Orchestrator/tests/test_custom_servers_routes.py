# Orchestrator/tests/test_custom_servers_routes.py
"""/onboarding/custom-servers CRUD + /onboarding/validate dispatch for provider 'custom'.

Two-tier house pattern:
- /validate dispatch + stamping: DIRECT route-function calls with monkeypatched
  ob.validators.validate_custom and ob._state.record_validation (precedent:
  test_onboarding_validate_route.py).
- CRUD roundtrip: TestClient over a minimal FastAPI app mounting the router
  (precedent: test_device_routes_mesh.py).

Both tiers monkeypatch custom_servers.REGISTRY_PATH to a tmp_path so no test
ever touches the real credentials/custom_models.json.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator.onboarding import custom_servers as cs
from Orchestrator.onboarding.validators import ValidationResult
from Orchestrator.routes import onboarding_routes as ob


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    path = tmp_path / "custom_models.json"
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(path))
    return path


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ob.router)
    return TestClient(app)


# ------------------------------------------------------------------ CRUD tier

def test_crud_roundtrip(client, tmp_registry):
    r = client.post("/onboarding/custom-servers", json={
        "alias": "gemma-box", "base_url": "http://192.168.1.50:8080/v1",
        "api_key": "sk-x", "context_tokens": 32768})
    assert r.status_code == 200
    created = r.json()["server"]
    assert "api_key" not in created          # never echo the secret back
    assert created["key_present"] is True
    assert created["key_last4"] == "sk-x"[-4:]
    sid = created["id"]

    listing = client.get("/onboarding/custom-servers").json()["servers"]
    assert len(listing) == 1
    assert listing[0]["key_last4"] == "sk-x"[-4:] and "api_key" not in listing[0]

    r = client.patch(f"/onboarding/custom-servers/{sid}", json={"alias": "box2"})
    assert r.status_code == 200
    assert r.json()["server"]["alias"] == "box2"
    assert "api_key" not in r.json()["server"]

    assert client.delete(f"/onboarding/custom-servers/{sid}").status_code == 200
    assert client.get("/onboarding/custom-servers").json()["servers"] == []


def test_add_duplicate_alias_400(client, tmp_registry):
    assert client.post("/onboarding/custom-servers", json={
        "alias": "box", "base_url": "http://x/v1"}).status_code == 200
    r = client.post("/onboarding/custom-servers", json={
        "alias": "box", "base_url": "http://y/v1"})
    assert r.status_code == 400
    assert "already in use" in r.json()["detail"]


def test_add_bad_base_url_400(client, tmp_registry):
    r = client.post("/onboarding/custom-servers", json={
        "alias": "box", "base_url": "192.168.1.50:8080/v1"})  # no scheme
    assert r.status_code == 400
    assert "http" in r.json()["detail"]


def test_patch_unknown_id_404(client, tmp_registry):
    r = client.patch("/onboarding/custom-servers/srv-nope", json={"alias": "x"})
    assert r.status_code == 404


def test_patch_bad_value_400(client, tmp_registry):
    sid = client.post("/onboarding/custom-servers", json={
        "alias": "box", "base_url": "http://x/v1"}).json()["server"]["id"]
    r = client.patch(f"/onboarding/custom-servers/{sid}",
                     json={"base_url": "not-a-url"})
    assert r.status_code == 400


def test_delete_unknown_id_404(client, tmp_registry):
    assert client.delete("/onboarding/custom-servers/srv-nope").status_code == 404


def test_patch_base_url_invalidates_validation_stamps(client, tmp_registry):
    """Re-pointing a server must clear validated_at/last_models — the old
    URL's model list must not survive, or resolve_model could route
    unqualified ids to a server that no longer hosts them."""
    sid = client.post("/onboarding/custom-servers", json={
        "alias": "box", "base_url": "http://x/v1"}).json()["server"]["id"]
    cs.update_server(sid, {"validated_at": "2026-07-08T00:00:00+00:00",
                           "last_models": ["old-model"]})

    r = client.patch(f"/onboarding/custom-servers/{sid}",
                     json={"base_url": "http://y:8080/v1"})
    assert r.status_code == 200

    listing = client.get("/onboarding/custom-servers").json()["servers"]
    assert listing[0]["validated_at"] is None
    stored = cs.get_server(sid)
    assert stored["validated_at"] is None
    assert stored["last_models"] == []


# ------------------------------------------------- /validate dispatch tier

def _fake_ok(called, models=("m1", "m2")):
    def _fn(base_url, api_key=""):
        called["base_url"] = base_url
        called["api_key"] = api_key
        return ValidationResult(
            ok=True, latency_ms=5,
            detail={"model_count": len(models), "models": list(models)})
    return _fn


def test_validate_custom_provider_dispatch(tmp_registry, monkeypatch):
    srv = cs.add_server(alias="gemma-box",
                        base_url="http://192.168.1.50:8080/v1", api_key="sk-x")
    sid = srv["id"]
    called = {}
    monkeypatch.setattr(ob.validators, "validate_custom", _fake_ok(called))
    stamped = []
    monkeypatch.setattr(ob._state, "record_validation", lambda p: stamped.append(p))

    resp = ob.validate(ob.ValidateRequest(provider="custom", credentials={
        "base_url": "http://192.168.1.50:8080/v1",
        "api_key": "sk-x", "server_id": sid}))

    assert resp.ok is True
    assert called == {"base_url": "http://192.168.1.50:8080/v1", "api_key": "sk-x"}
    assert stamped == [f"custom:{sid}"]      # per-server key, NOT bare "custom"
    updated = cs.get_server(sid)
    assert updated["validated_at"]           # stamped on success
    assert updated["last_models"] == ["m1", "m2"]


def test_validate_custom_server_id_only_resolves_from_registry(tmp_registry, monkeypatch):
    """Stored-server re-validation: credentials carry ONLY server_id;
    base_url + api_key resolve from the registry."""
    srv = cs.add_server(alias="box", base_url="http://10.0.0.2:8080/v1",
                        api_key="sk-stored")
    called = {}
    monkeypatch.setattr(ob.validators, "validate_custom", _fake_ok(called, models=("m",)))
    stamped = []
    monkeypatch.setattr(ob._state, "record_validation", lambda p: stamped.append(p))

    resp = ob.validate(ob.ValidateRequest(
        provider="custom", credentials={"server_id": srv["id"]}))

    assert resp.ok is True
    assert called["base_url"] == "http://10.0.0.2:8080/v1"
    assert called["api_key"] == "sk-stored"
    assert stamped == [f"custom:{srv['id']}"]
    assert cs.get_server(srv["id"])["last_models"] == ["m"]


def test_validate_custom_without_server_id_probes_but_does_not_stamp(tmp_registry, monkeypatch):
    """Ad-hoc pre-registration probe: ok result, but nothing to stamp/persist."""
    called = {}
    monkeypatch.setattr(ob.validators, "validate_custom", _fake_ok(called))
    stamped = []
    monkeypatch.setattr(ob._state, "record_validation", lambda p: stamped.append(p))

    resp = ob.validate(ob.ValidateRequest(
        provider="custom", credentials={"base_url": "http://x:8080/v1"}))

    assert resp.ok is True
    assert called["base_url"] == "http://x:8080/v1"
    assert stamped == []                     # no known server -> no stamp
    assert cs.list_servers() == []


def test_validate_custom_failure_does_not_stamp_or_persist(tmp_registry, monkeypatch):
    srv = cs.add_server(alias="box", base_url="http://x/v1", api_key="k")
    monkeypatch.setattr(
        ob.validators, "validate_custom",
        lambda base_url, api_key="": ValidationResult(
            ok=False, latency_ms=3, error=f"RuntimeError: Server unreachable at {base_url}"))
    stamped = []
    monkeypatch.setattr(ob._state, "record_validation", lambda p: stamped.append(p))

    resp = ob.validate(ob.ValidateRequest(
        provider="custom", credentials={"server_id": srv["id"]}))

    assert resp.ok is False
    assert "unreachable" in resp.error
    assert stamped == []
    assert cs.get_server(srv["id"])["validated_at"] is None
    assert cs.get_server(srv["id"])["last_models"] == []


def test_validate_custom_missing_base_url_and_unknown_server_400(tmp_registry, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(
        ob.validators, "validate_custom",
        lambda base_url, api_key="": pytest.fail("validator must not be called"))
    with pytest.raises(HTTPException) as exc:
        ob.validate(ob.ValidateRequest(
            provider="custom", credentials={"server_id": "srv-nope"}))
    assert exc.value.status_code == 400
    assert "base_url" in str(exc.value.detail)


def test_validate_custom_server_deleted_mid_probe_ok_but_unstamped(tmp_registry, monkeypatch):
    """TOCTOU seam: the server vanishes between dispatch and stamping.
    Contract: probe result still returned (ok True), but nothing is stamped —
    update_server is attempted BEFORE record_validation and its KeyError is
    swallowed fail-soft, so a deleted server can never leave a stale stamp."""
    srv = cs.add_server(alias="box", base_url="http://x/v1", api_key="k")

    def _fake(base_url, api_key=""):
        cs.delete_server(srv["id"])   # deleted while the probe is in flight
        return ValidationResult(ok=True, latency_ms=5,
                                detail={"model_count": 1, "models": ["m"]})

    monkeypatch.setattr(ob.validators, "validate_custom", _fake)
    stamped = []
    monkeypatch.setattr(ob._state, "record_validation", lambda p: stamped.append(p))

    resp = ob.validate(ob.ValidateRequest(
        provider="custom", credentials={"server_id": srv["id"]}))

    assert resp.ok is True            # the probe itself succeeded
    assert stamped == []              # but a vanished server is never stamped
    assert cs.list_servers() == []
