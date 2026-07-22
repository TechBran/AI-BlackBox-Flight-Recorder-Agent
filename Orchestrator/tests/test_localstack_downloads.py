"""Tests for the localstack weight-download endpoint (local-model-stack M2).

Mirrors the ollama_io test recipe: all HTTP mocked via httpx.MockTransport
injected through localstack_downloads._async_transport; the download singleton
is module state reset per test; MODELS_DIR + hardware.disk_free_mb monkeypatched
so nothing touches the real filesystem or the real disk gate.
"""
import json
from pathlib import Path

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


# ── M-A: per-artifact hf_snapshot dest routing (A1) + split keys (A2) ─────────

class _RecordingSnapshot:
    """Stand-in for huggingface_hub.snapshot_download that records the kwargs it
    was called with (local_dir vs cache_dir) and touches a marker so a test can
    assert WHERE the artifact would land — no network, no real weights."""

    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        target = kwargs.get("local_dir") or kwargs.get("cache_dir")
        from pathlib import Path as _P
        _P(target).mkdir(parents=True, exist_ok=True)
        (_P(target) / ".marker").write_text("ok")
        return target


@pytest.fixture
def hf_snapshot(monkeypatch):
    """Inject a recording snapshot_download into huggingface_hub so
    _stream_hf_snapshot resolves it at import time inside the coroutine."""
    import huggingface_hub
    rec = _RecordingSnapshot()
    monkeypatch.setattr(huggingface_hub, "snapshot_download", rec, raising=False)
    return rec


def _run_download(artifact):
    return _lines(_client().post("/local-models/download", json={"artifact": artifact}))


def test_manifest_has_split_qwen_variants_and_whisper():
    """A2: the bundled key is split into 3 per-variant keys + a whisper key; the
    bundled convenience key is RETAINED for back-compat, marked bundled."""
    m = dl.DOWNLOAD_MANIFEST
    for key in ("qwen-tts-base", "qwen-tts-custom-voice", "qwen-tts-voice-design", "whisper"):
        assert key in m, key
        assert m[key]["kind"] == "hf_snapshot"
    # Qwen variants are G3-validated (2026-07-22) -> gate cleared, buttons live.
    for key in ("qwen-tts-base", "qwen-tts-custom-voice", "qwen-tts-voice-design"):
        assert m[key].get("repo_pending_g3") is False, key
    # Whisper stays gated until G4 (STT streaming parity) confirms it.
    assert m["whisper"].get("repo_pending_g3") is True
    # each qwen split is a single repo into the qwen dir; whisper = two CT2 repos
    assert len(m["qwen-tts-base"]["repos"]) == 1
    assert len(m["whisper"]["repos"]) == 2
    assert m["whisper"]["dest_dir"] == "speaches_cache"
    # bundled key retained (D-2), flagged, so per-member status rows don't vanish
    assert m["qwen-tts"].get("bundled") is True


def test_qwen_variant_streams_into_qwen_weights_dir(monkeypatch, tmp_path, hf_snapshot):
    """A1: a Qwen variant lands in weights/qwen3-tts/<variant> (local_dir)."""
    qdir = tmp_path / "qwen3-tts"
    monkeypatch.setattr(dl, "_qwen_tts_model_dir", lambda: qdir)
    lines = _run_download("qwen-tts-base")
    assert lines[-1]["state"] == "done"
    assert hf_snapshot.calls, "snapshot_download never called"
    call = hf_snapshot.calls[-1]
    assert call.get("local_dir") == str(qdir / "base")
    assert "cache_dir" not in call
    assert (qdir / "base" / ".marker").exists()


def test_whisper_streams_into_speaches_cache(monkeypatch, tmp_path, hf_snapshot):
    """A1: whisper (dest_dir=speaches_cache) lands in the Speaches HF cache
    (cache_dir layout), NEVER the Qwen weights dir."""
    cache = tmp_path / "hf-cache" / "hub"
    qdir = tmp_path / "qwen3-tts"
    monkeypatch.setattr(dl, "_speaches_cache_dir", lambda: cache)
    monkeypatch.setattr(dl, "_qwen_tts_model_dir", lambda: qdir)
    lines = _run_download("whisper")
    assert lines[-1]["state"] == "done"
    # both CT2 repos routed into the speaches cache via cache_dir, not local_dir
    assert len(hf_snapshot.calls) == 2
    for call in hf_snapshot.calls:
        assert call.get("cache_dir") == str(cache)
        assert "local_dir" not in call
    assert not qdir.exists()  # nothing leaked into the qwen weights dir


def test_bundled_qwen_tts_dest_unchanged(monkeypatch, tmp_path, hf_snapshot):
    """A1 regression: the legacy bundled qwen-tts key keeps landing all three
    variants under _qwen_tts_model_dir()/<variant> (byte-identical routing)."""
    qdir = tmp_path / "qwen3-tts"
    monkeypatch.setattr(dl, "_qwen_tts_model_dir", lambda: qdir)
    lines = _run_download("qwen-tts")
    assert lines[-1]["state"] == "done"
    landed = {c["local_dir"] for c in hf_snapshot.calls}
    assert landed == {str(qdir / "base"), str(qdir / "custom_voice"), str(qdir / "voice_design")}


def test_speaches_cache_dir_honors_env(monkeypatch):
    """_speaches_cache_dir: explicit override > HF_HOME/hub > localstack default."""
    monkeypatch.setenv("SPEACHES_CACHE_DIR", "/x/cache")
    assert dl._speaches_cache_dir() == Path("/x/cache")
    monkeypatch.delenv("SPEACHES_CACHE_DIR")
    monkeypatch.setenv("HF_HOME", "/y/hf")
    assert dl._speaches_cache_dir() == Path("/y/hf") / "hub"
    monkeypatch.delenv("HF_HOME")
    # default sits under the localstack root, a sibling of MODELS_DIR
    assert dl._speaches_cache_dir() == dl.MODELS_DIR.parent / "hf-cache" / "hub"


def test_hf_snapshot_records_download_state(monkeypatch, tmp_path, hf_snapshot):
    """A3: a multi-file artifact persists state='downloaded' at terminal success
    (its only truth — it fails _member_gguf_present)."""
    from Orchestrator import local_stack
    state_path = tmp_path / "downloads.json"
    monkeypatch.setattr(local_stack, "DOWNLOAD_STATE_PATH", state_path)
    monkeypatch.setattr(dl, "_qwen_tts_model_dir", lambda: tmp_path / "qwen3-tts")
    lines = _run_download("qwen-tts-voice-design")
    assert lines[-1]["state"] == "done"
    recorded = local_stack.read_download_state()
    assert recorded.get("qwen-tts-voice-design", {}).get("state") == "downloaded"
