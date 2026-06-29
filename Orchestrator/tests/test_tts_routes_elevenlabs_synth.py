"""/tts + /tts/batch ElevenLabs synthesis routing (Task 18).

Contracts under test:
  1. POST /tts with an ``elevenlabs:`` voice → 200 MP3 stream (audio/mpeg),
     routed through ``elevenlabs.tts.synthesize_stream`` (streamed; HTTP never hit).
     (The /tts ElevenLabs path streams as of the 2026-06-29 reliability work; the
     buffered ``synthesize`` is still used by the /tts/batch path below.)
  2. POST /tts with ``return_json`` → success JSON: audio_url + format "mp3" +
     size_bytes (the same shape the OpenAI path returns).
  3. POST /tts with an ``openai:`` voice STILL hits the OpenAI path -- the
     ElevenLabs ``synthesize`` is NOT called (regression guard: openai untouched).
  4. POST /tts/batch with ``provider="elevenlabs"`` → the batch streaming
     contract (audio/mpeg), with per-chunk MP3 bytes concatenated.
  5. Long text → ``synthesize`` is called >1 time (chunked by ``max_chars_for``).

``synthesize``/``max_chars_for`` are monkeypatched on the ELEVENLABS module
(the routes do ``import ... tts as el_tts`` then attribute-access at call time,
so patching the module attribute is what the route sees). sync_embeddings is
mocked before app construction so the startup hook spawns no network call
(mirrors test_stt_routes_elevenlabs).
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_emb:
        m_emb.return_value = {"x": {"vector": [0.1]}}
        from Orchestrator.app import app
        with TestClient(app) as c:
            yield c


_FAKE_MP3 = b"ID3\x03\x00\x00\x00fake-mp3-frame-bytes"


def test_tts_elevenlabs_voice_streams_mp3(client):
    """voice='elevenlabs:abc' → 200 audio/mpeg from synthesize_stream (single call)."""
    # side_effect returns a FRESH generator per call (return_value would hand back
    # the same exhausted iterator on a 2nd call — see the multi-chunk test).
    with patch("Orchestrator.elevenlabs.tts.synthesize_stream",
               side_effect=lambda *a, **k: iter([_FAKE_MP3])) as m_syn, \
         patch("Orchestrator.elevenlabs.tts.max_chars_for", return_value=5000):
        resp = client.post("/tts", json={"text": "Quality first.", "voice": "elevenlabs:abc"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert resp.content == _FAKE_MP3
    m_syn.assert_called_once()
    # The full prefixed id reaches synthesize_stream (it strips the prefix itself).
    args, kwargs = m_syn.call_args
    assert args[0] == "Quality first."
    assert args[1] == "elevenlabs:abc"


def test_tts_elevenlabs_return_json_shape(client):
    """return_json=true → success JSON with audio_url + format mp3 + size_bytes."""
    with patch("Orchestrator.elevenlabs.tts.synthesize_stream",
               side_effect=lambda *a, **k: iter([_FAKE_MP3])), \
         patch("Orchestrator.elevenlabs.tts.max_chars_for", return_value=5000):
        resp = client.post(
            "/tts",
            json={"text": "Hello.", "voice": "elevenlabs:xyz", "return_json": True},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["audio_url"].startswith("/ui/uploads/")
    assert body["audio_url"].endswith("_tts.mp3")
    assert body["format"] == "mp3"
    assert body["voice"] == "elevenlabs:xyz"
    assert body["size_bytes"] == len(_FAKE_MP3)


def test_tts_provider_elevenlabs_without_prefix(client):
    """provider='elevenlabs' (no voice prefix) also routes to synthesize."""
    with patch("Orchestrator.elevenlabs.tts.synthesize_stream",
               side_effect=lambda *a, **k: iter([_FAKE_MP3])) as m_syn, \
         patch("Orchestrator.elevenlabs.tts.max_chars_for", return_value=5000):
        resp = client.post(
            "/tts",
            json={"text": "Hi.", "voice": "Rachel", "provider": "elevenlabs"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/mpeg")
    m_syn.assert_called_once()


def test_tts_openai_voice_untouched(client):
    """openai voice STILL hits the OpenAI HTTP path; ElevenLabs synthesize NOT called."""
    class _FakeResp:
        status_code = 200
        content = b"openai-mp3-bytes"
        text = ""

    with patch("Orchestrator.elevenlabs.tts.synthesize") as m_syn, \
         patch("Orchestrator.routes.tts_routes.requests.post", return_value=_FakeResp()) as m_post:
        resp = client.post("/tts", json={"text": "Hi there.", "voice": "openai:alloy"})

    assert resp.status_code == 200
    assert resp.content == b"openai-mp3-bytes"
    m_syn.assert_not_called()        # ElevenLabs path never entered
    m_post.assert_called_once()      # OpenAI HTTP path was taken


def test_tts_batch_elevenlabs_contract(client):
    """/tts/batch provider=elevenlabs → streaming MP3 (batch contract)."""
    with patch("Orchestrator.elevenlabs.tts.synthesize", return_value=_FAKE_MP3) as m_syn, \
         patch("Orchestrator.elevenlabs.tts.max_chars_for", return_value=5000):
        resp = client.post(
            "/tts/batch",
            json={"text": "Short batch text.", "provider": "elevenlabs", "voice": "elevenlabs:abc"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert resp.content == _FAKE_MP3  # single chunk -> single synthesize, concatenated
    m_syn.assert_called_once()


def test_tts_long_text_chunks(client):
    """Long text past the cap → synthesize called >1 time; bytes concatenated."""
    # cap=50 forces multi-chunk splitting of the long text below.
    long_text = ("This is sentence one. " * 10).strip()  # ~210 chars, many sentences
    with patch("Orchestrator.elevenlabs.tts.synthesize_stream",
               side_effect=lambda *a, **k: iter([_FAKE_MP3])) as m_syn, \
         patch("Orchestrator.elevenlabs.tts.max_chars_for", return_value=50):
        resp = client.post("/tts", json={"text": long_text, "voice": "elevenlabs:abc"})

    assert resp.status_code == 200
    assert m_syn.call_count > 1                      # chunked (one synthesize_stream call per piece)
    assert resp.content == _FAKE_MP3 * m_syn.call_count  # parts concatenated in order


def test_tts_batch_long_text_chunks(client):
    """/tts/batch elevenlabs long text → re-chunked by max_chars_for (>1 synth call)."""
    long_text = ("This is sentence one. " * 10).strip()
    with patch("Orchestrator.elevenlabs.tts.synthesize", return_value=_FAKE_MP3) as m_syn, \
         patch("Orchestrator.elevenlabs.tts.max_chars_for", return_value=50):
        resp = client.post(
            "/tts/batch",
            json={"text": long_text, "provider": "elevenlabs", "voice": "elevenlabs:abc"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert m_syn.call_count > 1
