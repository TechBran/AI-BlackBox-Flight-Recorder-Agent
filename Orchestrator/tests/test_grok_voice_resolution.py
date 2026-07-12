"""Tests for resolve_grok_voice — Grok session voice validation now accepts
catalog voices OR a cloned xAI custom-voice id (verified via the 60s-cached
is_custom_voice; fail-open to catalog default when unverifiable)."""
import asyncio

import pytest

from Orchestrator import xai_voices as xv
from Orchestrator.config import GROK_LIVE_DEFAULT_VOICE, GROK_LIVE_VOICES
from Orchestrator.routes.grok_live_routes import resolve_grok_voice


def test_catalog_voice_passes_without_network(monkeypatch):
    monkeypatch.setattr(
        xv, "is_custom_voice",
        lambda vid: pytest.fail("is_custom_voice called for a catalog voice"))
    voice = GROK_LIVE_VOICES[0]
    assert asyncio.run(resolve_grok_voice(voice)) == voice


def test_verified_custom_voice_is_accepted(monkeypatch):
    monkeypatch.setattr(xv, "is_custom_voice", lambda vid: vid == "cv-cloned-1")
    assert asyncio.run(resolve_grok_voice("cv-cloned-1")) == "cv-cloned-1"


def test_unverified_id_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(xv, "is_custom_voice", lambda vid: False)
    assert asyncio.run(resolve_grok_voice("cv-unknown")) == GROK_LIVE_DEFAULT_VOICE


def test_verifier_exception_falls_back_to_default(monkeypatch):
    def boom(vid):
        raise Exception("unexpected")
    monkeypatch.setattr(xv, "is_custom_voice", boom)
    assert asyncio.run(resolve_grok_voice("cv-unknown")) == GROK_LIVE_DEFAULT_VOICE
