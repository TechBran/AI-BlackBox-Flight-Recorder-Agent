"""POST /tts and /tts/batch: a request that chooses nothing speaks with the box
default, every catalog voice id is accepted, and Gemini Flash is never billed as Pro.

Measured defects (audit rank 39, O18-toolvault-modules-14, O07-tts-voice-06):
  * /tts with no voice assumed OpenAI, so text_to_speech on a box with no OpenAI
    key always came back {"status":"fallback"} even with Gemini or ElevenLabs set up;
  * /tts sent "gemini-pro:Charon" and "openai:onyx" -- the ids list_tts_voices
    hands a model -- straight to OpenAI, which rejects them;
  * /tts/batch with provider "gemini-flash" and no model synthesised with Pro.

TestClient without `with`: no lifespan, no startup work. Provider calls patched.
"""
import io
import wave
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from Orchestrator import config
from Orchestrator.config import GEMINI_TTS_MODELS, OPENAI_TTS_URL


@pytest.fixture
def client():
    from Orchestrator.app import app
    return TestClient(app)


@pytest.fixture
def routes(monkeypatch):
    from Orchestrator.routes import tts_routes
    from Orchestrator.onboarding import custom_servers
    from Orchestrator.elevenlabs import catalog as el_catalog
    from Orchestrator import qwen_tts
    monkeypatch.setattr(tts_routes, "AUDIO_ENGINE", "openai")
    monkeypatch.setattr(tts_routes, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(custom_servers, "resolve_audio", lambda kind: None)
    monkeypatch.setattr(custom_servers, "has_audio", lambda kind: False)
    monkeypatch.setattr(el_catalog, "get_voices", lambda *a, **k: None)
    monkeypatch.setattr(qwen_tts, "catalog_group", lambda: None)
    monkeypatch.setattr(config, "TTS_VOICE", "onyx")
    monkeypatch.setattr(tts_routes, "TTS_VOICE", "onyx")

    def _no_network(url, *a, **k):
        raise AssertionError(f"unexpected provider call to {url}")

    # Hermetic floor: any provider POST a test did not patch fails loudly
    # instead of reaching the network. Tests that expect one patch over it.
    monkeypatch.setattr(tts_routes.requests, "post", _no_network)
    return tts_routes


def _creds(monkeypatch, openai=False, google=False):
    monkeypatch.setattr(config, "_tts_credentials",
                        lambda: {"openai": openai, "google": google})


def _wav(frames=b"\x00\x00" * 240):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(frames)
    return buf.getvalue()


class _Resp:
    def __init__(self, status=200, text="", content=b"mp3-bytes"):
        self.status_code = status
        self.text = text
        self.content = content


def _capture_gemini(monkeypatch, routes):
    seen = []

    def _fake(inp):
        seen.append({"voice": inp.voice_name, "model": inp.model, "text": inp.text})
        return _wav()

    monkeypatch.setattr(routes, "call_gemini_tts", _fake)
    return seen


# ── POST /tts ────────────────────────────────────────────────────────────────
def test_bare_tts_on_an_openai_box_uses_the_configured_voice(client, routes, monkeypatch):
    _creds(monkeypatch, openai=True)
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp()) as m:
        resp = client.post("/tts", json={"text": "Hello there."})
    assert resp.status_code == 200
    assert m.call_args[0][0] == OPENAI_TTS_URL
    sent = m.call_args[1]["json"]
    assert sent["voice"] == "onyx"
    assert sent["model"] == routes.TTS_MODEL


def test_bare_tts_on_a_google_only_box_speaks_gemini(client, routes, monkeypatch):
    """No OpenAI key: the box default is a Gemini voice, and /tts can speak it."""
    _creds(monkeypatch, openai=False, google=True)
    seen = _capture_gemini(monkeypatch, routes)
    with patch("Orchestrator.routes.tts_routes.requests.post") as m:
        resp = client.post("/tts", json={"text": "Hello there."})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert m.call_count == 0, "nothing may go to OpenAI on a box without its key"
    assert seen and seen[0]["voice"] == config.GEMINI_TTS_DEFAULT_VOICE
    assert seen[0]["model"] == GEMINI_TTS_MODELS["gemini-pro"]


def test_bare_tts_on_an_elevenlabs_only_box_speaks_elevenlabs(client, routes, monkeypatch):
    from Orchestrator.elevenlabs import catalog as el_catalog
    _creds(monkeypatch, openai=False, google=False)
    monkeypatch.setattr(el_catalog, "get_voices", lambda *a, **k: {
        "my_voices": [{"id": "elevenlabs:own1", "name": "Mine", "description": "cloned"}],
        "premade": []})
    got = {}

    def _fake_stream(text, voice, model_id, voice_settings):
        got.update(voice=voice, model_id=model_id)
        yield b"el-mp3"

    monkeypatch.setattr(routes, "_elevenlabs_stream_chunked", _fake_stream)
    resp = client.post("/tts", json={"text": "Hello there."})
    assert resp.status_code == 200
    assert resp.content == b"el-mp3"
    assert got == {"voice": "elevenlabs:own1", "model_id": None}


def test_openai_catalog_id_is_sent_bare(client, routes, monkeypatch):
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp()) as m:
        resp = client.post("/tts", json={"text": "Hi.", "voice": "openai:nova"})
    assert resp.status_code == 200
    assert m.call_args[1]["json"]["voice"] == "nova"


def test_gemini_catalog_id_speaks_gemini_with_its_own_model(client, routes, monkeypatch):
    seen = _capture_gemini(monkeypatch, routes)
    with patch("Orchestrator.routes.tts_routes.requests.post") as m:
        resp = client.post("/tts", json={"text": "Hi.", "voice": "gemini-flash:Kore"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert m.call_count == 0
    assert seen == [{"voice": "Kore", "model": GEMINI_TTS_MODELS["gemini-flash"], "text": "Hi."}]


def test_gemini_failure_keeps_the_fallback_envelope(client, routes, monkeypatch, capsys):
    from fastapi import HTTPException

    def _boom(inp):
        raise HTTPException(status_code=400, detail="GOOGLE_API_KEY not configured.")

    monkeypatch.setattr(routes, "call_gemini_tts", _boom)
    resp = client.post("/tts", json={"text": "Hi.", "voice": "gemini-pro:Charon",
                                     "return_json": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fallback"
    assert body["api_status"] == 400
    assert "WARNING" in capsys.readouterr().out


def test_long_gemini_text_is_chunked_and_stitched(client, routes, monkeypatch):
    seen = _capture_gemini(monkeypatch, routes)
    text = " ".join(["This is one sentence of a long reply."] * 60)
    resp = client.post("/tts", json={"text": text, "voice": "gemini-pro:Charon"})
    assert resp.status_code == 200
    assert len(seen) > 1
    with wave.open(io.BytesIO(resp.content)) as w:
        assert w.getnframes() == 240 * len(seen)


# ── POST /tts/batch ──────────────────────────────────────────────────────────
def test_batch_gemini_flash_without_a_model_is_synthesised_with_flash(client, routes, monkeypatch):
    seen = _capture_gemini(monkeypatch, routes)
    resp = client.post("/tts/batch", json={"text": "Hi.", "provider": "gemini-flash",
                                           "voice": "Kore"})
    assert resp.status_code == 200
    assert seen[0]["model"] == GEMINI_TTS_MODELS["gemini-flash"]


def test_batch_with_nothing_chosen_uses_the_box_default(client, routes, monkeypatch):
    _creds(monkeypatch, openai=False, google=True)
    seen = _capture_gemini(monkeypatch, routes)
    with patch("Orchestrator.routes.tts_routes.requests.post") as m:
        resp = client.post("/tts/batch", json={"text": "Hi.", "provider": "", "voice": ""})
    assert resp.status_code == 200
    assert m.call_count == 0
    assert seen[0]["voice"] == config.GEMINI_TTS_DEFAULT_VOICE
    assert seen[0]["model"] == GEMINI_TTS_MODELS["gemini-pro"]


def test_batch_explicit_provider_is_never_replaced_by_the_default(client, routes, monkeypatch):
    def _no_default():
        raise AssertionError("an explicit provider must not consult the default")

    monkeypatch.setattr(routes, "_default_tts_voice", _no_default)
    with patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_Resp()) as m:
        resp = client.post("/tts/batch", json={"text": "Hi.", "provider": "openai"})
    assert resp.status_code == 200
    assert m.call_args[1]["json"]["voice"] == "onyx"


def test_batch_prefixed_voice_is_sent_bare(client, routes, monkeypatch):
    seen = _capture_gemini(monkeypatch, routes)
    resp = client.post("/tts/batch", json={"text": "Hi.", "provider": "gemini-pro",
                                           "voice": "gemini-pro:Charon"})
    assert resp.status_code == 200
    assert seen[0]["voice"] == "Charon"
