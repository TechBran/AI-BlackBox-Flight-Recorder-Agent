"""M8 on-box wizard-activation endpoints: POST /local-models/capability
(writes [local_models] stt/tts flags + mirrors STT_PROVIDER) and
GET /local-models/gpu-preflight (nvidia-smi near-idle blocking check).

Hermetic: config.ini path + .env writer path are redirected to tmp; the GPU
probe helper is monkeypatched so no real nvidia-smi runs."""
import configparser

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from Orchestrator.onboarding import secrets_writer
    from Orchestrator.routes import local_stack_routes as lsr
    cfg = tmp_path / "config.ini"
    cfg.write_text("[users]\nlist = A\n")
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setattr(lsr, "CONFIG_INI", cfg)
    monkeypatch.setattr(secrets_writer, "ENV_FILE", env)
    from Orchestrator.app import app
    with TestClient(app) as c:
        c._cfg, c._env = cfg, env
        yield c


def _flags(cfg):
    cp = configparser.ConfigParser()
    cp.read(cfg)
    return cp


def test_enable_tts_writes_config_flag(client):
    r = client.post("/local-models/capability",
                    json={"capability": "tts", "enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    cp = _flags(client._cfg)
    assert cp.getboolean("local_models", "tts") is True
    # tts must NOT touch STT_PROVIDER
    assert "STT_PROVIDER" not in client._env.read_text()


def test_enable_stt_writes_flag_and_mirrors_provider(client):
    r = client.post("/local-models/capability",
                    json={"capability": "stt", "enabled": True})
    assert r.status_code == 200
    cp = _flags(client._cfg)
    assert cp.getboolean("local_models", "stt") is True
    assert "STT_PROVIDER=onbox" in client._env.read_text()


def test_disable_stt_clears_provider(client):
    client.post("/local-models/capability", json={"capability": "stt", "enabled": True})
    client.post("/local-models/capability", json={"capability": "stt", "enabled": False})
    cp = _flags(client._cfg)
    assert cp.getboolean("local_models", "stt") is False
    # STT_PROVIDER cleared to "" (auto) — the on-box pin is removed.
    txt = client._env.read_text()
    assert "STT_PROVIDER=onbox" not in txt


def test_rejects_unknown_capability(client):
    r = client.post("/local-models/capability",
                    json={"capability": "embeddings", "enabled": True})
    assert r.status_code == 400  # embeddings/rerank activate via their own endpoints


def test_gpu_preflight_idle_ok(client, monkeypatch):
    from Orchestrator.routes import local_stack_routes as lsr
    monkeypatch.setattr(lsr, "_probe_gpu_usage",
                        lambda: {"present": True, "used_mib": 300,
                                 "total_mib": 16380, "processes": []})
    r = client.get("/local-models/gpu-preflight")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_gpu_preflight_busy_blocks(client, monkeypatch):
    from Orchestrator.routes import local_stack_routes as lsr
    monkeypatch.setattr(lsr, "_probe_gpu_usage",
                        lambda: {"present": True, "used_mib": 7100, "total_mib": 16380,
                                 "processes": [{"pid": 42, "name": "ollama", "used_mib": 6994}]})
    r = client.get("/local-models/gpu-preflight")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert r.json()["used_mib"] == 7100


def test_gpu_preflight_no_gpu_is_ok(client, monkeypatch):
    from Orchestrator.routes import local_stack_routes as lsr
    monkeypatch.setattr(lsr, "_probe_gpu_usage",
                        lambda: {"present": False, "used_mib": None,
                                 "total_mib": None, "processes": []})
    r = client.get("/local-models/gpu-preflight")
    assert r.status_code == 200 and r.json()["ok"] is True  # CPU box: no contention
