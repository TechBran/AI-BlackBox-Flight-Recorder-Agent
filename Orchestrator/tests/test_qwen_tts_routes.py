"""Qwen3-TTS route integration — catalog group + /tts and /tts/batch branch
routing (M7 Tasks 7.2-7.4). local_stack/qwen_tts are mocked so the suite runs
with no on-box stack (the dev-box / CI state). sync_embeddings is mocked
before app construction (mirrors test_tts_routes_elevenlabs_synth)."""
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


_FAKE_MP3 = b"ID3\x03\x00\x00\x00fake-qwen-mp3"


class _Resp:
    def __init__(self, content=_FAKE_MP3, status=200):
        self.content = content
        self.status_code = status
        self.text = ""


# ── 7.2 catalog ──────────────────────────────────────────────────────────
def test_catalog_appends_qwen_group_when_available(client):
    fake_group = {"id": "qwen", "label": "Qwen3-TTS (On-Box)", "dynamic": True,
                  "voices": [{"id": "qwen:Vivian", "name": "Vivian",
                              "description": "Warm, expressive"}]}
    with patch("Orchestrator.qwen_tts.catalog_group", return_value=fake_group):
        resp = client.get("/tts/catalog")
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    ids = [g["id"] for g in groups]
    assert "qwen" in ids
    qwen = next(g for g in groups if g["id"] == "qwen")
    assert qwen["voices"][0]["id"] == "qwen:Vivian"


def test_catalog_omits_qwen_group_when_unavailable(client):
    with patch("Orchestrator.qwen_tts.catalog_group", return_value=None):
        resp = client.get("/tts/catalog")
    assert resp.status_code == 200
    ids = [g["id"] for g in resp.json()["groups"]]
    assert "qwen" not in ids   # fail-open: cloud groups still returned


def test_catalog_survives_qwen_helper_raising(client):
    """A raising qwen_tts must never 500 the catalog (fail-open like the
    ElevenLabs/local blocks)."""
    with patch("Orchestrator.qwen_tts.catalog_group", side_effect=RuntimeError("boom")):
        resp = client.get("/tts/catalog")
    assert resp.status_code == 200
    assert "qwen" not in [g["id"] for g in resp.json()["groups"]]


# ── 7.3 POST /tts ────────────────────────────────────────────────────────
def test_tts_qwen_voice_streams_audio(client):
    with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp()) as m_syn:
        resp = client.post("/tts", json={"text": "Hello there.", "voice": "qwen:Vivian"})
    assert resp.status_code == 200
    # On-box Qwen emits WAV/PCM only — the branch always serves audio/wav.
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content == _FAKE_MP3
    m_syn.assert_called_once()
    args, kwargs = m_syn.call_args
    assert args[0] == "Vivian"          # prefix stripped -> bare token
    assert args[1] == "Hello there."
    # The member is asked for 'wav', never 'mp3' (which the M6 server 400s).
    assert kwargs.get("response_format", args[2] if len(args) > 2 else None) == "wav"


def test_tts_qwen_return_json_shape(client):
    with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp()):
        resp = client.post("/tts", json={"text": "Hi.", "voice": "qwen:Serena",
                                         "return_json": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["audio_url"].startswith("/ui/uploads/")
    assert body["voice"] == "Serena"
    assert body["model"] == "qwen-tts"
    assert body["size_bytes"] == len(_FAKE_MP3)


def test_tts_qwen_provider_bare_voice(client):
    """provider='qwen' with a BARE voice (the Android /tts shape) also routes."""
    with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp()) as m_syn:
        resp = client.post("/tts", json={"text": "Yo.", "voice": "Ryan",
                                         "provider": "qwen"})
    assert resp.status_code == 200
    m_syn.assert_called_once()
    assert m_syn.call_args[0][0] == "Ryan"


def test_tts_qwen_upstream_error_is_fallback(client):
    with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp(status=503)):
        resp = client.post("/tts", json={"text": "Hi.", "voice": "qwen:Vivian",
                                         "return_json": True})
    assert resp.status_code == 200
    assert resp.json()["status"] == "fallback"


# ── 7.4 POST /tts/batch ──────────────────────────────────────────────────
def test_tts_batch_qwen_provider(client):
    with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp()) as m_syn:
        resp = client.post("/tts/batch",
                           json={"text": "Short batch.", "provider": "qwen",
                                 "voice": "Vivian"})
    assert resp.status_code == 200
    # Qwen emits WAV — the batch stitches WAV and serves audio/wav (a single
    # chunk passes through stitch_wav_chunks unchanged, so content is preserved).
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content == _FAKE_MP3
    m_syn.assert_called_once()
    assert m_syn.call_args[0][0] == "Vivian"
    # The member is asked for 'wav', never mp3 (which the M6 server 400s).
    assert m_syn.call_args.kwargs.get("response_format") == "wav"


def test_tts_batch_qwen_prefix_forces_provider(client):
    """A 'qwen:'-prefixed voice forces provider=qwen even if provider omitted
    (parity with the local: override), and the bare token reaches synthesize."""
    with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp()) as m_syn:
        resp = client.post("/tts/batch",
                           json={"text": "Hi.", "voice": "qwen:Serena"})
    assert resp.status_code == 200
    m_syn.assert_called_once()
    assert m_syn.call_args[0][0] == "Serena"


def test_tts_batch_qwen_upstream_error_502(client):
    with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp(status=500)):
        resp = client.post("/tts/batch",
                           json={"text": "Hi.", "provider": "qwen", "voice": "Vivian"})
    assert resp.status_code == 502
