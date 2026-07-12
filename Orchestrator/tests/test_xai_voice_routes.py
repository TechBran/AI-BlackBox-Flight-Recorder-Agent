"""Hermetic tests for the xAI Custom Voices routes (Voice Lab xAI section).

Route layer only (TestClient). Provider calls are monkeypatched on
``Orchestrator.xai_voices`` (imported inside each handler), so no live xAI call
ever happens. Mirrors test_elevenlabs_voice_routes.py.

Contract the Portal/Android UI depends on:
  * GET /xai/voices no key -> {"configured": false, "voices": []} (zone hides)
  * GET /xai/voices        -> {"configured": true, "voices": [...]}
  * POST clone WITHOUT consent="true" -> 422, provider NEVER called (the gate)
  * POST clone WITH consent -> clone_voice called with parsed args -> {voice_id}
  * DELETE /xai/voices/{id} -> {"ok": true}; provider RuntimeError -> 400
"""
import pytest
from fastapi.testclient import TestClient

from Orchestrator import xai_voices as xv
from Orchestrator.app import app


@pytest.fixture
def cli():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _present_key(monkeypatch):
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "xai-fake")


def test_list_no_key_returns_unconfigured(cli, monkeypatch):
    monkeypatch.setattr(xv, "list_custom_voices", lambda: None)
    resp = cli.get("/xai/voices")
    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "voices": []}


def test_list_returns_voices(cli, monkeypatch):
    monkeypatch.setattr(xv, "list_custom_voices",
                        lambda: [{"voice_id": "cv-1", "name": "Narrator"}])
    resp = cli.get("/xai/voices")
    assert resp.status_code == 200
    assert resp.json() == {"configured": True,
                           "voices": [{"voice_id": "cv-1", "name": "Narrator"}]}


def test_list_provider_error_maps_to_400(cli, monkeypatch):
    def boom():
        raise RuntimeError("xAI error 401: invalid api key")
    monkeypatch.setattr(xv, "list_custom_voices", boom)
    resp = cli.get("/xai/voices")
    assert resp.status_code == 400


def test_clone_without_consent_returns_422_and_never_calls_provider(cli, monkeypatch):
    monkeypatch.setattr(
        xv, "clone_voice",
        lambda *a, **k: pytest.fail("clone_voice called despite missing consent"))
    resp = cli.post(
        "/xai/voices",
        data={"name": "Test", "consent": "false"},
        files={"file": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 422
    assert "consent" in resp.json()["detail"].lower()


def test_clone_with_consent_calls_provider_and_returns_voice_id(cli, monkeypatch):
    seen = {}

    def fake_clone(name, audio_path, description=None):
        import os
        seen.update(name=name, description=description,
                    path_exists=os.path.exists(audio_path))
        return {"voice_id": "cv-new", "name": name}

    monkeypatch.setattr(xv, "clone_voice", fake_clone)
    resp = cli.post(
        "/xai/voices",
        data={"name": "My Grok Voice", "consent": "true", "description": "warm"},
        files={"file": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"voice_id": "cv-new", "name": "My Grok Voice"}
    assert seen == {"name": "My Grok Voice", "description": "warm", "path_exists": True}


def test_clone_no_key_returns_400(cli, monkeypatch):
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "")
    monkeypatch.setattr(
        xv, "clone_voice",
        lambda *a, **k: pytest.fail("clone_voice called despite no key"))
    resp = cli.post(
        "/xai/voices",
        data={"name": "X", "consent": "true"},
        files={"file": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 400


def test_clone_runtime_error_maps_to_400(cli, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("xAI error 400: audio longer than 120 seconds")
    monkeypatch.setattr(xv, "clone_voice", boom)
    resp = cli.post(
        "/xai/voices",
        data={"name": "X", "consent": "true"},
        files={"file": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 400
    assert "120 seconds" in resp.json()["detail"]


def test_delete_ok(cli, monkeypatch):
    seen = {}
    monkeypatch.setattr(xv, "delete_voice", lambda vid: seen.update(deleted=vid))
    resp = cli.delete("/xai/voices/cv-1")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert seen["deleted"] == "cv-1"


def test_delete_runtime_error_maps_to_400(cli, monkeypatch):
    def boom(vid):
        raise RuntimeError("xAI error 404: voice not found")
    monkeypatch.setattr(xv, "delete_voice", boom)
    resp = cli.delete("/xai/voices/nope")
    assert resp.status_code == 400
