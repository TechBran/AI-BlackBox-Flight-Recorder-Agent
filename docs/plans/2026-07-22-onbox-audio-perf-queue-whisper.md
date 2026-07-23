# On-Box Audio: Performance Deep-Fix + Async Queue + Whisper Streaming — Plan

> **For Claude:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (workflow-orchestrated). MS02 is the free-reign testing ground.

**Date:** 2026-07-22 · **Status:** approved direction (Brandon: "deeply analyze and critique, check for weaknesses; build Whisper streaming + async queue + visibility")

## 0. The evidence (measured today, MS02 RTX 2000 Ada)

| Fact | Measurement |
|---|---|
| GPU **is** used during synth | 69% util, member holds 4.6 GB (not a CPU fallback) |
| Unoptimized RTF | 0.72 (G3) → a long reply ≈ 0.72× its audio length to synthesize |
| Real failure | ~11-chunk reply ≈ 6.5 min sequential → client timeout (120 s, then 300 s) |
| Kokoro-82M on CPU | faster than our 1.7B on GPU — because we run the fork's SLOW path |
| Fork's unused speedups | `enable_streaming_optimizations(use_compile=True, use_cuda_graphs=True, compile_talker=True, compile_codebook_predictor=True)` — torch.compile max-autotune decoder+talker, "bypasses HF generate() overhead", advertised ~6× |
| Unused native batching | `generate_custom_voice(text=[...], speaker=[...])` — all chunks in ONE GPU call |
| Known trap | `use_fast_codebook=False` (fork: "needs debugging, currently slower") |

**Diagnosis:** integration gap, not hardware. Three compounding losses: (1) unoptimized inference path; (2) per-chunk HTTP round-trips instead of native batch; (3) synchronous transport that ties a multi-minute job to whichever client timeout is tightest (server chunk timeout → GPU 429 pileup → OkHttp readTimeout — three layers whack-a-moled already).

## 1. Workstream A — TTS performance (the 6.5-min fix)

- **A1 Audit/critique (first):** parallel adversarial review of the serving path — adapter vs fork capabilities, per-request overhead (member HTTP, WAV re-encode, chunk boundaries), VariantManager lock granularity, llama-swap proxy hops, `max_new_tokens_for` interplay with batch mode, torch.compile warmup cost (first-call compile can be 30–120 s → must warm at member load, NOT first user request), CUDA-graphs vs variable batch shapes, bf16/sdpa choices, `QWEN_TTS_MODEL_DIR` load format. Deliverable: ranked weaknesses w/ measured or cited evidence.
- **A2 Enable fork optimizations** in `variant_manager.TorchQwenBackend.load()`: call `enable_streaming_optimizations(...)` per fork defaults (`use_fast_codebook=False`), env-gated (`QWEN_TTS_OPTIMIZE=1` default on). Warmup synth ("Ready.") at load so compile cost is paid once per residency, not per user request. Measure compile time + steady RTF.
- **A3 Native batch synth:** new member endpoint `POST /v1/audio/speech/batch` (list of inputs → list of WAVs, one `generate_custom_voice(text=[...])` call); `/tts/batch` qwen branch sends ALL chunks in one request; keep per-chunk fallback. Measure batched RTF vs sequential.
- **A4 Re-gate:** rerun the G3 RTF harness with optimizations + batch; record `eval/results/2026-07-2X-g3b-tts-optimized.json`. Target: long-reply wall-time ≥4× better (6.5 min → ≤ ~1.5 min for the same reply). If torch.compile is unstable on this stack (2000 Ada / torch 2.13), fall back tier-by-tier (compile only the decoder; disable cuda_graphs) and record what held.

## 2. Workstream B — Async TTS queue + visibility (permanent transport fix)

- **B1 Server:** `POST /tts/queue` → `{task_id, queue_position}` immediately; ONE sequential GPU worker drains a FIFO (asyncio queue; wraps `voice_session()`); task states `queued(n_ahead) → generating(chunk m/k, elapsed, eta) → done(audio_url) | failed(error, retryable)`; reuse `tasks.py` registry + cooperative cancel; result WAV/MP3 saved under `Portal/uploads/` (same lifecycle as Gemini TTS task audio); auto-retry once on transient (429/timeout) failures; `GET /tts/queue/status` for the whole queue (Updates panel).
- **B2 Web:** route on-box voices through the queue (submit+poll, reuse the Gemini task-poll pattern + "Audio still generating… (m:ss)" indicator); add queue position + chunk progress + failed→Retry affordance. Keep short cloud TTS synchronous (unchanged).
- **B3 Android:** same submit+poll in TtsRepository/NativeMainActivity; bubble status chip (queued/generating/failed-retry); kill the 300 s stopgap reliance (poll requests are short). Ships as v1.4.0 APK → adb install to the Fold6.
- **B4 Invariants:** cloud providers untouched; queue is additive (direct `/tts/batch` stays for API/MCP callers); fail-open when the stack is off.

## 3. Workstream C — Whisper streaming STT everywhere

- **C1 Audit the Design-B bridge end-to-end** (`stt_ws_routes._onbox_bridge` → `ws://127.0.0.1:9099/v1/realtime`): capture the REAL pre-1.0 Speaches `/v1/realtime` event schema live (the bridge's assumed shape is unverified — same class of bug as the qwen fork API); verify 24 kHz resample, trailing-silence stop, hallucination filter, per-utterance finals, `stt_done`, D10 loading affordance (~30 s ceiling → `stt_error`, never silent cloud switch).
- **C2 Fix what the capture disproves;** make the streaming model (`large-v3-turbo-ct2`) resident-warm behavior + first-turn latency acceptable (G4-streaming), including the cross-group swap story (voice turn evicts retrieval — D12 serialization).
- **C3 Wire + verify every streaming consumer:** Portal mic, Android mic (live partials chip), terminal mic — all through `/ws/stt` with `STT_PROVIDER=onbox`; batch consumers already validated (G4 batch).
- **C4 Gates:** G4-streaming parity (partial cadence + final WER vs the gemma-box path on a reference clip) + G6 eviction safety (stream survives/errors cleanly under a mid-stream retrieval demand) + G5 swap-latency numbers. Record to `eval/results/`.

## 4. Sequencing & validation

1. **A (perf)** first — it changes queue ETA math and user pain is highest. A1 audit → A2/A3 on MS02 → A4 re-gate.
2. **B (queue)** — server → web → Android (v1.4.0 APK to the Fold6 over adb).
3. **C (whisper streaming)** — capture-first, then fixes, then gates.
4. Every unit: implement → spec review → quality review (workflow); MS02 free-reign; additive invariant asserted per unit; `/snapshot-dev` at the end.

**Definition of done:** the SAME long reply that took 6.5 min + timeout now: queued instantly with visible progress, synthesized ≥4× faster, delivered reliably; mic streaming works on-box on all three surfaces; all measurements in eval/results/.
