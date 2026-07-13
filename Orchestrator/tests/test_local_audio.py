"""Per-server audio config: resolve_audio + has_audio + validation + storage."""
import pytest
from Orchestrator.onboarding import custom_servers as cs


def _fake_servers(monkeypatch, servers):
    monkeypatch.setattr(cs, "list_servers", lambda enabled_only=False: list(servers))


def test_resolve_audio_picks_capable_server(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "api_key": "k", "enabled": True,
         "audio": {"stt": True, "tts": True, "streaming": True,
                   "stt_model": "faster-whisper-turbo", "tts_model": "kokoro"}}])
    assert cs.resolve_audio("stt")[1] == "faster-whisper-turbo"
    assert cs.resolve_audio("tts")[1] == "kokoro"
    assert cs.resolve_audio("streaming")[1] == "faster-whisper-turbo"  # streaming uses stt_model
    assert cs.has_audio("stt") and cs.has_audio("tts") and cs.has_audio("streaming")


def test_resolve_audio_defaults_when_model_absent(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True, "audio": {"stt": True}}])
    assert cs.resolve_audio("stt")[1] == cs.SPEACHES_STT_DEFAULT
    assert cs.resolve_audio("tts") is None
    assert cs.has_audio("tts") is False


def test_resolve_audio_none_when_no_audio(monkeypatch):
    _fake_servers(monkeypatch, [{"alias": "box", "base_url": "http://h/v1", "enabled": True}])
    assert cs.resolve_audio("stt") is None
    assert cs.has_audio("streaming") is False


def test_audio_field_validation():
    cs._validate_field_types({"audio": {"stt": True, "stt_model": "x"}})  # ok
    with pytest.raises(ValueError):
        cs._validate_field_types({"audio": {"stt": "yes"}})
    with pytest.raises(ValueError):
        cs._validate_field_types({"audio": {"stt_model": 5}})
    with pytest.raises(ValueError):
        cs._validate_field_types({"audio": ["nope"]})


def test_audio_patchable_and_stored(monkeypatch):
    assert "audio" in cs._PATCHABLE_FIELDS
    captured = {}
    monkeypatch.setattr(cs, "_read", lambda: {"version": 1, "servers": []})
    monkeypatch.setattr(cs, "_write", lambda d: captured.update(d=d))
    srv = cs.add_server("box", "http://h/v1", "k", audio={"stt": True, "stt_model": "w"})
    assert srv["audio"] == {"stt": True, "stt_model": "w"}


# --- /validate audio probe + persistence -----------------------------------
class _M:
    def __init__(self, i):
        self.id = i


class _Client:
    def __init__(self, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    class models:
        @staticmethod
        def list():
            class _R:
                data = [_M("gemma-31b")]
            return _R()


def test_validate_custom_probes_audio(monkeypatch):
    from Orchestrator.onboarding import validators
    import openai
    import httpx
    monkeypatch.setattr(openai, "OpenAI", _Client)

    class _Resp:
        def __init__(self, c):
            self.status_code = c

    def fake_get(url, **k):
        if "/audio/transcriptions" in url or "/audio/speech" in url:
            return _Resp(405)
        if "/realtime" in url:
            return _Resp(307)
        return _Resp(404)

    monkeypatch.setattr(httpx, "get", fake_get)
    res = validators.validate_custom("http://h/v1", "k")
    assert res.ok
    assert res.detail["audio"] == {"stt": True, "tts": True, "streaming": True}


def test_revalidate_persists_audio_preserving_model_ids(monkeypatch):
    from Orchestrator.routes import onboarding_routes
    from Orchestrator.onboarding.validators import ValidationResult
    captured = {}
    stored = {"id": "srv-x", "base_url": "http://h/v1", "api_key": "k",
              "audio": {"stt": True, "stt_model": "user-whisper"}}  # user set the id
    monkeypatch.setattr(onboarding_routes.custom_servers, "get_server", lambda s: dict(stored))
    monkeypatch.setattr(onboarding_routes.custom_servers, "update_server",
                        lambda s, p: captured.update(p) or dict(stored, **p))
    monkeypatch.setattr(onboarding_routes.validators, "validate_custom",
                        lambda base_url, api_key="": ValidationResult(
                            ok=True, latency_ms=1, error=None,
                            detail={"models": ["gemma-31b"], "model_modalities": {"gemma-31b": "chat"},
                                    "capabilities": ["chat"],
                                    "audio": {"stt": True, "tts": True, "streaming": True}}))
    monkeypatch.setattr(onboarding_routes._state, "record_validation", lambda *a, **k: None)
    import Orchestrator.app  # noqa: F401
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    r = TestClient(app).post("/onboarding/validate",
                             json={"provider": "custom", "credentials": {"server_id": "srv-x"}})
    assert r.status_code == 200, r.text
    a = captured["audio"]
    assert a["stt"] and a["tts"] and a["streaming"]          # bools refreshed from probe
    assert a["stt_model"] == "user-whisper"                  # user's id PRESERVED
    assert a["tts_model"] == onboarding_routes.custom_servers.SPEACHES_TTS_DEFAULT  # tts id defaulted
