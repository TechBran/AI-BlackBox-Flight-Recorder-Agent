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
