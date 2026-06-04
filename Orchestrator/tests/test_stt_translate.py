import pytest
from unittest.mock import patch
from Orchestrator.stt import translate as tr

def test_prefers_gemini_when_google_key(monkeypatch):
    monkeypatch.setattr(tr.config, "GOOGLE_API_KEY", "g", raising=False)
    monkeypatch.setattr(tr.config, "OPENAI_API_KEY", "o", raising=False)
    with patch.object(tr, "_gemini_translate", return_value="hola") as mg, \
         patch.object(tr, "_openai_translate", return_value="X") as mo:
        assert tr.translate_text("hello", "es") == "hola"
        mg.assert_called_once(); mo.assert_not_called()

def test_falls_back_to_openai_when_no_google(monkeypatch):
    monkeypatch.setattr(tr.config, "GOOGLE_API_KEY", "", raising=False)
    monkeypatch.setattr(tr.config, "GEMINI_API_KEY", "", raising=False)
    monkeypatch.setattr(tr.config, "OPENAI_API_KEY", "o", raising=False)
    with patch.object(tr, "_openai_translate", return_value="bonjour") as mo:
        assert tr.translate_text("hello", "fr") == "bonjour"
        mo.assert_called_once()

def test_no_provider_raises(monkeypatch):
    monkeypatch.setattr(tr.config, "GOOGLE_API_KEY", "", raising=False)
    monkeypatch.setattr(tr.config, "GEMINI_API_KEY", "", raising=False)
    monkeypatch.setattr(tr.config, "OPENAI_API_KEY", "", raising=False)
    with pytest.raises(RuntimeError):
        tr.translate_text("hello", "fr")

def test_empty_text_short_circuits():
    assert tr.translate_text("", "fr") == ""

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload

def test_gemini_malformed_body_raises_runtimeerror(monkeypatch):
    # Safety-blocked / empty-candidates 200 body -> clean RuntimeError, not KeyError.
    monkeypatch.setattr(tr.config, "GOOGLE_API_KEY", "g", raising=False)
    monkeypatch.setattr(tr.config, "GEMINI_API_KEY", "", raising=False)
    with patch.object(tr.requests, "post", return_value=_FakeResp({"candidates": []})):
        with pytest.raises(RuntimeError, match="translation provider returned no text"):
            tr.translate_text("hello", "es")

def test_openai_malformed_body_raises_runtimeerror(monkeypatch):
    monkeypatch.setattr(tr.config, "GOOGLE_API_KEY", "", raising=False)
    monkeypatch.setattr(tr.config, "GEMINI_API_KEY", "", raising=False)
    monkeypatch.setattr(tr.config, "OPENAI_API_KEY", "o", raising=False)
    with patch.object(tr.requests, "post", return_value=_FakeResp({"choices": []})):
        with pytest.raises(RuntimeError, match="translation provider returned no text"):
            tr.translate_text("hello", "fr")

def test_route_text(monkeypatch):
    import Orchestrator.app  # noqa: F401 — register routes
    with patch("Orchestrator.stt.translate.translate_text", return_value="hola"):
        from fastapi.testclient import TestClient
        from Orchestrator.checkpoint import app
        r = TestClient(app).post("/stt/translate", json={"text":"hello","target_lang":"es"})
        assert r.status_code == 200
        assert r.json() == {"text":"hola","target_lang":"es"}

def test_route_requires_target_lang(monkeypatch):
    import Orchestrator.app  # noqa: F401
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    r = TestClient(app).post("/stt/translate", json={"text":"hello"})
    assert r.status_code == 400
