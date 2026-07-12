"""Consent-gate tests for the xai_clone_voice ToolVault executor.

The gate MUST refuse BEFORE any provider call when confirm_consent is not
explicitly true — mirroring elevenlabs_clone_voice verbatim. Provider calls are
monkeypatched on Orchestrator.xai_voices (imported inside the executor)."""
import asyncio

import pytest

from Orchestrator import xai_voices as xv
from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext


@pytest.fixture
def execute():
    ex = registry.get_executor("xai_clone_voice")
    assert ex is not None, f"executor failed to load: {registry.load_errors()}"
    return ex


def _ctx():
    return ToolContext(operator="TestOp", base_url="http://localhost:9091")


def test_tool_is_in_catalog():
    assert any(t["name"] == "xai_clone_voice" for t in registry.load_canonical())


def test_refuses_without_consent_and_never_calls_provider(execute, monkeypatch, tmp_path):
    sample = tmp_path / "s.mp3"
    sample.write_bytes(b"ID3fakeaudio")
    monkeypatch.setattr(
        xv, "clone_voice",
        lambda *a, **k: pytest.fail("clone_voice called despite missing consent"))
    r = asyncio.run(execute(
        {"name": "V", "audio_path": str(sample), "confirm_consent": False}, _ctx()))
    assert r.success is False
    assert "confirm" in r.result.lower()


def test_refuses_missing_audio_file(execute, monkeypatch):
    monkeypatch.setattr(
        xv, "clone_voice",
        lambda *a, **k: pytest.fail("clone_voice called despite missing file"))
    r = asyncio.run(execute(
        {"name": "V", "audio_path": "/nope/missing.mp3", "confirm_consent": True}, _ctx()))
    assert r.success is False
    assert "not found" in r.result.lower()


def test_clones_with_consent(execute, monkeypatch, tmp_path):
    sample = tmp_path / "s.mp3"
    sample.write_bytes(b"ID3fakeaudio")
    seen = {}

    def fake_clone(name, audio_path, description=None):
        seen.update(name=name, audio_path=audio_path, description=description)
        return {"voice_id": "cv-new", "name": name}

    monkeypatch.setattr(xv, "clone_voice", fake_clone)
    r = asyncio.run(execute(
        {"name": "My Grok Voice", "audio_path": str(sample),
         "confirm_consent": True, "description": "warm"}, _ctx()))
    assert r.success is True
    assert "cv-new" in r.result
    assert r.data == {"voice_id": "cv-new"}
    assert seen == {"name": "My Grok Voice", "audio_path": str(sample), "description": "warm"}


def test_provider_error_returns_failure_not_exception(execute, monkeypatch, tmp_path):
    sample = tmp_path / "s.mp3"
    sample.write_bytes(b"x")

    def boom(*a, **k):
        raise RuntimeError("xAI error 400: audio longer than 120 seconds")

    monkeypatch.setattr(xv, "clone_voice", boom)
    r = asyncio.run(execute(
        {"name": "V", "audio_path": str(sample), "confirm_consent": True}, _ctx()))
    assert r.success is False
    assert "120 seconds" in r.result
