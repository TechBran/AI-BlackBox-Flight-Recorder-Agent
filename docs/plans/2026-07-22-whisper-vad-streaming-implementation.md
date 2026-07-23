# On-Box Whisper Streaming — VAD-Gated Utterance Architecture — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (workflow-orchestrated). Executes Workstream C of `docs/plans/2026-07-22-onbox-audio-perf-queue-whisper.md`. MS02 is the live testing ground; Fold6 over adb for Android validation.

**Goal:** Live on-box speech-to-text on every surface (Portal mic, Android mic, terminal mic) via `/ws/stt`: **server-side VAD detects speech start/stop; every time the VAD closes (speech stops), that utterance chunk is transcribed** through the already-G4-validated Speaches batch path, and the final is emitted to the client. (Brandon's chosen architecture, 2026-07-22.)

**Why VAD-gated beats the old Design-B realtime bridge:**
- **No dependency on the unverified pre-1.0 `/v1/realtime` event schema.** The old `_onbox_bridge` assumes an event protocol that has never run live (same bug-class as the qwen fork-API mismatch). VAD-gating uses `POST /upstream/speaches/v1/audio/transcriptions` per utterance — the exact path G4 already proved (near-exact transcript, `eval/results/2026-07-22-g4-stt.json`).
- Whisper isn't incremental anyway — it transcribes segments. VAD utterances ARE the natural segment.
- One server-side implementation covers all three surfaces (they all speak `/ws/stt` already); the client event contract does not change.

**Architecture:**
```
client mic ──WS /ws/stt──▶ Orchestrator onbox branch
                             │ decode/resample (reuse existing /ws/stt audio pipeline)
                             ├─ silero VAD (CPU, streaming frames)
                             │    speech_start → buffer utterance (+~200ms pre-roll)
                             │    speech_end (VAD closes, ~600ms silence) ─▶
                             │       POST utterance → /upstream/speaches/v1/audio/transcriptions
                             │       (stream model: fit-resolved large-v3-turbo-ct2, GPU, warm)
                             │       → emit `final` (hallucination-filtered, per-utterance)
                             ├─ optional rolling `partial` every ~1.5s while speech active
                             └─ stt_status: loading_models (D10) / listening / speech / processing
                                 all inside voice_session() (D12); terminal stt_done on close
```

---

## Task W1: VAD module — `Orchestrator/stt/vad.py` (new)

**Files:** Create `Orchestrator/stt/vad.py`; Test `Orchestrator/tests/test_stt_vad.py`.

Silero-VAD ONNX, CPU, streaming. **Pin the dependency** (lesson: speaches' loose pins broke 3 APIs): prefer reusing an already-present silero onnx runtime path; else `pip install silero-vad==<pinned>` or onnxruntime + the model file vendored under `Orchestrator/stt/assets/` (decide at implementation; record the pin in requirements).

API (pure, no I/O — unit-testable with synthetic PCM):
```python
class UtteranceGate:
    def __init__(self, sample_rate=16000, min_speech_ms=250, close_silence_ms=600,
                 max_utterance_s=30.0, pre_roll_ms=200): ...
    def feed(self, pcm16: bytes) -> list[Event]   # Event: SPEECH_START | SPEECH_END(utterance_pcm) | NONE
    def flush(self) -> Event | None               # client stop mid-speech -> emit what we have
```
- `close_silence_ms` IS "the VAD closes" — the utterance boundary (default ~600 ms, env-tunable `ONBOX_STT_VAD_CLOSE_MS`).
- `max_utterance_s` force-commits a runaway monologue (chunk anyway; don't grow unbounded).
- Pre-roll ring buffer so the first syllable isn't clipped.

**Steps (TDD):** failing tests with synthetic audio (silence → tone burst → silence → burst): asserts SPEECH_START on burst, SPEECH_END exactly after `close_silence_ms`, utterance bytes contain the burst + pre-roll, `flush()` commits a trailing open utterance, `max_utterance_s` force-commit. Then implement; commit.

## Task W2: Rewrite the `/ws/stt` onbox branch to the VAD-gated loop

**Files:** Modify `Orchestrator/routes/stt_ws_routes.py` (`_onbox_bridge` → `_onbox_vad_loop`); Test `Orchestrator/tests/test_stt_ws_onbox_vad.py` (fake gate + fake transcriber).

- Reuse the existing `/ws/stt` inbound decode/resample pipeline (it already normalizes client audio for the cloud providers — VERIFY actual per-surface formats; resample to 16 k mono for the gate).
- Session start: warm the audio group (`/upstream/speaches/health`), emit `stt_status{state:"loading_models"}` (D10; ~30 s ceiling → `stt_error`, never a silent cloud fallback), then `stt_status{state:"listening"}`. All inside `voice_session()` (D12).
- On `SPEECH_START` → `stt_status{state:"speech"}`. On `SPEECH_END(utterance)` → `stt_status{state:"processing"}` → transcribe via `stt/file_transcribe._onbox_transcribe`-equivalent using **`stt_stream_model()`** (turbo — NOT the large-v3 batch model; utterance latency matters) → apply `is_whisper_hallucination` filter → emit `final{text}` (per-utterance, same event shape every consumer already parses).
- Client close/stop: `gate.flush()` → last final → terminal `stt_done`. Preserve the existing 429-retry on the transcription POST.
- The old realtime-WS `_onbox_bridge` is **parked behind `ONBOX_STT_REALTIME=1`** (default OFF) — not deleted — pending the protocol-audit verdict; the VAD loop is the default onbox path.
- **Steps:** failing tests (fake gate/transcriber): event sequence (loading→listening→speech→processing→final→…→stt_done), hallucination-filtered final suppressed, transcriber 429 retried, flush-on-disconnect, D10 ceiling → `stt_error`. Implement; commit.

## Task W3: Rolling partials (optional, env-gated, default ON)

**Files:** Modify `stt_ws_routes.py`; extend `test_stt_ws_onbox_vad.py`.

While speech is active, every `ONBOX_STT_PARTIAL_MS` (default 1500): transcribe the current utterance buffer (turbo, warm ≈ fast) and emit `partial{text}` — this feeds the existing Android live-partials chip and Portal interim text. **Drop-frame policy:** never more than one partial transcription in flight; skip the tick if busy (bounded GPU load; finals always win). `ONBOX_STT_PARTIALS=0` disables. Tests: partial cadence, in-flight skip, partials stop at SPEECH_END, final supersedes.

## Task W4: Surfaces verification (no client code changes expected)

- The event contract is unchanged, so Portal mic, Android mic (live-partials chip), and terminal mic should work as-is with `STT_PROVIDER=onbox`.
- Verify live on MS02: Portal mic (browser), terminal mic, and Android on the Fold6 (adb — logcat watch for the event flow). List any surface that special-cases provider names as a finding, fix additively.
- Wizard: confirm the transcription step's onbox card copy matches the new behavior ("transcribes each phrase when you pause").

## Task W5: Gates + eviction interplay (MS02)

- **G4-streaming:** reference clip (use a Qwen-TTS-generated known-text clip — self-contained loop) fed through `/ws/stt` at real-time pace: assert per-utterance finals' WER vs known text, end-of-speech→final latency (target: close_silence + transcribe ≤ ~1.5 s warm), partial cadence honored. Record `eval/results/2026-07-2X-g4s-stt-streaming.json`.
- **G6 eviction safety:** mid-stream, fire a retrieval query (embeddings search): D12 must hold it until the voice session ends; the stream must never lose the whisper model mid-utterance; retrieval recovers after. Record results.
- **G5:** swap-latency numbers for the voice-turn-after-search path (first-utterance D10 wait).

## Task W6: Docs + snapshot

Update the master plan's Workstream C status; `/snapshot-dev` with measurements + gotchas; note the parked realtime bridge and its env flag.

---

**Sequencing:** starts after Workstream A (perf) lands and alongside/after B (queue) per the master plan — W1/W2 unit work is dev-box-safe (inert without the stack) and can begin immediately after A is applied; W4/W5 are MS02+Fold6.

**Additive invariants:** cloud STT streaming (ElevenLabs Scribe et al.) untouched; `onbox` stays a resolver token; stack-off boxes never enter the onbox branch; the tree runs at every commit.
