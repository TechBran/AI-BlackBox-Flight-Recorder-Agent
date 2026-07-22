# Zero-Terminal Customer Onboarding Flow for the On-Box GPU Stack

**Status:** DESIGN ADDITION — extends SPEC v2.1 (`docs/plans/2026-07-20-local-model-stack-design.md`, decisions D1–D14 locked) and the M8 wizard step (`Portal/onboarding/steps/local_models.js`). Not a re-litigation of the stack architecture; it is the *activation-surface* layer that turns provisioned-but-idle hardware into live capabilities without a terminal. To be folded into `docs/plans/2026-07-20-local-model-stack-implementation.md` as implementation tasks.

**Target hardware (reference box):** MS02, RTX 2000 Ada, 16 GB VRAM. Corpus at design time: 7,611 snapshots.

---

## 1. Vision & the Single-Friction Principle

Today, bringing the on-box GPU stack live on a real box takes a senior engineer an afternoon in a terminal: building llama.cpp from source, retiring VRAM-hungry legacy services, downloading multi-GB weights, hand-editing `config.ini`, running a cross-model embedding cutover, and — the hardest part — **self-converting and quantizing an 8B reranker from a raw HuggingFace checkpoint because no working prebuilt GGUF exists**. A customer cannot do any of this.

**The principle: exactly one moment of friction, and it is a password prompt — not a terminal session.**

> The user runs `sudo bash Scripts/install.sh` **once** and types their sudo password **once**. That single script owns 100% of the root-requiring work: the `apt` CUDA toolkit install, `/etc` writes (systemd unit, llama-swap config), and the bounded NOPASSWD sudoers grants. After it finishes, **every remaining action is a button in the wizard.** No second sudo prompt. No `curl`. No `systemctl`. No `git clone`. No `python convert_hf_to_gguf.py`. Ever.

This is architecturally clean because **the Orchestrator already runs as the unprivileged `bbx` user with `ProtectHome=no`**. Every non-root job — weight downloads, the reranker convert+quantize (all `git`/`cmake`/`pip`/`hf` work in `$HOME`), config edits, the embedding cutover, TTS backend install — is a backend job the Orchestrator launches for itself. Root is needed for only two things after install: stopping the two legacy services and (during install) writing `/etc`. The former is pre-authorized by the installed NOPASSWD grants; the latter happens inside the one script. **The blast radius of root is the install script and two `systemctl stop` calls, and nothing else.**

The install script provisions but **activates nothing** — no weights land, no capability turns on. That is deliberate: it lets the wizard own the entire activation story, per-capability, reversibly, with honest progress.

---

## 2. The Complete Fresh-Box Journey (Numbered Walkthrough)

**What the user does, and what they see, from bare metal to a fully live on-box stack.**

1. **Clone + one script (the only terminal moment).** The user runs `git clone …` and `sudo bash Scripts/install.sh`, types the sudo password once, and walks away for ~15 min. The script installs the CUDA toolkit, **builds llama.cpp `llama-server` from source with CUDA (sm_89)**, installs llama-swap v241, creates the Speaches + qwen-tts venvs, writes the llama-swap config and `blackbox-models.service`, and installs the bounded NOPASSWD sudoers grants. It downloads no weights and activates nothing. *This is the last terminal command in the entire journey.*

2. **Open the Portal.** The onboarding wizard's **On-Box Models** step (M8) polls `GET /local-models/status`, detects the provisioned-but-idle stack (`installed:true, healthy:true`, all four routing decisions `off`, zero weights on disk), and shows a banner: **"On-box stack installed and idle — 0 of 4 capabilities active. Set them all up in one click."**

3. **One click (or four).** The user clicks **"Set up all on-box models"** — or, for a partial/manual setup, uses the individual per-capability buttons. No sudo, no terminal.

4. **Watch the 4-row checklist.** Four capability rows — **Memory (embeddings) · Search reranking · Speech-to-text · Text-to-speech** — each advance through honest, labeled states: `not-started → downloading (GB + %) → converting / rebuilding (phase + ETA) → validating (G2 / G3) → ready ●`. No bare spinners. Rows are independent: embeddings can read **ready** while TTS is still downloading.

5. **Confirm the one destructive precondition, in-app.** Just before the first on-box 8B needs the GPU, an inline card appears: **"Free the GPU & continue — this stops the old memory pair (vLLM reranker + Ollama 8B) to free ~10.5 GB VRAM. Reversible."** Clicking it runs the server-side `systemctl stop` calls through the installed NOPASSWD grants. Still no terminal, still no second password.

6. **Walk away — it survives.** The long jobs are long: **reranker convert ~40–60 min**, **corpus re-embed ~2–3 h** on the 8B. The user can close the tab, navigate away, even trigger a service restart. On return the checklist rehydrates from the persisted pipeline-status endpoint and resumes exactly where it left off. Partial success is first-class.

7. **The reranker earns its activation.** The reranker row runs its build to a `.candidate` GGUF, then **auto-runs the G2 benchmark gate**. On pass, the candidate is promoted and the row flips to **"On-box active ● — Validated: scores healthy, ranks correct."** On fail, it is **NOT** turned on (search is unaffected) and the card offers *Retry build / Use a validated community build / Leave search un-reranked*.

8. **Done — never touched a terminal after step 1.** Memory is on the on-box **Qwen3-Embedding-8B (Q8_0)**; the reranker passed G2 and is selectable; STT/TTS are live. The **Updates panel** afterward mirrors this state **read-only** (status only; all activation lives in the wizard, per the "panels = status, selection in the wizard" rule).

---

## 3. Manual Step → Button/Endpoint Mapping

Every step the human ran by hand today, mapped to its zero-terminal equivalent and current build status.

| # | Manual terminal step (today) | Button / endpoint equivalent | Build status |
|---|---|---|---|
| 1 | `sudo bash Scripts/install.sh` — CUDA toolkit, **build llama.cpp from source (sm_89)**, llama-swap v241, Speaches + qwen-tts venvs, systemd unit, llama-swap config, NOPASSWD grants (+ 6 installer bugs fixed) | **The one allowed friction.** Stays a script; the sudo prompt is the entire friction budget. | EXISTS (`installer/templates/*`, `Scripts/install.sh`) — **needs fresh-box end-to-end re-validation of the 6 fixes** |
| 2 | `sudo systemctl stop vllm-reranker.service` + `stop ollama.service` (free ~10.5 GB) | **"Free the GPU & continue"** → `POST /local-models/retire-pair` (server-side `sudo -n systemctl stop` via installed grants, re-probes VRAM) | **NEEDS BUILDING** — grants exist (`installer/templates/sudoers-blackbox-system:129–131`); nothing calls them. `gpu-preflight` only *detects* contention |
| 3 | `POST /local-models/download {artifact:"embed-qwen3-8b"}` (8.05 GB, NDJSON) | **"Download 8.05 GB"** on the Memory row (unchanged) | EXISTS (`Orchestrator/localstack_downloads.py`, disk-gated NDJSON) |
| 4 | Hand-edit `config.ini` `[local_models] enabled/base_url/embeddings=true` | **Config button** (or folded into activate) → `POST /local-models/config` (fresh read-modify-write, no restart) | **NEEDS BUILDING** — `/capability` hard-rejects `embeddings` with 400 (`local_stack_routes.py:74`); no endpoint sets `enabled`/`base_url` |
| 5 | `POST /embeddings/migrate {target:"qwen3-embedding-8b-local"}` — the **real** cutover (`set_active_slug` + `swap_active`) | **"Use on-box (Q8_0)"** → cutover orchestrator `POST /local-models/embeddings/cutover` (wraps `/embeddings/migrate`) | ENDPOINT EXISTS (`/embeddings/migrate`, `migrate.py:240`) — **orchestrator + M8 call-site fix needed** (M8 calls `/embeddings/reembed`, the wrong primitive — see §4.2, §5.4) |
| 6 | **Reranker self-convert** — `git clone` llama.cpp (post-#16407), venv + `pip`, `hf download Qwen3-Reranker-8B` (16 GB), `convert_hf_to_gguf.py`→f16, build `llama-quantize`, quantize→Q8_0 | **"Set up on-box reranker (~40–60 min)"** → `POST /local-models/reranker/build` (full pipeline as `bbx`, NDJSON multi-phase) | **NEEDS BUILDING — the single biggest missing piece.** No endpoint exists; install builds only `llama-server` (not `llama-quantize`) and the 0.6B vLLM, never the 8B GGUF |
| 7 | Manual `curl /v1/rerank` + eyeball scores for degeneracy/inversion | **Auto-gate** inside the build → `POST /local-models/reranker/validate` (wraps `eval/rerank_g2.py` on a throwaway CPU-only loopback server) | HARNESS EXISTS (`eval/rerank_g2.py`, `eval/rerank_golden.jsonl`) — **not wired to gate activation** |
| 8 | Fix embedding-model **labels** in registry (both 8B said "max quality"; now Ollama=Q4, on-box=Q8_0) | **Quantization badge** read live from `registry.py` `label`/`quality_note`, surfaced in `/embeddings/status` `models[]` | REGISTRY FIX likely LANDED — **wizard must render the Q4-vs-Q8_0 badge, not hardcode it** |

---

## 4. Consolidated New Backend Endpoints & Jobs

Four designers proposed overlapping names for the same jobs; this section is the **single canonical set** after reconciliation. Naming convention: everything on-box lives under `/local-models/*`; the two existing embedding primitives (`/embeddings/migrate`, `/embeddings/status`) are reused, not renamed.

### 4.1 Reranker Self-Conversion Service (the hardest piece)

**`POST /local-models/reranker/build`** — runs the entire self-convert → quantize → validate → activate pipeline **as the `bbx` user (no sudo; `git`/`cmake`/`pip`/`hf` all in `$HOME`)**, streaming NDJSON multi-phase progress, activating **only on G2 pass**. Replaces 100% of manual steps 6 and 7.
- **Body:** `{source?: "self-convert" | "community-fallback", resume?: bool}`.
- **Scratch:** `~/.blackbox/localstack/scratch/reranker-build`; final GGUF → `LOCALSTACK_MODELS/Qwen3-Reranker-8B-Q8_0.gguf` (the exact path `installer/templates/llama-swap-config.yaml.template` already points the `rerank-qwen3-8b` member at).
- **Phases** (each emits `{phase, status, completed, total, pct, eta_s, state}`): `preflight` (disk gate 507 at ~40 GB free) → `clone` (pin the tag in `installer/templates/llamacpp-version`, currently `b10084`, which post-dates the `convert_hf_to_gguf.py` #16407 reranker fix) → `build-quantize-tool` (`cmake --build --target llama-quantize` — install builds only `llama-server`) → `deps` (venv + `pip install -r requirements/requirements-convert_hf_to_gguf.txt`) → `download` (`snapshot_download Qwen/Qwen3-Reranker-8B`, ~16 GB, reuse the `_stream_hf_snapshot` pattern to scratch) → `convert` (`--outtype f16`) → `quantize` (`llama-quantize f16 Q8_0 → .candidate`) → `validate` (→ 4.1b) → `activate`.
- **Zero VRAM:** convert/quantize are CPU/RAM/disk only, so the whole build can run **while the old pair still serves live search** — it does not need the GPU freed.
- **Single-flight + resumable:** mirror `localstack_downloads.start_download` (409 "already running", `_finish()` stuck-guard on client disconnect); skip any phase whose output already exists (clone dir, quantize binary, checkpoint, f16). **On success:** back up any existing member GGUF to `.pre-<ts>.gguf`, `os.replace(.candidate → member)`, delete the 16 GB checkpoint + f16 intermediates, then internally call `POST /rerank/select {provider:"localstack", model:"qwen3-reranker-8b-local", enabled:true}`.

**`POST /local-models/reranker/validate`** (4.1b) — the **activation gate**, wiring `eval/rerank_g2.py` into the wizard. Serves the target GGUF on a **throwaway CPU-only loopback** `llama-server --reranking --pooling rank --port 9097 -ngl 0` (never contends for VRAM or the retrieval group), imports the harness's factored pieces (`is_degenerate`, `separation_ok`, `score_via_endpoint`, `load_golden` over `eval/rerank_golden.jsonl`), streams per-query pass/fail, then tears the server down. **Primary gate = non-degenerate (`|score|` & spread > 1e-6) AND separation (`min(relevant) > max(negative)`)** — exactly the checks that caught the broken community Q8_0 (~1e-31 scores, missing `cls.output.weight`, rank inverted). Optional `--hf-reference` Spearman check reuses the just-downloaded checkpoint before cleanup. Standalone-callable on `{target: "candidate" | "installed"}` for the row's **Re-check** button. Writes `eval/results/{date}-rerank-g2.{md,json}`.

**`GET /local-models/reranker/status`** — out-of-band poll of the build singleton (phase/pct/eta/state, member-GGUF presence, last G2 report summary) so the wizard re-attaches after navigate-away. Mirror `localstack_downloads.download_status()`; foldable into `/local-models/status`'s `models[]` entry for `rerank-qwen3-8b`.

**`POST /local-models/reranker/cancel`** — cooperative cancel (mirror `/embeddings/migrate/cancel`); phases check the flag at boundaries and abort cleanly, leaving scratch resumable and the live member GGUF untouched.

### 4.2 Embedding Cutover Orchestrator

**`POST /local-models/embeddings/cutover`** — one idempotent, tracked job that **sequences the whole cross-model cutover** so no surface (Portal JS, Android) re-implements the VRAM-ordering invariant:

`gpu-preflight` gate → `retire-pair` (**only if contended**) → **`/embeddings/migrate {target:"qwen3-embedding-8b-local"}`** → on job done, `/toolvault/reload` + set the `[local_models].embeddings` flag.

> **Load-bearing correction (the M8 bug).** The cutover **MUST** call `start_migration` (`migrate.py:240` — diff-and-fill → `set_active_slug` → `search.swap_active`), **NOT** `start_reembed`. `reembed` is a same-model full rebuild that in-service candidate-swaps but does **not** repoint `active` to the `-local` slug on a cross-*model* move — so the M8 poll (which waits for `active` to end with `-local`, `local_models.js`) can spin forever. `migrate` is the actual cross-model cutover **and** completes instantly on an empty fresh-box corpus (`missing == 0`). The stale docstrings at `local_stack_routes.py:38–39,76` and the `local_models.js` header (lines 6–7) that say "embeddings activate via `/embeddings/reembed`" must be corrected to `/embeddings/migrate`.

### 4.3 GPU + Config Actuators

**`POST /local-models/retire-pair`** — the auto-retire actuator: `sudo -n /usr/bin/systemctl stop vllm-reranker.service` + `stop ollama.service` via the installed NOPASSWD grants, freeing ~10.5 GB, then re-probes and returns freed/used VRAM so the UI can confirm near-idle. Build on the proven sudo-argv-list actuator in `Orchestrator/onboarding/mcp_actuator.py` (argv lists never shell strings; token-for-token match to `sudoers-blackbox-system:129–131`; per-op `asyncio.Lock`; idempotent when already stopped). *Note: `ollama.service` also served the Q4 embedder we are leaving — stopping it is intended.*

**`POST /local-models/config`** — write `[local_models] enabled=true`, `base_url`, `embeddings=true` to `config.ini` via the existing fresh read-modify-write helper (no restart). Turns manual step 4 into a button. `is_installed() == master_enabled()` (`local_stack.py`), so this flag gates the whole wizard UI.

### 4.4 Audio Jobs

**`POST /local-models/tts/backend-install`** — `pip`-install the pinned Qwen3-TTS streaming inference fork (`kunzite-app/Qwen3-TTS-streaming`: `load_variant()` + `stream_generate_pcm()`) into the qwen-tts venv. **This closes the known gap where the member `/health`-passes but synthesis `ImportError`s** (venv has fastapi/torch/transformers but not the model backend). Runs as `bbx`, no sudo; single-flight; import-probe first for idempotency; streams pip phases. *The exact fork commit pin is still unresolved (`requirements.txt` is only a comment) — resolve on MS02 during first bring-up.*

**`POST /local-models/tts/validate`** (G3) — the TTS activation gate: synthesize through the member, measure **RTF + first-packet latency + sample-rate-read-from-output**, sample the CustomVoice→Base→VoiceDesign transition VRAM peak, and **decide the streaming variant (0.6B vs 1.7B per the <0.9 RTF rule)**. Wraps the existing manual harnesses (`diagnostics/localstack/tts_rtf.py`, `vram.py`, `metrics.py`; `LocalModels/qwen_tts_server/smoke_gpu.py`). **Precondition:** `gpu-preflight` near-idle (audio and retrieval groups are mutually exclusive on 16 GB). On pass, persist `QWEN_TTS_STREAMING` + the chosen variant. *A broken TTS backend fails loudly, but a too-large streaming variant is a silent latency regression — so this both proves synthesis and sizes it.*

**`POST /local-models/stt/prefetch`** — warm the audio group (`local_stack.speaches_warm_url()`), make Speaches fetch+cache `faster-whisper-large-v3-turbo` (+ optional `large-v3` for files), then verify a short reference clip round-trips through `/ws/stt`. Whisper is deliberately **not** in `DOWNLOAD_MANIFEST` (Speaches auto-pulls), so this is a new warm+preload+verify job — otherwise the first live transcription is an invisible multi-GB cold pull. *Speaches pull-progress granularity may be coarse — flag it.* STT *enable* itself stays the existing `/local-models/capability {capability:"stt"}` (which also mirrors `STT_PROVIDER=onbox` so `resolve_stt_provider` routes `/ws/stt` to the on-box member).

### 4.5 Master Orchestrator + Status Spine

**`POST /local-models/provision-all`** — the one-click spine: a single-flight, resumable, background pipeline that sequences the whole setup respecting VRAM order and validation gates. Body selects capabilities (default = all applicable to the detected tier). Runs entirely as `bbx` except the two `systemctl stop` calls. Idempotent: skips phases already `ready`, resumes a failed phase on retry.

**`GET /local-models/provision/status`** (poll + SSE variant) — per-capability sub-state for the checklist: `{embeddings, rerank, stt, tts}` each `{phase, pct, eta_seconds, detail, error}`. **Backed by a new persisted pipeline-state file** so resume survives a `blackbox.service` restart. ETAs derived from content-length+rate (downloads), measured throughput (convert), and corpus size (re-embed). Additive to the fast-probe `GET /local-models/status`.

**`GET /local-models/status` (additive fields)** — extend the rollup with per-audio readiness (`tts {weights_downloaded, backend_installed, validated, streaming_variant}`, `stt {whisper_prefetched}`) plus the folded reranker member state, so the wizard can gate buttons ("Use on-box" for TTS stays disabled until weights+backend+validate all report done). And a **migration-aware health hardening** on `GET /embeddings/status`: while `job.state=="running"` or immediately after a cutover to a new-dims local store, the watcher writing `health.json` must not flag the store `broken`/`unhealthy` (a poll landing right after the Q8_0 4096-dim swap must not false-alarm).

---

## 5. The Wizard UI

Extends `Portal/onboarding/steps/local_models.js` (M8). Ships across all three surfaces (Portal web, Android Kotlin, WebView) per the frontend-three-surfaces rule — the `/local-models/*` contracts are additive and status-poll-driven, so Android renders the same phases.

### 5.1 Top-level: the one-click orchestrator

A single primary CTA — **"Set up all on-box models"** — appears above the four per-capability rows when `installed && healthy && no capability active`, driven by the fresh-box auto-detect banner ("On-box stack installed and idle — 0 of 4 capabilities active"). The four rows remain as the manual/partial path.

### 5.2 The 4-capability checklist

Each row is a state chip with its own inline progress, never a bare spinner:

`not-started → downloading (GB + %) → converting | rebuilding (phase + ETA) → validating (G2/G3) → ready ● → failed (red, Retry-that-row)`

Rows are **independent and non-blocking** — embeddings can read **ready** while TTS still downloads; a failed capability shows its real error and never stalls the others. On revisit/refresh/after-restart a **resume banner** rehydrates from `GET /local-models/provision/status` and continues live.

### 5.3 Per-capability card behavior

- **Memory (embeddings):** quantization badge read live from `registry.py` `label`/`quality_note` via `/embeddings/status` `models[]` — **"Qwen3 8B — on-box CUDA · Q8_0 · max quality"** (~8.1 GB) vs the Ollama-served **Q4** fallback; **do not hardcode.** Activate button shows the honest one-time cost up front ("~7,611 snapshots re-embed on-box, ~2–3 h; voice slow until done") and the **fresh-box path** ("instant cutover — nothing to re-embed") when the corpus is empty. Progress panel is phase-aware — *Retiring old models → Re-embedding N/M (P%) · ~X h left → Activating (atomic swap)* — reusing the ETA sampler + job-running-suppression pattern already in `embeddings.js` (`etaSamples`, `renderJobPanel`) rather than the bare percentage in `startEmbedPoll`.
- **Search reranking:** replace `activateRerank()`'s blind `POST /rerank/select` (which "succeeds" on a fresh box where the member GGUF is absent — the exact silent-garbage trap) with a **build-driven state machine**: `NOT BUILT → "Set up on-box reranker (~40–60 min · downloads 16 GB · benchmark-validated before it turns on)" → BUILDING (phase card: Downloading 8.2/16 GB → Converting → Quantizing to Q8_0) → VALIDATING (distinct trust-moment sub-state: "Checking scores aren't degenerate and relevant results rank first…", per-query ticks) → VALIDATED + ACTIVE ● (green "Validated" badge) → G2 FAIL (red: "produced unreliable scores, so it was NOT turned on — your search is unaffected"; Retry build / Use a validated community build / Leave un-reranked)`. **Remove the `autoNote` lie** ("provisioned automatically — converted at install", `local_models.js:234`): conversion is not done at install; the wizard owns it. Drive the row off `GET /local-models/reranker/status`.
- **Text-to-speech:** a 4-state stepper — `[Download voices GB] → [Install voice engine (pip phases)] → [Test voices (G3: RTF/first-packet + Play sample)] → [Use on-box]` — later buttons disabled until earlier states report done (driven off the new audio-readiness fields). After validate, render RTF per voice, first-packet ms, the decided streaming variant, and the canonical `QWEN_STREAM_TIER_NOTE` copy (`qwen_tts.py`) so the UI never over-promises 1.7B streaming on the 2000 Ada.
- **Speech-to-text:** a 2-state control — `[Pre-download speech model (optional, verify)] → [Use on-box]` — with a note that Speaches otherwise auto-pulls on first use, surfacing the verify-transcription result.

### 5.4 Shared UI plumbing

- **Reuse the existing NDJSON progress reader** (`startDownload`/`updateDownloadBar`) for weights; add sibling phase-line readers (same shape, phase text instead of %) for reranker-build, backend-install, and validate.
- **Kill the dead-end preflight branch** (`local_models.js:362–366`, which just says "free the GPU… then retry"): on `gpu-preflight ok===false`, render **"Free the GPU & continue"** → `POST /local-models/retire-pair` → re-run preflight → proceed. **No terminal instruction is ever shown to a user.**
- **Fix `isActive('embeddings')`** to treat the registry active slug (`/embeddings/status` `active` ending `-local`) as the source of truth, not just `routing.embeddings.decision` — after a migrate cutover the registry is `-local` even if the `[local_models].embeddings` flag lags; reconcile the two or the row shows a stale "Use on-box".
- Keep `/toolvault/reload` firing on embeddings completion (`startEmbedPoll:393`) so ToolVault + code-search caches re-embed at the new 4096-dim.
- **Updates panel stays status-only** (`status_rollup._derive_local_models` + pipeline status), no activation controls.
- Every read is **fail-open**: the new controls simply don't render until the stack is installed/healthy.

---

## 6. Cross-Cutting Constraints the Flow Must Encode

1. **VRAM sequencing (16 GB ceiling, D13 `swap:true`).** The two retrieval members (embed 8B, rerank 8B) are **sequential, never co-resident**. Strict order for the embedding cutover: (1) download the GGUF → (2) `gpu-preflight` near-idle (used ≤ ~2048 MiB) → (3) **if contended, retire the old pinned pair FIRST** (a pointer flip alone leaves the `keep_alive`-pinned ~7 GB Ollama 8B resident → CUDA-OOM when the retrieval group lazy-loads ~11.5–13 GB) → (4) `/embeddings/migrate` → (5) stream re-embed → (6) `/toolvault/reload` + set the flag. **Never load the on-box 8B before the pair is retired.** Audio is independent of retrieval but shares the same card via mutually-exclusive llama-swap groups (audio vs retrieval): its provision phase (download/backend-install/prefetch) is VRAM-free and can run anytime, but its **validate+activate (G3) loads a variant and requires `gpu-preflight` near-idle** just like the embedding cutover. The reranker **build** is pure CPU/RAM/disk (zero VRAM) and can run in parallel with everything; only its brief loopback validation touches CPU, `-ngl 0`.
2. **Validation gates — and the silent-broken-reranker asymmetry.** A broken **embedder** fails **loudly** (dim mismatch, embedding errors), so its only gate is "re-embed completed." A broken **reranker** fails **silently** — it returns garbage (~1e-31) scores with **no error**, and search quietly degrades. Therefore the reranker **MUST pass G2 (non-degenerate scores AND relevant-above-negative separation) before it is ever selectable**; activation is downstream of the gate, not parallel to it. This is the single most important safety property in the flow.
3. **Honest progress + ETA, never a silent spinner.** Downloads show GB + %; the reranker build shows its named phases with a ~40–60 min ETA; the re-embed shows N/M snapshots with a ~2–3 h ETA. Every long job surfaces phase + ETA.
4. **Idempotency & resume.** Every job is single-flight (409 on double-fire) and resumable: skip phases whose outputs already exist; the persisted `provision/status` file survives a service restart so a 2–3 h re-embed is safe to walk away from. Partial success is first-class.
5. **Reversibility.** Embedding cutover keeps `.pre-*` store backups and the old store as a rollback asset (cloud stays searchable during the rebuild; picking a cloud model later reverts without data loss). Reranker build backs up any prior member to `.pre-<ts>.gguf` and offers **Turn off** (fall back to un-reranked/cloud). Audio is stateless — capability flags flip both ways with no corpus to manage; saved Qwen clone/design profiles under `Manifest/voices/qwen` persist regardless.

---

## 7. Prioritized Gap List

Ordered biggest-lever-first. The top three are the load-bearing new services; #4 is the trust-critical fresh-box check; the rest are enablers.

1. **Reranker-conversion-as-a-service** (`/local-models/reranker/{build,status,validate,cancel}`) — *L, ~1–2 wk.* The multi-phase job engine (clone/deps/download/convert/quantize), the G2 loopback gate wiring, activate-only-on-pass, resume, and 3-surface UI. Removes the single hardest terminal step; nothing exists today.
2. **Embedding cutover orchestrator + M8 reembed→migrate fix** (`/local-models/embeddings/cutover`) — *M, ~3–5 days.* Sequence gpu-preflight → retire → `/embeddings/migrate` → flag; fix `activateEmbeddings` to call migrate not reembed; correct the stale docstrings. Without this the current M8 path can spin forever on a cross-model move.
3. **Qwen-TTS model backend install + G3 validate** (`/local-models/tts/{backend-install,validate}`) — *M, ~3–5 days* (blocked on resolving the streaming-fork commit pin on MS02). Closes the `/health`-passes-but-synthesis-`ImportError`s gap and sizes the streaming variant.
4. **Fresh-box install re-validation** — *M, ~2–3 days on a real fresh box.* End-to-end re-run of `install.sh` proving all six fixed installer bugs (source-build llama.cpp @ b10084, llama-swap v241 checksums, gcc-12 host pin, git-install Speaches, SONAME symlink copy `-type l`, `-fa on`) survive a clean box. Requires hardware, not just code.
5. **`retire-pair` actuator** (`/local-models/retire-pair`) — *S, ~1 day.* Server-side `systemctl stop` on the proven `mcp_actuator` argv pattern; grants already installed. The last thing standing between the user and a terminal for VRAM freeing.
6. **Master provision-all orchestrator + persisted provision/status** (`/local-models/provision-all`, `GET /local-models/provision/status`) — *M–L, ~1 wk.* The one-click spine + restart-surviving pipeline-state file. Can ship after the per-capability pieces; the four rows work without it.
7. **`config`-write endpoint** (`/local-models/config`) — *S, ~1 day.* Turns hand-editing `[local_models] enabled/base_url/embeddings` into a button; unblocks the "on-box active" truth state.
8. **STT prefetch/warmup + verify** (`/local-models/stt/prefetch`) — *S, ~1–2 days.* Optional but removes the invisible cold-pull on first transcription; Speaches progress granularity is a known unknown.
9. **Migration-aware health watcher + reembed/migrate drift cleanup** — *S, ~1–2 days.* Ensure `health.json` isn't written `broken` mid/post-cutover to a new-dims store; correct the reembed-vs-migrate docstrings/routing across `migrate.py`, `local_stack_routes.py`, and the M8 header.
10. **Quantization-label rendering** — *XS, ~half day.* Registry label fix (Ollama Q4 / on-box Q8_0) is likely landed; the remaining work is the wizard reading the badge live from `/embeddings/status`, never hardcoding.

---

## 8. Relationship to Existing Work

This document is an **addition**, not a rewrite. It extends:

- **The M8 wizard step** (`Portal/onboarding/steps/local_models.js`) — the tier/disk/download/activation skeleton, `onboarding/state.py`'s `local_models` step, and `status_rollup`. The new per-capability state machines, the one-click orchestrator, and the corrected quant badge slot into that existing surface.
- **SPEC v2.1** (`docs/plans/2026-07-20-local-model-stack-design.md`, decisions D1–D14 locked, D13/D14 amendments) — this is the *activation-surface* layer the spec assumes but does not itself flesh out. It honors D7 (two mutually-exclusive groups) and D13 (sequential retrieval members, `swap:true`).

**Next step:** fold sections §4 (endpoints), §5 (UI), and §7 (gap list) into `docs/plans/2026-07-20-local-model-stack-implementation.md` as ordered implementation tasks, then execute via `superpowers:writing-plans` → `superpowers:subagent-driven-development`. The reranker-conversion service (gap #1) is the natural first milestone — it is self-contained, VRAM-free, independently testable against the existing G2 harness, and unblocks the highest-value capability the box cannot deliver today.

---

## ⚠ ADDENDUM (2026-07-22) — Reranker serving method is UNDER RESEARCH; the "conversion-as-a-service" flow is provisional

This spec was written assuming the reranker is served as a **self-converted Qwen3-Reranker-8B GGUF via llama.cpp `/v1/rerank`** (per locked decision D13). **A live-hardware finding invalidates that assumption:**

- **Qwen3-Reranker-8B is a `Qwen3ForCausalLM`** — a *causal-LM* reranker that scores by comparing "yes"/"no" token logits, **NOT a cross-encoder with a classification head.**
- llama.cpp's `--reranking --pooling rank` / `/v1/rerank` is built for **cross-encoders** (BGE, Jina). It produced **degenerate ~1e-31 scores** for a community Qwen3-Reranker GGUF, and llama.cpp's `convert_hf_to_gguf.py` has no reranker handling. The audit's "missing `cls.output.weight`" premise was wrong — **there is no classification head to preserve.** Self-converting a GGUF would hit the identical wall.

**Consequence for this onboarding spec:** the **"Reranker-conversion-as-a-service"** gap item and the reranker capability card's `convert→quantize→G2` pipeline are **provisional**. The *shape* of the flow (a button → an unprivileged backend job → a G2 validity gate → activate-only-on-pass → honest progress) is still correct and reusable, but the **job's internals depend on the chosen serving method**, which is under research (does llama.cpp support causal-LM rerankers at all? vLLM native scoring + its VRAM/coexistence reality on a shared 16GB card? a true cross-encoder alternative like bge-reranker-v2 that keeps the clean swappable design?). **This may require amending D13.** The G2 gate is the invariant that survives regardless: whatever serves the reranker, it must pass a non-degenerate-score + correct-rank check before it can go live, because a broken reranker fails *silently*.

Everything else in this spec (embeddings cutover orchestrator, STT/TTS flows, the one-click orchestrator, VRAM sequencing, the single-sudo-friction principle) is unaffected by the reranker finding.

### RESOLUTION (2026-07-22, same day) — D13 STANDS; the reranker flow is NOT provisional after all

Research (source-level, llama.cpp PRs/master) resolved the finding: **llama.cpp DOES serve Qwen3-Reranker's causal-LM scoring**, via PR #15824 (merged 2025-09-25). The logic lives in `conversion/qwen.py` (`convert_hf_to_gguf.py` is now a thin shim — grepping it found nothing, hence the false "no handling" conclusion). At conversion time it slices the yes/no rows out of `lm_head` into a `cls.output.weight` `[2,hidden]` classifier, sets `pooling_type=RANK`, and bakes the rerank chat template — exactly vLLM's mechanism, done at build time. **The ~1e-31 degenerate scores were caused by a stale community GGUF converted with an OLD tool (pre-#15824), NOT the architecture.**

**Verdict:** D13 (Qwen3-Reranker-8B GGUF, sequential swap) **stands with a one-line amendment**: *the reranker GGUF MUST be converted by llama.cpp ≥ #15824 (contains `cls.output.weight` + `pooling_type=RANK` + rerank template); old community GGUFs are broken; self-convert from the full HF snapshot (README present triggers auto-detection) or use an official ggml-org GGUF; validate with a relevant-vs-irrelevant probe before trusting.* The reranker-as-a-service onboarding flow is therefore CORRECT as specced (self-convert with the current converter + G2 gate), not provisional.

**Documented fallback (Architecture 2):** `bge-reranker-v2-m3` — a 568M TRUE cross-encoder (~1GB at Q8) that llama.cpp `/v1/rerank` has always supported and that **co-resides with the 14.3GB embedder (no swap needed)**. Lower ceiling than the 8B but simpler ops; keep as the low-risk option if the 8B GGUF proves flaky on a given box. AVOID bge-reranker-v2-gemma / mxbai-rerank-large-v2 as "escapes" — they're ALSO causal-LM rerankers (same conversion requirement).
