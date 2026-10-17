"""POST /tts: loud upstream-failure logging + unprefixed local-voice routing (D-5).

Measured defect this pins:
  voice "am_onyx"        -> HTTP 200 {"status":"fallback","api_status":400} — the bare
                            Kokoro id fell through to OpenAI, whose voice enum rejected
                            it ("Input should be 'nova','shimmer',…"), and NOTHING was
                            logged server-side. Eight days of silent provider swap.
  voice "local:am_onyx"  -> {"status":"success"}, Kokoro, 6860 bytes.

The HTTP-200 {"status":"fallback"} envelope is a CONTRACT (Portal + Android depend on
its shape) and is asserted UNCHANGED here. What is new: (a) a loud "[TTS] WARNING …"
line naming voice/provider/status/body on every non-2xx upstream, and (b) an unprefixed
id that the local voice catalog knows resolving to the local provider, not the cloud.
"""
from unittest.mock import patch

import pytest

from Orchestrator.config import OPENAI_TTS_URL


@pytest.fixture
def client(app_client):
    """The shared app_client (Orchestrator/tests/conftest.py): lifespan side
    effects off, ToolVault embedding sync mocked, stores in tmp_path."""
    return app_client


class _Resp:
    def __init__(self, status=200, text="", content=b"audio-bytes"):
        self.status_code = status
        self.text = text
        self.content = content


@pytest.fixture
def cloud_ok(monkeypatch):
    """OpenAI branch reachable regardless of this box's AUDIO_ENGINE / key."""
    from Orchestrator.routes import tts_routes
    monkeypatch.setattr(tts_routes, "AUDIO_ENGINE", "openai")
    monkeypatch.setattr(tts_routes, "OPENAI_API_KEY", "sk-test")


@pytest.fixture
def no_local(monkeypatch):
    from Orchestrator.onboarding import custom_servers
    monkeypatch.setattr(custom_servers, "resolve_audio", lambda kind: None)


def _local_server(monkeypatch, voices):
    from Orchestrator.onboarding import custom_servers
    srv = {"alias": "gemma-box", "base_url": "http://kokoro/v1", "api_key": ""}
    monkeypatch.setattr(
        custom_servers, "resolve_audio",
        lambda kind: (srv, "kokoro-tts") if kind == "tts" else None)
    monkeypatch.setattr(custom_servers, "list_local_tts_voices", lambda s: list(voices))
    return srv


# ── (a) non-2xx upstream is LOUD, envelope unchanged ─────────────────────
def test_openai_upstream_non2xx_logs_loudly(client, cloud_ok, no_local, capsys):
    """The exact measured failure: bare 'am_onyx' -> OpenAI 400 -> fallback envelope.
    The envelope stays; the silence does not."""
    upstream = _Resp(400, "Input should be 'nova','shimmer','echo','onyx','fable'")
    with patch("Orchestrator.routes.tts_routes.requests.post", return_value=upstream):
        resp = client.post("/tts", json={"text": "Hi.", "voice": "am_onyx",
                                         "return_json": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fallback"
    assert body["api_status"] == 400

    out = capsys.readouterr().out
    assert "[TTS]" in out
    assert "WARNING" in out
    assert "am_onyx" in out          # the voice the caller asked for
    assert "openai" in out           # the provider actually used
    assert "400" in out              # the upstream status
    assert "Input should be" in out  # the upstream body


def test_local_upstream_non2xx_logs_loudly(client, cloud_ok, monkeypatch, capsys):
    """A local (Kokoro) upstream failure is equally loud, and names provider=local."""
    _local_server(monkeypatch, ["am_onyx", "af_heart"])
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp(503, "model is still loading")):
        resp = client.post("/tts", json={"text": "Hi.", "voice": "local:am_onyx",
                                         "return_json": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fallback"
    assert body["api_status"] == 503

    out = capsys.readouterr().out
    assert "[TTS]" in out
    assert "WARNING" in out
    assert "local" in out
    assert "503" in out
    assert "model is still loading" in out


# ── (b) unprefixed known-local voice resolves LOCAL, not cloud ───────────
def test_unprefixed_known_local_voice_routes_local(client, cloud_ok, monkeypatch):
    _local_server(monkeypatch, ["af_heart", "am_onyx"])
    calls = []

    def _post(url, **kw):
        calls.append((url, kw))
        return _Resp(200, "", b"kokoro-audio-bytes")

    with patch("Orchestrator.routes.tts_routes.requests.post", side_effect=_post):
        resp = client.post("/tts", json={"text": "Hi.", "voice": "am_onyx",
                                         "return_json": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["voice"] == "am_onyx"
    assert body["model"] == "kokoro-tts"
    assert len(calls) == 1
    assert calls[0][0] == "http://kokoro/v1/audio/speech"   # never OpenAI
    assert calls[0][1]["json"]["voice"] == "am_onyx"


def test_unprefixed_openai_catalog_voice_still_cloud(client, cloud_ok, monkeypatch):
    """'onyx' is in build_tts_catalog()'s OpenAI group — a local server advertising the
    same bare id must NOT steal a cloud voice."""
    _local_server(monkeypatch, ["onyx", "am_onyx"])
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp(200, "", b"openai-mp3")) as m:
        resp = client.post("/tts", json={"text": "Hi.", "voice": "onyx"})

    assert resp.status_code == 200
    assert m.call_args[0][0] == OPENAI_TTS_URL


def test_unprefixed_unknown_voice_still_cloud(client, cloud_ok, monkeypatch):
    """A voice the local catalog does not know is left on its existing cloud path."""
    _local_server(monkeypatch, ["af_heart"])
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp(200, "", b"openai-mp3")) as m:
        resp = client.post("/tts", json={"text": "Hi.", "voice": "Rachel"})

    assert resp.status_code == 200
    assert m.call_args[0][0] == OPENAI_TTS_URL


def test_explicit_provider_is_never_hijacked(client, cloud_ok, monkeypatch):
    """An EXPLICIT provider is honoured verbatim — the whole point of this fix family
    is that nothing silently substitutes a provider, in either direction."""
    _local_server(monkeypatch, ["am_onyx"])
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp(200, "", b"openai-mp3")) as m:
        resp = client.post("/tts", json={"text": "Hi.", "voice": "am_onyx",
                                         "provider": "openai"})

    assert resp.status_code == 200
    assert m.call_args[0][0] == OPENAI_TTS_URL


def test_no_local_server_leaves_cloud_path_untouched(client, cloud_ok, no_local):
    """With no local TTS server registered the route behaves exactly as before."""
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp(200, "", b"openai-mp3")) as m:
        resp = client.post("/tts", json={"text": "Hi.", "voice": "am_onyx"})

    assert resp.status_code == 200
    assert resp.content == b"openai-mp3"
    assert m.call_args[0][0] == OPENAI_TTS_URL


# ── (c) POST /tts/batch has the same routing rule (audit O07-tts-voice-11) ──
# The batch route defaulted a missing provider to "openai", so a bare Kokoro id
# like "am_onyx" still went to the cloud there even after POST /tts was fixed.
def _batch(client, **body):
    payload = {"text": "Hi there.", "format": "mp3"}
    payload.update(body)
    return client.post("/tts/batch", json=payload)


def test_batch_unprefixed_known_local_voice_routes_local(client, cloud_ok, monkeypatch):
    _local_server(monkeypatch, ["af_heart", "am_onyx"])
    calls = []

    def _post(url, **kw):
        calls.append((url, kw))
        return _Resp(200, "", b"kokoro-audio-bytes")

    with patch("Orchestrator.routes.tts_routes.requests.post", side_effect=_post):
        resp = _batch(client, voice="am_onyx")

    assert resp.status_code == 200
    assert resp.content == b"kokoro-audio-bytes"
    assert calls, "no upstream call was made"
    assert all(url == "http://kokoro/v1/audio/speech" for url, _kw in calls)  # never OpenAI
    assert calls[0][1]["json"]["voice"] == "am_onyx"
    assert calls[0][1]["json"]["model"] == "kokoro-tts"


def test_batch_unprefixed_openai_catalog_voice_still_cloud(client, cloud_ok, monkeypatch):
    _local_server(monkeypatch, ["onyx", "am_onyx"])
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp(200, "", b"openai-mp3")) as m:
        resp = _batch(client, voice="onyx")

    assert resp.status_code == 200
    assert m.call_args[0][0] == OPENAI_TTS_URL


def test_batch_unprefixed_unknown_voice_still_cloud(client, cloud_ok, monkeypatch):
    _local_server(monkeypatch, ["af_heart"])
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp(200, "", b"openai-mp3")) as m:
        resp = _batch(client, voice="Rachel")

    assert resp.status_code == 200
    assert m.call_args[0][0] == OPENAI_TTS_URL


def test_batch_explicit_provider_is_never_hijacked(client, cloud_ok, monkeypatch):
    _local_server(monkeypatch, ["am_onyx"])
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp(200, "", b"openai-mp3")) as m:
        resp = _batch(client, voice="am_onyx", provider="openai")

    assert resp.status_code == 200
    assert m.call_args[0][0] == OPENAI_TTS_URL


def test_batch_no_local_server_leaves_cloud_path_untouched(client, cloud_ok, no_local):
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp(200, "", b"openai-mp3")) as m:
        resp = _batch(client, voice="am_onyx")

    assert resp.status_code == 200
    assert resp.content == b"openai-mp3"
    assert m.call_args[0][0] == OPENAI_TTS_URL
