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
