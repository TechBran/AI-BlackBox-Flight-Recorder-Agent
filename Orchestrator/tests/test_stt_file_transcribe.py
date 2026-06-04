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
