"""Hermetic tests for the ElevenLabs client core. No network, no live key."""
import pytest
from Orchestrator.elevenlabs import client as el


def test_resolve_key_prefers_env_file(monkeypatch, tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text('ELEVENLABS_API_KEY="xi-from-file"\n')
    monkeypatch.setattr(el, "_env_file_path", lambda: str(envfile))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert el.resolve_api_key() == "xi-from-file"


def test_resolve_key_falls_back_to_os_environ(monkeypatch, tmp_path):
    monkeypatch.setattr(el, "_env_file_path", lambda: str(tmp_path / "missing.env"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-from-env")
    assert el.resolve_api_key() == "xi-from-env"


def test_resolve_key_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(el, "_env_file_path", lambda: str(tmp_path / "missing.env"))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert el.resolve_api_key() is None


def test_map_error_normalizes_provider_taxonomy():
    assert el.map_error(401, {"detail": {"status": "auth_error"}}).startswith("ElevenLabs auth")
    assert "quota" in el.map_error(429, {"detail": {"status": "quota_exceeded"}}).lower()
    assert el.map_error(500, {}).startswith("ElevenLabs error")
