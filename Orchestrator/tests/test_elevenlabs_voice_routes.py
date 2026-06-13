"""Hermetic tests for the ElevenLabs Voice Lab routes (Task 22).

Route layer only (TestClient). Every provider call is monkeypatched where the
handler looks it up (imported inside the handler: ``Orchestrator.elevenlabs
.voices`` and ``.catalog``), so no live ElevenLabs call ever happens.

Covers the contract the Portal/Android UI depends on:
  * clone WITHOUT consent="true" -> 422, provider NEVER called (the consent gate)
  * clone WITH consent="true" -> calls clone_instant with the parsed args + returns voice_id
  * design -> previews passed through
  * design/save missing a required field -> 400 (provider not called)
  * design/save success -> {voice_id}
  * GET /elevenlabs/voices -> grouped voices
  * DELETE -> {ok, in_use} (in-use list surfaced for the UI warning)
"""
import pytest
from fastapi.testclient import TestClient

from Orchestrator.app import app
from Orchestrator.elevenlabs import catalog as cat
from Orchestrator.elevenlabs import client as el
from Orchestrator.elevenlabs import voices as vox


@pytest.fixture
def cli():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _present_key(monkeypatch):
    """A present (fake) key so the no-key guards pass; cache cleared each test."""
    cat._cache.clear()
    monkeypatch.setattr(el, "resolve_api_key", lambda: "xi-fake")
    monkeypatch.setattr(el, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})
    yield
    cat._cache.clear()


# =============================================================================
# clone — the consent gate
# =============================================================================

def test_clone_without_consent_returns_422_and_never_calls_provider(cli, monkeypatch):
    """consent != "true" -> 422 BEFORE any provider call (the gate)."""
    monkeypatch.setattr(
        vox, "clone_instant",
        lambda *a, **k: pytest.fail("clone_instant called despite missing consent"),
    )
    resp = cli.post(
        "/elevenlabs/voices/clone",
        data={"name": "Test", "consent": "false"},
        files={"files": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 422
    assert "consent" in resp.json()["detail"].lower()


def test_clone_with_consent_calls_clone_instant_and_returns_voice_id(cli, monkeypatch):
    """consent == "true" -> clone_instant is called (with parsed name + temp paths
    that exist at call time) and the voice_id is returned."""
    seen = {}

    def fake_clone(name, file_paths, *, description=None, remove_background_noise=True):
        import os
        seen.update(
            name=name,
            num_files=len(file_paths),
            description=description,
            remove_background_noise=remove_background_noise,
            # temp files must still exist when the provider is called
            paths_exist=all(os.path.exists(p) for p in file_paths),
        )
        return {"voice_id": "cloned-abc", "requires_verification": False}

    monkeypatch.setattr(vox, "clone_instant", fake_clone)

    resp = cli.post(
        "/elevenlabs/voices/clone",
        data={
            "name": "My Narrator",
            "consent": "true",
            "description": "warm",
            "remove_background_noise": "false",
        },
        files={"files": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"voice_id": "cloned-abc", "requires_verification": False}
    assert seen["name"] == "My Narrator"
    assert seen["num_files"] == 1
    assert seen["description"] == "warm"
    assert seen["remove_background_noise"] is False
    assert seen["paths_exist"] is True


def test_clone_runtime_error_maps_to_400(cli, monkeypatch):
    """Provider RuntimeError -> HTTP 400 with the human message."""
    def boom(*a, **k):
        raise RuntimeError("ElevenLabs quota exceeded - add credits or upgrade plan")

    monkeypatch.setattr(vox, "clone_instant", boom)
    resp = cli.post(
        "/elevenlabs/voices/clone",
        data={"name": "X", "consent": "true"},
        files={"files": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 400
    assert "quota exceeded" in resp.json()["detail"]


def test_clone_no_key_returns_400(cli, monkeypatch):
    """No key (consent present) -> 400, provider not called."""
    monkeypatch.setattr(el, "resolve_api_key", lambda: None)
    monkeypatch.setattr(
        vox, "clone_instant",
        lambda *a, **k: pytest.fail("clone_instant called despite no key"),
    )
    resp = cli.post(
        "/elevenlabs/voices/clone",
        data={"name": "X", "consent": "true"},
        files={"files": ("sample.mp3", b"ID3fakeaudio", "audio/mpeg")},
    )
    assert resp.status_code == 400


# =============================================================================
# design (preview) + design/save
# =============================================================================

def test_design_returns_previews(cli, monkeypatch):
    """POST /design proxies design_previews and returns {text, previews}."""
    seen = {}

    def fake_previews(voice_description, *, text=None, **kwargs):
        seen.update(voice_description=voice_description, text=text)
        return {
            "text": "sample sentence",
            "previews": [
                {"generated_voice_id": "gen-1", "audio_url": "/ui/uploads/a.mp3",
                 "duration_secs": 3.2, "language": "en"},
            ],
        }

    monkeypatch.setattr(vox, "design_previews", fake_previews)

    resp = cli.post(
        "/elevenlabs/voices/design",
        json={"voice_description": "a gravelly wizard", "text": "hello world"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "sample sentence"
    assert body["previews"][0]["generated_voice_id"] == "gen-1"
    assert seen == {"voice_description": "a gravelly wizard", "text": "hello world"}


def test_design_missing_description_returns_400(cli, monkeypatch):
    monkeypatch.setattr(
        vox, "design_previews",
        lambda *a, **k: pytest.fail("design_previews called despite missing description"),
    )
    resp = cli.post("/elevenlabs/voices/design", json={})
    assert resp.status_code == 400


def test_design_save_missing_field_returns_400(cli, monkeypatch):
    """Missing generated_voice_id OR name -> 400, provider never called."""
    monkeypatch.setattr(
        vox, "design_save",
        lambda *a, **k: pytest.fail("design_save called despite missing field"),
    )
    # missing name
    resp = cli.post("/elevenlabs/voices/design/save", json={"generated_voice_id": "gen-1"})
    assert resp.status_code == 400
    # missing generated_voice_id
    resp = cli.post("/elevenlabs/voices/design/save", json={"name": "Gandalf"})
    assert resp.status_code == 400
    # empty body
    resp = cli.post("/elevenlabs/voices/design/save", json={})
    assert resp.status_code == 400


def test_design_save_success(cli, monkeypatch):
    seen = {}

    def fake_save(generated_voice_id, name, description):
        seen.update(generated_voice_id=generated_voice_id, name=name, description=description)
        return {"voice_id": "saved-xyz"}

    monkeypatch.setattr(vox, "design_save", fake_save)

    resp = cli.post(
        "/elevenlabs/voices/design/save",
        json={"generated_voice_id": "gen-1", "name": "Gandalf", "description": "old wizard"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"voice_id": "saved-xyz"}
    assert seen == {"generated_voice_id": "gen-1", "name": "Gandalf", "description": "old wizard"}


# =============================================================================
# GET voices + DELETE
# =============================================================================

def test_get_voices_returns_grouping(cli, monkeypatch):
    grouped = {
        "my_voices": [{"id": "elevenlabs:mine-1", "name": "Mine"}],
        "premade": [{"id": "elevenlabs:pm-1", "name": "Rachel"}],
    }
    monkeypatch.setattr(cat, "get_voices", lambda *a, **k: grouped)
    resp = cli.get("/elevenlabs/voices")
    assert resp.status_code == 200
    assert resp.json() == grouped


def test_get_voices_no_key_returns_empty(cli, monkeypatch):
    monkeypatch.setattr(cat, "get_voices", lambda *a, **k: None)
    resp = cli.get("/elevenlabs/voices")
    assert resp.status_code == 200
    assert resp.json() == {"my_voices": [], "premade": []}


def test_delete_returns_ok_and_in_use(cli, monkeypatch):
    """DELETE checks in-use, deletes, and surfaces the in-use list for the UI."""
    calls = {}
    monkeypatch.setattr(vox, "voice_in_use", lambda vid: ["Brandon"])

    def fake_delete(vid):
        calls["deleted"] = vid
        return {"ok": True}

    monkeypatch.setattr(vox, "delete_voice", fake_delete)

    resp = cli.delete("/elevenlabs/voices/elevenlabs:abc123")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "in_use": ["Brandon"]}
    assert calls["deleted"] == "elevenlabs:abc123"


def test_delete_not_in_use(cli, monkeypatch):
    monkeypatch.setattr(vox, "voice_in_use", lambda vid: [])
    monkeypatch.setattr(vox, "delete_voice", lambda vid: {"ok": True})
    resp = cli.delete("/elevenlabs/voices/abc123")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "in_use": []}


def test_delete_runtime_error_maps_to_400(cli, monkeypatch):
    monkeypatch.setattr(vox, "voice_in_use", lambda vid: [])

    def boom(vid):
        raise RuntimeError("ElevenLabs error 404: voice not found")

    monkeypatch.setattr(vox, "delete_voice", boom)
    resp = cli.delete("/elevenlabs/voices/nope")
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"]
