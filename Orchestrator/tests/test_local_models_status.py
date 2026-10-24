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
    # additive (2026-08-11 plan Task 10): the GPU capability signal
    "gpu",
}
MODEL_KEYS = {"model", "capability", "group", "label", "running", "state", "download",
              "downloadable"}
DISK_KEYS = {"free_mb", "required_mb", "ok"}
# additive (plan Task 10 / audit O19-local-stack-05): why, and the breaker
ROUTING_KEYS = {"enabled", "healthy", "decision", "reason", "breaker"}

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
        # audio members (qwen-tts, speaches) carry an ADDITIVE per-artifact
        # `artifacts` list (M-A/A4: the 3 Qwen variants + whisper); the
        # self-converted reranker an ADDITIVE `acquire` block (how it gets
        # built, O19-local-stack-10); every other member keeps exactly MODEL_KEYS.
        expected = MODEL_KEYS | ({"artifacts"} if m["model"] in {"speaches", "qwen-tts"} else set())
        expected |= {"acquire"} if m["model"] == "rerank-qwen3-8b" else set()
        assert set(m) == expected
    by_id = {m["model"]: m for m in models}
    assert by_id["speaches"]["running"] is True
    assert by_id["speaches"]["state"] == "loading"
    assert by_id["embed-qwen3-8b"]["running"] is False
    assert by_id["embed-qwen3-8b"]["state"] is None
    assert by_id["embed-qwen3-8b"]["download"] == {"state": "downloaded"}
    assert by_id["qwen-tts"]["download"] == {"state": "pending"}  # absent -> pending
    # downloadable == "id is a DOWNLOAD_MANIFEST key": embeddings + tts are
    # fetchable; rerank (self-converted) + stt (auto-pulled) are NOT, so the
    # wizard must never render a Download button for them.
    assert by_id["embed-qwen3-8b"]["downloadable"] is True
    assert by_id["qwen-tts"]["downloadable"] is True
    assert by_id["rerank-qwen3-8b"]["downloadable"] is False
    assert by_id["speaches"]["downloadable"] is False


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


# ── honest degradation (2026-08-11 plan Task 10, audit O19-local-stack-05) ──

DEAD_DRIVER_HW = dict(FAKE_HW, vram_mb=None, source="lspci", driver_ok=False,
                      gpu_reason="NVIDIA driver not responding: RTX 2000 Ada is on the PCI bus")


def test_gpu_block_and_healthy_routing_carry_no_reason(client, monkeypatch):
    _wire(monkeypatch, enabled_caps=("tts", "stt"))
    body = client.get("/local-models/status").json()
    assert body["gpu"] == {"ok": True, "reason": None}
    for cap in ("tts", "stt"):
        assert body["routing"][cap]["decision"] == "on-box"
        assert body["routing"][cap]["reason"] is None
        assert body["routing"][cap]["breaker"]["state"] == "closed"


def test_dead_driver_reports_gpu_unavailable_for_tts_and_cpu_for_the_rest(client, monkeypatch):
    """The storm-outage box: the front door is up (healthy), the driver is
    dead. It used to report every capability on-box. TTS has no CPU path
    (D4) -> gpu-unavailable; STT/retrieval still serve, on CPU, and say so."""
    _wire(monkeypatch, enabled_caps=("tts", "stt", "embeddings", "rerank"))
    monkeypatch.setattr(hardware, "probe", lambda: dict(DEAD_DRIVER_HW))
    body = client.get("/local-models/status").json()
    assert body["healthy"] is True
    assert body["gpu"] == {"ok": False, "reason": DEAD_DRIVER_HW["gpu_reason"]}
    assert body["routing"]["tts"]["decision"] == "gpu-unavailable"
    assert body["routing"]["tts"]["reason"] == DEAD_DRIVER_HW["gpu_reason"]
    for cap in ("stt", "embeddings", "rerank"):
        assert body["routing"][cap]["decision"] == "on-box"
        assert "running on CPU" in body["routing"][cap]["reason"]


def test_dead_driver_with_the_stack_down_names_the_gpu_not_just_the_service(client, monkeypatch):
    """With the unit preflight the stack refuses to start on a dead driver, so
    "unhealthy" alone would send the operator to the wrong place."""
    _wire(monkeypatch, reachable=False, enabled_caps=("stt",))
    monkeypatch.setattr(hardware, "probe", lambda: dict(DEAD_DRIVER_HW))
    stt = client.get("/local-models/status").json()["routing"]["stt"]
    assert stt["decision"] == "gpu-unavailable"
    assert "NVIDIA driver not responding" in stt["reason"]


def test_open_breaker_reports_degraded_with_the_last_error(client, monkeypatch):
    """Stack up, member failing: the wizard shows "member failing" instead of
    green (O19-local-stack-05)."""
    _wire(monkeypatch, enabled_caps=("tts",))
    for _ in range(local_stack.BREAKER_THRESHOLD):
        local_stack.report_failure("tts", "HTTP 500: CUDA error")
    tts = client.get("/local-models/status").json()["routing"]["tts"]
    assert tts["decision"] == "degraded"
    assert "HTTP 500: CUDA error" in tts["reason"]
    assert tts["breaker"]["state"] == "open"
    assert tts["breaker"]["consecutive_failures"] == local_stack.BREAKER_THRESHOLD


def test_report_only_breakers_keep_routing_on_box_and_say_why(client, monkeypatch):
    """The embeddings / rerank breakers only report: routing still goes
    on-box (their own retry / evidence regimes gate them), and a flipped
    decision would turn the wizard card into "Use on-box", whose click
    starts a corpus re-embed."""
    _wire(monkeypatch, enabled_caps=("embeddings", "rerank"))
    for _ in range(local_stack.BREAKER_THRESHOLD):
        local_stack.report_failure("embeddings", "HTTP 500: cudaMalloc failed")
    routing = client.get("/local-models/status").json()["routing"]
    emb = routing["embeddings"]
    assert emb["decision"] == "on-box"
    assert emb["reason"] == "member failing: HTTP 500: cudaMalloc failed"
    assert emb["breaker"]["state"] == "open"
    assert routing["rerank"]["reason"] is None
    assert local_stack.capability_problem("embeddings") is None


def test_self_converted_reranker_row_says_how_it_gets_built(client, monkeypatch):
    """O19-local-stack-10: a silent `downloadable: false` row left the operator
    nowhere; the row now names the build command and the runbook, and whether
    llama-swap can start the member."""
    _wire(monkeypatch)
    monkeypatch.setattr(local_stack, "member_built", lambda m: {
        "built": False, "reason": "Qwen3-Reranker-8B-Q8_0.gguf is not built on this box"})
    row = {m["model"]: m for m in client.get("/local-models/status").json()["models"]}["rerank-qwen3-8b"]
    assert row["downloadable"] is False
    acq = row["acquire"]
    assert acq["kind"] == "self-convert" and acq["built"] is False
    assert acq["command"] == "bash installer/templates/blackbox-build-reranker-gguf.sh"
    assert acq["runbook"] == "LocalModels/reranker/CONVERT-QWEN3-RERANKER-GGUF.md"
    assert "not built" in acq["reason"]


def test_unhealthy_reason_points_at_the_service(client, monkeypatch):
    _wire(monkeypatch, reachable=False, enabled_caps=("stt",))
    stt = client.get("/local-models/status").json()["routing"]["stt"]
    assert stt["decision"] == "unhealthy"
    assert "blackbox-models.service" in stt["reason"]
