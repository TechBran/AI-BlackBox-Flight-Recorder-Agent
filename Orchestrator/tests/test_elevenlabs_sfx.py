"""Hermetic tests for ElevenLabs Sound Effects (POST /v1/sound-generation).

No network, no live key: ``requests.post`` is monkeypatched into a recorder so we
can assert the exact URL/params/body, and ``auth_headers`` is stubbed so no key
lookup hits ``.env``.
"""
import pytest

from Orchestrator.elevenlabs import client as el_client
from Orchestrator.elevenlabs import sfx


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
    """Never touch a real key; auth_headers returns a fixed fake header."""
    monkeypatch.setattr(el_client, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})
    yield


def _record_post(monkeypatch, responses):
    """Patch sfx.requests.post to pop canned responses; return the call recorder."""
    calls = []

    def fake_post(url, headers=None, params=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "params": params, "json": json, "timeout": timeout})
        return responses[len(calls) - 1]

    monkeypatch.setattr(sfx.requests, "post", fake_post)
    return calls


def test_posts_to_sound_generation_with_text(monkeypatch):
    """Hits /v1/sound-generation (NOT /v1/text-to-sound-effects) with text in the body."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"SFX")])

    out = sfx.generate("rain on a tin roof")

    assert out == b"SFX"
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/v1/sound-generation")
    assert calls[0]["json"]["text"] == "rain on a tin roof"


def test_duration_and_loop_sent_when_provided(monkeypatch):
    """duration_seconds + loop reach the body only when provided."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"A"), _FakeResp(200, content=b"B")])

    sfx.generate("door whoosh", duration_seconds=3, loop=True)
    body = calls[0]["json"]
    assert body["duration_seconds"] == 3
    assert body["loop"] is True

    # Defaults: no duration, loop omitted (not sent as False).
    sfx.generate("thunderclap")
    body2 = calls[1]["json"]
    assert "duration_seconds" not in body2
    assert "loop" not in body2


def test_prompt_influence_and_output_format(monkeypatch):
    """prompt_influence rides in the body; output_format rides in the query params."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"A")])

    sfx.generate("engine hum", prompt_influence=0.7, output_format="mp3_44100_128")

    assert calls[0]["json"]["prompt_influence"] == 0.7
    assert calls[0]["params"]["output_format"] == "mp3_44100_128"


def test_4xx_raises_mapped_runtimeerror(monkeypatch):
    """A 401 surfaces the auth hint via map_error as a RuntimeError."""
    _record_post(monkeypatch, [_FakeResp(401, json_body={"detail": {"status": "auth_error"}})])

    with pytest.raises(RuntimeError) as exc:
        sfx.generate("rain")
    assert "auth" in str(exc.value).lower()


def test_non_json_error_body_tolerated(monkeypatch):
    """A non-JSON error body still maps (map_error tolerates None)."""
    _record_post(monkeypatch, [_FakeResp(500, content=b"<html>oops</html>")])

    with pytest.raises(RuntimeError) as exc:
        sfx.generate("rain")
    assert "500" in str(exc.value)


# --- route: POST /generate/elevenlabs_sound_effect ----------------------------

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


def test_sfx_route_returns_success_audio_url(app_client):
    """200 -> {status:'success', audio_url, size_bytes}; sfx.generate is the source."""
    fake = b"ID3\x03\x00\x00\x00sfx-bytes"
    with patch("Orchestrator.elevenlabs.sfx.generate", return_value=fake) as m_gen:
        resp = app_client.post(
            "/generate/elevenlabs_sound_effect",
            json={"text": "rain on a tin roof", "duration_seconds": 3, "loop": True},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["audio_url"].startswith("/ui/uploads/")
    assert body["audio_url"].endswith("_sfx.mp3")
    assert body["size_bytes"] == len(fake)
    # text + options were forwarded to the provider fn.
    m_gen.assert_called_once()
    args, kwargs = m_gen.call_args
    assert args[0] == "rain on a tin roof"
    assert kwargs["duration_seconds"] == 3
    assert kwargs["loop"] is True


def test_sfx_route_missing_text_is_400(app_client):
    """No text -> 400 (the provider fn is never called)."""
    with patch("Orchestrator.elevenlabs.sfx.generate") as m_gen:
        resp = app_client.post("/generate/elevenlabs_sound_effect", json={})
    assert resp.status_code == 400
    m_gen.assert_not_called()
