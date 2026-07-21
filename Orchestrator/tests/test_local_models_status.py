"""Tests for GET /local-models/status (M1).

The JSON shape is an ADDITIVE binding contract (local_models wizard step +
Updates panels) — shape assertions are deliberate lock-in. A minimal FastAPI
with just this router; every local_stack/hardware seam is monkeypatched (same
recipe as test_embeddings_routes.py). No real network, no real config.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import hardware, local_stack
from Orchestrator.routes.local_models_routes import router

STATUS_KEYS = {
    "installed", "enabled", "healthy", "base_url",
    "hardware", "disk", "llama_swap", "models", "routing",
}
MODEL_KEYS = {"model", "capability", "group", "label", "running", "state", "download"}
DISK_KEYS = {"free_mb", "required_mb", "ok"}
ROUTING_KEYS = {"enabled", "healthy", "decision"}

FAKE_HW = {
    "gpu": True, "gpu_name": "NVIDIA RTX 2000 Ada Generation", "vram_mb": 16380,
    "ram_mb": 128000, "source": "nvidia-smi", "tier": "HIGH",
}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _wire(monkeypatch, *, installed=True, reachable=True, running=None,
          downloads=None, enabled_caps=(), disk_free=50 * 1024):
    monkeypatch.setattr(local_stack, "is_installed", lambda: installed)
    monkeypatch.setattr(local_stack, "master_enabled", lambda: installed)
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    monkeypatch.setattr(local_stack, "llama_swap_health", lambda: {
        "reachable": reachable, "status_code": 200 if reachable else None,
    })
    monkeypatch.setattr(local_stack, "running_members", lambda: running)
    monkeypatch.setattr(local_stack, "read_download_state", lambda: downloads or {})
    monkeypatch.setattr(local_stack, "enabled", lambda cap: cap in enabled_caps)
    monkeypatch.setattr(hardware, "probe", lambda: dict(FAKE_HW))
    monkeypatch.setattr(hardware, "disk_free_mb", lambda: disk_free)


def test_status_shape_and_no_store(client, monkeypatch):
    _wire(monkeypatch, running=[{"model": "embed-qwen3-8b", "state": "ready"}],
          enabled_caps=("embeddings",))
    r = client.get("/local-models/status")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == STATUS_KEYS
    assert body["installed"] is True
    assert body["healthy"] is True
    assert body["base_url"] == "http://127.0.0.1:9098/v1"
    assert body["hardware"]["tier"] == "HIGH"


def test_disk_block(client, monkeypatch):
    _wire(monkeypatch, disk_free=50 * 1024)
    disk = client.get("/local-models/status").json()["disk"]
    assert set(disk) == DISK_KEYS
    assert disk == {"free_mb": 50 * 1024, "required_mb": 40 * 1024, "ok": True}


def test_disk_block_insufficient(client, monkeypatch):
    _wire(monkeypatch, disk_free=10 * 1024)
    assert client.get("/local-models/status").json()["disk"]["ok"] is False


def test_disk_block_unknown(client, monkeypatch):
    _wire(monkeypatch, disk_free=None)
    disk = client.get("/local-models/status").json()["disk"]
    assert disk["free_mb"] is None and disk["ok"] is None


def test_models_rollup(client, monkeypatch):
    _wire(monkeypatch,
          running=[{"model": "speaches", "state": "loading"}],
          downloads={"embed-qwen3-8b": {"state": "downloaded"}})
    models = client.get("/local-models/status").json()["models"]
    assert [m["model"] for m in models] == \
        ["embed-qwen3-8b", "rerank-qwen3-8b", "speaches", "qwen-tts"]
    for m in models:
        assert set(m) == MODEL_KEYS
    by_id = {m["model"]: m for m in models}
    assert by_id["speaches"]["running"] is True
    assert by_id["speaches"]["state"] == "loading"
    assert by_id["embed-qwen3-8b"]["running"] is False
    assert by_id["embed-qwen3-8b"]["state"] is None
    assert by_id["embed-qwen3-8b"]["download"] == {"state": "downloaded"}
    assert by_id["qwen-tts"]["download"] == {"state": "pending"}  # absent -> pending


def test_llama_swap_running_passthrough_and_null(client, monkeypatch):
    _wire(monkeypatch, running=None, reachable=False, installed=True)
    body = client.get("/local-models/status").json()
    assert body["llama_swap"]["running"] is None       # unreachable -> null
    assert body["llama_swap"]["reachable"] is False
    assert body["healthy"] is False


def test_routing_decisions(client, monkeypatch):
    _wire(monkeypatch, reachable=True, enabled_caps=("embeddings", "rerank"))
    routing = client.get("/local-models/status").json()["routing"]
    assert set(routing) == set(local_stack.CAPABILITIES)
    for cap in local_stack.CAPABILITIES:
        assert set(routing[cap]) == ROUTING_KEYS
    assert routing["embeddings"]["decision"] == "on-box"   # seeded + healthy
    assert routing["rerank"]["decision"] == "on-box"
    assert routing["stt"]["decision"] == "off"             # not seeded
    assert routing["tts"]["decision"] == "off"


def test_routing_seeded_but_unhealthy(client, monkeypatch):
    _wire(monkeypatch, reachable=False, enabled_caps=("stt",))
    routing = client.get("/local-models/status").json()["routing"]
    assert routing["stt"]["decision"] == "unhealthy"       # seeded on, stack down


def test_not_installed(client, monkeypatch):
    _wire(monkeypatch, installed=False, reachable=True, enabled_caps=())
    body = client.get("/local-models/status").json()
    assert body["installed"] is False
    assert body["healthy"] is False
    assert all(v["decision"] == "off" for v in body["routing"].values())
