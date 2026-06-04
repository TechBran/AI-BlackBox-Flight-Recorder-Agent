from Orchestrator.stt.catalog import build_stt_catalog

def test_two_providers_in_order():
    assert [p["id"] for p in build_stt_catalog()] == ["openai", "google"]

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

def test_catalog_route_ok():
    import Orchestrator.app  # noqa: F401  -- side-effect: registers routes onto the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    r = TestClient(app).get("/stt/catalog")
    assert r.status_code == 200
    body = r.json()
    assert [p["id"] for p in body["providers"]] == ["openai", "google"]
    assert "resolved" in body and "default" in body
