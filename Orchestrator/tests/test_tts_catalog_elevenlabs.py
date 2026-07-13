"""Hermetic tests for the hybrid /tts/catalog merge (ElevenLabs 4th group).

The route imports ``catalog as el_catalog`` inside the handler, so we monkeypatch
``el_catalog.get_voices`` where it is looked up -- no live ElevenLabs call. The
no-key path (get_voices -> None) is a regression guard that the three static
groups are returned byte-for-byte unchanged.
"""
import pytest
from fastapi.testclient import TestClient

from Orchestrator.app import app
from Orchestrator.elevenlabs import catalog as el_catalog


@pytest.fixture
def cli():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_local_audio(monkeypatch):
    """Isolate these ElevenLabs-merge tests from the box's real custom-server
    registry. The /tts/catalog handler also appends a 'local' group when a
    registered server hosts a TTS model; force it OFF so the exact group-list
    assertions here stay hermetic regardless of what audio servers are
    registered. Positive coverage for the local group lives in test_local_tts.py."""
    monkeypatch.setattr("Orchestrator.onboarding.custom_servers.has_audio",
                        lambda kind: False)


def test_no_key_returns_exactly_three_static_groups(cli, monkeypatch):
    """get_voices None (no key) -> the original 3 groups only (regression guard)."""
    monkeypatch.setattr(el_catalog, "get_voices", lambda *a, **k: None)

    resp = cli.get("/tts/catalog")
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert [g["id"] for g in groups] == ["openai", "gemini-flash", "gemini-pro"]


def test_key_present_appends_elevenlabs_group_my_voices_first(cli, monkeypatch):
    """With voices -> 4th dynamic 'elevenlabs' group, My Voices first (star-prefixed)."""
    monkeypatch.setattr(el_catalog, "get_voices", lambda *a, **k: {
        "my_voices": [
            {"id": "elevenlabs:own1", "name": "My Clone",
             "description": "personal", "preview_url": "https://p/own.mp3", "category": "cloned"},
        ],
        "premade": [
            {"id": "elevenlabs:pre1", "name": "Rachel",
             "description": "female", "preview_url": "https://p/r.mp3", "category": "premade"},
            {"id": "elevenlabs:pre2", "name": "Adam",
             "description": "male", "preview_url": "https://p/a.mp3", "category": "premade"},
        ],
    })

    resp = cli.get("/tts/catalog")
    assert resp.status_code == 200
    groups = resp.json()["groups"]

    assert [g["id"] for g in groups] == ["openai", "gemini-flash", "gemini-pro", "elevenlabs"]
    el = groups[-1]
    assert el["label"] == "ElevenLabs"
    assert el["dynamic"] is True

    voices = el["voices"]
    assert len(voices) == 3  # 1 cloned + 2 premade
    # My Voices come FIRST and are star-prefixed.
    assert voices[0]["name"].startswith("⭐ ")
    assert voices[0]["name"] == "⭐ My Clone"
    # Premade follow and are NOT star-prefixed.
    assert voices[1]["name"] == "Rachel"
    assert voices[2]["name"] == "Adam"
    # Every id carries the elevenlabs: prefix.
    assert all(v["id"].startswith("elevenlabs:") for v in voices)
    # Additive fields pass through harmlessly.
    assert voices[0]["preview_url"] == "https://p/own.mp3"
    assert voices[0]["category"] == "cloned"


def test_catalog_unreachable_fails_open(cli, monkeypatch):
    """get_voices raising -> fail-open, still exactly the 3 static groups."""
    def boom(*a, **k):
        raise RuntimeError("ElevenLabs unreachable")
    monkeypatch.setattr(el_catalog, "get_voices", boom)

    resp = cli.get("/tts/catalog")
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert [g["id"] for g in groups] == ["openai", "gemini-flash", "gemini-pro"]
