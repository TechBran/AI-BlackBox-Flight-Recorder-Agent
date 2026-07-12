"""HTTP surface for the xAI sovereign line (house pattern: TestClient over a
minimal FastAPI app mounting the router — test_custom_servers_routes.py precedent)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator.routes import xai_phone_routes as xr
from Orchestrator.xai_phone import provisioning as pv


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "xai_phone.json"
    monkeypatch.setattr(pv, "STORE_PATH", str(path))
    return path


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(xr.router)
    return TestClient(app)


@pytest.fixture
def fake_api(monkeypatch):
    async def _fake_post(path, payload):
        return {"phone_number": "+15550100", "signing_secret": "whsec_abc"}
    monkeypatch.setattr(pv, "_api_post", _fake_post)


def test_status_unprovisioned(client):
    r = client.get("/xai/phone/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provisioned"] is False
    assert "signing_secret" not in body


def test_provision_happy_path(client, fake_api):
    r = client.post("/xai/phone/provision", json={
        "name": "BlackBox line",
        "webhook_url": "https://box.ts.net:10000/xai/voice/incoming",
    })
    assert r.status_code == 200
    assert r.json()["phone_number"] == "+15550100"
    assert "signing_secret" not in r.json()


def test_provision_second_call_409_unless_force(client, fake_api):
    first = client.post("/xai/phone/provision", json={
        "name": "l", "webhook_url": "https://x/hook"})
    assert first.status_code == 200
    second = client.post("/xai/phone/provision", json={
        "name": "l", "webhook_url": "https://x/hook"})
    assert second.status_code == 409
    forced = client.post("/xai/phone/provision", json={
        "name": "l", "webhook_url": "https://x/hook", "force": True})
    assert forced.status_code == 200


def test_provision_rejects_missing_or_insecure_webhook(client, fake_api):
    assert client.post("/xai/phone/provision", json={"name": "l"}).status_code == 400
    assert client.post("/xai/phone/provision", json={
        "name": "l", "webhook_url": "http://insecure/hook"}).status_code == 400


def test_status_preflight_reports_webhook_reachability(client, fake_api, monkeypatch):
    client.post("/xai/phone/provision", json={"name": "l", "webhook_url": "https://x/hook"})

    async def fake_unsigned_post(url):
        assert url == "https://x/hook"
        return 401                       # unsigned POST rejected = endpoint live + enforcing
    monkeypatch.setattr(xr, "_unsigned_post", fake_unsigned_post)

    r = client.get("/xai/phone/status?preflight=true")
    assert r.status_code == 200
    assert r.json()["webhook_preflight"]["ok"] is True

    async def fake_unreachable(url):
        raise xr.httpx.ConnectError("no route")
    monkeypatch.setattr(xr, "_unsigned_post", fake_unreachable)
    r = client.get("/xai/phone/status?preflight=true")
    assert r.json()["webhook_preflight"]["ok"] is False
