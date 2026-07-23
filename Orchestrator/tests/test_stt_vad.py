"""W1 — VAD utterance gate (Orchestrator/stt/vad.py).

Pure streaming unit tests with SYNTHETIC PCM and an injected fake scorer
(frame -> probability), so no onnxruntime / model file / network is needed.
Frame math (silero v5 contract @16 kHz): 512 samples/frame = 32 ms = 1024
bytes of pcm16.  All ms params in these tests are exact multiples of 32 so
event timing asserts are frame-exact.

One optional integration test runs the real silero ONNX session — skipped
unless onnxruntime is importable AND the model file is already on disk
(CI/dev-box safe; never downloads).
"""
import numpy as np
import httpx
import pytest

from Orchestrator.stt.vad import (
    Event,
    EventKind,
    UtteranceGate,
    SileroScorer,
    default_vad_model_path,
    ensure_vad_model,
)
from Orchestrator.stt import vad as vad_mod

SR = 16000
FRAME = 512                    # samples per silero v5 frame @16k
FRAME_BYTES = FRAME * 2        # pcm16
FRAME_MS = 32

# Exact-multiple params: min_speech 256ms = 8 frames, close 640ms = 20 frames,
# pre-roll 192ms = 6 frames.
MIN_SPEECH_MS = 256
CLOSE_MS = 640
PRE_ROLL_MS = 192
MIN_FRAMES = MIN_SPEECH_MS // FRAME_MS      # 8
CLOSE_FRAMES = CLOSE_MS // FRAME_MS         # 20
PRE_ROLL_FRAMES = PRE_ROLL_MS // FRAME_MS   # 6


def fake_scorer(frame: np.ndarray) -> float:
    """Energy VAD stand-in: 'speech' iff peak amplitude clears 0.1."""
    assert isinstance(frame, np.ndarray)
    assert frame.dtype == np.float32
    assert len(frame) == FRAME
    return 1.0 if float(np.abs(frame).max()) > 0.1 else 0.0


def tone_frame(i: int = 0) -> bytes:
    """One 32ms frame of a 440Hz sine at amplitude 0.5 (scores as speech)."""
    t = (np.arange(FRAME) + i * FRAME) / SR
    x = (0.5 * np.sin(2 * np.pi * 440.0 * t) * 32767).astype(np.int16)
    return x.tobytes()


def faint_frame() -> bytes:
    """Distinctive sub-threshold frame (amp 0.02) — pre-roll content marker."""
    t = np.arange(FRAME) / SR
    x = (0.02 * np.sin(2 * np.pi * 220.0 * t) * 32767).astype(np.int16)
    return x.tobytes()


SILENCE = b"\x00" * FRAME_BYTES


def make_gate(**kw) -> UtteranceGate:
    kw.setdefault("sample_rate", SR)
    kw.setdefault("min_speech_ms", MIN_SPEECH_MS)
    kw.setdefault("close_silence_ms", CLOSE_MS)
    kw.setdefault("pre_roll_ms", PRE_ROLL_MS)
    kw.setdefault("scorer", fake_scorer)
    return UtteranceGate(**kw)


def feed_frames(gate, frames):
    events = []
    for f in frames:
        events.extend(gate.feed(f))
    return events


# ── silence / blips ───────────────────────────────────────────────────────

def test_silence_only_no_events_and_flush_none():
    gate = make_gate()
    assert feed_frames(gate, [SILENCE] * 50) == []
    assert gate.flush() is None


def test_min_speech_suppresses_blips():
    """A burst shorter than min_speech_ms never opens an utterance."""
    gate = make_gate()
    frames = [SILENCE] * 10 + [tone_frame(i) for i in range(MIN_FRAMES - 1)] \
        + [SILENCE] * (CLOSE_FRAMES + 10)
    assert feed_frames(gate, frames) == []
    assert gate.flush() is None


# ── speech start / end timing ─────────────────────────────────────────────

def test_speech_start_after_min_speech_ms():
    gate = make_gate()
    feed_frames(gate, [SILENCE] * 10)
    for i in range(MIN_FRAMES - 1):        # frames 1..7: not yet
        assert gate.feed(tone_frame(i)) == []
    ev = gate.feed(tone_frame(MIN_FRAMES - 1))   # 8th frame = 256ms
    assert [e.kind for e in ev] == [EventKind.SPEECH_START]
    assert ev[0].pcm is None


def test_speech_end_exactly_after_close_silence_ms():
    gate = make_gate()
    pre = [faint_frame()] * 10
    burst = [tone_frame(i) for i in range(25)]
    events = feed_frames(gate, pre + burst)
    assert [e.kind for e in events] == [EventKind.SPEECH_START]
    for _ in range(CLOSE_FRAMES - 1):      # silence frames 1..19: still open
        assert gate.feed(SILENCE) == []
    ev = gate.feed(SILENCE)                # 20th silence frame = 640ms: closes
    assert [e.kind for e in ev] == [EventKind.SPEECH_END]
    assert ev[0].pcm is not None


def test_utterance_contains_burst_and_pre_roll():
    gate = make_gate()
    pre = [faint_frame()] * 10
    burst = [tone_frame(i) for i in range(25)]
    events = feed_frames(gate, pre + burst + [SILENCE] * CLOSE_FRAMES)
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.SPEECH_START, EventKind.SPEECH_END]
    pcm = events[-1].pcm
    # pre-roll (6 frames) + burst (25) + trailing silence up to close (20)
    assert len(pcm) == (PRE_ROLL_FRAMES + 25 + CLOSE_FRAMES) * FRAME_BYTES
    assert pcm.startswith(faint_frame() * PRE_ROLL_FRAMES)          # pre-roll kept
    assert b"".join(tone_frame(i) for i in range(25)) in pcm        # burst intact


def test_speech_resumes_before_close_keeps_one_utterance():
    """Silence shorter than close_silence_ms does NOT split the utterance."""
    gate = make_gate()
    frames = ([tone_frame(i) for i in range(10)] + [SILENCE] * (CLOSE_FRAMES - 1)
              + [tone_frame(i) for i in range(10)] + [SILENCE] * CLOSE_FRAMES)
    events = feed_frames(gate, frames)
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.SPEECH_START, EventKind.SPEECH_END]
    assert len(events[-1].pcm) == (10 + (CLOSE_FRAMES - 1) + 10 + CLOSE_FRAMES) * FRAME_BYTES


# ── flush ─────────────────────────────────────────────────────────────────

def test_flush_commits_trailing_open_utterance():
    gate = make_gate()
    feed_frames(gate, [faint_frame()] * 10)
    events = feed_frames(gate, [tone_frame(i) for i in range(12)])
    assert [e.kind for e in events] == [EventKind.SPEECH_START]
    ev = gate.flush()
    assert ev is not None and ev.kind is EventKind.SPEECH_END
    assert len(ev.pcm) == (PRE_ROLL_FRAMES + 12) * FRAME_BYTES
    assert gate.flush() is None            # nothing left after commit


def test_flush_commits_pending_short_speech():
    """Client stop right after a short word (< min_speech_ms): emit what we have."""
    gate = make_gate()
    feed_frames(gate, [tone_frame(i) for i in range(3)])   # pending, no START yet
    ev = gate.flush()
    assert ev is not None and ev.kind is EventKind.SPEECH_END
    assert len(ev.pcm) == 3 * FRAME_BYTES


# ── max_utterance_s force-commit ──────────────────────────────────────────

def test_force_commit_at_max_utterance():
    """A runaway monologue is chunked: SPEECH_END at max_utterance_s, then a
    fresh SPEECH_START while speech continues (no unbounded buffer)."""
    gate = make_gate(max_utterance_s=1.0)  # 16000 samples = 31.25 frames
    events = feed_frames(gate, [faint_frame()] * 10)
    events += feed_frames(gate, [tone_frame(i) for i in range(60)])
    kinds = [e.kind for e in events]
    assert kinds[:3] == [EventKind.SPEECH_START, EventKind.SPEECH_END,
                         EventKind.SPEECH_START]
    first_end = next(e for e in events if e.kind is EventKind.SPEECH_END)
    n_samples = len(first_end.pcm) // 2
    assert n_samples >= SR * 1.0                       # at least max_utterance_s
    assert n_samples < SR * 1.0 + FRAME                # ...but only by < 1 frame
    # chunks keep coming while the monologue runs
    assert kinds.count(EventKind.SPEECH_END) >= 1
    tail = gate.flush()
    assert tail is not None and tail.kind is EventKind.SPEECH_END


# ── env tunable close_silence_ms ──────────────────────────────────────────

def test_close_silence_env_default(monkeypatch):
    monkeypatch.setenv("ONBOX_STT_VAD_CLOSE_MS", "320")   # 10 frames
    gate = UtteranceGate(sample_rate=SR, min_speech_ms=MIN_SPEECH_MS,
                         pre_roll_ms=PRE_ROLL_MS, scorer=fake_scorer)
    feed_frames(gate, [tone_frame(i) for i in range(10)])
    for _ in range(9):
        assert gate.feed(SILENCE) == []
    ev = gate.feed(SILENCE)                # 10th silence frame = 320ms
    assert [e.kind for e in ev] == [EventKind.SPEECH_END]


def test_close_silence_explicit_param_beats_env(monkeypatch):
    monkeypatch.setenv("ONBOX_STT_VAD_CLOSE_MS", "320")
    gate = make_gate()                     # explicit close_silence_ms=640
    feed_frames(gate, [tone_frame(i) for i in range(10)])
    assert feed_frames(gate, [SILENCE] * 10) == []        # 320ms: still open
    events = feed_frames(gate, [SILENCE] * (CLOSE_FRAMES - 10))
    assert [e.kind for e in events] == [EventKind.SPEECH_END]


# ── chunking robustness ───────────────────────────────────────────────────

def test_single_blob_feed_returns_ordered_events():
    gate = make_gate()
    blob = (faint_frame() * 10 + b"".join(tone_frame(i) for i in range(25))
            + SILENCE * CLOSE_FRAMES)
    events = gate.feed(blob)
    assert [e.kind for e in events] == [EventKind.SPEECH_START, EventKind.SPEECH_END]


def test_odd_sized_chunks_are_reassembled():
    """Client chunk sizes never align to frames; the gate must rebuffer."""
    gate = make_gate()
    blob = (faint_frame() * 10 + b"".join(tone_frame(i) for i in range(25))
            + SILENCE * CLOSE_FRAMES)
    events = []
    for off in range(0, len(blob), 700):   # 700 bytes: not a frame multiple
        events.extend(gate.feed(blob[off:off + 700]))
    assert [e.kind for e in events] == [EventKind.SPEECH_START, EventKind.SPEECH_END]


# ── lazy model fetch (MockTransport — no network) ─────────────────────────

def test_ensure_vad_model_downloads_once_then_skips(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"ONNX-FAKE-BYTES")

    monkeypatch.setattr(vad_mod, "_transport", httpx.MockTransport(handler))
    dest = tmp_path / "silero_vad_v5.onnx"
    p = ensure_vad_model(path=dest)
    assert p == dest and dest.read_bytes() == b"ONNX-FAKE-BYTES"
    assert len(calls) == 1
    assert "huggingface.co" in calls[0] and "silero" in calls[0]
    ensure_vad_model(path=dest)            # already present: no re-fetch
    assert len(calls) == 1
    assert not dest.with_name(dest.name + ".part").exists()


def test_ensure_vad_model_retries_then_raises(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, text="upstream sad")

    monkeypatch.setattr(vad_mod, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(vad_mod, "_FETCH_RETRY_DELAY", 0.0)
    dest = tmp_path / "silero_vad_v5.onnx"
    with pytest.raises(httpx.HTTPStatusError):
        ensure_vad_model(path=dest)
    assert len(calls) == vad_mod._FETCH_RETRIES
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_manifest_has_silero_entry():
    from Orchestrator.localstack_downloads import DOWNLOAD_MANIFEST
    entry = DOWNLOAD_MANIFEST.get("silero-vad")
    assert entry is not None
    assert entry["kind"] == "file"
    assert entry["dest"] == "silero_vad_v5.onnx"
    assert entry["repo"] and entry["filename"].endswith(".onnx")


# ── optional real-model integration (skipped when absent) ─────────────────

_ort_available = True
try:                                        # pragma: no cover - env probe
    import onnxruntime  # noqa: F401
except Exception:                           # pragma: no cover
    _ort_available = False


@pytest.mark.skipif(
    not _ort_available or not default_vad_model_path().exists(),
    reason="onnxruntime or the silero ONNX model file is not present",
)
def test_real_silero_session_scores_silence_low():
    scorer = SileroScorer()
    for _ in range(5):
        p = scorer(np.zeros(FRAME, dtype=np.float32))
        assert 0.0 <= p <= 1.0
    assert p < 0.5                          # digital silence is not speech
    # a scored frame of noise still yields a valid probability
    rng = np.random.default_rng(0)
    p = scorer((rng.standard_normal(FRAME) * 0.05).astype(np.float32))
    assert 0.0 <= p <= 1.0
