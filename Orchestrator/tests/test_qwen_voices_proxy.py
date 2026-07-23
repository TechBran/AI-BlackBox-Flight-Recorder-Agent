"""Qwen voice-management proxy endpoints (M7 Task 7.5): list/delete are
filesystem ops over qwen_tts.{list_profiles,delete_profile}; clone/design/save
proxy /upstream/qwen-tts/… Clone enforces the consent flag (422 without it)."""
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


class _JResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._p


def test_qwen_voices_list(client):
    prof = [{"slug": "brandon", "name": "Brandon", "variant": "base"}]
    with patch("Orchestrator.qwen_tts.list_profiles", return_value=prof):
        resp = client.get("/qwen/voices")
    assert resp.status_code == 200
    body = resp.json()
    assert body["voices"][0]["slug"] == "brandon"


def test_qwen_clone_requires_consent(client):
    # No consent -> 422, and the upstream is never called.
    with patch("Orchestrator.routes.tts_routes.requests.post") as m_post:
        resp = client.post(
            "/qwen/voices/clone",
            data={"name": "Test"},
            files={"files": ("clip.wav", b"RIFFxxxx", "audio/wav")},
        )
    assert resp.status_code == 422
    m_post.assert_not_called()


def test_qwen_clone_proxies_upstream_with_consent(client, monkeypatch):
    """Clone now TRANSCODES the reference to WAV and synthesizes a PREVIEW after
    the store (2026-07-23 phone-M4A fix) — mock both seams; the proxy contract
    (singular 'file' field to upstream_url('/v1/voices/clone')) is unchanged."""
    import Orchestrator.audio_transcode as at
    monkeypatch.setattr(at, "to_wav_pcm16", lambda b, **k: b"WAVDATA")
    with patch("Orchestrator.qwen_tts.upstream_url",
               return_value="http://127.0.0.1:9098/upstream/qwen-tts/v1/voices/clone") as m_url, \
         patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_JResp({"voice_id": "test-slug"})), \
         patch("Orchestrator.qwen_tts.synthesize") as m_syn:
        m_syn.return_value = type("R", (), {"status_code": 200, "content": b"RIFFwav", "text": ""})()
        resp = client.post(
            "/qwen/voices/clone",
            data={"name": "Test", "consent": "true"},
            files={"files": ("clip.wav", b"RIFFxxxx", "audio/wav")},
        )
    assert resp.status_code == 200
    assert resp.json()["voice_id"] == "test-slug"
    assert resp.json().get("preview_b64")           # at-clone preview returned
    m_url.assert_called_once_with("/v1/voices/clone")
    m_syn.assert_called_once()                       # preview synthesized with the new slug
    assert m_syn.call_args[0][0] == "test-slug"
    m_post.assert_called_once()
    # The reference audio MUST be forwarded under the singular field name 'file'
    # to match the M6 server (`file: UploadFile = File(...)`); 'files' would 422.
    fwd = m_post.call_args.kwargs["files"]
    assert [part[0] for part in fwd] == ["file"]


def test_qwen_design_proxies_upstream(client):
    with patch("Orchestrator.qwen_tts.upstream_url",
               return_value="http://x/v1/voices/design"), \
         patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_JResp({"text": "sample", "previews": []})):
        resp = client.post("/qwen/voices/design",
                           json={"voice_description": "a warm narrator"})
    assert resp.status_code == 200
    assert "previews" in resp.json()


def test_qwen_design_save_proxies_upstream(client):
    with patch("Orchestrator.qwen_tts.upstream_url",
               return_value="http://x/v1/voices/design/save"), \
         patch("Orchestrator.routes.tts_routes.requests.post",
               return_value=_JResp({"voice_id": "design-slug"})):
        resp = client.post("/qwen/voices/design/save",
                           json={"generated_voice_id": "g1", "name": "Narrator"})
    assert resp.status_code == 200
    assert resp.json()["voice_id"] == "design-slug"


def test_qwen_delete_profile(client):
    with patch("Orchestrator.qwen_tts.delete_profile", return_value=True) as m_del:
        resp = client.delete("/qwen/voices/brandon")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    m_del.assert_called_once_with("brandon")


def test_qwen_delete_missing_profile(client):
    with patch("Orchestrator.qwen_tts.delete_profile", return_value=False):
        resp = client.delete("/qwen/voices/nope")
    assert resp.status_code == 404
