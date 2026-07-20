# On-Box Local Model Stack + CU Virtual Displays — Design Spec

**Date:** 2026-07-20
**Status:** SPEC v1 — iterating with Brandon before an implementation plan is written
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
| D2 | Routing precedence | **On-box wins when installed & healthy.** STT/TTS/embeddings/rerank wholesale local; custom API servers are for LLMs; cloud is the customer's explicit fallback choice |
| D3 | Qwen3-TTS variants | **All three 1.7B variants** (Base + CustomVoice + VoiceDesign), MS02 benchmark decides streaming size (G3) |
| D4 | GPU orchestrator | **llama-swap front door** — supervises all model servers, native drain/swap/TTL semantics |
| D5 | Embedding model | **Qwen3-Embedding-8B @ Q8_0** default on GPU boxes (4096-dim), eval-gated vs 4B-FP16 on MS02 (G1) |
| D6 | CU display | **Own virtual screen per session** (Xvfb at model-native resolution); live view in Portal + Android; agent opens apps in its own display |
| D7 | GPU residency policy | **Two co-resident groups** — `audio` (whisper + Qwen TTS) and `retrieval` (embeddings + reranker); groups are mutually exclusive; 10-min idle TTL |

### Premise corrections discovered during recon (2026-07-20)

- **Qwen3-TTS open weights shipped 2026-01-22** (Apache-2.0), not today. Family max is **1.7B (~4GB BF16)** — there is no bigger open variant. Today's news was the API-only Qwen-Audio-3.0-TTS-Plus. Companion **Qwen3-ASR** (0.6B/1.7B, 52 languages, Apache-2.0, released 2026-01-29) exists as a future whisper alternative.
- **Embeddings/reranker already run on THIS box's Ollama** (`127.0.0.1:11434`) — but the *active* embedding model is **gemini-embedding-2 (cloud)** and the *active* reranker is **Vertex (cloud)**. "Wholesale local" therefore means a **full re-embed migration** of the ~8,164-snapshot corpus, using the existing `POST /embeddings/reembed` machinery.
- **The network box (gemma-box, 192.168.1.50)** hosts chat LLMs + Speaches audio (faster-whisper-large-v3-turbo + Kokoro) as a custom server. That's the "whisper across the network" being brought on-box.
- **CU already never changes the desktop resolution** — native mode downscales screenshots in software (PIL → 1280×720) and scales coordinates back. The real gap is that CU *takes over* the physical desktop; D6 fixes that.

---

## 4. Architecture Overview

```
                      ┌────────────────────────────────────────────────┐
                      │  blackbox-models.service  (llama-swap, :9098)  │
 Orchestrator ──────► │                                                │
  (per-capability     │  group "retrieval"  (co-resident, ~10.5GB)     │
   clients, on-box    │    • embed-qwen3-8b   llama-server GGUF Q8_0   │
   wins when healthy) │    • rerank-qwen3-0.6b llama-server --reranking│
                      │                                                │
                      │  group "audio"      (co-resident, ~7–9.5GB)    │
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
- **VRAM budget (16GB):** retrieval group ≈ 8.5 (8B Q8_0) + 1.2 (reranker 0.6B) + ctx ≈ **10.5GB**. Audio group ≈ 1.6–4.5 (whisper turbo fp16 → large-v3 fp16) + ~4–5 (Qwen 1.7B BF16) ≈ **7–9.5GB**. All four at once ≈ 19GB — does not fit; hence two exclusive groups.
- **CPU-only boxes run the identical topology** with CPU builds and smaller models (§7). Same front door, same routing, same wizard — just different tier defaults. This keeps one code path for the fresh-box gate.

### Orchestrator-side integration

A new thin module `Orchestrator/local_stack.py` (name TBD) is the single source of truth for:
- `is_installed()` / `is_healthy()` — llama-swap reachable + configured members present.
- `base_url()` — `http://127.0.0.1:9098/v1` (config: `[local_models] base_url`).
- Per-capability resolution used by every consumer (D2 precedence): **on-box → (existing custom-server audio, unchanged, for the separate Kokoro system) → cloud per wizard choice.**
- `GET /local-models/status` — aggregates llama-swap `/running`+`/health`, hardware tier, disk, per-model download state, and the routing decision per capability. Consumed by the wizard step and the Updates panel (status-only, per the panels convention).

---

## 5. Component Specs

### 5.1 Embeddings (Qwen3-Embedding-8B @ Q8_0)

- **New provider key** in `Orchestrator/embeddings/providers.py`: `localstack` — OpenAI-compatible `POST {base_url}/embeddings` against llama-swap (bearer not needed on loopback). Sits beside `gemini`/`openai`/`ollama`.
- **New registry entries** (`Orchestrator/embeddings/registry.py`): `qwen3-embedding-8b-local` (provider `localstack`, model id `embed-qwen3-8b`, 4096-dim, `max_input_tokens 8192`) and `qwen3-embedding-0.6b-local` (1024-dim, CPU-tier default). Thresholds (`semantic_threshold`, `junk_floor`) start from the existing Ollama Qwen entries and are **recalibrated during G1** — per-model thresholds are mandatory (memory: per-model calibration discipline).
- **Upstream:** llama-server with official `Qwen/Qwen3-Embedding-8B` GGUF at Q8_0 (official 0.6B/4B GGUFs confirmed; 8B official GGUF to verify — else we convert with current `convert_hf_to_gguf.py`; G1 validates output quality either way).
- **Migration:** wizard-driven `POST /embeddings/reembed {target: qwen3-embedding-8b-local}` — existing schema-2 chunked build + atomic swap. ~36K rows through a GPU-served 8B is fast; wizard shows the existing progress UI.
- **Watcher/status compatibility:** `_model_preflight` blockers, `watcher.py` catalog checks, and `ollama_io` assumptions are Ollama-shaped; the `localstack` provider needs its own blocker strings ("local stack not installed / model not downloaded / llama-swap down") and health probe (embed-one-string through :9098).
- **Ollama's future:** stays installed (step 1d) and registry-supported for existing boxes; new GPU boxes never activate it for embeddings. Formal deprecation is an open question (Q7).

### 5.2 Reranker (Qwen3-Reranker-0.6B via llama-server)

- **Primary:** llama-server `--reranking --pooling rank` serving a **correctly-converted** Qwen3-Reranker-0.6B GGUF, exposed as `/v1/rerank` through llama-swap. New `RERANK_MODELS` entry `qwen3-reranker-0.6b-local` (provider `localstack`), scoring via the existing `_score_*` pattern; the mandatory `query_instruction` prefix carries over verbatim (ranker inverts without it — measured 2026-07-03).
- **⚠ Known trap:** community reranker GGUFs are frequently broken (missing `cls.output.weight` → near-zero scores). We convert ourselves with current `convert_hf_to_gguf.py` and **G2 gates on score validity** against HF-reference scores on golden query/passage pairs before this provider can be selected.
- **Why 0.6B not 4B:** rerank is the latency-critical inner loop of every search (candidate_n=40 passages per query); 0.6B GGUF loads in ~1–2s after an audio→retrieval group swap and scores well inside the 500ms-class ceiling. 4B is a config-only upgrade later if G2 shows quality headroom is needed.
- **vLLM seam:** the existing dark `/score` seam (port 8091, `vllm-reranker.service` template) is kept as the FP16 quality alternative — installable but not default (vLLM cold-load 15–25s makes it a poor swap citizen). The M11 activation memory gets superseded by this design.
- **Selection stays sidecar-driven:** `POST /rerank/select` writes `Manifest/embeddings/rerank.json` exactly as today; the wizard flips it after G2 passes.

### 5.3 STT (Speaches on-box: faster-whisper)

- **Upstream:** Speaches (active project, CPU compose exists) as a llama-swap member in the `audio` group. Models: `deepdml/faster-whisper-large-v3-turbo-ct2` (streaming default — same model the gemma-box serves today, so behavior parity) + `Systran/faster-whisper-large-v3` (batch-quality option). Speaches' internal `stt_model_ttl` set to harmonize with llama-swap's ttl (Speaches manages *model* residency inside the process; llama-swap manages the *process*).
- **Batch path:** `file_transcribe._local_transcribe` gains the D2 precedence — resolve on-box stack first, then the existing custom-server audio, then cloud. Same OpenAI-compatible `POST /audio/transcriptions` shape, just a different base URL.
- **Streaming path (`/ws/stt` `_local_bridge`):** connects to Speaches `/v1/realtime` (WebSocket). **Open engineering risk:** llama-swap's WS proxying is unverified (SSE is documented, WS is not). Two designs, gated by **G6 on MS02**:
  - **(A) Proxy through llama-swap** — if WS passthrough works, activity accounting and group exclusivity are automatic. Preferred.
  - **(B) Direct-to-Speaches port + keepalive** — the bridge connects to the member's port directly and the Orchestrator pings `/upstream/speaches` periodically during an active stream so the TTL never fires mid-utterance and the group stays claimed.
- The existing bridge quirks carry over unchanged (24kHz resample, trailing-silence stop instead of explicit commit, per-utterance finals, hallucination filter, `stt_done` terminal frame).
- **Tool schema fix (recon find):** `speech_to_text` ToolVault schema's provider enum omits `local` — add it while we're here.

### 5.4 TTS (Qwen3-TTS server — ours, in-repo)

- **We write a thin FastAPI server** (e.g. `LocalModels/qwen_tts_server/`, own lean venv) on the official `qwen-tts` package rather than adopting a community server — production-quality control over: all three variants, streaming, consent-gated cloning, profile persistence. The community FastAPI servers (groxaxo, cornball-ai) are references for the streaming implementation, not dependencies.
- **One llama-swap member (`qwen-tts`), one process, three variants managed in-process:** the server lazy-loads the variant a request needs (CustomVoice for presets — the hot path; Base for cloned-voice synthesis; VoiceDesign for design) and drops the previous one. Loading all three at once (~12GB+) would blow the audio group's budget; in-process variant swap (~4GB each, seconds to load) keeps llama-swap's view simple: one member, one port.
- **Endpoints (OpenAI-compatible where a convention exists):**
  - `GET /health` — llama-swap `checkEndpoint`.
  - `POST /v1/audio/speech` — `{model, input, voice, response_format, stream}`; `stream:true` emits chunked audio as Qwen generates (12.5Hz token frames → 24kHz PCM/MP3). Routes through llama-swap's documented `/v1/audio/speech` proxying.
  - `GET /v1/audio/voices` — presets + saved clone/design profiles (llama-swap proxies this path too).
  - `POST /v1/voices/clone` — reference audio (~3s min) + name; **requires the literal consent flag**, mirroring the ElevenLabs gate exactly (422 without it; ToolVault wrapper requires `confirm_consent=true`).
  - `POST /v1/voices/design` + `/v1/voices/design/save` — 2-step preview→save, mirroring ElevenLabs design UX.
- **Voice profiles** persist under `Manifest/voices/qwen/{slug}/` — `profile.json` (name, variant, operator, consent record, created) + reference audio / design params. Survives restarts; never in git.
- **Catalog:** new dynamic group in `GET /tts/catalog`: `{id:'qwen', label:'Qwen3-TTS (On-Box)', dynamic:true}` — voice ids `qwen:<Voice>` (9 presets: Vivian, Serena, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee) plus `qwen:<profile-slug>` for clones/designs, star-prefixed like ElevenLabs My-Voices. Present only when the local stack is healthy (fail-open like the other dynamic groups). **`local:` (Kokoro) remains untouched** (non-goal).
- **Synthesis routing:** `POST /tts` and `/tts/batch` gain a `qwen:`-prefix branch (same pattern as the `elevenlabs:`/`local:` branches); `sanitize_for_speech` applies as today. Streaming browser playback mirrors the ElevenLabs `StreamingResponse` path.
- **Frontends (three-surfaces rule):** Portal voice picker picks the group up automatically from the catalog. **Android prerequisite fix (recon find):** `TtsRepository.generateWithVoice` hardcodes provider `openai` in its else-branch — a selected `local:`/`qwen:` voice would 400. Fix: pass the parsed provider through generically instead of enumerating branches.
- **Voice Lab:** a Qwen tab beside ElevenLabs/xAI — clone (consent gate), design (preview→save), manage/delete profiles. Gated on local-stack health instead of an API key.
- **Speed reality (G3):** RTX 3090 measures RTF 0.83–0.97 on the 1.7B; the 2000 Ada is slower, so 1.7B may be sub-realtime. Policy: **1.7B is the quality default for batch/file synthesis; the streaming default (1.7B vs 0.6B-CustomVoice) is decided by G3 measurements**, surfaced as an honest wizard note. First-packet latency claim (~97ms) also gets verified in G3.

### 5.5 Routing & precedence (D2, all capabilities)

For each of STT / TTS / embeddings / rerank:
1. **On-box stack installed & healthy → on-box wins.** No per-request user choice needed.
2. The **separate** custom-server audio system (Kokoro `local:` group, gemma-box Speaches) keeps working exactly as today — it is not part of this precedence fight; it's a distinct provider the user can still pick explicitly.
3. **No local stack (or user preference) → cloud provider** chosen in the wizard, exactly as today.

Provider naming follows the provider-explicit convention: the catalog group is `qwen` (model-explicit), the embeddings/rerank provider key is `localstack` (infrastructure-explicit) — bikeshed welcome (Q8).

---

## 6. GPU Sharing Semantics & Edge Cases

- **Queue/drain/evict/TTL:** llama-swap defaults. `ttl: 600` per member. Requests for a member of the *other* group trigger: drain in-flight → stop current group's members → start requested member → serve queue. Same-group members co-load.
- **Live voice loop (the reason for D7):** a voice conversation is duplex — whisper listens while Qwen speaks. Both live in the `audio` group, co-resident; zero swaps inside a conversation.
- **Search (the other pair):** every query runs embed→rerank back-to-back; both live in `retrieval`, co-resident; zero swaps inside a query.
- **Streaming sessions are in-flight work:** an open STT stream (or TTS stream) must block eviction until it closes — via WS proxy activity (G6-A) or keepalive claims (G6-B). A retrieval request during a voice conversation queues until the conversation's current utterance drains; this is the accepted trade-off of one GPU.
- **Initial re-embed migration thrash:** the first corpus re-embed (~36K rows in 32-chunk batches) is thousands of short requests; interleaved audio requests would swap groups between batches and crawl. Mitigation: the wizard runs migration as a foreground step with a "voice features will be slow until the index finishes building" notice; optionally set the retrieval group `persistent: true` for the duration via a config reload, restoring it after cutover. (Detail for the implementation plan.)
- **Keep-warm interplay (wizard checkbox — work with it, don't fight it):** today's embeddings keep-warm setting (`Manifest/embeddings/keep_alive.json` → Ollama `keep_alive`, e.g. `-1` pin; MS02 currently holds its 8B embedder resident this way at ~7GB). Under llama-swap, keep-warm maps to **`ttl: 0` on that member** (never idle-unloaded). Adopted semantics: a kept-warm model is **immune to the 10-min idle TTL but still yields to a cross-group swap** when the other group is demanded, then reloads on next use — anything stronger (`persistent: true`) would deadlock VRAM (10.5 + 9.5 > 16GB). The wizard copy states exactly this. The legacy Ollama path keeps its current keep-warm meaning unchanged.
- **Crash/health:** llama-swap `checkEndpoint` + `healthCheckTimeout` per member; `GET /local-models/status` surfaces member state; the embeddings watcher's daily probe covers the localstack provider like any other. If the stack dies, D2 precedence falls through to the wizard-chosen cloud provider (existing degradation paths: vector-less mints + gap-heal; un-reranked retrieval fall-through; cloud STT/TTS).

---

## 7. Hardware Tiers & CPU Fallback

Tier detection reuses `Orchestrator/hardware.py` (`probe()`/`derive_tier`); **add a disk probe** (recon: none exists; the full GPU-tier weight set is ~28GB — gate downloads on ≥40GB free).

| Capability | GPU tier (≥8GB VRAM) | CPU tier (no GPU, ≥32GB RAM) | Floor tier |
|---|---|---|---|
| Embeddings | Qwen3-Embedding-**8B Q8_0** (4096d) | Qwen3-Embedding-**0.6B** GGUF (1024d) — fast on CPU | cloud choice |
| Rerank | Qwen3-Reranker-**0.6B** GGUF (4B = config upgrade) | 0.6B GGUF CPU (G2 latency-gates it; else existing CrossEncoder path / cloud) | cloud choice |
| STT stream | whisper **large-v3-turbo** fp16 | large-v3-turbo **int8** (strong CPU); `distil`/`small` on weak CPUs — wizard shows measured RTF estimate | cloud choice |
| STT batch | whisper **large-v3** fp16 | large-v3 int8 (slow, labeled) | cloud choice |
| TTS | Qwen3-TTS **1.7B** (streaming size per G3) | **Off by default** — CPU Qwen is far sub-realtime; offered as "experimental (slow)" opt-in (`qwentts.cpp` GGUF is the escape hatch), cloud recommended | cloud choice |

Principles: local is the *default* only where the experience is honest (per Brandon: "extremely slow" is acceptable **as an informed choice**, never a silent default). The wizard shows per-capability estimates from the tier probe before the user commits. Same llama-swap topology on every tier — only members/builds differ.

---

## 8. Install & Onboarding

### install.sh (follows the proven Step-2d gated-template pattern)

New **Step 2f — `installer/templates/blackbox-install-localstack.sh`** (non-fatal, self-gating, re-run-safe):
1. Download **llama-swap** release binary, sha256-pinned (same pattern as zellij, version pinned in `installer/templates/`).
2. Download **llama.cpp `llama-server`** prebuilt (CUDA build behind the `nvidia-smi` gate, CPU build otherwise).
3. Create lean venvs: **Speaches** and **our qwen-tts server** (own venvs — the MCP lean-venv lesson applies).
4. Write `llama-swap config.yaml` from a template, tier-adjusted members (§7).
5. Install `blackbox-models.service` via `blackbox-write-systemd` — **requires a new `target_kind` in its whitelist** (and a `systemctl restart blackbox-models.service` sudoers grant). `/etc` writes happen install-time as root; the wizard never writes `/etc` (ProtectSystem=strict).
6. **No weights at install time.** Weights download in the wizard (below) — keeps install fast and disk-gated.

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

- **Technology: per-session Xvfb (X11).** The recon nailed the constraint: on Wayland, ydotool injects into the *physical seat* and XDG-Portal screenshots capture the *real compositor* — neither is display-addressable. The X11 trio (Xvfb, xdotool, scrot) all honor `DISPLAY`, and the existing sandbox path (`browser/display.py`) already proves the pattern (Xvfb + openbox + x11vnc). We generalize it from a singleton to an allocator.
- **DisplayAllocator** (evolves `display_arbiter.py`): allocates display numbers `:100+n`, spawns per-session `Xvfb :N -screen 0 WxH x24`, `openbox`, a session dbus, and per-session `x11vnc` on `127.0.0.1:<5901+n>`. Tears down on session end; reaps orphans by pid + TTL. The arbiter's existing mutual-exclusion role survives *only* for native-mode (real desktop) sessions.
- **Per-backend native resolution — scaling deleted, not improved:** Anthropic/OpenAI sessions get 1280×720 Xvfb (scale 1.0, pixel-exact coordinates, no LANCZOS blur — the sandbox path already demonstrates unscaled coords); Gemini gets 1440×900 (its recommended resolution, currently an unused constant) with its 0–999 normalized mapping.
- **Input/capture inside the session:** xdotool + scrot with `DISPLAY=:N` — no ydotool, no Portal screenshots, no Wayland involvement. Chrome launches inside with `--window-size` matched. Any X11 app works; GNOME/Wayland-only apps are a documented limitation of virtual sessions (use native mode for those).
- **Live view:** noVNC/websockify per session, reverse-proxied by the Orchestrator (`/cu/view/{session_id}` WS). Auth = the Tailscale perimeter (by design, no app-layer auth). Portal gets a live-view panel (iframe pattern like the zellij terminal); Android renders the same page in a WebView (three-surfaces rule satisfied).
- **Default flip:** `use_computer`, `/browser/run`, and scheduler runs default to virtual sessions; the three interactive chat CU launch sites in `chat_routes.py` need the same audit (recon flag). `native_mode` becomes a per-session choice ("act on my desktop") instead of a global default.
- **No GPU interaction:** Xvfb renders on CPU (llvmpipe) — CU sessions never contend with the model stack for VRAM.
- **Concurrency:** the allocator naturally permits parallel CU sessions (separate displays); a sane cap (e.g. 3) guards CPU/RAM.

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
- **Live today on MS02:** active embeddings = `qwen3-embedding-8b` served by Ollama (kept warm, llama-server resident ~6,994 MiB) **and** `vllm-reranker.service` running Qwen/Qwen3-Reranker-0.6B on :8091 (~3,284 MiB) → **≈10.5GB VRAM pinned at idle. M11 rerank is LIVE on MS02, not dark.**
- **Migration implication:** enabling the new stack on MS02 must first retire the always-resident pair — disable `vllm-reranker.service` and take embeddings duty off Ollama — because the audio group (~7–9.5GB) cannot fit beside the pinned 10.5GB. The grouped llama-swap stack *replaces* both (embeddings/rerank move into the `retrieval` group; keep-warm becomes `ttl: 0` per §6). Phase 2 runs as an **update-in-place through the production update path** (exercises the real customer journey: `install.sh` re-run picks up Step 2f), with a from-scratch fresh-box validation pass after.

**Phase 1 — build on this box (no GPU):** all code paths, CPU-tier behavior, install.sh idempotency, wizard flow, catalog/routing, CU virtual displays (CPU-only feature anyway), unit/integration tests. The fresh-box portable gate applies: no hardcoded hosts/operators; empty-store behavior verified.

**Phase 2 — MS02 Ultra:** update the existing install in place through the production update path (see topology above), walk the wizard's new `local_models` step end-to-end, retire the pinned Ollama-embeddings + vllm-reranker pair, then run the benchmark gates. A from-scratch fresh-box validation pass (separate `BLACKBOX_ROOT` or reinstall) follows once gates pass.

### Benchmark gates (all measured on MS02, results into `eval/results/`)

| Gate | Question | Pass criterion |
|---|---|---|
| **G1** | 8B-Q8_0 vs 4B-FP16 vs current gemini-embedding-2 on the chunk-gate harness | 8B-Q8 ≥ 4B and within agreed delta of gemini before cutover; thresholds recalibrated |
| **G2** | Reranker GGUF validity + latency | Scores match HF reference on golden pairs (no cls.output.weight breakage); 40-passage rerank inside ceiling |
| **G3** | Qwen3-TTS 1.7B RTF + first-packet latency, streaming, on the 2000 Ada | RTF < ~0.9 → 1.7B streams; else 0.6B streaming / 1.7B batch split |
| **G4** | On-box whisper streaming parity vs today's gemma-box path | Latency/quality parity in the Portal + Android mic flows |
| **G5** | Group swap cost | audio↔retrieval evict+load time measured; voice-turn and search latencies acceptable with cold group |
| **G6** | llama-swap WebSocket proxy for `/v1/realtime` | Works → design A; fails → design B (direct + keepalive) |

### Acceptance (end-to-end on MS02)

- Fresh install → wizard → **all four capabilities local**; `GET /local-models/status` green.
- Full local voice conversation (live STT → chat → streaming Qwen TTS) with zero cloud audio calls.
- Search E2E on the migrated 8B-Q8 store, reranked locally.
- CU session on a virtual display, agent opens apps privately, live view working in Portal + Android, desktop untouched throughout.
- GPU behavior observed: queue → drain → evict → load; 10-min TTL eviction confirmed; voice loop never thrashes.
- Kill the stack mid-use → graceful cloud fallback per D2.

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

---

## 12. Key References

- Recon digest (full agent findings + sources): workflow `wf_e282c331-27c`, 2026-07-20 session.
- Qwen3-TTS: github.com/QwenLM/Qwen3-TTS · HF collection `Qwen/qwen3-tts` · arXiv:2601.15621 · vLLM-Omni speech API docs · community servers: groxaxo/Qwen3-TTS-Openai-Fastapi, cornball-ai/qwen3-tts-api · GGUF: Serveurperso/Qwen3-TTS-GGUF (qwentts.cpp)
- Qwen3-ASR: github.com/QwenLM/Qwen3-ASR (0.6B/1.7B + ForcedAligner)
- llama-swap: github.com/mostlygeek/llama-swap (config.example.yaml: groups/ttl/hooks; README endpoint list)
- Qwen3-Embedding/Reranker: github.com/QwenLM/Qwen3-Embedding · official GGUFs (0.6B/4B confirmed) · reranker GGUF conversion caveats (HF discussion #22, llama.cpp #16407)
- Speaches: github.com/speaches-ai/speaches (config: `stt_model_ttl`/`tts_model_ttl`, CPU compose)
- faster-whisper VRAM/CPU benchmarks: github.com/SYSTRAN/faster-whisper
- In-repo anchors: `Orchestrator/embeddings/{registry,providers}.py` · `Orchestrator/rerank.py` · `Orchestrator/hardware.py` · `Orchestrator/routes/{stt_ws_routes,tts_routes}.py` · `Orchestrator/onboarding/custom_servers.py` · `Scripts/install.sh` + `installer/templates/` · `Orchestrator/browser/{display,display_arbiter,config,actions,screenshot}.py`
