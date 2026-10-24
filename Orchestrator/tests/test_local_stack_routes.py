"""M8 on-box wizard-activation endpoints: POST /local-models/capability
(writes [local_models] stt/tts flags + mirrors STT_PROVIDER) and
GET /local-models/gpu-preflight (nvidia-smi near-idle blocking check).

Hermetic: config.ini path + .env writer path are redirected to tmp; the GPU
probe helper is monkeypatched so no real nvidia-smi runs."""
import configparser

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch, app_client):
    from Orchestrator.onboarding import secrets_writer
    from Orchestrator.routes import local_stack_routes as lsr
    cfg = tmp_path / "config.ini"
    cfg.write_text("[users]\nlist = A\n")
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setattr(lsr, "CONFIG_INI", cfg)
    monkeypatch.setattr(secrets_writer, "ENV_FILE", env)
    app_client._cfg, app_client._env = cfg, env
    return app_client


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


_CPU_BOX_HW = {"gpu": False, "gpu_name": None, "vram_mb": None, "ram_mb": 31167,
               "source": "none", "tier": "LOW", "driver_ok": None, "gpu_reason": None}


def test_gpu_preflight_no_gpu_is_ok(client, monkeypatch):
    from Orchestrator import hardware
    from Orchestrator.routes import local_stack_routes as lsr
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: dict(_CPU_BOX_HW))
    monkeypatch.setattr(lsr, "_probe_gpu_usage",
                        lambda: {"present": False, "used_mib": None,
                                 "total_mib": None, "processes": []})
    r = client.get("/local-models/gpu-preflight")
    assert r.status_code == 200 and r.json()["ok"] is True  # CPU box: no contention
    assert r.json()["present"] is False


def test_gpu_preflight_fails_closed_when_the_driver_is_dead(client, monkeypatch):
    """O19-local-stack-06, the storm-outage shape: the card is on the bus but
    nvidia-smi fails. The wizard used to hear "No NVIDIA GPU detected — no
    VRAM contention" and run the embeddings cutover onto a GPU it cannot use."""
    from Orchestrator import hardware
    from Orchestrator.routes import local_stack_routes as lsr
    monkeypatch.setattr(lsr, "_probe_gpu_usage",
                        lambda: {"present": False, "used_mib": None,
                                 "total_mib": None, "processes": []})
    reason = ("NVIDIA driver not responding: RTX 2000 Ada is on the PCI bus but "
              "nvidia-smi failed (exit 9: NVIDIA-SMI has failed)")
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: dict(
        _CPU_BOX_HW, gpu=True, source="lspci", tier="HIGH",
        driver_ok=False, gpu_reason=reason))
    body = client.get("/local-models/gpu-preflight").json()
    assert body["ok"] is False
    assert body["present"] is True
    assert body["driver_ok"] is False
    assert reason in body["detail"]
    assert "No NVIDIA GPU detected" not in body["detail"]
