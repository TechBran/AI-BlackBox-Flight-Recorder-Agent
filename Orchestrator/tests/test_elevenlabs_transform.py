"""Hermetic tests for ElevenLabs audio transforms (Voice Changer + Isolator).

No network, no live key: ``requests.post`` is monkeypatched into a recorder so we
can assert the exact URL + multipart part, and ``auth_headers`` is stubbed so no
key lookup hits ``.env``. Source audio is a tiny temp file on disk.
"""
import pytest

from Orchestrator.elevenlabs import client as el_client
from Orchestrator.elevenlabs import transform


class _FakeResp:
    """Minimal stand-in for requests.Response: status + bytes + JSON body."""
    def __init__(self, status_code, content=b"", json_body=None):
        self.status_code = status_code
        self.content = content
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture(autouse=True)
def _stub_auth(monkeypatch):
    monkeypatch.setattr(el_client, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})
    yield


@pytest.fixture
def sample_audio(tmp_path):
    """A tiny on-disk .mp3 so change_voice/isolate can read real bytes."""
    p = tmp_path / "clip.mp3"
    p.write_bytes(b"ID3\x03\x00\x00\x00fake-mp3")
    return str(p)


def _record_post(monkeypatch, responses):
    """Patch transform.requests.post to pop canned responses; capture files=/params=/url."""
    calls = []

    def fake_post(url, headers=None, params=None, files=None, timeout=None):
        calls.append({"url": url, "headers": headers, "params": params, "files": files, "timeout": timeout})
        return responses[len(calls) - 1]

    monkeypatch.setattr(transform.requests, "post", fake_post)
    return calls


# --- change_voice -------------------------------------------------------------

def test_change_voice_strips_prefix_and_posts_multipart(monkeypatch, sample_audio):
    """'elevenlabs:abc123' -> URL path has the RAW id; audio rides the 'audio' part."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"REVOICED")])

    out = transform.change_voice(sample_audio, "elevenlabs:abc123")

    assert out == b"REVOICED"
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/v1/speech-to-speech/abc123")
    assert "elevenlabs:" not in calls[0]["url"]
    # Multipart 'audio' part: (filename, bytes, mime).
    assert "audio" in calls[0]["files"]
    fname, fbytes, mime = calls[0]["files"]["audio"]
    assert fname == "clip.mp3"
    assert fbytes == b"ID3\x03\x00\x00\x00fake-mp3"
    assert mime == "audio/mpeg"


def test_change_voice_accepts_raw_id(monkeypatch, sample_audio):
    """A raw id (no prefix) reaches the path unchanged."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"X")])

    transform.change_voice(sample_audio, "rawVoiceId")
    assert calls[0]["url"].endswith("/v1/speech-to-speech/rawVoiceId")


def test_change_voice_output_format_in_params(monkeypatch, sample_audio):
    """output_format rides the query params only when provided."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"X"), _FakeResp(200, content=b"Y")])

    transform.change_voice(sample_audio, "v1", output_format="mp3_44100_128")
    assert calls[0]["params"]["output_format"] == "mp3_44100_128"

    transform.change_voice(sample_audio, "v1")
    assert "output_format" not in calls[1]["params"]


def test_change_voice_4xx_raises_mapped(monkeypatch, sample_audio):
    _record_post(monkeypatch, [_FakeResp(401, json_body={"detail": {"status": "auth_error"}})])
    with pytest.raises(RuntimeError) as exc:
        transform.change_voice(sample_audio, "v1")
    assert "auth" in str(exc.value).lower()


# --- isolate ------------------------------------------------------------------

def test_isolate_posts_to_audio_isolation(monkeypatch, sample_audio):
    """Hits /v1/audio-isolation with the 'audio' multipart part; returns bytes."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"CLEAN")])

    out = transform.isolate(sample_audio)

    assert out == b"CLEAN"
    assert calls[0]["url"].endswith("/v1/audio-isolation")
    assert "audio" in calls[0]["files"]
    fname, fbytes, mime = calls[0]["files"]["audio"]
    assert fname == "clip.mp3"
    assert mime == "audio/mpeg"


def test_isolate_4xx_raises_mapped(monkeypatch, sample_audio):
    _record_post(monkeypatch, [_FakeResp(429, json_body={"detail": {"status": "rate_limited"}})])
    with pytest.raises(RuntimeError) as exc:
        transform.isolate(sample_audio)
    assert "rate limit" in str(exc.value).lower()


def test_isolate_non_json_error_tolerated(monkeypatch, sample_audio):
    _record_post(monkeypatch, [_FakeResp(500, content=b"<html>oops</html>")])
    with pytest.raises(RuntimeError) as exc:
        transform.isolate(sample_audio)
    assert "500" in str(exc.value)


# --- routes: /elevenlabs/voice-changer + /elevenlabs/isolate ------------------

from unittest.mock import patch  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def app_client():
    """TestClient with the embeddings startup hook stubbed (no network on boot)."""
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_emb:
        m_emb.return_value = {"x": {"vector": [0.1]}}
        from Orchestrator.app import app
        with TestClient(app) as c:
            yield c


def test_voice_changer_route_success(app_client, sample_audio):
    """200 -> {status, audio_url}; change_voice gets path + target_voice."""
    fake = b"REVOICED-mp3"
    with patch("Orchestrator.elevenlabs.transform.change_voice", return_value=fake) as m_cv:
        resp = app_client.post(
            "/elevenlabs/voice-changer",
            json={"audio_path": sample_audio, "target_voice": "elevenlabs:abc123"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["audio_url"].startswith("/ui/uploads/")
    assert body["size_bytes"] == len(fake)
    m_cv.assert_called_once()
    args, kwargs = m_cv.call_args
    assert args[0] == sample_audio
    assert args[1] == "elevenlabs:abc123"


def test_voice_changer_missing_fields_400(app_client, sample_audio):
    """Missing target_voice -> 400; provider fn never called."""
    with patch("Orchestrator.elevenlabs.transform.change_voice") as m_cv:
        resp = app_client.post("/elevenlabs/voice-changer", json={"audio_path": sample_audio})
    assert resp.status_code == 400
    m_cv.assert_not_called()


def test_voice_changer_missing_file_400(app_client):
    """A path that doesn't exist -> 400 before any provider call."""
    with patch("Orchestrator.elevenlabs.transform.change_voice") as m_cv:
        resp = app_client.post(
            "/elevenlabs/voice-changer",
            json={"audio_path": "/no/such/file.mp3", "target_voice": "elevenlabs:x"},
        )
    assert resp.status_code == 400
    m_cv.assert_not_called()


def test_isolate_route_success(app_client, sample_audio):
    """200 -> {status, audio_url}; isolate gets the path."""
    fake = b"CLEAN-mp3"
    with patch("Orchestrator.elevenlabs.transform.isolate", return_value=fake) as m_iso:
        resp = app_client.post("/elevenlabs/isolate", json={"audio_path": sample_audio})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["audio_url"].startswith("/ui/uploads/")
    assert body["size_bytes"] == len(fake)
    m_iso.assert_called_once_with(sample_audio)


def test_isolate_route_missing_path_400(app_client):
    """No audio_path -> 400; provider fn never called."""
    with patch("Orchestrator.elevenlabs.transform.isolate") as m_iso:
        resp = app_client.post("/elevenlabs/isolate", json={})
    assert resp.status_code == 400
    m_iso.assert_not_called()
