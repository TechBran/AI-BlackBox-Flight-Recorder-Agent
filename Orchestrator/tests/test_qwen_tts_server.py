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


def test_speech_whitespace_input_422(client):
    # Body(...)/Pydantic satisfies "present" — the whitespace-only guard is ours.
    r = client.post("/v1/audio/speech", json={"input": "   ", "voice": "qwen:Vivian"})
    assert r.status_code == 422


# ---- streaming (Task 6.4/6.5) ------------------------------------------------
def test_speech_stream_fallback_yields_full_pcm(client):
    # G3 flag OFF (fixture default) -> StreamingResponse OVER a full generation:
    # the body is the complete PCM chunked by _frame_iter, with the headers set.
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "qwen:Vivian", "stream": True})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["x-sample-rate"] == str(SR)
    assert r.headers["x-audio-format"] == "pcm_s16le"
    # FakeManager.synthesize_full returns b"\x11\x22" * 50 — the fallback must
    # reassemble to exactly that (no truncation, no true-stream branch taken).
    assert r.content == b"\x11\x22" * 50
    assert client.fake.calls[0][:3] == ("synthesize_full", "custom_voice", "Vivian")


def test_speech_stream_true_uses_stream_true_when_flag_on(client, monkeypatch):
    # G3 flag ON -> the true-chunked branch: mgr.stream_true + StreamingResponse.
    monkeypatch.setenv("QWEN_TTS_STREAMING", "1")
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "qwen:Vivian", "stream": True})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["x-sample-rate"] == str(SR)
    assert r.headers["x-audio-format"] == "pcm_s16le"
    assert r.content == b"\x00\x00"          # FakeManager.stream_true's single frame
    assert client.fake.calls[0][:2] == ("stream_true", "custom_voice")


def test_frame_iter_chunks_at_12hz():
    from qwen_tts_server.app import _frame_iter

    sr = 24000                       # samples_per_frame = 2000 -> step = 4000 bytes
    pcm = b"\x00" * 10000
    chunks = list(_frame_iter(pcm, sr))
    assert b"".join(chunks) == pcm   # lossless
    assert len(chunks[0]) == 4000    # sr//12 samples * 2 bytes/int16
    assert [len(c) for c in chunks] == [4000, 4000, 2000]


def test_stream_true_flag_off_uses_full_generation_fallback(client):
    # stream:true with the G3 flag OFF -> StreamingResponse OVER a full
    # generation (correction [8]): synthesize_full runs, stream_true does NOT.
    r = client.post("/v1/audio/speech", json={"input": "hello", "voice": "qwen:Vivian", "stream": True})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["x-sample-rate"] == str(SR)
    assert r.headers["x-audio-format"] == "pcm_s16le"
    assert any(c[0] == "synthesize_full" for c in client.fake.calls)
    assert all(c[0] != "stream_true" for c in client.fake.calls)


def test_stream_fallback_frames_reassemble_to_full_pcm(client):
    # The framed body must equal the full PCM the manager produced (b"\x11\x22"*50).
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "qwen:Vivian", "stream": True})
    assert r.content == b"\x11\x22" * 50


def test_stream_true_flag_on_uses_stream_true(client, monkeypatch):
    monkeypatch.setenv("QWEN_TTS_STREAMING", "1")   # G3 gate ON
    r = client.post("/v1/audio/speech", json={"input": "hello", "voice": "qwen:Vivian", "stream": True})
    assert r.status_code == 200
    assert r.headers["x-sample-rate"] == str(SR)
    assert any(c[0] == "stream_true" for c in client.fake.calls)
    assert all(c[0] != "synthesize_full" for c in client.fake.calls)


# ---- profile-variant voice resolution (Task 6.4) ----------------------------
def test_speech_base_clone_uses_ref_audio(client):
    from qwen_tts_server import profile_store

    profile_store.save_clone_profile(
        slug="myclone", name="My Clone", operator="system",
        consent=True, ref_bytes=b"\x00\x00" * 100, ref_filename="ref.wav",
    )
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "myclone"})
    assert r.status_code == 200
    _, variant, _preset, ref_audio, _design = client.fake.calls[0]
    assert variant == "base"
    assert ref_audio and ref_audio.endswith("reference.wav")


def test_speech_voice_design_uses_design_params(client):
    from qwen_tts_server import profile_store

    profile_store.save_design_profile(
        slug="mydesign", name="My Design", operator="system",
        description="warm narrator", design_params={"seed": 7},
    )
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "mydesign"})
    assert r.status_code == 200
    _, variant, _preset, _ref, design = client.fake.calls[0]
    assert variant == "voice_design"
    assert design == {"seed": 7}


def test_speech_voice_path_traversal_blocked(client, tmp_path):
    # Plant a profile.json OUTSIDE voices_dir (voices_dir is tmp_path/qwen).
    outside = tmp_path / "secretdir"
    outside.mkdir()
    (outside / "profile.json").write_text(
        json.dumps({"variant": "voice_design", "design": {"seed": 99}})
    )
    # Without sanitization this reads tmp_path/qwen/../secretdir/profile.json and
    # returns 200 with the planted design. sanitize_slug collapses it to
    # 'secretdir' under voices_dir -> not found -> 404, file never read.
    r = client.post("/v1/audio/speech", json={"input": "x", "voice": "qwen:../secretdir"})
    assert r.status_code == 404


# ---- voices list + consent-gated cloning (Task 6.6) -------------------------
def test_voices_lists_nine_presets(client):
    voices = client.get("/v1/audio/voices").json()["voices"]
    ids = [v["id"] for v in voices]
    assert "Vivian" in ids and "Sohee" in ids
    assert len([v for v in voices if v["type"] == "preset"]) == 9


def test_clone_without_consent_422_no_profile(client):
    r = client.post(
        "/v1/voices/clone",
        data={"name": "Brandon", "consent": "false"},
        files={"file": ("ref.wav", _wav_bytes(4.0), "audio/wav")},
    )
    assert r.status_code == 422
    assert not (client.voices_dir / "brandon").exists()


def test_clone_too_short_422(client):
    r = client.post(
        "/v1/voices/clone",
        data={"name": "Brandon", "consent": "true"},
        files={"file": ("ref.wav", _wav_bytes(1.0), "audio/wav")},
    )
    assert r.status_code == 422


def test_clone_ok_persists_base_profile(client):
    r = client.post(
        "/v1/voices/clone",
        data={"name": "Brandon", "consent": "true", "operator": "Brandon"},
        files={"file": ("ref.wav", _wav_bytes(4.0), "audio/wav")},
    )
    assert r.status_code == 200 and r.json()["voice_id"] == "brandon"
    prof = json.loads((client.voices_dir / "brandon" / "profile.json").read_text())
    assert prof["variant"] == "base" and prof["consent"] is True and prof["operator"] == "Brandon"
    # the cloned voice now appears in the voices list as a clone
    listed = {v["id"]: v for v in client.get("/v1/audio/voices").json()["voices"]}
    assert listed["brandon"]["type"] == "clone"


def test_clone_name_traversal_sanitized(client):
    r = client.post(
        "/v1/voices/clone",
        data={"name": "../../etc/passwd", "consent": "true"},
        files={"file": ("ref.wav", _wav_bytes(4.0), "audio/wav")},
    )
    assert r.status_code == 200
    slug = r.json()["voice_id"]
    assert "/" not in slug and ".." not in slug
    assert (client.voices_dir / slug / "profile.json").exists()


def test_speech_with_cloned_voice_resolves_base(client):
    client.post(
        "/v1/voices/clone",
        data={"name": "Brandon", "consent": "true"},
        files={"file": ("ref.wav", _wav_bytes(4.0), "audio/wav")},
    )
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "brandon"})
    assert r.status_code == 200
    call = client.fake.calls[-1]              # (kind, variant, preset, ref_audio, design)
    assert call[1] == "base" and call[3] is not None
