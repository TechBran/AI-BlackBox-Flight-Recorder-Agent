"""API-layer tests for the qwen-tts server. The variant manager is REPLACED by a
FakeManager via dependency_overrides so no torch/CUDA is touched — the real model
never loads. Voice profiles land in a tmp QWEN_TTS_VOICES_DIR."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "LocalModels"))

import io
import json
import wave

import pytest
from fastapi.testclient import TestClient

from qwen_tts_server.app import app, get_manager

# DISTINCTIVE non-24k rate — proves sample rate is read from the model output,
# not hardcoded 24000 (correction [23]).
SR = 16000


class FakeManager:
    def __init__(self):
        self.calls = []

    async def synthesize_full(self, variant, text, *, preset=None, ref_audio=None, design_params=None):
        self.calls.append(("synthesize_full", variant, preset, ref_audio, design_params))
        return (b"\x11\x22" * 50, SR)

    async def stream_true(self, variant, text, *, preset=None, ref_audio=None, design_params=None):
        self.calls.append(("stream_true", variant))

        async def _g():
            yield b"\x00\x00"

        return SR, _g()

    async def design_preview(self, description, text):
        self.calls.append(("design_preview", description, text))
        return [{"generated_voice_id": "gvid-1", "pcm": b"\x33\x44" * 10, "sr": SR, "params": {"seed": 7}}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_TTS_VOICES_DIR", str(tmp_path / "qwen"))
    monkeypatch.delenv("QWEN_TTS_STREAMING", raising=False)  # G3 flag default OFF
    fake = FakeManager()
    app.dependency_overrides[get_manager] = lambda: fake
    with TestClient(app) as c:
        c.fake = fake
        c.voices_dir = tmp_path / "qwen"
        yield c
    app.dependency_overrides.clear()


def _wav_bytes(seconds, sr=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(sr * seconds))
    return buf.getvalue()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_speech_preset_wav_uses_model_sample_rate(client):
    r = client.post("/v1/audio/speech", json={"input": "hello", "voice": "qwen:Vivian"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    with wave.open(io.BytesIO(r.content), "rb") as w:
        assert w.getframerate() == SR   # NOT 24000 — read from the model output
    assert client.fake.calls[0][:3] == ("synthesize_full", "custom_voice", "Vivian")


def test_speech_bare_preset_name_ok(client):
    assert client.post("/v1/audio/speech", json={"input": "hi", "voice": "Serena"}).status_code == 200


def test_speech_pcm_format_sets_headers(client):
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "qwen:Vivian", "response_format": "pcm"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["x-sample-rate"] == str(SR)
    assert r.headers["x-audio-format"] == "pcm_s16le"


def test_speech_missing_input_422(client):
    assert client.post("/v1/audio/speech", json={"voice": "qwen:Vivian"}).status_code == 422


def test_speech_missing_voice_422(client):
    assert client.post("/v1/audio/speech", json={"input": "hi"}).status_code == 422


def test_speech_unknown_voice_404(client):
    assert client.post("/v1/audio/speech", json={"input": "x", "voice": "qwen:Nope"}).status_code == 404


def test_speech_bad_format_400(client):
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "qwen:Vivian", "response_format": "mp3"})
    assert r.status_code == 400
