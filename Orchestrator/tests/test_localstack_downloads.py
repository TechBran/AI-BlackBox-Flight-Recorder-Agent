"""Tests for the localstack weight-download endpoint (local-model-stack M2).

Mirrors the ollama_io test recipe: all HTTP mocked via httpx.MockTransport
injected through localstack_downloads._async_transport; the download singleton
is module state reset per test; MODELS_DIR + hardware.disk_free_mb monkeypatched
so nothing touches the real filesystem or the real disk gate.
"""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import hardware
from Orchestrator import localstack_downloads as dl
from Orchestrator.routes.local_models_routes import router

ARTIFACT = "embed-qwen3-0.6b"
FAKE = b"GGUF" + b"\x00" * (3 * 1024)  # a few KB of fake weights


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    dl._DL = None
    monkeypatch.setattr(dl, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(dl, "_async_transport", None)
    # Default: plenty of disk so the gate passes unless a test overrides it.
    monkeypatch.setattr(hardware, "disk_free_mb", lambda *a, **k: 500 * 1024)
    yield
    dl._DL = None


def _bytes_transport(payload: bytes):
    """Async MockTransport that streams `payload` with a content-length."""
    def handler(request):
        return httpx.Response(200, content=payload,
                              headers={"content-length": str(len(payload))})
    return httpx.MockTransport(handler)


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _lines(resp):
    return [json.loads(l) for l in resp.text.splitlines() if l.strip()]


def test_download_streams_progress_and_writes_file(monkeypatch):
    monkeypatch.setattr(dl, "_async_transport", _bytes_transport(FAKE))
    resp = _client().post("/local-models/download", json={"artifact": ARTIFACT})
    assert resp.status_code == 200
    lines = _lines(resp)
    assert lines[-1]["state"] == "done"
    assert lines[-1]["completed"] == len(FAKE)
    # progress is monotonic non-decreasing
    comp = [l["completed"] for l in lines]
    assert comp == sorted(comp)
    dest = dl.MODELS_DIR / dl.DOWNLOAD_MANIFEST[ARTIFACT]["dest"]
    assert dest.read_bytes() == FAKE
    assert not (dl.MODELS_DIR / (dest.name + ".part")).exists()  # renamed away


def test_download_disk_gate_507(monkeypatch):
    monkeypatch.setattr(hardware, "disk_free_mb", lambda *a, **k: 10 * 1024)
    resp = _client().post("/local-models/download", json={"artifact": ARTIFACT})
    assert resp.status_code == 507
    assert "40" in resp.json()["detail"]
    assert not (dl.MODELS_DIR / dl.DOWNLOAD_MANIFEST[ARTIFACT]["dest"]).exists()


def test_download_disk_gate_failsoft_allows_when_unknown(monkeypatch):
    """disk_free_mb None (unreadable) → gate allows (fail-soft)."""
    monkeypatch.setattr(hardware, "disk_free_mb", lambda *a, **k: None)
    monkeypatch.setattr(dl, "_async_transport", _bytes_transport(FAKE))
    resp = _client().post("/local-models/download", json={"artifact": ARTIFACT})
    assert resp.status_code == 200
    assert _lines(resp)[-1]["state"] == "done"


def test_download_unknown_artifact_404():
    resp = _client().post("/local-models/download", json={"artifact": "nope"})
    assert resp.status_code == 404


def test_download_concurrent_409(monkeypatch):
    dl._DL = {"artifact": ARTIFACT, "status": "downloading", "completed": 1,
              "total": 2, "state": "running", "error": None}
    resp = _client().post("/local-models/download", json={"artifact": ARTIFACT})
    assert resp.status_code == 409


def test_download_already_present_is_done(monkeypatch):
    dl.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = dl.MODELS_DIR / dl.DOWNLOAD_MANIFEST[ARTIFACT]["dest"]
    dest.write_bytes(FAKE)
    # No transport set — if it tried to download, it would fail; it must not.
    resp = _client().post("/local-models/download", json={"artifact": ARTIFACT})
    assert resp.status_code == 200
    assert _lines(resp)[-1]["state"] == "done"
    assert dest.read_bytes() == FAKE
