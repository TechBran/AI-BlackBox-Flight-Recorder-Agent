"""Silero-VAD utterance gate for on-box streaming STT (W1, plan 2026-07-22).

Pure, streaming, unit-testable: `UtteranceGate.feed(pcm16)` consumes raw
16-bit mono PCM in arbitrary chunk sizes and emits SPEECH_START /
SPEECH_END(utterance_pcm) events. "The VAD closes" == `close_silence_ms` of
trailing sub-threshold audio (env-tunable `ONBOX_STT_VAD_CLOSE_MS`) — that is
the utterance boundary the /ws/stt onbox loop transcribes (W2). A pre-roll
ring buffer keeps the ~200 ms BEFORE the detector fires so first syllables
are not clipped; `max_utterance_s` force-commits a runaway monologue (the
buffer never grows unbounded — SPEECH_END then an immediate fresh
SPEECH_START while speech continues).

The scorer is INJECTABLE (tests pass a fake `frame -> probability`
callable); the real path is `SileroScorer`, a thin wrapper over the silero
VAD v5 ONNX graph on CPU via onnxruntime (lazy import — this module imports
clean on a box without onnxruntime installed; only constructing a real
session needs it).

Model weights: `silero_vad_v5.onnx` (~2.2 MB) under
`localstack_downloads.MODELS_DIR`, fetched either by the wizard's
POST /local-models/download (manifest key "silero-vad") or lazily by
`ensure_vad_model()` on first real use (download-once, lock-guarded, atomic
.part rename, retries + read-timeout hardening mirroring the localstack
download helpers). Repo note: the canonical snakers4/silero-vad HF repo
returns 401 to anonymous requests (verified 2026-07-23), so the manifest
pins the public onnx-community/silero-vad export of the same v5 graph
(onnx/model.onnx, fp32).
"""
from __future__ import annotations

import enum
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
import numpy as np

# ── events ────────────────────────────────────────────────────────────────


class EventKind(enum.Enum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


@dataclass(frozen=True)
class Event:
    kind: EventKind
    pcm: bytes | None = None   # SPEECH_END only: full utterance pcm16 mono

    @property
    def utterance_pcm(self) -> bytes | None:  # plan-spelling alias
        return self.pcm


SPEECH_START = EventKind.SPEECH_START
SPEECH_END = EventKind.SPEECH_END

# ── silero v5 frame contract ──────────────────────────────────────────────
# The v5 graph scores fixed frames: 512 samples @16 kHz (32 ms) / 256 @8 kHz,
# with 64/32 samples of leading context carried between frames.
_FRAME_SAMPLES = {16000: 512, 8000: 256}
_CONTEXT_SAMPLES = {16000: 64, 8000: 32}

_ENV_CLOSE_MS = "ONBOX_STT_VAD_CLOSE_MS"
_DEFAULT_CLOSE_MS = 600


class UtteranceGate:
    """Streaming VAD state machine: pcm16 in, utterance events out.

    States: idle → pending (speech seen, < min_speech_ms yet — blips are
    discarded) → active (SPEECH_START emitted) → close on `close_silence_ms`
    of trailing silence (SPEECH_END carries pre-roll + speech + that trailing
    silence) → idle. `flush()` commits whatever is open (client stop
    mid-speech), including a pending-but-unconfirmed short word.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        min_speech_ms: int = 250,
        close_silence_ms: int | None = None,   # None → $ONBOX_STT_VAD_CLOSE_MS → 600
        max_utterance_s: float = 30.0,
        pre_roll_ms: int = 200,
        *,
        scorer: Callable[[np.ndarray], float] | None = None,
        threshold: float = 0.5,
    ) -> None:
        if sample_rate not in _FRAME_SAMPLES:
            raise ValueError(f"sample_rate must be one of {sorted(_FRAME_SAMPLES)}, "
                             f"got {sample_rate}")
        if close_silence_ms is None:
            try:
                close_silence_ms = int(os.environ.get(_ENV_CLOSE_MS, "") or _DEFAULT_CLOSE_MS)
            except ValueError:
                close_silence_ms = _DEFAULT_CLOSE_MS
        self.sample_rate = sample_rate
        self.close_silence_ms = close_silence_ms
        self.min_speech_ms = min_speech_ms
        self.max_utterance_s = max_utterance_s
        self.pre_roll_ms = pre_roll_ms
        self.threshold = threshold

        self._frame_samples = _FRAME_SAMPLES[sample_rate]
        self._frame_bytes = self._frame_samples * 2
        self._frame_ms = self._frame_samples * 1000.0 / sample_rate
        self._min_speech_frames = max(1, -(-min_speech_ms // int(self._frame_ms)))
        self._close_frames = max(1, -(-close_silence_ms // int(self._frame_ms)))
        self._max_samples = int(max_utterance_s * sample_rate)
        ring_frames = max(0, -(-pre_roll_ms // int(self._frame_ms)))

        self._scorer = scorer          # lazy SileroScorer when None
        self._buf = bytearray()        # partial-frame reassembly
        self._ring: deque[bytes] = deque(maxlen=ring_frames)  # pre-roll frames
        self._state = "idle"           # idle | pending | active
        self._utt: list[bytes] = []
        self._utt_samples = 0
        self._pending_speech_frames = 0
        self._silence_frames = 0

    # ── public API ────────────────────────────────────────────────────────

    def feed(self, pcm16: bytes) -> list[Event]:
        """Consume a chunk of 16-bit mono PCM (any size); return the events
        that fired, in order."""
        self._buf.extend(pcm16)
        events: list[Event] = []
        fb = self._frame_bytes
        while len(self._buf) >= fb:
            frame = bytes(self._buf[:fb])
            del self._buf[:fb]
            events.extend(self._process_frame(frame))
        return events

    def flush(self) -> Event | None:
        """Client stop / stream end: commit whatever utterance is open
        (including a pending short word that never reached min_speech_ms),
        with any trailing partial frame. Resets the gate."""
        ev: Event | None = None
        if self._state in ("pending", "active") and self._utt:
            pcm = b"".join(self._utt) + bytes(self._buf)
            ev = Event(EventKind.SPEECH_END, pcm)
        self._buf.clear()
        self._reset_utterance()
        if isinstance(self._scorer, SileroScorer):
            self._scorer.reset()
        return ev

    # ── internals ─────────────────────────────────────────────────────────

    def _reset_utterance(self) -> None:
        self._state = "idle"
        self._utt = []
        self._utt_samples = 0
        self._pending_speech_frames = 0
        self._silence_frames = 0

    def _score(self, frame: bytes) -> float:
        if self._scorer is None:
            self._scorer = SileroScorer(sample_rate=self.sample_rate)
        f32 = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        return float(self._scorer(f32))

    def _append_utt(self, frame: bytes) -> None:
        self._utt.append(frame)
        self._utt_samples += self._frame_samples

    def _commit(self) -> Event:
        pcm = b"".join(self._utt)
        self._reset_utterance()
        return Event(EventKind.SPEECH_END, pcm)

    def _process_frame(self, frame: bytes) -> list[Event]:
        speech = self._score(frame) >= self.threshold
        events: list[Event] = []

        if self._state == "idle":
            if speech:
                # open a tentative utterance seeded with the pre-roll ring
                self._utt = list(self._ring)
                self._utt_samples = len(self._utt) * self._frame_samples
                self._append_utt(frame)
                self._pending_speech_frames = 1
                self._state = "pending"
                if self._pending_speech_frames >= self._min_speech_frames:
                    self._state = "active"
                    self._silence_frames = 0
                    events.append(Event(EventKind.SPEECH_START))
        elif self._state == "pending":
            if speech:
                self._append_utt(frame)
                self._pending_speech_frames += 1
                if self._pending_speech_frames >= self._min_speech_frames:
                    self._state = "active"
                    self._silence_frames = 0
                    events.append(Event(EventKind.SPEECH_START))
            else:
                # blip shorter than min_speech_ms — discard (frames stay
                # available via the ring, refilled below)
                self._reset_utterance()
        else:  # active
            self._append_utt(frame)
            if speech:
                self._silence_frames = 0
            else:
                self._silence_frames += 1
                if self._silence_frames >= self._close_frames:
                    # the VAD closes: this IS the utterance boundary
                    events.append(self._commit())

        # runaway-monologue force-commit: chunk, never grow unbounded
        if self._state == "active" and self._utt_samples >= self._max_samples:
            events.append(self._commit())
            if speech:
                # speech continues seamlessly — new chunk, no pre-roll needed
                self._state = "active"
                self._silence_frames = 0
                events.append(Event(EventKind.SPEECH_START))

        self._ring.append(frame)   # ring always tracks the latest audio
        return events


# ── model weights: manifest-pinned file + lazy download-once fallback ─────

_MANIFEST_KEY = "silero-vad"
_FETCH_LOCK = threading.Lock()
_FETCH_RETRIES = 3
_FETCH_RETRY_DELAY = 2.0       # seconds; monkeypatched to 0 in tests
_FETCH_READ_TIMEOUT = 30.0     # mirrors localstack _HF_SNAPSHOT_READ_TIMEOUT

# Test seam (localstack_downloads._async_transport pattern), sync flavor.
_transport: "httpx.BaseTransport | None" = None


def _manifest_entry() -> dict:
    from Orchestrator import localstack_downloads as _dl
    return _dl.DOWNLOAD_MANIFEST[_MANIFEST_KEY]


def default_vad_model_path() -> Path:
    """Canonical on-disk location — the SAME dest the wizard download button
    (manifest key "silero-vad") writes, so either fetch path satisfies both."""
    from Orchestrator import localstack_downloads as _dl
    return _dl.MODELS_DIR / _manifest_entry()["dest"]


def ensure_vad_model(path: "Path | str | None" = None) -> Path:
    """Return the silero ONNX model path, downloading it once (~2.2 MB) if
    absent. Lock-guarded (one fetch per process), atomic .part → rename,
    bounded read timeout + retries (the localstack download hardening — a
    dropped CDN connection must not wedge forever). Same-URL scheme as the
    manifest "file" artifact stream so the wizard button and this fallback
    are interchangeable."""
    canonical = path is None
    dest = Path(path) if path is not None else default_vad_model_path()
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    entry = _manifest_entry()
    url = (f"https://huggingface.co/{entry['repo']}"
           f"/resolve/main/{entry['filename']}?download=true")
    with _FETCH_LOCK:
        if dest.exists() and dest.stat().st_size > 0:   # lost the race: done
            return dest
        # Xet-off parity with the localstack snapshot path (harmless for a
        # direct CDN GET; an operator env override always wins).
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        last_err: Exception | None = None
        for attempt in range(1, _FETCH_RETRIES + 1):
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(_FETCH_READ_TIMEOUT, connect=15.0),
                    transport=_transport, follow_redirects=True,
                ) as client:
                    with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        with open(part, "wb") as fh:
                            for chunk in resp.iter_bytes(1 << 20):
                                fh.write(chunk)
                os.replace(part, dest)
                if canonical:
                    _record_state_fail_soft()
                return dest
            except Exception as e:  # noqa: BLE001 — retry then surface
                last_err = e
                try:
                    part.unlink()
                except OSError:
                    pass
                if attempt < _FETCH_RETRIES:
                    time.sleep(_FETCH_RETRY_DELAY)
        assert last_err is not None
        raise last_err


def _record_state_fail_soft() -> None:
    """Keep the wizard's download-state bookkeeping honest when the lazy path
    fetched the weights; a bookkeeping failure never fails the fetch."""
    try:
        from Orchestrator import local_stack
        local_stack.record_download_state(_MANIFEST_KEY)
    except Exception:  # noqa: BLE001 — bookkeeping only
        pass


class SileroScorer:
    """Real scorer: silero VAD v5 ONNX on CPU. One instance per stream
    (carries recurrent state + 64-sample context between frames). Lazy: the
    onnxruntime import, model download and session build all happen on first
    call, so merely importing/constructing is safe on any box."""

    def __init__(self, model_path: "Path | str | None" = None,
                 sample_rate: int = 16000) -> None:
        if sample_rate not in _FRAME_SAMPLES:
            raise ValueError(f"sample_rate must be one of {sorted(_FRAME_SAMPLES)}")
        self._sr = sample_rate
        self._model_path = Path(model_path) if model_path is not None else None
        self._session = None
        self._context = np.zeros((1, _CONTEXT_SAMPLES[sample_rate]), dtype=np.float32)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def reset(self) -> None:
        """Clear recurrent state between utterances/streams."""
        self._context = np.zeros((1, _CONTEXT_SAMPLES[self._sr]), dtype=np.float32)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def _ensure_session(self):
        if self._session is None:
            import onnxruntime as ort  # lazy — pinned in requirements.txt
            path = self._model_path or ensure_vad_model()
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1   # 32ms frames are tiny; stay off the GPU/cores
            self._session = ort.InferenceSession(
                str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        return self._session

    def __call__(self, frame: np.ndarray) -> float:
        expected = _FRAME_SAMPLES[self._sr]
        if len(frame) != expected:
            raise ValueError(f"silero v5 expects {expected}-sample frames "
                             f"@{self._sr} Hz, got {len(frame)}")
        sess = self._ensure_session()
        x = np.concatenate(
            [self._context, frame.astype(np.float32).reshape(1, -1)], axis=1)
        out, state = sess.run(
            None,
            {"input": x, "state": self._state,
             "sr": np.array(self._sr, dtype=np.int64)},
        )
        self._state = state
        self._context = x[:, -_CONTEXT_SAMPLES[self._sr]:]
        return float(out[0][0])
