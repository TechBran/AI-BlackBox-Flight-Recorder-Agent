"""Local STT (/v1/audio/transcriptions, file-only): resolver, availability, dispatch."""
from Orchestrator.onboarding import custom_servers as cs
from Orchestrator.stt import resolve as stt_resolve
from Orchestrator.stt import catalog as stt_catalog
from Orchestrator.stt import file_transcribe


def _fake_servers(monkeypatch, servers):
    monkeypatch.setattr(cs, "list_servers", lambda enabled_only=False: list(servers))


def test_local_stt_available(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True,
         "last_models": ["gemma-31b", "whisper-1"]}])
    assert stt_resolve.local_stt_available() is True
    assert cs.resolve_stt_server()[1] == "whisper-1"


def test_local_stt_unavailable_when_no_model(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True, "last_models": ["gemma-31b"]}])
    assert stt_resolve.local_stt_available() is False


def test_catalog_includes_local_when_available(monkeypatch):
    monkeypatch.setattr(stt_catalog, "local_stt_available", lambda: True)
    ids = [p["id"] for p in stt_catalog.build_stt_catalog()]
    assert "local" in ids


def test_resolve_returns_local_when_only_one():
    assert stt_resolve.resolve_stt_provider("", openai_ok=False, google_ok=False,
                                            elevenlabs_ok=False, local_ok=True) == "local"


def test_resolve_local_is_last_tiebreak():
    # openai still wins when both available (local is a file-only fallback)
    assert stt_resolve.resolve_stt_provider("", openai_ok=True, google_ok=False,
                                            elevenlabs_ok=False, local_ok=True) == "openai"


def test_transcribe_bytes_local(monkeypatch):
    monkeypatch.setattr(cs, "resolve_stt_server",
                        lambda model=None: ({"base_url": "http://h/v1", "api_key": "k"}, "whisper-1"))
    captured = {}

    class _R:
        status_code = 200

        def json(self):
            return {"text": "hello world"}

    def fake_post(url, headers=None, data=None, files=None, timeout=None, **kw):
        captured.update(url=url, data=data, headers=headers)
        return _R()

    monkeypatch.setattr(file_transcribe.requests, "post", fake_post)
    out = file_transcribe.transcribe_bytes(b"AUDIO", "audio/wav", provider="local", filename="a.wav")
    assert out == "hello world"
    assert captured["url"] == "http://h/v1/audio/transcriptions"
    assert captured["data"] == {"model": "whisper-1"}
    assert captured["headers"]["Authorization"] == "Bearer k"
