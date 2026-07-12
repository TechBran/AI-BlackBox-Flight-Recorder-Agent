"""POST /xai/voice/incoming — the only publicly-funneled path.
Unsigned/stale/replayed => 401 before any processing; unprovisioned => 503;
verified realtime.call.incoming => spawns attach; other events acked, ignored."""
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.xai_phone.signature as sig_mod
from Orchestrator.routes import xai_phone_routes as xr
from Orchestrator.xai_phone import provisioning as pv

SECRET = "whsec_" + base64.b64encode(b"test-signing-key-32-bytes-long!!").decode()


def sign(body: bytes, msg_id: str, ts: str | None = None):
    ts = str(int(time.time())) if ts is None else ts
    key = base64.b64decode(SECRET[len("whsec_"):])
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": "v1," + base64.b64encode(mac).decode(),
    }


@pytest.fixture(autouse=True)
def fresh_replay_cache(monkeypatch):
    monkeypatch.setattr(sig_mod, "_default_replay_cache", sig_mod.ReplayCache())


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "STORE_PATH", str(tmp_path / "xai_phone.json"))
    pv._write_store({"version": 1, "phone_number": "+15550100",
                     "webhook_url": "https://x/hook", "signing_secret": SECRET})


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(xr, "_spawn_attach", lambda call_id, event: calls.append(call_id))
    return calls


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(xr.router)
    return TestClient(app)


def test_verified_incoming_call_spawns_attach(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "call-123"}).encode()
    r = client.post("/xai/voice/incoming", content=body, headers=sign(body, "m1"))
    assert r.status_code == 200
    assert r.json()["handled"] is True
    assert spawned == ["call-123"]


def test_unsigned_rejected_401(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "call-123"}).encode()
    r = client.post("/xai/voice/incoming", content=body)
    assert r.status_code == 401
    assert spawned == []


def test_tampered_body_rejected_401(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "call-123"}).encode()
    headers = sign(body, "m2")
    r = client.post("/xai/voice/incoming", content=body + b" ", headers=headers)
    assert r.status_code == 401
    assert spawned == []


def test_stale_timestamp_rejected_401(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "call-123"}).encode()
    r = client.post("/xai/voice/incoming", content=body,
                    headers=sign(body, "m3", ts=str(int(time.time()) - 400)))
    assert r.status_code == 401
    assert spawned == []


def test_replay_rejected_401(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "call-123"}).encode()
    headers = sign(body, "m4")
    assert client.post("/xai/voice/incoming", content=body, headers=headers).status_code == 200
    assert client.post("/xai/voice/incoming", content=body, headers=headers).status_code == 401
    assert spawned == ["call-123"]                 # attached exactly once


def test_other_event_types_acked_not_attached(client, store, spawned):
    body = json.dumps({"type": "realtime.call.ended", "call_id": "call-123"}).encode()
    r = client.post("/xai/voice/incoming", content=body, headers=sign(body, "m5"))
    assert r.status_code == 200
    assert r.json()["handled"] is False
    assert spawned == []


def test_unprovisioned_returns_503(client, tmp_path, monkeypatch, spawned):
    monkeypatch.setattr(pv, "STORE_PATH", str(tmp_path / "empty.json"))
    body = json.dumps({"type": "realtime.call.incoming", "call_id": "c"}).encode()
    r = client.post("/xai/voice/incoming", content=body)
    assert r.status_code == 503
    assert spawned == []


def test_missing_call_id_rejected_400(client, store, spawned):
    body = json.dumps({"type": "realtime.call.incoming"}).encode()
    r = client.post("/xai/voice/incoming", content=body, headers=sign(body, "m6"))
    assert r.status_code == 400
    assert spawned == []
