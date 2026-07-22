# On-Box Local Model Stack + CU Virtual Displays — Design Spec

**Date:** 2026-07-20
**Status:** SPEC v2.1 — hardened by 6-dimension Opus audit 2026-07-20 (see §14); **decisions D1–D14 locked** (Q10–Q15 answered by Brandon 2026-07-20). Remaining open items (Q1–Q9) are implementation-level. **Ready for the implementation plan (superpowers:writing-plans).**; D13/D14 amendments 2026-07-20
**Scope:** Bring STT, TTS, embeddings, and reranking wholesale onto the box (GPU-shared via queue/evict/TTL), integrate the full open-weights Qwen3-TTS capability surface, deliver it all through `Scripts/install.sh` + the onboarding wizard with honest CPU fallback — plus per-session virtual displays for computer use with live view in Portal and Android.

---

## 1. Goals

1. **Wholesale-local peripherals.** STT (whisper), TTS (Qwen3-TTS), embeddings (Qwen3-Embedding), and reranking (Qwen3-Reranker) run ON the box when hardware allows. The only thing customers reach off-box for is the chat LLM itself (custom API servers or cloud). Customers without capable hardware explicitly choose a cloud provider per capability.
2. **One shared GPU, graceful arbitration.** Target GPU: RTX 2000 Ada (16GB). Requests queue; in-flight work drains before any swap; loading a different model group evicts the current one; when the queue empties, the last group stays resident with a **10-minute idle TTL**.
3. **Qwen3-TTS full circle.** All three 1.7B variants — Base (3-second zero-shot cloning), CustomVoice (9 preset voices), VoiceDesign (text-described voices) — surfaced through the existing catalog, Voice Lab, Portal, and Android, as an **additive** provider alongside ElevenLabs/OpenAI/Gemini.
4. **Install-script delivery, production quality.** A fresh box runs `install.sh`, walks the wizard, and ends with a working local stack — no hand-provisioning. Works on GPU boxes (fast) and CPU boxes (slow, honestly labeled), per the fresh-box portable build gate.
5. **CU virtual displays.** Computer-use sessions get their own private virtual screen at the model's native resolution; the agent opens its own application windows there without ever intruding on the user's desktop. The user watches through a live-view panel in the Portal and the Android MVP. The physical desktop resolution is never touched (it already isn't — see §3).

## 2. Non-Goals

- **Chat LLMs on the box.** Big models stay on custom API servers (gemma-box, MS02) or cloud. Nothing here changes the `custom` provider.
- **The existing separate local TTS engines.** Kokoro/Speaches-TTS on LAN custom servers stays exactly as it is — a *separate* system, per Brandon. This plan does not touch the `local:` catalog group or its routing.
- **Factory image pre-baking.** `build-factory-image.sh` stays a stub; weights download at wizard time.
- **Qwen-Audio-3.0-TTS-Plus (API).** The July-2026 arena-topping model is API-only on Alibaba Cloud. Out of scope; could become a cloud provider later.

---

## 3. Decisions Locked (2026-07-20 brainstorm session)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Qwen3-TTS role | **Additive provider** — new catalog group next to ElevenLabs/OpenAI/Gemini; cloud keeps working |
| D2 | Routing precedence | **On-box is the wizard-time default recommendation** — installing the stack seeds persisted per-capability enable/precedence flags so STT/TTS/embeddings/rerank resolve on-box wholesale by default; an **explicit credentialed user pick (e.g. Brandon's ElevenLabs) is never overridden at runtime**. Custom API servers are for LLMs; cloud is the customer's explicit fallback choice |
| D3 | Qwen3-TTS variants | **All three 1.7B variants** (Base + CustomVoice + VoiceDesign), MS02 benchmark decides streaming size (G3) |
| D4 | GPU orchestrator | **llama-swap front door** — supervises all model servers, native drain/swap/TTL semantics |
| D5 | Embedding model | **Qwen3-Embedding-8B @ Q8_0** default on GPU boxes (4096-dim), eval-gated on MS02 (G1). *Amended 2026-07-20 (Brandon-ratified): G1 gates against the **gemini-embedding-2 incumbent** — the no-regression bar — not a 4B arm (unbuildable in-plan; adding 4B is the remediation step only if 8B fails the bar)* |
| D6 | CU display | **Own virtual screen per session** (Xvfb at model-native resolution); live view in Portal + Android; agent opens apps in its own display |
| D7 | GPU residency policy | **Two co-resident groups** — `audio` (whisper + Qwen TTS) and `retrieval` (embeddings + reranker); groups are mutually exclusive; 10-min idle TTL *(retrieval residency amended by D13)* |
| D8 | MS02 Step-0 reset scope *(answers Q11)* | ~~Snapshots only~~ **SUPERSEDED 2026-07-22: NO reset — keep the existing corpus.** Brandon opted to keep MS02's snapshot Volume/Fossils/Manifest in place (he added his own operator+brain to it) — it's a richer memory-retrieval test. The embedding cutover re-embeds the EXISTING corpus onto Qwen3-Embedding-8B (validates the full migration on a real large corpus), rather than starting empty. Task 10.11 (destructive reset) is skipped |
| D9 | Cross-group swap latency *(answers Q12)* | **~6–10s first interaction accepted** — two-exclusive-groups stands, no hybrid. Routing always follows the user's selection (UI or system config); never silently overridden |
| D10 | Voice requests during swaps *(answers Q13)* | **Queue through the swap** with a client "loading models…" affordance; NEVER silently switch provider; generous ceiling (~30s) then an honest error |
| D11 | CU live view *(answers Q14)* | **One shared live view, any BlackBox user may watch.** When a CU session is active, an **in-use flag/indicator** is shown to other users (Portal + Android). No per-operator gating *(indicator reframed by D14)* |
| D12 | STT streaming architecture *(answers Q15)* | **Design B: Orchestrator-level serialization** — everything runs through the BlackBox; phones view/interact through it. Streaming STT stays **WebSocket end-to-end** (client ↔ `/ws/stt` ↔ on-box Speaches `/v1/realtime`) for near-real-time latency |
| D13 | Sequential max-quality retrieval *(amends D7)* | **The retrieval group is NO LONGER co-resident.** Members swap WITHIN the group (llama-swap `swap: true`): the embedder loads for query/mint embeds, is evicted when the reranker is demanded, and vice versa. Freeing the whole card per model upgrades the reranker from Qwen3-Reranker-0.6B to **Qwen3-Reranker-8B @ Q8_0** (same "highest quality that fits in one shot" rule as the embedder; embedder stays Qwen3-Embedding-8B Q8_0). Accepted cost, stated honestly: **~6–12s of model-swap overhead on EVERY search** (embed → evict → load reranker; the next search reloads the embedder). Benefits: top-of-family rerank quality, and better reliability — one-model-at-a-time leaves **~5GB+ VRAM headroom** instead of the tight 11.5–13GB co-resident budget. The **0.6B reranker remains ONLY as the CPU-tier fallback**. The **audio group is UNCHANGED** (still co-resident whisper + qwen-tts, still exclusive vs retrieval) |
| D14 | Concurrent CU sessions, badge not lock *(amends D11)* | **Multiple CU virtual-display sessions may run simultaneously** (cap 3, RAM/CPU bound). The Portal/Android "in use" indicator is reframed as an **active-sessions badge/list** (e.g. "2 agents running — watch") driven by `GET /cu/sessions`, opening the live view. An exclusive **"desktop in use" warning appears ONLY for native-mode sessions** (real desktop, still serialized by `display_arbiter`). This amends D11's framing, not its substance (any user may watch; no per-operator gating) |

### Premise corrections discovered during recon (2026-07-20)

- **Qwen3-TTS open weights shipped 2026-01-22** (Apache-2.0), not today. Family max is **1.7B (~4GB BF16 checkpoint at rest)** — there is no bigger open variant. Today's news was the API-only Qwen-Audio-3.0-TTS-Plus. Companion **Qwen3-ASR** (0.6B/1.7B, 52 languages, Apache-2.0, released 2026-01-29) exists as a future whisper alternative.
- **Embeddings/reranker already run on THIS box's Ollama** (`127.0.0.1:11434`) — but the *active* embedding model is **gemini-embedding-2 (cloud)** and the *active* reranker is **Vertex (cloud)**. "Wholesale local" therefore means a **full re-embed migration** of the ~8,164-snapshot corpus, using the existing `POST /embeddings/reembed` machinery.
- **The network box (gemma-box, 192.168.1.50)** hosts chat LLMs + Speaches audio (faster-whisper-large-v3-turbo + Kokoro) as a custom server. That's the "whisper across the network" being brought on-box.
- **CU already never changes the desktop resolution** — native mode downscales screenshots in software (PIL → 1280×720) and scales coordinates back. The real gap is that CU *takes over* the physical desktop; D6 fixes that.

---

## 4. Architecture Overview

```
                      ┌────────────────────────────────────────────────┐
                      │  blackbox-models.service  (llama-swap, :9098)  │
 Orchestrator ──────► │                                                │
  (per-capability     │  group "retrieval"  swap:true — one at a time  │
   clients, on-box    │    • embed-qwen3-8b   Q8_0  (~9.5–11GB alone)  │
   wins when healthy) │    • rerank-qwen3-8b  Q8_0  (~10–11GB alone)   │
                      │                                                │
                      │  group "audio"     (co-resident, ~7–12GB peak) │
                      │    • speaches         faster-whisper STT       │
                      │    • qwen-tts         our FastAPI server       │
                      │                       (Base/CustomVoice/Design)│
                      │                                                │
                      │  groups mutually exclusive · drain-then-swap   │
                      │  ttl: 600s idle · queueing built in            │
                      └────────────────────────────────────────────────┘
```

- **One systemd unit** (`blackbox-models.service`) runs the llama-swap binary with a generated `config.yaml`. llama-swap spawns/kills the member servers on demand — they are *not* independent systemd units.
- **llama-swap gives us the required semantics natively** (all confirmed against docs, v240 2026-07-15): arbitrary `cmd` upstreams ("if you can run it on the CLI…"), proxying of `/v1/embeddings`, `/v1/rerank`, `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/audio/voices`, drain-before-swap, request queueing across swaps, `groups` with `swap`/`exclusive`/`persistent`, `ttl: 600`, `checkEndpoint` readiness, `/upstream/:model` passthrough, `/running` + `/health` for status.
- **Port map:** `9098` llama-swap front door (new; infra range next to 9091/9093/9097). Member servers get llama-swap-assigned `${PORT}` on 127.0.0.1. Legacy contract port `8091` (vllm-reranker) is superseded but kept working via config (§5.3).
- **VRAM budget (16GB) — itemized, budgeted at PEAK:** *Retrieval group — sequential (D13, `swap: true`), ONE member resident at a time:* the embedder alone ≈ Q8_0 weights 8.05 + embed KV cache 1.13 (@ ctx 8192; Qwen3-8B = 36L × 8 KV heads × 128 hd) + non-causal embed compute buffer (large at ub=8192; bound with `-fa`, **measured in G1**) ≈ **~9.5–11GB peak alone**; OR the **Qwen3-Reranker-8B @ Q8_0** alone ≈ ~8.1 weights + rerank KV/compute ≈ **~10–11GB peak alone** — **never both** (they swap within the group), so a SINGLE CUDA context, not two. Sequential residency frees the whole card per model and leaves **~5GB+ headroom** instead of the old tight ~3GB co-resident budget — the reliability gain that motivated D13. Accepted cost: ~6–12s of intra-group model-swap on every search (§5.2/§6). *Audio group* — at most ONE whisper resident (~2.5GB turbo fp16; **1.6GB is the int8 figure**) + ONE resident Qwen variant (~4–5GB BF16 steady-state = ~4GB weights + activation), free-before-load so a variant transition never holds two ≈ **~7–12GB at peak** (§5.4) — this group stays **co-resident (unchanged by D13)**. Retrieval and audio never co-load (exclusive groups), and within retrieval the two 8B members never co-load (D13 swap), so the largest single footprint the card ever holds is ~10–12GB, never the old ~20–25GB "all four" sum — the exclusive-groups design stands. NB: today's MS02 "≈10.5GB pinned" (6994 MiB Ollama **Q4** embed + 3284 MiB vLLM reservation = 10,278 MiB) is a *different* topology from the planned Q8_0 llama-server budget — the two ~10.5GB figures are coincidental; G1 measures the real Q8_0 steady-state and peak footprint before Q8_0 locks as default.
- **CPU-only boxes run the identical topology** with CPU builds and smaller models (§7). Same front door, same routing, same wizard — just different tier defaults. This keeps one code path for the fresh-box gate.

### Orchestrator-side integration

A new thin module `Orchestrator/local_stack.py` (name TBD) is the single source of truth for:
- `is_installed()` / `is_healthy()` — llama-swap reachable + configured members present. **These key on install + config + process-liveness of llama-swap itself, NOT on live per-member VRAM residency** — so a normal group swap (the demanded group's members transiently down) never flips a capability to cloud; llama-swap's request-queueing transparently absorbs the swap and a mid-swap request waits rather than routing out. Routing decisions are config/install-state, never turn-to-turn health flapping.
- `base_url()` — `http://127.0.0.1:9098/v1` (config: `[local_models] base_url`).
- Per-capability resolution used by every consumer: an **explicit credentialed user pick always wins**; absent one, the wizard-seeded default resolves **on-box (new `localstack`/`onbox` token, ranked above the custom-server `local`) → existing custom-server audio (the separate Kokoro system, unchanged) → cloud per wizard choice** (D2, §5.5).
- `GET /local-models/status` — aggregates llama-swap `/running`+`/health`, hardware tier, disk, per-model download state, and the routing decision per capability. Consumed by the wizard step and the Updates panel (status-only, per the panels convention).

---

## 5. Component Specs

### 5.1 Embeddings (Qwen3-Embedding-8B @ Q8_0)

- **New provider key** in `Orchestrator/embeddings/providers.py`: `localstack` — OpenAI-compatible `POST {base_url}/embeddings` against llama-swap (bearer not needed on loopback). Sits beside `gemini`/`openai`/`ollama`. **It is a net-new provider class** — it cannot subclass/reuse `OpenAIProvider` (which hardcodes `OPENAI_API_KEY` and has no `base_url`).
- **New registry entries** (`Orchestrator/embeddings/registry.py`): `qwen3-embedding-8b-local` (provider `localstack`, model id `embed-qwen3-8b`, 4096-dim) and `qwen3-embedding-0.6b-local` (1024-dim, CPU-tier default). Each slug must declare **all four guard-tested fields** (registry.py:20-33): `tokenizer` (e.g. `hf:qwen3` — mandatory, omitted in v1), `max_input_tokens` (8192 for the 8B), `semantic_threshold`, and `junk_floor`. Thresholds start from the existing Ollama Qwen entries and are **recalibrated during G1** — per-model thresholds are mandatory (memory: per-model calibration discipline).
- **Upstream:** llama-server with the **official `Qwen/Qwen3-Embedding-8B-GGUF` at Q8_0** (8.05GB file confirmed to exist — no self-conversion needed for embeddings; that contingency was dropped). Qwen3-Embedding needs **last-token pooling** — confirm the pinned llama.cpp auto-detects it from GGUF metadata, else set `--pooling last` explicitly. **Context/batch must be set explicitly:** llama.cpp non-causal (pooling) embeddings require `n_ubatch ≥ the full input sequence`, and `chunks_for_snapshot` prepends the WHOLE snapshot as the ordinal-0 vector (clamped ~7.4k tokens, p99 ~7k), so `-b`/`-ub` are effectively **forced to ~7400–8192** — a too-small ubatch hard-CRASHES the re-embed, it does not truncate. Set `-c 8192 -b 8192 -ub 8192 -fa` (flash-attention bounds the non-causal compute buffer) and **measure that compute-buffer VRAM in G1** (fold into the §4 budget). Do NOT naively drop `max_input_tokens` to 512–1024 — that collapses the ordinal-0 whole-doc vector into a single chunk and degrades the `max(whole, chunks)` scoring unless it is a deliberate, documented policy change.
- **Migration:** wizard-driven `POST /embeddings/reembed {target: qwen3-embedding-8b-local}` — existing schema-2 chunked build + atomic swap. ~36K rows through a GPU-served 8B is fast; wizard shows the existing progress UI. **Cache-coherence step:** `ToolVault/embeddings.json` (tool-selection cache) and `code_embeddings.json` are keyed by the active embedding slug and self-invalidate only *lazily* on next sync/query — so **immediately after the corpus cutover the migration checklist must fire `POST /toolvault/reload` and rebuild code-embeddings**, otherwise the first hot-path query embeds at 4096-dim while the caches still hold 3072-dim vectors (on-demand re-embeds + a transient dim-mismatch window).
- **Watcher/status compatibility:** `_model_preflight` blockers, `watcher.py` catalog checks, and `ollama_io` assumptions are Ollama-shaped; the `localstack` provider needs its own blocker strings ("local stack not installed / model not downloaded / llama-swap down") and health probe (embed-one-string through :9098).
- **Ollama's future:** stays installed (step 1d) and registry-supported for existing boxes; new GPU boxes never activate it for embeddings. Formal deprecation is an open question (Q7).

### 5.2 Reranker (Qwen3-Reranker-8B @ Q8_0 via llama-server)

- **Primary:** llama-server `--reranking --pooling rank` serving a **correctly-converted** Qwen3-Reranker-8B GGUF **@ Q8_0**, exposed as `/v1/rerank` through llama-swap. New `RERANK_MODELS` entry `qwen3-reranker-8b-local` (provider `localstack`). **This is the D13 upgrade:** because the retrieval group is now sequential (`swap: true`, §8), the reranker gets the whole card, so we run the best-that-fits-in-one-shot 8B (the 0.6B drops to CPU-tier fallback only, §7). **Wire shape is net-new:** llama.cpp `/v1/rerank` takes `{query, documents}` → `{results:[{index, relevance_score}]}`, which is NOT the vLLM `/score` `{text_1, text_2:[…]}` → `{data:[{index, score}]}` shape — so add a `_score_localstack` (or reuse `_scatter_relevance_scores`, rerank.py:697-724, which already parses `results:[{index, relevance_score}]`) plus `KNOWN_PROVIDERS` and `score()`-dispatch entries. The mandatory `query_instruction` prefix carries over verbatim, prepended to the `query` field (ranker inverts without it — measured 2026-07-03).
- **⚠ Known trap:** community reranker GGUFs are frequently broken (missing `cls.output.weight` → near-zero ~1e-28 scores). We convert ourselves with a **llama.cpp build pinned post-dating the `convert_hf_to_gguf.py` Qwen3-Reranker fix** (detects Qwen3-Reranker, extracts `cls.output.weight`, sets `pooling_type=RANK` + classifier labels; llama.cpp #16407 resolved). **G2 gates on score validity** against HF-reference scores on golden query/passage pairs — optionally cross-checked against a known-good pre-converted GGUF — before this provider can be selected.
- **Why 8B, not 0.6B (D13 — sequential frees the card):** the retrieval group is now sequential (`swap: true`, §8), so only ONE model is resident at a time and the reranker gets the whole card — we therefore run the **best that fits in one shot, Qwen3-Reranker-8B @ Q8_0**, exactly the embedder's "highest quality that fits" rule. The **accepted cost is the ~6–12s intra-group model-swap on every search** (embed → evict → load reranker; the next search reloads the embedder). Reliability improves too: one-model-at-a-time leaves ~5GB+ VRAM headroom vs the old tight 11.5–13GB co-resident budget. The rerank **scoring latency is now a per-scoring-call *target once the model is loaded*** (candidate_n=40 passages per query) — a G2 target, not an established fact; **G2 also measures the per-search swap overhead (~6–12s) separately**. The **0.6B reranker is kept ONLY as the CPU-tier fallback** (§7). The vLLM `/score` seam remains the FP16 alternative if the 8B misses.
- **Swap cost (honest numbers) — two kinds now:** *(a) Cross-group:* the audio→retrieval swap cold-loads the 8B embedder first (~3–5s warm page-cache on MS02's 125GB RAM, ~6–8s cold, plus PCIe + CUDA init) for the query embed. **First search after a voice turn ≈ 6–10s to first embed; first voice turn after a search ≈ 5–8s.** *(b) Intra-group (NEW under D13):* every search then pays a second swap inside the retrieval group — the embedder is evicted and the **Qwen3-Reranker-8B** loads to score, **~6–12s per search**. keep-warm (`ttl: 0`) gives ZERO relief for either: it cannot keep both groups resident, and by design **cannot keep both retrieval members resident** — it only defeats idle-unload of whichever single member is currently loaded (§6). If these stalls are unacceptable the real levers are a hybrid (one capability stays cloud/CPU so both groups live), a client-side "loading models…" affordance, or a smaller resident model — decided with Brandon (Q12); **G5 measures both the cross-group and the intra-group swap**.
- **vLLM seam:** the existing dark `/score` seam (port 8091, `vllm-reranker.service` template) is kept as the FP16 quality alternative — installable but not default (vLLM cold-load 15–25s makes it a poor swap citizen). **Hard rule:** the vLLM `/score` seam may **never co-run on the same GPU as the llama-swap retrieval group** — it is invisible to llama-swap's budgeting and pre-allocates ~90% of VRAM via `gpu_memory_utilization`, so the two together guarantee an OOM. Enabling one requires the other be down. The M11 activation memory gets superseded by this design.
- **Selection stays sidecar-driven:** `POST /rerank/select` writes `Manifest/embeddings/rerank.json` exactly as today; the wizard flips it after G2 passes.

### 5.3 STT (Speaches on-box: faster-whisper)

- **Upstream:** Speaches (active project, CPU compose exists) as a llama-swap member in the `audio` group. **Pin a specific Speaches version** (latest is pre-1.0, v0.9.0-rc.3 2025-12-27 — note the rc-maturity supply-chain risk; the `/v1/realtime` event shape is thinly documented, so capture the actual event schema from the running server during G4/G6). Models: `deepdml/faster-whisper-large-v3-turbo-ct2` (streaming default — same model the gemma-box serves today, so behavior parity) + `Systran/faster-whisper-large-v3` (batch-quality option). **Constraint — at most ONE whisper model resident:** Speaches holds each model warm under its own `stt_model_ttl` with no concurrency cap, so a batch (large-v3, ~4.5GB) and a streaming (turbo, ~2.5GB) session inside the TTL window would BOTH be resident (invisible to llama-swap's process-level budget). Prefer serving both streaming+batch from `large-v3-turbo`, or force-unload the batch model around each batch request — a short `stt_model_ttl` alone does not guarantee single residency. Harmonize `stt_model_ttl` with llama-swap's ttl (Speaches manages *model* residency inside the process; llama-swap manages the *process*).
- **Distinct on-box STT token (routing fix):** the existing `local` STT token already means *custom-server audio* (`has_audio` via `custom_models.json`), so it can never reach the on-box `:9098` Speaches member — and on a fresh on-box-only box (no custom server registered, exactly the §10 state) `local_stt_available()` is False, so on-box STT would be unreachable and the "all four local" acceptance would fail. Introduce a **distinct on-box token** (`localstack`/`onbox`) ranked ABOVE the custom-server `local` in `resolve_stt_provider`'s ordered avail dict, with an on-box availability signal independent of the custom-server registry (`local_stack.is_healthy()`), plumbed through `resolve_stt_provider`, `run_stt_bridge`, the streaming `_local_bridge` (resolve the on-box `base_url` first) and batch `_local_transcribe`. `local`/`resolve_audio` (custom-server) semantics stay unchanged.
- **Batch path:** `file_transcribe._local_transcribe` resolves the **on-box stack first via the distinct on-box token** (above), then the existing custom-server audio, then cloud. Same OpenAI-compatible `POST /audio/transcriptions` shape, just a different base URL (`:9098`).
- **Streaming path (`/ws/stt` `_local_bridge`):** connects to Speaches `/v1/realtime` (WebSocket). **Design A (proxy WS through llama-swap) is effectively DEAD on current llama-swap** — WebSocket proxying is a *known-missing* feature (open FR mostlygeek/llama-swap#754), not merely undocumented; don't burn G6 rediscovering it. **Design B (direct-to-member port) ships, but NOT via "keepalive claims":** pinging `/upstream/speaches` resets only the *idle* TTL — it does NOT block an exclusive cross-group swap, and a direct-to-port WS stream is invisible to llama-swap's in-flight drain counter. So the group must be protected at the **Orchestrator level by SERIALIZATION:** while a local voice stream is open, the Orchestrator does not dispatch retrieval-group requests to `:9098`, sequencing them into the STT-finalize → retrieve → TTS gap (the Orchestrator owns both sides). G6 is repurposed to fire a retrieval/embedding request *while* a voice stream is mid-utterance and assert no cut-off. Direction **DECIDED 2026-07-20 (D12): Orchestrator serialization ships**; the STT path stays WebSocket end-to-end (client ↔ `/ws/stt` ↔ on-box Speaches `/v1/realtime`) for near-real-time latency — no investment in patching #754.
- The existing bridge quirks carry over unchanged (24kHz resample, trailing-silence stop instead of explicit commit, per-utterance finals, hallucination filter, `stt_done` terminal frame).
- **Tool schema fix (recon find):** `speech_to_text` ToolVault schema's provider enum omits `local` — add `local` **and the new on-box token** to the enum.

### 5.4 TTS (Qwen3-TTS server — ours, in-repo)

- **We write a thin FastAPI server** (e.g. `LocalModels/qwen_tts_server/`, own lean venv) — production-quality control over all three variants, consent-gated cloning, and profile persistence. **Streaming is NOT a reason to prefer the official `qwen-tts` package** — its high-level `generate_custom_voice`/`generate_voice_clone`/`generate_voice_design` each return a *complete* `(wavs, sr)` tuple (`non_streaming_mode=False` only simulates streaming over complete text and still returns a full tuple). True chunked yield must come from a lower-level KV-cache streamer — **source it from a dedicated fork** (`kunzite-app/Qwen3-TTS-streaming`'s `stream_generate_pcm()`, `rekuenkdr`, `andimarafioti/faster-qwen3-tts`) or vLLM-Omni, NOT the high-level package and NOT the groxaxo/cornball batch wrappers (those are preset/clone references only).
- **One llama-swap member (`qwen-tts`), one process, three variants managed in-process:** the server lazy-loads the variant a request needs (CustomVoice for presets — the hot path; Base for cloned-voice synthesis; VoiceDesign for design) and drops the previous one. Loading all three at once (~12GB+) would blow the audio group's budget. **FREE-BEFORE-LOAD is mandatory:** a naive load-then-drop transiently holds old+new (~5–6GB each at the mid-swap activation peak) + whisper → up to ~16–18GB, a plausible OOM on the 16,380 MiB card — llama-swap budgets at the *process* level and cannot see this intra-process balloon. The server must drop refs → `gc.collect()` → `torch.cuda.empty_cache()` → **verify free VRAM** before allocating the next variant, keeping llama-swap's view simple (one member, one port). Define the process concurrency policy explicitly (serialize batch vs streaming synthesis, or co-resident) so two variants never load concurrently.
- **Endpoints (OpenAI-compatible where a convention exists):**
  - `GET /health` — llama-swap `checkEndpoint` (startup readiness only; see §6).
  - `POST /v1/audio/speech` — `{model, input, voice, response_format, stream}`. **Body-`model` auto-routed** by llama-swap (a known OpenAI path). `stream:true` chunk-as-generated is a **G3-gated spike**, not a settled primitive (§5.4 rationale above); the accepted Q1/Q2 fallback is a **`StreamingResponse` over a full generation** (12Hz token frames — the tokenizer is officially `Qwen3-TTS-Tokenizer-12Hz`, not 12.5Hz). Read the output sample rate from the model's returned `sr` at runtime (do NOT hardcode 24kHz — confirm empirically in G3 and adjust browser-playback/resample). Base-clone streaming needs ~3s initial token buffering to avoid drift.
  - `GET /v1/audio/voices` — presets + saved clone/design profiles (a known OpenAI path — body-model auto-routed).
  - `POST /v1/voices/clone` — reference audio (~3s min) + name; **requires the literal consent flag**, mirroring the ElevenLabs gate exactly (422 without it; ToolVault wrapper requires `confirm_consent=true`). **Routing:** clone/design are NON-OpenAI paths that llama-swap does NOT auto-route (it extracts `model` only from known endpoints — open #245), so the Orchestrator calls them through **`/upstream/qwen-tts/v1/voices/…`** (auto-loads the member, honors group swap/exclusivity).
  - `POST /v1/voices/design` + `/v1/voices/design/save` — 2-step preview→save, mirroring ElevenLabs design UX; routed through `/upstream/qwen-tts/…` like clone.
- **Voice profiles** persist under `Manifest/voices/qwen/{slug}/` — `profile.json` (name, variant, operator, consent record, created) + reference audio / design params. Survives restarts; never in git.
- **Catalog:** new dynamic group in `GET /tts/catalog`: `{id:'qwen', label:'Qwen3-TTS (On-Box)', dynamic:true}` — voice ids `qwen:<Voice>` (9 presets: Vivian, Serena, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee) plus `qwen:<profile-slug>` for clones/designs, star-prefixed like ElevenLabs My-Voices. Present only when the local stack is healthy (fail-open like the other dynamic groups). **`local:` (Kokoro) remains untouched** (non-goal).
- **Synthesis routing:** `POST /tts` and `/tts/batch` each gain a `qwen:`-prefix branch (same pattern as the `elevenlabs:`/`local:` branches — **neither route has one today**; `tts_routes.py` handles only openai/gemini/elevenlabs/local); `sanitize_for_speech` applies as today. Streaming browser playback mirrors the ElevenLabs `StreamingResponse` path.
- **Frontends (three-surfaces rule):** Portal voice picker picks the group up automatically from the catalog. **Android prerequisite fix (recon find):** `TtsRepository.generateWithVoice` hardcodes provider `openai` in its else-branch — a selected `local:`/`qwen:` voice would 400. Fix: pass the parsed provider through generically instead of enumerating branches.
- **Voice Lab:** a Qwen tab beside ElevenLabs/xAI — clone (consent gate), design (preview→save), manage/delete profiles. Gated on local-stack health instead of an API key.
- **Speed reality (G3):** RTX 3090 measures RTF 0.83–0.97 on the 1.7B; the **2000 Ada is only ~0.24–0.34× a 3090** (FP32/bandwidth/cores), so its extrapolated 1.7B RTF ≈ 2.4–4.0× realtime — **1.7B streaming will near-certainly FAIL the G3 <0.9 gate**, not merely "may be sub-realtime". Planning-time expectation: **the 2000 Ada streaming default is 0.6B-CustomVoice; 1.7B is the batch/file quality tier.** Pre-wire the wizard copy/default for that likely outcome so it is not a surprise. G3 remains the measurement; the ~97ms first-packet figure is an A100+FlashAttention *paper* number — treat as to-be-measured, not a target.

### 5.5 Routing & precedence (D2, all capabilities)

For each of STT / TTS / embeddings / rerank the resolver is **seeded, not hard-wired**:
1. **Installing the on-box stack seeds a persisted per-capability enable/precedence flag** so the capability *resolves on-box by default* — this is the **wizard-time default recommendation**, not a runtime override. An **explicit credentialed user pick is NEVER overridden at runtime** (e.g. Brandon's recorded "ElevenLabs for STT AND TTS" keeps winning); this preserves the wholesale-local *intent* (D2) via a persisted seed rather than an implicit runtime takeover, and stays consistent with §8's "nothing activates implicitly on install".
2. On-box resolution uses a **distinct on-box token** (`localstack`/`onbox`) ranked ABOVE the existing custom-server `local`, with an availability signal independent of the custom-server registry (`local_stack.is_healthy()`). The existing `local`/`resolve_audio` custom-server semantics are unchanged.
3. The **separate** custom-server audio system (Kokoro `local:` group, gemma-box Speaches) keeps working exactly as today — it is a distinct provider the user can still pick explicitly.
4. **No local stack (or an explicit cloud preference) → cloud provider** chosen in the wizard, exactly as today.

Provider naming follows the provider-explicit convention: the catalog group is `qwen` (model-explicit), the embeddings/rerank provider key is `localstack` (infrastructure-explicit) — bikeshed welcome (Q8).

---

## 6. GPU Sharing Semantics & Edge Cases

- **Queue/drain/evict/TTL:** llama-swap defaults. `ttl: 600` per member. Requests for a member of the *other* group trigger: drain in-flight → stop current group's members → start requested member → serve queue. Same-group members co-load. Two source-verified facts underpin this (see §14): llama-swap's `checkEndpoint`/`healthCheckTimeout` is a **one-time STARTUP readiness gate, never re-probed on a running process** (so an in-process TTS variant load can never trigger a health-based SIGTERM mid-synthesis), and a chunked streaming response **counts as in-flight for its full duration *through the proxy*** (a cross-group swap queues behind a mid-stream request, never SIGTERMs it — but a *direct-to-port* stream is invisible to that counter, which is why Design-B STT needs Orchestrator serialization).
- **Live voice loop (the reason for D7):** a voice conversation is duplex — whisper listens while Qwen speaks. Both live in the `audio` group, co-resident; zero swaps inside a conversation.
- **Search (D13 — sequential retrieval):** every query performs **embed → evict → rerank-load → score**: the embedder produces the query vector, is then evicted, and the **Qwen3-Reranker-8B** loads to score the candidates (`swap: true` within the `retrieval` group). This is **one intra-group swap per search (~6–12s)** — the accepted cost of running top-of-family 8B rerank on a single card (§5.2). keep-warm (`ttl: 0`) now applies **per member** and by design **cannot keep both retrieval members resident**; it only defeats idle-unload of whichever member is currently loaded. Cross-group semantics (audio↔retrieval exclusivity) are unchanged.
- **Streaming sessions & in-flight accounting:** llama-swap's drain-before-swap waits for in-flight *proxied* requests to reach zero, and a chunked streaming RESPONSE stays in-flight for its FULL duration through the proxy (verified from source — see §14). BUT the STT stream ships as **Design B (direct-to-member WS)**, which is invisible to that counter, and WS proxying (Design A) is a known-missing llama-swap feature (#754). So an open local voice stream is protected by **Orchestrator-level serialization, not keepalive**: while a voice stream is open the Orchestrator holds retrieval-group dispatch. The real per-turn shape is **audio (listen) → [finalize] → retrieval (embed+rerank) → audio (speak)** — the Orchestrator sequences the retrieval work into the finalize→speak gap so the audio group is never force-swapped mid-utterance. A retrieval request that *does* arrive during an open utterance is held (not dropped) until that gap; auto-mint embeds during a long voice call degrade to vector-less rather than blocking (starvation policy below). This is the accepted trade-off of one GPU.
- **Concurrency & 429 contract:** llama-swap's per-process concurrency semaphore returns **HTTP 429** when it can't be acquired (default limit ~10). Define `concurrencyLimit` per member and the per-capability client contract on 429 — retry-with-backoff vs immediate cloud fallback vs a user "busy" message — with special care on the real-time voice paths, where queue latency has a hard human ceiling (a 429 mid-voice-turn must not surface as a raw error to Portal/Android; Q13). Assume a **single active user** (this is a personal box); add a per-group concurrency cap so an open mic that pins the audio group for a whole conversation cannot silently starve every search/auto-mint embed queued behind it.
- **Initial re-embed migration thrash:** the first corpus re-embed (~36K rows in 32-chunk batches) is thousands of short requests; interleaved audio requests would swap groups between batches and crawl. Mitigation: the wizard runs migration as a foreground step with a "voice features will be slow until the index finishes building" notice. Optionally pin the retrieval group for the duration — but **two caveats:** (a) llama-swap `--watch-config` **auto-restarts the whole proxy (unloads ALL members) on any config edit** — there is no in-place SIGHUP/endpoint reload (open #160/#547) — so the `persistent: true` flip does NOT keep the retrieval group warm across the toggle; batch config changes and design migration/wizard flows to tolerate a brief full-stack reload. (b) A pinned retrieval group means a concurrent voice call **cannot load the audio group** — so use a *time-boxed* pin WITH an explicit user-facing "voice temporarily uses cloud" message rather than a silent stall, and let auto-mint embeds during the window degrade to vector-less rather than blocking the mint. (Detail for the implementation plan.)
- **Keep-warm interplay (wizard checkbox — work with it, don't fight it):** today's embeddings keep-warm setting (`Manifest/embeddings/keep_alive.json` → Ollama `keep_alive`, e.g. `-1` pin; MS02 currently holds its 8B embedder resident this way at ~7GB). Under llama-swap, keep-warm maps to **`ttl: 0` on that member** (never idle-unloaded). Adopted semantics: a kept-warm model is **immune to the 10-min idle TTL but still yields to a cross-group swap** when the other group is demanded, then reloads on next use — anything stronger (`persistent: true`) would deadlock VRAM (>16GB). Under D13's sequential retrieval group, keep-warm is **per-member and cannot hold both the embedder and the 8B reranker resident** — each search swaps one for the other regardless. **Keep-warm therefore gives ZERO cross-group relief** — the first interaction after a mode switch still pays the full cold-load (~6–10s, dominated by the 8B embedder; §5.2, Q12). **Write-path caveat:** `store.set_keep_alive`/`set_placement` (store.py) raise `ValueError` for any provider ≠ `ollama`, so a `localstack` slug is rejected today — new plumbing must translate the keep-warm/placement toggles into llama-swap `ttl: 0` / group config (they cannot be reused unchanged). The wizard copy states exactly this. The legacy Ollama path keeps its current keep-warm meaning unchanged.
- **Crash/health & degradation invariant:** `GET /local-models/status` surfaces member state; the embeddings watcher's daily probe covers the localstack provider like any other. **Degradation is per-capability, and the embedding model is NEVER health-switched:** the active embedding model is set by a **wizard-time re-embed cutover ONLY** (the sole writer of `active.json`) — a health/crash-based active-slug switch would fragment the corpus across dim-incompatible stores (cloud gemini 3072-dim vs local 4096-dim), leaving half the corpus invisible to search. So if the stack dies: **STT/TTS** fall back per-request to the wizard-chosen cloud provider; **rerank** falls through to un-reranked retrieval (`score()` returns `None`, never raises — a dead reranker costs latency, never recall); **embeddings** degrade to vector-less mints + gap-heal on recovery, with **no model switch**. Routing/`is_healthy()` keys on install + config + process-liveness — NOT live per-member VRAM residency — so a normal group swap never routes to cloud (llama-swap queues the request through the swap).

---

## 7. Hardware Tiers & CPU Fallback

Tier detection reuses `Orchestrator/hardware.py` (`probe()`/`derive_tier`); **add a disk probe** (recon: none exists; the full GPU-tier weight set is ~28GB — gate downloads on ≥40GB free).

| Capability | GPU tier (≥8GB VRAM) | CPU tier (no GPU, ≥32GB RAM) | Floor tier |
|---|---|---|---|
| Embeddings | Qwen3-Embedding-**8B Q8_0** (4096d) | Qwen3-Embedding-**0.6B** GGUF (1024d) — fast on CPU | cloud choice |
| Rerank | Qwen3-Reranker-**8B Q8_0** (sequential with the embedder — D13) | 0.6B GGUF CPU (G2 latency-gates it; else existing CrossEncoder path / cloud) | cloud choice |
| STT stream | whisper **large-v3-turbo** fp16 | large-v3-turbo **int8** — *near-realtime for batch; low-latency streaming may lag*; `distil`/`small` on weak CPUs — wizard shows measured RTF (verify streaming, not just batch) | cloud choice |
| STT batch | whisper **large-v3** fp16 | large-v3 int8 (slow, labeled) | cloud choice |
| TTS | Qwen3-TTS: **0.6B-CustomVoice streaming default on the 2000 Ada, 1.7B batch/file quality** (G3 confirms — 1.7B streaming near-certainly fails <0.9 RTF) | **Off by default** — CPU Qwen is far sub-realtime; offered as "experimental (slow)" opt-in (`qwentts.cpp` GGUF is the escape hatch), cloud recommended | cloud choice |

Principles: local is the *default* only where the experience is honest (per Brandon: "extremely slow" is acceptable **as an informed choice**, never a silent default). The wizard shows per-capability estimates from the tier probe before the user commits. Same llama-swap topology on every tier — only members/builds differ.

---

## 8. Install & Onboarding

### install.sh (follows the proven Step-2d gated-template pattern)

New **Step 2f — `installer/templates/blackbox-install-localstack.sh`** (non-fatal, self-gating, re-run-safe):
1. Download **llama-swap** release binary, sha256-pinned (same pattern as zellij, version pinned in `installer/templates/`).
2. Download **llama.cpp `llama-server`** prebuilt (CUDA build behind the `nvidia-smi` gate, CPU build otherwise).
3. Create lean venvs: **Speaches** and **our qwen-tts server** (own venvs — the MCP lean-venv lesson applies).
4. Write `llama-swap config.yaml` from **`installer/templates/llama-swap-config.yaml.template`** (the full design template is below), tier-adjusted members (§7). The service runs with `--watch-config`, which **auto-restarts the whole proxy on any config edit** (§6) — treat every config write as a brief full-stack reload.
5. Install `blackbox-models.service` via `blackbox-write-systemd` — **requires a new `models-unit` `target_kind`** in `blackbox-write-systemd.sh`'s whitelist (hardcoded dest `/etc/systemd/system/blackbox-models.service`) and a **`systemctl restart blackbox-models.service`** line in `sudoers-blackbox-system` (only `daemon-reload` is covered today). The Phase-2 Step-0 reset additionally needs grants to **stop `vllm-reranker.service` and `ollama`** (§10) — add them to the sudoers policy or mark them explicit interactive-sudo manual steps. `/etc` writes happen install-time as root; the wizard never writes `/etc` (ProtectSystem=strict).
6. **No weights at install time.** Weights download in the wizard (below) — keeps install fast and disk-gated.
7. **CU virtual-display system packages:** add `xvfb`, `websockify`, and `novnc` (if not vendored) to `Scripts/onboarding/system-packages.txt` — the apt dispatcher (`installer/templates/blackbox-apt-install.sh`) refuses any package not in that allowlist, so without them a fresh box cannot install the CU framebuffer. `xvfb` is MUST_HAVE (CU virtual displays gate on it, §9); `websockify`/`novnc` are SHOULD_HAVE (live view). `xdotool`/`scrot`/`openbox`/`x11vnc` are already listed.

**llama-swap `config.yaml` — DESIGN TEMPLATE** (the heart of D4; to be landed verbatim as `installer/templates/llama-swap-config.yaml.template` and shell-substituted at install time. Within a group `swap: false` = members CO-RESIDENT (the **audio** group); `swap: true` = members swap within the group, one resident at a time (the **retrieval** group — D13); across groups `exclusive: true` = loading one group unloads the other; keep-warm maps to `ttl: 0` per member):

```yaml
# installer/templates/llama-swap-config.yaml.template — DESIGN TEMPLATE
# Landed by install.sh Step 2f, tier-adjusted (§7). ${...} are shell-substituted
# at write time; ${PORT} is llama-swap's OWN per-member assigned loopback port
# (left literal for llama-swap to fill). One binary (blackbox-models.service)
# supervises all four members, each bound to 127.0.0.1.

healthCheckTimeout: 120        # global default (seconds); per-member overrides below
logLevel: info

macros:
  llama-server: "${LOCALSTACK_BIN}/llama-server"
  models-dir:   "${LOCALSTACK_MODELS}"

models:
  # ── retrieval group ────────────────────────────────────────────────
  "embed-qwen3-8b":
    # Official Qwen/Qwen3-Embedding-8B-GGUF @ Q8_0 (8.05GB). Last-token pooling.
    # -b/-ub forced to the full input seq (non-causal pooling); -fa bounds the
    # compute buffer. --pooling last only if the build doesn't auto-detect it.
    cmd: |
      ${llama-server}
      --model ${models-dir}/Qwen3-Embedding-8B-Q8_0.gguf
      --host 127.0.0.1 --port ${PORT}
      --embeddings --pooling last
      -c 8192 -b 8192 -ub 8192 -fa
      -ngl 99 --no-warmup
    proxy: "http://127.0.0.1:${PORT}"
    checkEndpoint: "/health"
    healthCheckTimeout: 300      # 8GB weights load is slow — long startup gate
    ttl: 600                     # keep-warm ⇒ set ttl: 0 (immune to idle unload,
                                 #   still yields to a cross-group swap; §6)
    concurrencyLimit: 4

  "rerank-qwen3-8b":
    # SELF-CONVERTED GGUF (Qwen3-Reranker-8B @ Q8_0) from a llama.cpp build
    # post-dating the convert_hf_to_gguf.py fix (extracts cls.output.weight,
    # pooling_type=RANK). G2 gates score validity before this member can be
    # selected. D13: retrieval is sequential (swap: true) — this member loads
    # AFTER the embedder is evicted, a ~6–12s per-search swap (accepted cost).
    cmd: |
      ${llama-server}
      --model ${models-dir}/Qwen3-Reranker-8B-Q8_0.gguf
      --host 127.0.0.1 --port ${PORT}
      --reranking --pooling rank
      -c 8192 -ngl 99 --no-warmup
    proxy: "http://127.0.0.1:${PORT}"
    checkEndpoint: "/health"
    healthCheckTimeout: 300      # ~8GB Q8_0 weights load — long startup gate (D13)
    ttl: 600
    concurrencyLimit: 2

  # ── audio group ────────────────────────────────────────────────────
  "speaches":
    # Pinned Speaches version (pre-1.0; capture /v1/realtime schema in G4/G6).
    # At most ONE whisper model resident (§5.3). Streaming via Design B
    # (direct-to-${PORT}); llama-swap WS proxy (#754) is unavailable.
    cmd: |
      ${SPEACHES_VENV}/bin/uvicorn --factory speaches.main:create_app
      --host 127.0.0.1 --port ${PORT}
    proxy: "http://127.0.0.1:${PORT}"
    checkEndpoint: "/health"
    healthCheckTimeout: 120
    ttl: 600
    concurrencyLimit: 4

  "qwen-tts":
    # Our in-repo FastAPI server (LocalModels/qwen_tts_server). Three variants
    # managed in-process with FREE-BEFORE-LOAD (§5.4). Clone/design routed via
    # /upstream/qwen-tts/... (not body-model auto-routed); speech/voices are.
    cmd: |
      ${QWEN_TTS_VENV}/bin/uvicorn qwen_tts_server.app:app
      --host 127.0.0.1 --port ${PORT}
    proxy: "http://127.0.0.1:${PORT}"
    checkEndpoint: "/health"
    healthCheckTimeout: 180      # first variant load
    ttl: 600
    concurrencyLimit: 2

groups:
  # persistent:false on BOTH groups (persistent:true on both would deadlock
  # VRAM: >16GB). ttl:600 idle-unload applies per member within a live group.
  "retrieval":
    swap: true                   # D13: members swap within the group — embed and rerank are never co-resident
    exclusive: true              # loading retrieval unloads the audio group
    persistent: false
    members:
      - "embed-qwen3-8b"
      - "rerank-qwen3-8b"
  "audio":
    swap: false                  # whisper + qwen-tts CO-RESIDENT
    exclusive: true              # loading audio unloads the retrieval group
    persistent: false
    members:
      - "speaches"
      - "qwen-tts"
```

### Onboarding wizard

New step **`local_models`** (state.py `ALL_STEPS` + Portal `steps/local_models.js`, patterned on the embeddings step):
- Shows hardware tier, disk headroom, per-capability recommendation from §7.
- **One-click downloads** with streamed progress — new `POST /local-models/download` (HF CDN / llama-swap-triggered pulls), cloned from the `/embeddings/ollama/pull` NDJSON-progress pattern.
- **Activation is a deliberate flip per capability:** embeddings → `/embeddings/reembed` (with migration notice, §6); rerank → `/rerank/select` after G2; STT/TTS → precedence flags in `[local_models]` config. Nothing activates implicitly on install.
- Existing `transcription` step gains an "On-box (local)" option; embeddings step lists the localstack models. (Exact step-vs-step split: Q6.)

Config: new `[local_models]` section in config.ini — `enabled`, `base_url` (default `http://127.0.0.1:9098/v1`), per-capability enables. Runtime state (downloads, profiles) under `Manifest/`, secrets none (loopback, keyless).

---

## 9. CU Virtual Displays (per-session, live-viewed)

**Goal (D6):** every CU session gets a private virtual screen where the agent opens any application it needs, at the model's native resolution, never touching the user's desktop. The user watches from a live-view panel in the Portal and Android MVP. "Act on my real desktop" remains an explicit mode.

- **Technology: per-session Xvfb (X11).** The recon nailed the constraint: on Wayland, ydotool injects into the *physical seat* and XDG-Portal screenshots capture the *real compositor* — neither is display-addressable. The X11 trio (Xvfb, xdotool, scrot) all honor `DISPLAY`, and the existing sandbox path (`browser/display.py`) already proves the pattern (Xvfb + openbox + x11vnc). We generalize it from a singleton to an allocator. **Install gate:** `xvfb`, `websockify`, and `novnc` must be added to `Scripts/onboarding/system-packages.txt` (the apt dispatcher refuses any package off that allowlist; `xdotool`/`scrot`/`openbox`/`x11vnc` are already there but `xvfb`/`websockify`/`novnc` are NOT) — **CU virtual displays gate on `xvfb` being present** (MUST_HAVE); live view gates on `websockify`/`novnc` (SHOULD_HAVE).
- **DisplayAllocator — a start/stop REWRITE, not a parameter tweak.** The Xvfb/openbox/x11vnc spawner is **`browser/display.py`'s `VirtualDisplay`** (not `display_arbiter.py`, which is pure in-memory mutex bookkeeping and spawns nothing — the v1 "evolves `display_arbiter.py`" attribution was wrong). Target `VirtualDisplay` for the per-session lifecycle; keep `display_arbiter.py` scoped to **native-mode mutual exclusion only**. `VirtualDisplay`'s current helpers use GLOBAL process patterns that break multi-session correctness (a §9 acceptance criterion): `pkill -f x11vnc` (kills ALL sessions), `pgrep -f x11vnc`/`pgrep -f openbox` (return True on *any* session), hardcoded `-rfbport 5900` (and `display.py:97`'s `pkill openbox` is already a dead no-op — `DISPLAY` is passed via env, not argv). The rewrite: allocate display `:100+n` + `Xvfb :N -screen 0 WxH x24` + `openbox`; **track per-session PIDs and do all liveness/teardown by pid** (no global pkill/pgrep); assign **per-session `x11vnc -rfbport 5901+n` bound to 127.0.0.1**; reap orphans by pid at **boot** and on **TTL**. Drop the per-session dbus (the working virtual path uses none) unless a bootstrapped app requires it.
- **Per-backend native resolution — scaling deleted, not improved:** Anthropic/OpenAI sessions get 1280×720 Xvfb (scale 1.0, pixel-exact coordinates, no LANCZOS blur — the sandbox path already demonstrates unscaled coords); Gemini gets 1440×900 (its recommended resolution, currently an unused constant) with its 0–999 normalized mapping.
- **Input/capture inside the session:** xdotool + scrot with `DISPLAY=:N` — no ydotool, no Portal screenshots, no Wayland involvement. Chrome launches inside with `--window-size` matched. Any X11 app works; GNOME/Wayland-only apps are a documented limitation of virtual sessions (use native mode for those).
- **Live view:** noVNC/websockify per session, reverse-proxied by the Orchestrator (`/cu/view/{session_id}` WS). Auth = the Tailscale perimeter (by design, no app-layer auth) — **DECIDED 2026-07-20 (D11): one shared live view, any BlackBox user may watch; no per-operator gating.** **Reframed 2026-07-20 (D14):** because virtual-display sessions are **concurrent** (multiple may run at once, cap 3), the indicator is an **active-sessions badge/list** — e.g. "2 agents running — watch" — driven by `GET /cu/sessions` and opening the live view, NOT an exclusive lock. An exclusive **"desktop in use" warning is shown ONLY for native-mode sessions** (the real desktop, still serialized by `display_arbiter`). Both surfaced in the Portal and the Android MVP. noVNC/websockify is the **only genuinely-new piece** (the Xvfb/openbox/x11vnc trio already runs under the real sandbox today) — smoke-check it under the unit (loopback bind is allowed by `RestrictAddressFamilies=AF_INET`; pin the dependency on `PrivateTmp=false`, or move the X11 socket under `/run/user`, so future hardening doesn't silently break CU). Portal gets a live-view panel (iframe pattern like the zellij terminal); Android renders the same page in a WebView (three-surfaces rule satisfied).
- **Service restart orphans, does not kill:** under `blackbox.service`'s real sandbox (`KillMode=process`, `PrivateTmp=no`, `ProtectHome=no`) a restart reparents the Xvfb/openbox/x11vnc/websockify children to init — **the virtual session persists and live view reconnects**; only the uvicorn-served `/cu/view` WS blips. Because those orphans survive, the **pid+TTL reaper must ALSO run at boot** to sweep restart-survivors (this is why teardown is by pid, not global pkill).
- **Default flip:** `use_computer`, `/browser/run`, and scheduler runs default to virtual sessions; the three interactive chat CU launch sites in `chat_routes.py` need the same audit (recon flag). `native_mode` becomes a per-session choice ("act on my desktop") instead of a global default.
- **No GPU interaction:** Xvfb renders on CPU (llvmpipe) — CU sessions never contend with the model stack for VRAM.
- **Concurrency (D14):** the allocator permits parallel virtual CU sessions (separate displays); the cap is **3**, RAM/CPU-bound. Concurrent sessions surface as the **active-sessions badge/list** (`GET /cu/sessions`), not a lock; only **native mode** is serialized (one real desktop, `display_arbiter`).

---

## 10. Testing & Rollout

### Deployment topology — Brandon's fleet (verified by SSH recon 2026-07-20)

- **Dev box (A620AI, this machine):** no GPU, 31GB RAM → tier LOW. **Stays cloud for all four capabilities in day-to-day use** (Brandon's call). Its role in this plan is Phase-1 code development only.
- **MS02 Ultra is THE local-inference + test host.** Access: **`ssh bbx@192.168.1.153`** (LAN, ed25519 key auth already provisioned from this box; user `bbx`). Tailscale shows it as `mini-rack-node-1-ms-02-ultra` / `100.72.92.12`, but **always connect via LAN**, not Tailscale (Brandon's instruction; the old Tailscale node entry `100.122.114.77` is stale).

| MS02 verified inventory | |
|---|---|
| OS / kernel | Ubuntu 24.04.4 LTS / 6.17.0-35 |
| CPU | Intel Core Ultra 9 285HX, 24 cores |
| RAM | 125GB (≈112GB available) |
| GPU | RTX 2000 Ada, **16,380 MiB**, driver 595.71.05 |
| Disk | 915GB NVMe, **816GB free** (weights budget is a non-issue) |
| Ollama | 0.30.8 active — `qwen3-embedding:0.6b` + `:8b` already pulled |

- **MS02 is already a full customer-shaped BlackBox install, tier HIGH:** `blackbox.service` active on :9091, repo at `/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main` — currently `main @ b8167dd` (~12 days behind origin; stray `config.ini.bak-pre-rerank` + modified `ToolVault/embeddings.json` to reconcile before Phase 2).
- **Live today on MS02:** active embeddings = `qwen3-embedding-8b` served by Ollama at **Q4_K_M** (kept warm, llama-server resident ~6,994 MiB — *below* Q8_0's ~7,676 MiB weights) **and** `vllm-reranker.service` running Qwen/Qwen3-Reranker-0.6B on :8091 (~3,284 MiB, a `gpu_memory_utilization` reservation) → **6,994 + 3,284 = 10,278 MiB ≈ 10GB pinned at idle. M11 rerank is LIVE on MS02, not dark.** NB this is a *different topology* from the planned Q8_0 llama-server retrieval group (§4) — the two "~10.5GB" figures are coincidental; neither this footprint nor the "giant re-embed worked great" throughput (which also ran on the Q4 Ollama + vLLM reservation) validates the Q8_0 config, which G1 must measure directly.
- **MS02's snapshot Volume is a transplant and does not belong there.** The dev box's corpus was copied over purely to stress-test the embeddings layer — Brandon ran a giant-corpus re-embed through the GPU-served 8B and **it worked great** (a strong G1 throughput/feasibility signal). **SUPERSEDED 2026-07-22 (D8 update):** the reset is CANCELLED — Brandon keeps the existing corpus on MS02 (he added his own operator+brain to it) as a richer memory-retrieval test. Phase 2 re-embeds the **existing** corpus onto the on-box 8B instead of starting empty. This transplant experience still motivates the parked export/import workstream (§12).
- **Migration implication:** enabling the new stack on MS02 must first retire the always-resident pair — disable `vllm-reranker.service` and **explicitly unload** the Ollama 8B (a pointer flip alone leaves it resident — see Step 0) — because neither the audio group (~7–12GB peak) nor the retrieval group (~11.5–13GB) can load beside the pinned ~10GB. The grouped llama-swap stack *replaces* both (embeddings/rerank move into the `retrieval` group; keep-warm becomes `ttl: 0` per §6). Phase 2 runs as an **update-in-place through the production update path** (exercises the real customer journey: `install.sh` re-run picks up Step 2f), with a from-scratch fresh-box validation pass after.

**Build→ship→test loop (topology pinned 2026-07-20).** The two phases below run as one loop across two boxes. The **dev box** (this machine, no GPU) is authoring + staging only: write code, run the mocked unit tests + CPU-path checks, and commit. **GitHub is the only transport between the two boxes** — `git push origin main` (github.com/TechBran/blackbox-poc, private) ships; **MS02 `git pull`s the same `main`** (never `scp`/`rsync` a working tree — both boxes stay identical). **MS02 Ultra runs ALL GPU testing** over the permanent SSH key (`ssh bbx@192.168.1.153`, LAN, never Tailscale; clone at `/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main`): every GPU-dependent step — the G1–G6 gates, real llama-server/Speaches/qwen-tts loads, VRAM measurements, the reranker GGUF conversion — is *build + unit-test on the dev box → commit → push → ssh + pull on MS02 → run the GPU step → iterate*. Model weights/GGUFs are never in git (gitignored, multi-GB) and download on MS02. Phase 1 is the dev-box build; Phase 2 is the MS02 run.

**Phase 1 — build on this box (no GPU):** all code paths, CPU-tier behavior, install.sh idempotency, wizard flow, catalog/routing, CU virtual displays (CPU-only feature anyway), unit/integration tests. The fresh-box portable gate applies: no hardcoded hosts/operators; empty-store behavior verified.

**Phase 2 — MS02 Ultra:**

*Step 0 — snapshot-data reset (destructive; run only when Phase 2 begins):* **scope DECIDED by Brandon 2026-07-20 (D8): wipe ONLY the transplanted snapshot data — `Volume/`, `Fossils/`, and `Manifest/` (snapshot index + all embedding stores/sidecars). Everything else stays the same:** operators (`config.ini [users]`), personas/preferences, `.env`, `devices.json`, `credentials/custom_models.json`, onboarding state, uploads. MS02 keeps its identity and configuration; only the ledger that was transplanted for the embeddings stress-test is removed, and the box starts minting its own history from zero.

**Retire the pinned pair — with a real VRAM unload, not just a pointer flip:** `systemctl disable --now vllm-reranker.service` (correct as-is), then **explicitly UNLOAD the pinned Ollama 8B** — `ollama stop qwen3-embedding:8b` (or stop `ollama.service`). "Drop Ollama's embeddings duty" in v1 mapped only to `store.set_active_slug()` (writes `active.json`); that does NOT unload the `keep_alive=-1`-pinned ~7GB resident (keep_alive is only re-asserted on an embed request, and after the pointer flip no embed is sent). Without the explicit unload, a stale Ollama ~7GB + the retrieval group ~11.5–13GB = ~18–20GB > 16,380 MiB → **CUDA OOM the moment the retrieval group first lazy-loads (at the wizard re-embed, since llama-swap starts idle).**

**VRAM-free precondition:** bake into the install/migration script an assertion that `nvidia-smi` shows the GPU near-idle **BEFORE the retrieval group is first activated (before the re-embed, not merely before service-start)**. Retiring the pair needs sudoers grants to stop `vllm-reranker.service`/`ollama` (§8) or explicit interactive-sudo steps.

*Then:* update the repo to main, re-run `Scripts/install.sh` (picks up Step 2f), and walk the **new `local_models` wizard step on the existing install** (one-click downloads, per-capability activation, embedding cutover onto an empty store that grows with MS02's own history). Run the benchmark gates. This exercises the production **update-in-place** journey; the from-scratch fresh-box wizard validation (a product gate, independent of Brandon's boxes) runs later as a separate pass — a scratch `BLACKBOX_ROOT` clone or a VM.

### Benchmark gates (all measured on MS02, results into `eval/results/`)

| Gate | Question | Pass criterion |
|---|---|---|
| **G1** | 8B-Q8_0 vs the current gemini-embedding-2 incumbent on the chunk-gate harness (4B arm dropped — Brandon-ratified 2026-07-20, see D5); **AND measure real Q8_0 VRAM** (steady-state + peak during a heavy re-embed batch, incl. the ub=8192 non-causal compute buffer) and the reranker separately on the RTX 2000 Ada | 8B-Q8 within agreed delta of gemini before cutover; thresholds recalibrated; measured Q8_0 footprint fits the §4 budget with headroom |
| **G2** | **8B** reranker GGUF validity + latency + **per-search swap overhead** (D13) | Scores match HF reference on golden pairs (no cls.output.weight breakage) for the **Qwen3-Reranker-8B Q8_0** GGUF; 40-passage rerank inside ceiling once loaded; the ~6–12s intra-group embed→rerank swap measured separately |
| **G3** | Qwen3-TTS 1.7B RTF + first-packet latency, streaming, on the 2000 Ada | RTF < ~0.9 → 1.7B streams; else 0.6B streaming / 1.7B batch split |
| **G4** | On-box whisper streaming parity vs today's gemma-box path | Latency/quality parity in the Portal + Android mic flows |
| **G5** | Swap cost — **cross-group** (audio↔retrieval, expect ~6–10s first interaction, dominated by the 8B embedder cold-load) **and intra-group** (D13 embed↔rerank within retrieval, ~6–12s per search) | both cross-group directions AND the intra-group embed↔rerank swap measured (warm + cold page-cache); keep-warm gives zero relief either way; Brandon signs off on the stalls (Q12) or the design shifts to a hybrid |
| **G6** | Streaming-STT eviction safety (Design A / WS-proxy is **blocked on llama-swap #754** — ships as Design B: direct-to-member + **Orchestrator serialization**, not keepalive) | Fire a retrieval/embedding request WHILE a voice stream is mid-utterance → assert zero audio cut-off under the shipped design |

### Acceptance (end-to-end on MS02)

- Fresh install → wizard → **all four capabilities local**; `GET /local-models/status` green.
- Full local voice conversation (live STT → chat → streaming Qwen TTS) with zero cloud audio calls.
- Search E2E on the migrated 8B-Q8 store, reranked locally.
- CU session on a virtual display, agent opens apps privately, live view working in Portal + Android, desktop untouched throughout.
- GPU behavior observed: queue → drain → evict → load; 10-min TTL eviction confirmed; voice loop never thrashes.
- Kill the stack mid-use → **per-capability** graceful degradation (D2): STT/TTS fall back to cloud, retrieval returns un-reranked, mints go vector-less + gap-heal on recovery — **the active embedding model is NOT switched** (no cloud/local dim fragmentation of the corpus; §6 invariant).

---

## 11. Open Questions (iterate here)

1. **Q1 — Streaming TTS wiring:** Portal's sentence-level `StreamingTTSQueue` exists but is orphaned (auto-TTS waits for the full response today). Does "live TTS streaming the way we get it now" mean reviving sentence-level streaming feed for Qwen, or keeping complete-response auto-TTS with a streaming HTTP body?
2. **Q2 — Qwen streaming size default** pends G3 (decision rule is written, number isn't).
3. **Q3 — Wizard step split:** one `local_models` step owning everything vs folding STT into `transcription` and embeddings into `embeddings` steps.
4. **Q4 — CU session app toolkit:** which apps ship "expected working" in virtual sessions (Chrome certainly; file manager? LibreOffice?) — drives what openbox session bootstrap installs.
5. **Q5 — Live-view interactivity:** view-only noVNC, or allow the user to click *into* the agent's virtual screen (takeover/assist)?
6. **Q6 — Existing boxes migration:** for boxes already using gemma-box audio/custom setups, does the wizard offer a "move audio on-box" migration prompt on update, or stay opt-in silent?
7. **Q7 — Ollama deprecation timeline** for embeddings once localstack is proven (install.sh still installs it today).
8. **Q8 — Naming bikeshed:** `localstack` provider key, `qwen` catalog group id, `blackbox-models.service`, `[local_models]` — confirm or rename.
9. **Q9 — Qwen3-ASR:** evaluate as a whisper alternative/addition later (52 languages, aligner)? Parked, not planned.
10. **Q10 — MS02 dual role:** ~~after testing, does MS02 stay a customer-shaped reference box?~~ **Answered by recon 2026-07-20:** MS02 *is* already a full customer-shaped BlackBox install (tier HIGH, local embeddings + reranker live) — it is the permanent local-inference reference box; the dev box stays cloud (§10 topology).
11. **Q11 — MS02 reset scope:** ~~keep/wipe split~~ **ANSWERED 2026-07-20 → D8.** Snapshots only (`Volume/`, `Fossils/`, `Manifest/` index + embedding stores); everything else — operators/`config.ini`, `.env`, `devices.json`, custom servers, onboarding state — stays the same.
12. **Q12 — Cross-group swap-latency UX:** ~~acceptable, or hybrid?~~ **ANSWERED 2026-07-20 → D9.** ~6–10s first interaction accepted; two-exclusive-groups stands; routing always follows the user's selection (UI or system config).
13. **Q13 — Voice give-up-to-cloud timeout:** ~~queue vs fallback?~~ **ANSWERED 2026-07-20 → D10** (per recommendation): queue through the swap with a "loading models…" affordance; never silently switch provider; ~30s ceiling then an honest error.
14. **Q14 — CU live-view auth isolation:** ~~per-operator gating?~~ **ANSWERED 2026-07-20 → D11**, **reframed by D14.** One shared live view — any BlackBox user may watch; no per-operator gating. Sessions are **concurrent** (cap 3), so the indicator is an **active-sessions badge/list** from `GET /cu/sessions` (Portal + Android), not a lock; an exclusive **"desktop in use" warning is native-mode only**.
15. **Q15 — STT streaming direction:** ~~serialize vs patch upstream?~~ **ANSWERED 2026-07-20 → D12.** Design B, Orchestrator-level serialization; streaming STT stays WebSocket end-to-end for near-real-time; all processing through the BlackBox, phones view/interact through it.

---

## 12. Parked Adjacent Workstream — BlackBox Export/Import (Portability)

**Not in this implementation's scope — gets its own brainstorm + design doc.** Recorded here because the MS02 situation is the motivating case: the only way to move a BlackBox today is a manual transplant (copy the Volume over), which drags one box's *identity* (operators, settings, ledger) onto another box with no clean separation — hence the Phase-2 reset.

What a first-class **export → bundle → import** would need to cover (sketch, for the future brainstorm):

- **Identity & config:** operators + personas + per-operator preferences; `config.ini` (non-secret); `.env` secrets (explicit opt-in, separately encrypted or re-entered on import); `credentials/custom_models.json`; `devices.json` pairings; voice profiles (`Manifest/voices/`); registered apps.
- **The ledger:** `Volume/` (append-only, immutable) + `Fossils/` + `Manifest/` snapshot index — export as a sealed archive with an integrity manifest (hashes), honoring the flight-recorder guarantee end-to-end.
- **Embedding stores:** portable **only when the target runs the same model slug**; otherwise import keeps the ledger and triggers a re-embed through the existing `/embeddings/reembed` machinery (the giant-corpus run on MS02 proves this is practical on GPU boxes).
- **The flip side — factory reset:** a documented/scripted `blackbox-reset` (the Phase-2 Step-0 checklist, productized) so "start this box fresh" stops being hand-run `rm` commands. Import and reset are the two halves of the same portability story.
- **Open questions for that brainstorm:** operator consent/ownership when a ledger changes machines; partial export (one operator's slice?); version skew between boxes; whether export doubles as the backup story.

## 13. Key References

- Recon digest (full agent findings + sources): workflow `wf_e282c331-27c`, 2026-07-20 session.
- Qwen3-TTS: github.com/QwenLM/Qwen3-TTS · HF collection `Qwen/qwen3-tts` · arXiv:2601.15621 · vLLM-Omni speech API docs · community servers: groxaxo/Qwen3-TTS-Openai-Fastapi, cornball-ai/qwen3-tts-api · GGUF: Serveurperso/Qwen3-TTS-GGUF (qwentts.cpp)
- Qwen3-ASR: github.com/QwenLM/Qwen3-ASR (0.6B/1.7B + ForcedAligner)
- llama-swap: github.com/mostlygeek/llama-swap (config.example.yaml: groups/ttl/hooks; README endpoint list)
- Qwen3-Embedding/Reranker: github.com/QwenLM/Qwen3-Embedding · official GGUFs (0.6B/4B confirmed) · reranker GGUF conversion caveats (HF discussion #22, llama.cpp #16407)
- Speaches: github.com/speaches-ai/speaches (config: `stt_model_ttl`/`tts_model_ttl`, CPU compose)
- faster-whisper VRAM/CPU benchmarks: github.com/SYSTRAN/faster-whisper
- In-repo anchors: `Orchestrator/embeddings/{registry,providers}.py` · `Orchestrator/rerank.py` · `Orchestrator/hardware.py` · `Orchestrator/routes/{stt_ws_routes,tts_routes}.py` · `Orchestrator/onboarding/custom_servers.py` · `Scripts/install.sh` + `installer/templates/` · `Orchestrator/browser/{display,display_arbiter,config,actions,screenshot}.py`

## 14. Hardening Audit Log (2026-07-20)

**Verdict (condensed).** The spec is factually strong on external model facts and on its reading of the existing codebase — the great majority of load-bearing claims verified TRUE, and the core two-exclusive-group architecture (D7) survives verification and does **not** deadlock. But v1 was **not implementation-ready**: it carried four blockers — the llama-swap `config.yaml` (the heart of D4) was never written; `xvfb`/`websockify`/`novnc` were absent from the apt allowlist so a fresh box could not install the CU framebuffer; Step-0's "drop Ollama's embeddings duty" was a pointer flip that does NOT unload the pinned ~7GB Ollama 8B (guaranteed CUDA OOM at the first re-embed); and Step-0 omitted `config.ini`, so the dev-box operator roster would survive the "fresh operators" reset. On top of those sat a cluster of majors where the routing/degradation/streaming/VRAM narratives were wrong or under-specified: D2 "on-box wins, no user choice" inverted the real resolver and would override Brandon's explicit ElevenLabs picks; the existing STT `local` token could not reach the on-box `:9098` member; embeddings crash fall-through would fragment the corpus across dim-incompatible stores; llama-swap WebSocket proxying is a known-MISSING feature so Design A is effectively dead and Design B's keepalive does not block exclusive eviction; the official `qwen-tts` package is batch-only so "chunked-as-generated" streaming is unsupported; embedding ubatch is forced to ~8192 with an unbudgeted compute buffer; and cross-group swap latency was understated ~5× (~6–10s first interaction). All 32 corrections are applied above (§3–§11); the five follow-on decisions are Q11–Q15.

**Confirmed solid — verified from primary sources (do not re-open):**
- **keep-warm `ttl: 0` does NOT deadlock the two-group design** — llama-swap's `EvictionFor()` (`internal/router/group.go`) gates cross-group eviction solely on `Exclusive && !Persistent` and never consults per-model `ttl`/`unloadAfter`, so a kept-warm retrieval member IS evicted when the exclusive audio group is demanded and reloads on next use (exactly as §6 asserts).
- **llama-swap group/ttl/exclusive/persistent semantics** are correct as described: `ttl:0` disables idle-unload only; `exclusive:true` unloads other non-persistent groups; `persistent` blocks cross-group eviction; `persistent:true` on BOTH groups would deadlock VRAM (>16GB) — so keep-warm = `ttl:0` (not `persistent`) is the sound choice.
- **The official `Qwen/Qwen3-Embedding-8B-GGUF` Q8_0 file exists at 8.05GB** — the "8B official GGUF to verify" uncertainty is resolved; no self-conversion for embeddings.
- **Qwen3-TTS family facts are accurate:** Apache-2.0, shipped 2026-01-22, family max 1.7B, three distinct 1.7B checkpoints (Base/CustomVoice/VoiceDesign), 3s cloning, natural-language VoiceDesign, and the 9 CustomVoice presets match the card (Vivian, Serena, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee).
- **llama-swap's proxied endpoint surface** is correct: `/v1/embeddings`, `/v1/rerank`, `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/audio/voices`, plus `/upstream/:model`, `/running`, `/health`, `${PORT}`, `checkEndpoint`, `healthCheckTimeout`.
- **Drain-before-swap + in-flight accounting** (recovered-verdict facts, source-verified): `checkEndpoint`/`healthCheckTimeout` is a **one-time STARTUP readiness gate, never re-probed on a running process** (no health-based SIGTERM during an in-process TTS variant load); the FIFO scheduler drains in-flight *proxied* requests before any swap and a **chunked streaming response stays in-flight for its FULL duration through the proxy** (a cross-group swap QUEUES behind a mid-stream request, never SIGTERMs it; `unloadTimeout` only bounds an already-idle process) — but a **direct-to-port** stream is invisible to that counter (hence Design-B STT needs Orchestrator serialization).
- **Reranker facts** are correct: `RERANK_MODELS` holds the literals, `query_instruction` is mandatory (ranker inverts without it, measured 2026-07-03), the vLLM `/score` seam on :8091 is retained as the FP16 alternative, and `score()` returns `None` on ANY failure and never raises (a dead reranker costs latency, never recall).
- **The community reranker-GGUF trap is real and G2 is the right gate:** broken conversions yield ~1e-28 scores from a missing `cls.output.weight`; current `convert_hf_to_gguf.py` detects Qwen3-Reranker and sets `pooling_type=RANK` + classifier labels; llama.cpp #16407 is resolved.
- **STT bridge mechanics carry over correctly:** 24kHz resample, ~0.7s trailing-silence stop (not explicit commit), per-utterance finals, `is_whisper_hallucination` filter, and the `stt_done` terminal frame are all present in `stt_ws_routes.py`.
- **CU already never changes desktop resolution:** native mode resizes screenshots in software (PIL → 1280×720) and scales coordinates back; the sandbox path returns unscaled pixel-exact captures — supporting §9's per-session native-resolution design.
- **`ydotool` writes to `/dev/uinput`** at the kernel/physical-seat layer (not display-addressable) on both X11 and Wayland, while the X11 trio (Xvfb/xdotool/scrot) honors `DISPLAY` — so virtual sessions must force the xdotool/scrot path, as §9 states.
- **Exactly three interactive chat CU launch sites** in `chat_routes.py` (`try_claim` at :4293, :4518, :4775), and Gemini's recommended 1440×900 is currently an unused constant — both as claimed.
- **The CU virtual-display stack already runs under `blackbox.service`'s real sandbox** (`PrivateTmp=no`, `ProtectHome=no`, `KillMode=process`): `display.py` already spawns Xvfb+openbox+x11vnc today, `/tmp` and `/run/user` are reachable, and a service restart **orphans (not kills)** those children.
- **The Tailscale perimeter holds for the live view:** Funnel exposes only :8443→:9093 (MCP); :9091 is tailnet-only via serve; `x11vnc` already binds 127.0.0.1 — so a `/cu/view` WS reverse-proxied through :9091 is not internet-reachable (conditional on websockify binding loopback and no dedicated Funnel being added).
- **The disk gate is adequate:** the full GPU-tier weight set is ~34GB (three separate ~4.5GB Qwen3-TTS variant checkpoints = 13.5GB, plus D13's ~8.1GB Qwen3-Reranker-8B Q8_0 — up from ~27.5GB when the reranker was the 0.6B), so ≥40GB free is still a sound download gate (tighter headroom, ~6GB); `hardware.py` has `probe()`/`derive_tier` but no disk field, so a fail-soft disk probe genuinely needs adding.
- **The install-mechanics gaps are correctly identified:** `blackbox-write-systemd.sh` has no `blackbox-models` target and `sudoers-blackbox-system` has no `blackbox-models` restart grant (only `daemon-reload` is covered).

**Refuted alarms — do NOT re-litigate** (adversarially verified against primary sources; the spec's original posture stands, only genuine residue was folded in at minor severity):
- **keep-warm `ttl:0` deadlock** — refuted (eviction ignores ttl; `ttl:0` members still yield to a cross-group swap).
- **retrieval-pin "equivalence"** — refuted; the residue (a pinned retrieval group blocks a concurrent audio load, and `--watch-config` restarts the whole proxy) is captured in §6.
- **`is_healthy()` cloud-flapping** — refuted; routing keys on install/config/process-liveness, not live per-member residency (§4/§6 clarified).
- **systemd-sandbox breakage** — refuted; the trio already runs under the real unit and a restart orphans (not kills). Only noVNC/websockify is genuinely new (§9).
- **TTS health-SIGTERM / streaming-not-in-flight** — refuted from llama-swap source: health is startup-only and a proxied stream is in-flight for its full duration; the proposed "raise `unloadTimeout` above worst-case synthesis" fix was actively wrong and rejected.

**Audit provenance.** Six-dimension Opus audit (code-claims, external-facts, vram-math, failure-modes, impl-readiness, consistency) with adversarial per-finding verification; 44/45 agents completed (one verify agent's result was lost and recovered from its written verdict — the drain/health/streaming-in-flight confirmations above). Workflow `wf_9ccf920e-432`.
