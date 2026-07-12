from Orchestrator.stt import catalog as stt_catalog
from Orchestrator.stt.catalog import build_stt_catalog

def test_three_providers_in_order(monkeypatch):
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: False)  # hermetic vs registry
    assert [p["id"] for p in build_stt_catalog()] == ["openai", "google", "elevenlabs"]

def test_provider_shape():
    for p in build_stt_catalog():
        assert p["label"] and p["blurb"]
        assert "available" in p and isinstance(p["available"], bool)
        assert "streaming" in p["models"] and "file" in p["models"]

def test_models_reflect_config():
    g = {p["id"]: p for p in build_stt_catalog()}
    assert g["openai"]["models"]["streaming"] == "gpt-realtime-whisper"
    assert g["openai"]["models"]["file"] == "gpt-4o-transcribe"
    assert g["google"]["models"]["streaming"] == "chirp_2"
    assert g["google"]["models"]["file"] == "chirp_2"
    assert g["elevenlabs"]["models"]["streaming"] == "scribe_v2_realtime"
    assert g["elevenlabs"]["models"]["file"] == "scribe_v2"

def test_available_flags_follow_stt_availability(monkeypatch):
    monkeypatch.setattr(stt_catalog, "stt_availability", lambda: (True, False, False))
    g = {p["id"]: p for p in build_stt_catalog()}
    assert g["openai"]["available"] is True
    assert g["google"]["available"] is False
    assert g["elevenlabs"]["available"] is False
    monkeypatch.setattr(stt_catalog, "stt_availability", lambda: (False, True, True))
    g = {p["id"]: p for p in build_stt_catalog()}
    assert g["openai"]["available"] is False
    assert g["google"]["available"] is True
    assert g["elevenlabs"]["available"] is True

def test_catalog_route_ok(monkeypatch):
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: False)  # hermetic vs registry
    import Orchestrator.app  # noqa: F401  -- side-effect: registers routes onto the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    r = TestClient(app).get("/stt/catalog")
    assert r.status_code == 200
    body = r.json()
    assert [p["id"] for p in body["providers"]] == ["openai", "google", "elevenlabs"]
    assert "resolved" in body and "default" in body
