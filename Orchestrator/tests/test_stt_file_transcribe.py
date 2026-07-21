import pytest
from unittest.mock import patch
from Orchestrator.stt import file_transcribe as ft

def test_openai_branch_uses_helper():
    with patch.object(ft, "_openai_transcribe", return_value="hello") as m, \
         patch.object(ft, "resolve_stt_provider", return_value="openai"):
        assert ft.transcribe_bytes(b"x", "audio/wav") == "hello"
        m.assert_called_once()

def test_google_branch_uses_helper():
    with patch.object(ft, "_google_transcribe", return_value="bonjour") as m, \
         patch.object(ft, "resolve_stt_provider", return_value="google"):
        assert ft.transcribe_bytes(b"x", "audio/wav") == "bonjour"
        m.assert_called_once()

def test_explicit_provider_overrides_resolver():
    with patch.object(ft, "_openai_transcribe", return_value="hi") as mo, \
         patch.object(ft, "_google_transcribe", return_value="salut") as mg:
        assert ft.transcribe_bytes(b"x", "audio/wav", provider="google") == "salut"
        mo.assert_not_called(); mg.assert_called_once()

def test_no_provider_raises():
    with patch.object(ft, "resolve_stt_provider", return_value=None):
        with pytest.raises(RuntimeError):
            ft.transcribe_bytes(b"x", "audio/wav")

def test_google_missing_creds_raises_runtimeerror(monkeypatch):
    monkeypatch.setattr(ft.config, "GOOGLE_APPLICATION_CREDENTIALS", "")
    with pytest.raises(RuntimeError):
        ft._google_transcribe(b"x", "audio/wav", "audio.wav")


def test_onbox_transcribe_posts_to_9098_with_model(monkeypatch):
    from Orchestrator.stt import file_transcribe as ft
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    monkeypatch.setattr(local_stack, "stt_batch_model", lambda: "Systran/faster-whisper-large-v3")
    captured = {}

    class _Resp:
        status_code = 200
        def json(self): return {"text": " hello "}

    def _post(url, **kw):
        captured["url"] = url
        captured["model"] = kw["data"]["model"]
        return _Resp()

    monkeypatch.setattr(ft.requests, "post", _post)
    out = ft.transcribe_bytes(b"RIFF...", "audio/wav", provider="onbox", filename="a.wav")
    assert out == "hello"
    assert captured["url"] == "http://127.0.0.1:9098/v1/audio/transcriptions"
    assert captured["model"] == "Systran/faster-whisper-large-v3"


def test_onbox_transcribe_retries_on_429(monkeypatch):
    from Orchestrator.stt import file_transcribe as ft
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    monkeypatch.setattr(local_stack, "stt_batch_model", lambda: "m")
    monkeypatch.setattr(ft.time, "sleep", lambda *_: None)  # no real backoff wait
    calls = {"n": 0}

    class _Resp:
        def __init__(self, code, text=""):
            self.status_code = code
            self._t = text
        def json(self): return {"text": self._t}

    def _post(url, **kw):
        calls["n"] += 1
        return _Resp(429) if calls["n"] < 3 else _Resp(200, "done")

    monkeypatch.setattr(ft.requests, "post", _post)
    assert ft.transcribe_bytes(b"x", "audio/wav", provider="onbox") == "done"
    assert calls["n"] == 3   # two 429s then success


def test_onbox_transcribe_raises_after_429_exhaustion(monkeypatch):
    import pytest
    from Orchestrator.stt import file_transcribe as ft
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    monkeypatch.setattr(local_stack, "stt_batch_model", lambda: "m")
    monkeypatch.setattr(ft.time, "sleep", lambda *_: None)

    class _Resp:
        status_code = 429
        text = "busy"
        def json(self): return {"error": "busy"}

    monkeypatch.setattr(ft.requests, "post", lambda url, **kw: _Resp())
    with pytest.raises(RuntimeError):
        ft.transcribe_bytes(b"x", "audio/wav", provider="onbox")
