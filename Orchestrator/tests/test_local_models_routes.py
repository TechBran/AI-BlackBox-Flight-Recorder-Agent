"""Tests for GET /local-models/status per-artifact audio children (M-A / A4).

The status endpoint enumerates the 3 Qwen3-TTS variants + whisper as artifact
CHILDREN under the qwen-tts / speaches audio members. INERT WHEN OFF: with no
[local_models] section the stack is not installed and no artifact rows appear.
All heavy probes (llama-swap health, hardware, disk) are monkeypatched so the
test is hermetic and never touches the network or real hardware.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import hardware, local_stack
from Orchestrator.routes.local_models_routes import router


@pytest.fixture(autouse=True)
def stub_probes(monkeypatch):
    # Neutralize every non-manifest probe so the test asserts only the artifact
    # enumeration; llama-swap is treated as down (irrelevant to artifact rows).
    monkeypatch.setattr(local_stack, "llama_swap_health",
                        lambda *a, **k: {"reachable": False, "status_code": None})
    monkeypatch.setattr(local_stack, "running_members", lambda *a, **k: [])
    monkeypatch.setattr(local_stack, "read_download_state", lambda: {})
    monkeypatch.setattr(local_stack, "master_enabled", lambda: True)
    monkeypatch.setattr(local_stack, "enabled", lambda cap: False)
    monkeypatch.setattr(local_stack, "model_downloaded", lambda key: False)
    monkeypatch.setattr(hardware, "probe", lambda *a, **k: {"tier": "test"})
    monkeypatch.setattr(hardware, "disk_free_mb", lambda *a, **k: 500 * 1024)


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _members(resp):
    return {m["model"]: m for m in resp.json()["models"]}


def test_status_inert_when_stack_off(monkeypatch):
    """No [local_models] section -> not installed -> members carry NO artifact
    rows (empty list), so the dev box surfaces nothing to download."""
    monkeypatch.setattr(local_stack, "is_installed", lambda: False)
    members = _members(_client().get("/local-models/status"))
    # audio members still listed (MEMBERS is static) but with empty artifacts
    assert members["qwen-tts"]["artifacts"] == []
    assert members["speaches"]["artifacts"] == []
    # no artifact child anywhere in the payload
    assert all(not m.get("artifacts") for m in members.values())


def test_status_enumerates_audio_artifacts_when_installed(monkeypatch):
    """Installed -> qwen-tts exposes its 3 variant children + speaches exposes
    whisper, each with the A4 child contract."""
    monkeypatch.setattr(local_stack, "is_installed", lambda: True)
    members = _members(_client().get("/local-models/status"))

    qwen_keys = [a["key"] for a in members["qwen-tts"]["artifacts"]]
    assert qwen_keys == ["qwen-tts-base", "qwen-tts-custom-voice", "qwen-tts-voice-design"]

    whisper = members["speaches"]["artifacts"]
    assert [a["key"] for a in whisper] == ["whisper"]

    # each child carries the full A4 contract
    for a in members["qwen-tts"]["artifacts"] + whisper:
        assert set(a) == {"key", "label", "downloadable", "downloaded",
                          "size_gb", "repo_pending_g3"}
        assert a["downloaded"] is False
        assert isinstance(a["size_gb"], (int, float))
    # Qwen variants are G3-validated -> gate cleared -> button live (downloadable).
    for a in members["qwen-tts"]["artifacts"]:
        assert a["repo_pending_g3"] is False
        assert a["downloadable"] is True
    # Whisper stays gated until G4 -> button disabled.
    assert whisper[0]["repo_pending_g3"] is True
    assert whisper[0]["downloadable"] is False

    # non-audio members (retrieval) never get an artifacts key
    assert "artifacts" not in members["embed-qwen3-8b"]
    assert "artifacts" not in members["rerank-qwen3-8b"]


def test_status_downloaded_reflects_state(monkeypatch):
    """A downloaded whisper (state-file truth via model_downloaded) surfaces
    downloaded=True on its child row."""
    monkeypatch.setattr(local_stack, "is_installed", lambda: True)
    monkeypatch.setattr(local_stack, "model_downloaded", lambda key: key == "whisper")
    members = _members(_client().get("/local-models/status"))
    whisper = members["speaches"]["artifacts"][0]
    assert whisper["downloaded"] is True
