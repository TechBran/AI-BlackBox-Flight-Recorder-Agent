"""Local TTS (/v1/audio/speech): resolver, voice probe, and the /tts/catalog group."""
from Orchestrator.onboarding import custom_servers as cs


def _fake_servers(monkeypatch, servers):
    monkeypatch.setattr(cs, "list_servers", lambda enabled_only=False: list(servers))


def test_resolve_tts_server(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True,
         "last_models": ["gemma-31b", "kokoro-tts"]}])
    assert cs.resolve_tts_server()[1] == "kokoro-tts"
    assert cs.has_modality_model("tts") is True


def test_list_local_tts_voices_probe(monkeypatch):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"voices": [{"id": "af_bella"}, "am_adam", {"name": "nova"}]}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    voices = cs.list_local_tts_voices({"base_url": "http://h/v1", "api_key": "k"})
    assert voices == ["af_bella", "am_adam", "nova"]


def test_list_local_tts_voices_fallback(monkeypatch):
    import requests

    def _boom(*a, **k):
        raise RuntimeError("no /audio/voices endpoint")

    monkeypatch.setattr(requests, "get", _boom)
    # Fallback is the Kokoro roster (not a bare 'default'), so the picker is usable.
    assert cs.list_local_tts_voices({"base_url": "http://h/v1"}) == list(cs.KOKORO_VOICES)
    assert "af_heart" in cs.list_local_tts_voices({"base_url": "http://h/v1"})


def test_tts_catalog_includes_local_group(monkeypatch):
    import Orchestrator.app  # noqa: F401 -- registers routes
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    from Orchestrator.onboarding import custom_servers
    monkeypatch.setattr(custom_servers, "has_audio", lambda kind: kind == "tts")
    monkeypatch.setattr(custom_servers, "resolve_audio",
                        lambda kind: ({"base_url": "http://h/v1", "api_key": ""}, "kokoro-tts"))
    monkeypatch.setattr(custom_servers, "list_local_tts_voices", lambda srv: ["af_bella"])
    r = TestClient(app).get("/tts/catalog")
    assert r.status_code == 200
    groups = r.json()["groups"]
    ids = [g["id"] for g in groups]
    assert "local" in ids
    local = next(g for g in groups if g["id"] == "local")
    assert local["voices"][0]["id"] == "local:af_bella"
