"""The Google API key travels in the x-goog-api-key header, never in a URL
(audit rank 23: O07-tts-voice-01, X03-security-backend-11, O02-chat-routes-A-07,
O03-chat-routes-B-15).

requests/httpx/websockets put the full request URL in their exception text, so
a ``?key=`` URL leaked the key into journald, HTTPException details and the
tasks.db error rows that /tasks polling serves to the Portal and Android. For
every Google call site these tests assert:

  * the request carries ``x-goog-api-key`` and the URL has no key,
  * the error path returns the upstream STATUS (a 4xx requests.Response is
    falsy, so ``if e.response`` always fell back to 502) and only the parsed
    error message, never the raw body or ``str(e)`` with the URL,
  * nothing printed on the error path carries the key.

Hermetic: requests/httpx/websockets are replaced; no network. The fake key is
assembled from pieces so no key shape sits in this public repo.
"""
import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from fastapi import HTTPException

FAKE_KEY = "AIza" + "K" * 35
HDR = "x-goog-api-key"
# "?key=<value>" as a leaked URL carries it; split so the URL-key guard
# (test_no_key_in_url_guard.py) does not flag this file.
KEY_QS = "?" + "key="


def _assert_header_auth(url, headers):
    assert "key=" not in url, f"key still in the URL: {url}"
    assert FAKE_KEY not in url
    assert (headers or {}).get(HDR) == FAKE_KEY


def _google_error_response(status, url):
    """A real requests.Response (falsy for >=400) whose body echoes the key."""
    r = requests.Response()
    r.status_code = status
    r.reason = "Forbidden"
    r.url = url
    r.headers["Content-Type"] = "application/json"
    r._content = json.dumps({
        "error": {
            "code": status,
            "message": "API key not valid. Please pass a valid API key.",
            "status": "INVALID_ARGUMENT",
            "details": [{"echo": f"{url}{KEY_QS}{FAKE_KEY}"}],
        }
    }).encode()
    return r


def _http_error(status, url):
    resp = _google_error_response(status, url)
    return requests.HTTPError(
        f"{status} Client Error: Forbidden for url: {url}{KEY_QS}{FAKE_KEY}", response=resp)


# --------------------------------------------------------------------------- #
# chat_routes.call_gemini (requests)                                          #
# --------------------------------------------------------------------------- #

@pytest.fixture
def cr(monkeypatch):
    import Orchestrator.routes.chat_routes as cr
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", FAKE_KEY, raising=False)
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    return cr


class _JsonResp:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)
        self.reason = "OK" if status_code == 200 else "Bad Request"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


def test_call_gemini_sends_key_in_header(cr, monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen["url"], seen["headers"] = url, kw.get("headers")
        return _JsonResp({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]})

    monkeypatch.setattr(cr.requests, "post", fake_post)
    text, *_ = cr.call_gemini([{"role": "user", "content": "x"}], "gemini-test", operator="test-op")
    assert text == "hi"
    _assert_header_auth(seen["url"], seen["headers"])
    assert seen["url"].endswith("/gemini-test:generateContent")


def test_call_gemini_error_returns_status_and_parsed_message_only(cr, monkeypatch, capsys):
    body = _google_error_response(400, "https://example.invalid").json()
    monkeypatch.setattr(cr.requests, "post",
                        lambda url, **kw: _JsonResp(body, status_code=400,
                                                    text=json.dumps(body)))
    with pytest.raises(HTTPException) as exc:
        cr.call_gemini([{"role": "user", "content": "x"}], "gemini-test", operator="test-op")
    assert exc.value.status_code == 400
    assert "API key not valid" in exc.value.detail
    assert FAKE_KEY not in exc.value.detail
    assert "details" not in exc.value.detail, "raw upstream body must not be echoed"
    assert FAKE_KEY not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# chat_routes.stream_gemini_with_thinking (httpx streaming)                   #
# --------------------------------------------------------------------------- #

class _FakeStreamResp:
    def __init__(self, status_code, lines=(), body=b""):
        self.status_code = status_code
        self._lines = list(lines)
        self._body = body

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _fake_async_client(resp=None, raise_exc=None, seen=None):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, **kw):
            if seen is not None:
                seen["url"], seen["headers"] = url, kw.get("headers")
            if raise_exc is not None:
                raise raise_exc
            return _FakeStreamCtx(resp)

    return _Client


async def _drain(agen):
    return [ev async for ev in agen]


def test_stream_gemini_sends_key_in_header(cr, monkeypatch):
    import asyncio
    seen = {}
    line = "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": "hello"}]}}]})
    monkeypatch.setattr(cr.httpx, "AsyncClient",
                        _fake_async_client(_FakeStreamResp(200, [line]), seen=seen))
    events = asyncio.run(_drain(cr.stream_gemini_with_thinking(
        [{"role": "user", "content": "x"}], "gemini-test", "test-op")))
    _assert_header_auth(seen["url"], seen["headers"])
    assert seen["url"].endswith(":streamGenerateContent?alt=sse")
    assert any(e["type"] == "content" and e["data"] == "hello" for e in events)


def test_stream_gemini_error_body_is_not_echoed(cr, monkeypatch, capsys):
    import asyncio
    body = json.dumps(_google_error_response(403, "https://example.invalid").json()).encode()
    monkeypatch.setattr(cr.httpx, "AsyncClient",
                        _fake_async_client(_FakeStreamResp(403, body=body)))
    events = asyncio.run(_drain(cr.stream_gemini_with_thinking(
        [{"role": "user", "content": "x"}], "gemini-test", "test-op")))
    errors = [e["data"] for e in events if e["type"] == "error"]
    assert errors and "403" in errors[0] and "API key not valid" in errors[0]
    assert all(FAKE_KEY not in e for e in errors)
    assert FAKE_KEY not in capsys.readouterr().out


def test_stream_gemini_transport_error_is_masked(cr, monkeypatch):
    import asyncio
    boom = RuntimeError(f"connect failed for https://x/m:streamGenerateContent{KEY_QS}{FAKE_KEY}")
    monkeypatch.setattr(cr.httpx, "AsyncClient", _fake_async_client(raise_exc=boom))
    events = asyncio.run(_drain(cr.stream_gemini_with_thinking(
        [{"role": "user", "content": "x"}], "gemini-test", "test-op")))
    errors = [e["data"] for e in events if e["type"] == "error"]
    assert errors and all(FAKE_KEY not in e for e in errors)


# --------------------------------------------------------------------------- #
# tts_routes: Cloud TTS voices / synthesize, Gemini TTS preview, Imagen       #
# --------------------------------------------------------------------------- #

@pytest.fixture
def tr(monkeypatch):
    import Orchestrator.routes.tts_routes as tr
    monkeypatch.setattr(tr, "GOOGLE_API_KEY", FAKE_KEY, raising=False)
    return tr


def test_google_tts_voices_sends_key_in_header(tr, monkeypatch):
    seen = {}

    def fake_get(url, **kw):
        seen["url"], seen["headers"] = url, kw.get("headers")
        return _JsonResp({"voices": []})

    monkeypatch.setattr(tr.requests, "get", fake_get)
    assert tr.call_google_tts_voices() == {"voices": []}
    _assert_header_auth(seen["url"], seen["headers"])


def test_google_tts_voices_http_error_keeps_status_and_hides_key(tr, monkeypatch, capsys):
    def fake_get(url, **kw):
        raise _http_error(403, url)

    monkeypatch.setattr(tr.requests, "get", fake_get)
    with pytest.raises(HTTPException) as exc:
        tr.call_google_tts_voices()
    assert exc.value.status_code == 403, "a falsy 4xx Response must not collapse to 502"
    assert "API key not valid" in exc.value.detail
    assert FAKE_KEY not in exc.value.detail
    assert FAKE_KEY not in capsys.readouterr().out


def test_google_tts_voices_transport_error_hides_url(tr, monkeypatch, capsys):
    def fake_get(url, **kw):
        raise requests.ConnectionError(
            f"HTTPSConnectionPool(host='texttospeech.googleapis.com', port=443): "
            f"Max retries exceeded with url: /v1/voices{KEY_QS}{FAKE_KEY}")

    monkeypatch.setattr(tr.requests, "get", fake_get)
    with pytest.raises(HTTPException) as exc:
        tr.call_google_tts_voices()
    assert exc.value.status_code == 502
    assert FAKE_KEY not in exc.value.detail
    assert FAKE_KEY not in capsys.readouterr().out


def _ssml_in():
    from Orchestrator.startup import GoogleSSMLIn
    return GoogleSSMLIn(text="hello", voice_name="en-US-Wavenet-F")


def test_google_tts_synthesize_sends_key_in_header(tr, monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen["url"], seen["headers"] = url, kw.get("headers")
        return _JsonResp({"audioContent": base64.b64encode(b"mp3").decode()})

    monkeypatch.setattr(tr.requests, "post", fake_post)
    assert tr.call_google_tts_synthesize(_ssml_in()) == b"mp3"
    _assert_header_auth(seen["url"], seen["headers"])


def test_google_tts_synthesize_error_keeps_status_and_hides_key(tr, monkeypatch, capsys):
    def fake_post(url, **kw):
        raise _http_error(400, url)

    monkeypatch.setattr(tr.requests, "post", fake_post)
    with pytest.raises(HTTPException) as exc:
        tr.call_google_tts_synthesize(_ssml_in())
    assert exc.value.status_code == 400
    assert "API key not valid" in exc.value.detail
    assert FAKE_KEY not in exc.value.detail
    assert FAKE_KEY not in capsys.readouterr().out


def _gemini_tts_in():
    from Orchestrator.startup import GeminiProTTSIn
    return GeminiProTTSIn(text="hello", voice_name="Kore", model="gemini-2.5-flash-tts")


def test_gemini_tts_preview_sends_key_in_header(tr, monkeypatch):
    seen = {}
    pcm = base64.b64encode(b"\x00\x00" * 8).decode()

    def fake_post(url, **kw):
        seen["url"], seen["headers"] = url, kw.get("headers")
        return _JsonResp({"candidates": [{"content": {"parts": [
            {"inlineData": {"data": pcm, "mimeType": "audio/L16;rate=24000"}}]}}]})

    monkeypatch.setattr(tr.requests, "post", fake_post)
    wav = tr.call_gemini_tts_preview(_gemini_tts_in())
    assert wav[:4] == b"RIFF"
    _assert_header_auth(seen["url"], seen["headers"])


def test_gemini_tts_preview_error_keeps_status_and_hides_key(tr, monkeypatch, capsys):
    def fake_post(url, **kw):
        raise _http_error(429, url)

    monkeypatch.setattr(tr.requests, "post", fake_post)
    with pytest.raises(HTTPException) as exc:
        tr.call_gemini_tts_preview(_gemini_tts_in())
    assert exc.value.status_code == 429
    assert FAKE_KEY not in exc.value.detail
    assert FAKE_KEY not in capsys.readouterr().out


def test_imagen_http_error_detail_is_parsed_not_raw(tr, monkeypatch, capsys):
    def fake_post(url, **kw):
        assert "key=" not in url
        resp = _google_error_response(400, url)
        return resp   # status 400 -> raise_for_status raises HTTPError(response=resp)

    monkeypatch.setattr(tr.requests, "post", fake_post)
    with pytest.raises(HTTPException) as exc:
        tr.call_imagen("a cat", "gemini-test-image")
    assert "API key not valid" in exc.value.detail
    assert FAKE_KEY not in exc.value.detail
    assert FAKE_KEY not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# gemini_live_routes.connect_to_gemini (websockets)                           #
# --------------------------------------------------------------------------- #

@pytest.fixture
def glr(monkeypatch):
    import Orchestrator.routes.gemini_live_routes as glr
    monkeypatch.setattr(glr, "GOOGLE_API_KEY", FAKE_KEY)
    monkeypatch.setattr(glr, "WEBSOCKETS_AVAILABLE", True)
    return glr


def test_gemini_live_sends_key_in_ws_handshake_header(glr, monkeypatch):
    import asyncio
    from Orchestrator.models import GeminiLiveSession
    seen = {}

    async def fake_connect(url, **kw):
        seen["url"], seen["headers"] = url, kw.get("additional_headers")
        return MagicMock()

    monkeypatch.setattr(glr, "websockets", SimpleNamespace(connect=fake_connect))
    assert asyncio.run(glr.connect_to_gemini(GeminiLiveSession(session_id="t-hdr"))) is True
    _assert_header_auth(seen["url"], seen["headers"])


def test_gemini_live_connect_failure_log_hides_key(glr, monkeypatch, capsys):
    import asyncio
    from Orchestrator.models import GeminiLiveSession

    async def fake_connect(url, **kw):
        raise OSError(f"cannot reach {url}{KEY_QS}{FAKE_KEY}")

    monkeypatch.setattr(glr, "websockets", SimpleNamespace(connect=fake_connect))
    assert asyncio.run(glr.connect_to_gemini(GeminiLiveSession(session_id="t-fail"))) is False
    out = capsys.readouterr().out
    assert "OSError" in out
    assert FAKE_KEY not in out


# --------------------------------------------------------------------------- #
# diagnostics voice probe harness (mirrors the box's Live connect idiom)      #
# --------------------------------------------------------------------------- #

def test_voice_probe_gemini_url_and_headers_keep_key_out_of_url():
    from diagnostics.voice_probes.harness import build_gemini_headers, build_gemini_url
    url = build_gemini_url("v1alpha")
    assert "key=" not in url
    assert ".v1alpha." in url
    assert build_gemini_headers(FAKE_KEY) == {HDR: FAKE_KEY}
