"""Hermetic tests for Orchestrator/xai_voices.py — the xAI Custom Voices provider
module. httpx is mocked at the module seam (xai_voices calls httpx.get/post/delete
as module attributes), so no live xAI call ever happens."""
import json
import time as time_module

import pytest

from Orchestrator import xai_voices as xv


class FakeResp:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "xai-fake-key")
    xv._cache["ts"] = 0.0
    xv._cache["ids"] = frozenset()
    yield
    xv._cache["ts"] = 0.0
    xv._cache["ids"] = frozenset()


def test_list_parses_voices_envelope(monkeypatch):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(
        200, {"voices": [{"voice_id": "cv-1", "name": "Narrator"}]}))
    voices = xv.list_custom_voices()
    assert voices == [{"voice_id": "cv-1", "name": "Narrator"}]


def test_list_parses_bare_list(monkeypatch):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(
        200, [{"id": "cv-2", "name": "Alt"}]))
    assert xv.list_custom_voices() == [{"id": "cv-2", "name": "Alt"}]


def test_list_no_key_returns_none(monkeypatch):
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "")
    monkeypatch.setattr(xv.httpx, "get",
                        lambda *a, **k: pytest.fail("network hit despite no key"))
    assert xv.list_custom_voices() is None


def test_list_provider_error_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(
        401, {"error": "invalid api key"}))
    with pytest.raises(RuntimeError, match="401"):
        xv.list_custom_voices()


def test_clone_posts_multipart_and_returns_body(monkeypatch, tmp_path):
    sample = tmp_path / "sample.mp3"
    sample.write_bytes(b"ID3fakeaudio")
    seen = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        seen.update(url=url, data=data, file_field=list(files.keys()))
        return FakeResp(200, {"voice_id": "cv-new", "name": "My Voice"})

    monkeypatch.setattr(xv.httpx, "post", fake_post)
    result = xv.clone_voice("My Voice", str(sample), description="warm")
    assert result["voice_id"] == "cv-new"
    assert seen["url"] == xv.XAI_VOICES_URL
    assert seen["data"]["name"] == "My Voice"
    assert seen["data"]["description"] == "warm"
    assert seen["file_field"] == ["file"]


def test_clone_no_key_raises(monkeypatch, tmp_path):
    sample = tmp_path / "s.mp3"
    sample.write_bytes(b"x")
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "")
    with pytest.raises(RuntimeError, match="not configured"):
        xv.clone_voice("X", str(sample))


def test_delete_hits_id_url(monkeypatch):
    seen = {}
    monkeypatch.setattr(xv.httpx, "delete",
                        lambda url, **kw: (seen.update(url=url), FakeResp(200, {"ok": True}))[1])
    xv.delete_voice("cv-1")
    assert seen["url"] == f"{xv.XAI_VOICES_URL}/cv-1"


# =============================================================================
# is_custom_voice — 60s cache + fail-open (design: workstream 5 / scope item 3)
# =============================================================================

def test_is_custom_voice_hits_and_misses(monkeypatch):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(
        200, {"voices": [{"voice_id": "cv-1", "name": "N"}]}))
    assert xv.is_custom_voice("cv-1") is True
    assert xv.is_custom_voice("not-a-voice") is False


def test_is_custom_voice_caches_for_ttl(monkeypatch):
    calls = {"n": 0}

    def counting_get(url, **kw):
        calls["n"] += 1
        return FakeResp(200, {"voices": [{"voice_id": "cv-1"}]})

    monkeypatch.setattr(xv.httpx, "get", counting_get)
    assert xv.is_custom_voice("cv-1") is True
    assert xv.is_custom_voice("cv-1") is True
    assert xv.is_custom_voice("cv-2") is False
    assert calls["n"] == 1  # ONE fetch inside the 60s window


def test_is_custom_voice_refetches_after_ttl(monkeypatch):
    calls = {"n": 0}

    def counting_get(url, **kw):
        calls["n"] += 1
        return FakeResp(200, {"voices": [{"voice_id": "cv-1"}]})

    monkeypatch.setattr(xv.httpx, "get", counting_get)
    assert xv.is_custom_voice("cv-1") is True
    xv._cache["ts"] = time_module.time() - 61  # age the cache past TTL
    assert xv.is_custom_voice("cv-1") is True
    assert calls["n"] == 2


def test_is_custom_voice_fail_open_when_unreachable(monkeypatch):
    def boom(url, **kw):
        raise Exception("connection refused")

    monkeypatch.setattr(xv.httpx, "get", boom)
    assert xv.is_custom_voice("cv-1") is False  # empty cache + unreachable -> catalog-only


def test_is_custom_voice_keeps_stale_ids_on_refresh_failure(monkeypatch):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(
        200, {"voices": [{"voice_id": "cv-1"}]}))
    assert xv.is_custom_voice("cv-1") is True
    xv._cache["ts"] = time_module.time() - 61

    def boom(url, **kw):
        raise Exception("xai down")

    monkeypatch.setattr(xv.httpx, "get", boom)
    assert xv.is_custom_voice("cv-1") is True  # stale set survives the outage


def test_is_custom_voice_no_key_is_false(monkeypatch):
    monkeypatch.setattr(xv, "resolve_api_key", lambda: "")
    assert xv.is_custom_voice("cv-1") is False


def test_clone_and_delete_bust_the_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(xv.httpx, "get", lambda url, **kw: FakeResp(200, {"voices": []}))
    xv.is_custom_voice("cv-1")
    assert xv._cache["ts"] > 0
    sample = tmp_path / "s.mp3"
    sample.write_bytes(b"x")
    monkeypatch.setattr(xv.httpx, "post", lambda *a, **k: FakeResp(200, {"voice_id": "cv-9"}))
    xv.clone_voice("V", str(sample))
    assert xv._cache["ts"] == 0.0
