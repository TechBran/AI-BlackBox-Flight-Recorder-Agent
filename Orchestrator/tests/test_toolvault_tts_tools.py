"""text_to_speech / list_tts_voices ToolVault executors follow /tts/catalog.

Measured defects (audit O18-toolvault-modules-14, rank 39):
  * text_to_speech always sent voice "onyx" and model "tts-1-hd", so
    voice="elevenlabs:<id>" reached ElevenLabs with an OpenAI model id, and a box
    without an OpenAI key could not speak through the tool at all;
  * list_tts_voices listed 20 English Google Cloud voice names from
    /tts/google/voices -- none of which text_to_speech accepts -- and never the
    OpenAI, Gemini, ElevenLabs (premade + cloned), local or on-box voices.

aiohttp.ClientSession is faked; no backend is needed.
"""
import asyncio

import pytest

from Orchestrator.toolvault.context import ToolContext

import ToolVault.tools.text_to_speech.executor as tts_exec
import ToolVault.tools.list_tts_voices.executor as voices_exec


class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, *a, **k):
        return self._body


class _FakeSession:
    calls: list = []
    body: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, timeout=None, **k):
        _FakeSession.calls.append(("POST", url, json))
        return _FakeResp(_FakeSession.body)

    def get(self, url, timeout=None, **k):
        _FakeSession.calls.append(("GET", url, None))
        return _FakeResp(_FakeSession.body)


@pytest.fixture
def session(monkeypatch):
    _FakeSession.calls = []
    _FakeSession.body = {}
    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    return _FakeSession


CTX = ToolContext(operator="tester", base_url="http://box:9091")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── text_to_speech ───────────────────────────────────────────────────────────
def test_text_only_sends_no_voice_or_model(session):
    """The box default (GET /tts/catalog "default") applies server-side."""
    session.body = {"status": "success", "audio_url": "/ui/uploads/a.wav",
                    "voice": "Charon", "model": "gemini-2.5-pro-tts"}
    res = _run(tts_exec.execute({"text": "Hello"}, CTX))
    method, url, payload = session.calls[-1]
    assert (method, url) == ("POST", "http://box:9091/tts")
    assert payload == {"text": "Hello", "return_json": True}
    assert res.success is True
    # The reply names what the server actually used, not a client guess.
    assert res.data == {"audio_url": "/ui/uploads/a.wav", "voice": "Charon",
                        "model": "gemini-2.5-pro-tts"}


def test_elevenlabs_voice_is_sent_without_an_openai_model(session):
    session.body = {"status": "success", "audio_url": "/ui/uploads/b.mp3",
                    "voice": "elevenlabs:abc123", "model": "eleven_v3"}
    res = _run(tts_exec.execute({"text": "Hi", "voice": "elevenlabs:abc123"}, CTX))
    payload = session.calls[-1][2]
    assert payload == {"text": "Hi", "voice": "elevenlabs:abc123", "return_json": True}
    assert res.success is True and res.data["model"] == "eleven_v3"


def test_supplied_model_is_forwarded(session):
    session.body = {"status": "success", "audio_url": "/ui/uploads/c.mp3",
                    "voice": "nova", "model": "tts-1"}
    _run(tts_exec.execute({"text": "Hi", "voice": "openai:nova", "model": "tts-1"}, CTX))
    assert session.calls[-1][2] == {"text": "Hi", "voice": "openai:nova",
                                    "model": "tts-1", "return_json": True}


def test_fallback_envelope_is_a_failure(session):
    session.body = {"status": "fallback", "message": "OPENAI_API_KEY not configured"}
    res = _run(tts_exec.execute({"text": "Hi"}, CTX))
    assert res.success is False
    assert "OPENAI_API_KEY" in res.result


def test_missing_text_is_refused_without_a_call(session):
    res = _run(tts_exec.execute({"voice": "openai:nova"}, CTX))
    assert res.success is False
    assert session.calls == []


# ── list_tts_voices ──────────────────────────────────────────────────────────
CATALOG = {
    "groups": [
        {"id": "openai", "label": "OpenAI TTS HD", "available": False,
         "unavailable_reason": "Needs OPENAI_API_KEY", "model": "tts-1-hd",
         "voices": [{"id": "openai:alloy", "name": "Alloy", "description": "Neutral"},
                    {"id": "openai:onyx", "name": "Onyx", "description": "Deep"}]},
        {"id": "gemini-pro", "label": "Gemini Pro TTS", "available": True,
         "model": "gemini-2.5-pro-tts",
         "voices": [{"id": "gemini-pro:Charon", "name": "Charon", "description": "Calm"}]},
        {"id": "elevenlabs", "label": "ElevenLabs", "dynamic": True, "available": True,
         "model": "eleven_v3",
         "voices": [{"id": "elevenlabs:own1", "name": "⭐ My Clone", "description": "cloned",
                     "preview_url": "https://example.invalid/p.mp3"}]
                   + [{"id": f"elevenlabs:p{i}", "name": f"Premade {i}", "description": ""}
                      for i in range(60)]},
    ],
    "default": {"id": "gemini-pro:Charon", "provider": "gemini-pro", "voice": "Charon",
                "model": "gemini-2.5-pro-tts", "available": True, "source": "fallback"},
}


def test_list_tts_voices_reads_the_catalog(session):
    session.body = CATALOG
    res = _run(voices_exec.execute({}, CTX))
    assert session.calls == [("GET", "http://box:9091/tts/catalog", None)]
    assert res.success is True
    text = res.result
    # Every provider group, the ids text_to_speech accepts, and availability.
    assert "gemini-pro:Charon" in text
    assert "elevenlabs:own1" in text
    assert "OpenAI TTS HD" in text and "Needs OPENAI_API_KEY" in text
    assert "Default voice: gemini-pro:Charon" in text
    # The full voice list rides in data even when the text is truncated.
    groups = {g["id"]: g for g in res.data["groups"]}
    assert len(groups["elevenlabs"]["voices"]) == 61
    assert groups["openai"]["available"] is False
    assert res.data["default"]["id"] == "gemini-pro:Charon"
    assert "elevenlabs:p59" not in text, "long groups are truncated in the summary"


def test_list_tts_voices_never_uses_google_cloud_voice_names(session):
    session.body = CATALOG
    _run(voices_exec.execute({}, CTX))
    assert all("/tts/google/voices" not in url for _, url, _ in session.calls)


def test_list_tts_voices_reports_an_unreachable_catalog(monkeypatch):
    class _Boom(_FakeSession):
        def get(self, url, timeout=None, **k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr("aiohttp.ClientSession", _Boom)
    res = _run(voices_exec.execute({}, CTX))
    assert res.success is False
    assert "connection refused" in res.result


# ── gemini_pro_tts ───────────────────────────────────────────────────────────
# The executor pinned voice_name "Charon" and model "gemini-2.5-pro-tts" on
# every POST /generate/gemini_tts (O18-toolvault-modules-14) although the
# route's GeminiProTTSIn already carries both defaults: two copies of one
# choice, and the tool's copy is the one that rots when the id moves.

import ToolVault.tools.gemini_pro_tts.executor as gemini_exec


def test_gemini_pro_tts_names_no_voice_or_model_unless_supplied(session):
    session.body = {"task_id": "t-1"}
    res = _run(gemini_exec.execute({"text": "hi"}, CTX))
    assert res.success, res.result
    _, url, payload = session.calls[0]
    assert url == "http://box:9091/generate/gemini_tts"
    assert payload["text"] == "hi"
    assert payload["operator"] == "tester"
    assert "voice_name" not in payload, payload
    assert "model" not in payload, payload
    assert res.data["task_id"] == "t-1"


def test_gemini_pro_tts_forwards_a_supplied_voice(session):
    session.body = {"task_id": "t-2"}
    res = _run(gemini_exec.execute({"text": "hi", "voice": "Kore"}, CTX))
    assert res.success, res.result
    _, _, payload = session.calls[0]
    assert payload["voice_name"] == "Kore"
    assert "model" not in payload, payload
    assert res.data["voice"] == "Kore"
