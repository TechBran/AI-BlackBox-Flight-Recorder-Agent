# Provider Context-Window Audit — M3 / WI-10 (verification half)

**Date:** 2026-07-02 · **Milestone:** M3 of `docs/plans/2026-07-01-retrieval-upgrade-implementation.md` (design: WI-10 in `docs/plans/2026-07-01-retrieval-upgrade-spec-audit.md` §5.6, item 6)
**Scope:** MEASUREMENT ONLY — no cap changes, no production code changes. This table is the gate for M7 (cap removal + transport hardening).
**Raw probe data:** `docs/plans/artifacts/2026-07-02-provider-window-probes.json` · **Harness:** `scripts/audit_provider_windows.py` (re-runnable, idempotent).

---

## 1. Persistence recon — probing `/chat/stream` is ledger-safe (verified)

The immutable-ledger constraint required proving the probe route persists nothing before any live traffic:

- **`POST /chat/stream` persists NOTHING.** The handler (`Orchestrator/routes/chat_routes.py:6059`, GET variant `:5957`) builds context via `build_streaming_context` (`:5886`, read-only retrieval through `Orchestrator/context_builder.py:build_fossil_context`) and relays the provider SSE stream. No turn is saved, no mint fires.
- **Persistence lives in exactly two other places:** `POST /chat/save` (`chat_routes.py:6151` — the explicit endpoint the *frontend* calls after a stream completes; `get_state` → snapshot build → auto-mint with `turns_threshold=1` per `config.ini:8-14`) and the `POST /chat` async task worker (`chat_routes.py:6483` → `Orchestrator/tasks.py` `process_chat_task`, which persists + auto-mints). Neither was touched.
- **Residual risk = the tool loop.** All four stream functions attach ToolVault tools (`_get_tools`, `chat_routes.py:196`) and will execute model-emitted tool calls — and `mint_snapshot` is in the catalog. Mitigations used: payload instruction forbidding tool calls, a fresh operator (`M3-Window-Probe` — unknown operator ⇒ all four retrieval sources return empty, per `context_builder.py:17-19`, confirmed in logs: 0 recent/keyword/semantic/checkpoint), and a hard ledger guard in the harness (abort if the probe marker/operator ever appears in `Volumes/SNAPSHOT_VOLUME.txt`).
- **Result:** snapshot count 7,963 before and after the full 12-probe run; marker guard passed on every inter-probe check. **Zero ledger writes.** One deviation observed: GPT-5.1 ignored the no-tools instruction once (210k probe) and called `get_current_time` — read-only, harmless, but proof the tool loop is live during probes (see §5).

**Payload shape note:** the synthetic document rode in a **system message** — the same shape production fossil context uses (`build_streaming_context` emits fossils as a system message, `chat_routes.py:5951`). This matters because the anthropic stream path truncates each **non-system** message to 15,000 chars (`truncate_large_content`, `chat_routes.py:2507`) while system content passes through whole — a user-message payload would never have reached Anthropic intact. (This 15k per-message truncation is itself a delivery cap **missing from the WI-10 cap inventory** — flagged for M7, §6.)

## 2. Model inventory (production ids, resolved from config — file:line)

| Surface | Provider | Model id in production | Source |
|---|---|---|---|
| Chat | anthropic | `claude-opus-4-8` | `Orchestrator/config.py:691` (`ANTHROPIC_MODEL_DEFAULT`); pre-selected in Portal via `_DEFAULT_MODEL` `admin_routes.py:630` |
| Chat | openai | `gpt-5.1` | `config.py:690` |
| Chat | google | `gemini-3.1-pro-preview` | `config.py:729` |
| Chat | xai | `grok-4.3` | `config.py:730` |
| Voice (OpenAI Realtime) | openai | `gpt-realtime-2` | `config.py:426` |
| Voice (Gemini Live) | google | `gemini-2.5-flash-native-audio-latest` | `config.py:466` |
| Voice (Grok Live) | xai | *(no pinned id — Voice Agent API, `wss://api.x.ai/v1/realtime`)* | `config.py:585`; reported as `grok-voice-agent` (`grok_live_routes.py:1401`) |
| Computer Use (default) | anthropic | `claude-opus-4-7` | `config.ini:76` → `config.py:154` |
| Computer Use (gemini backend) | google | `gemini-2.5-computer-use-preview-10-2025` | `config.py:155` |
| Computer Use (openai backend) | openai | `gpt-5.5` | `CU_MODEL_FILTERS`, `config.py:183-187`; Portal fallback `state-management.js` |
| On-device | local | `gemma-4-e4b` / `gemma-4-e2b` (litertlm) | `Orchestrator/local_provider/catalog.py:127-152` |

**Route-default drift (flag, no change made):** when the client sends an empty model id, `/chat/stream` falls back to hardcoded per-provider defaults that DIVERGE from config: anthropic → `claude-sonnet-4-5` (`chat_routes.py:5995,6085` — a legacy model, not the configured Opus default) and gemini → `gemini-3.1-pro-preview-customtools` (`:6001,6091`). The Portal pre-selects the config default from `/models/{provider}`, so normal traffic sends explicit ids — but any bare-model caller silently gets Sonnet 4.5 on the anthropic path. M7 should unify these fallbacks onto the config constants.

## 3. Documented windows (doc pass — provider-API-as-SoT where a discovery endpoint exists)

| Model | Context window | Max output | Source (live API preferred) |
|---|---:|---:|---|
| `claude-opus-4-8` | **1,000,000** tok | **128,000** tok | LIVE `GET /v1/models/claude-opus-4-8` (`max_input_tokens`/`max_tokens`), 2026-07-02, box key |
| `claude-opus-4-7` (CU) | 1,000,000 tok | 128,000 tok | LIVE `GET /v1/models/claude-opus-4-7`, 2026-07-02 |
| `gpt-5.1` | **400,000** tok total (≈272k input + 128k output) | 128,000 tok | https://platform.openai.com/docs/models/gpt-5.1 (input/output split per OpenAI's 400k = 272k-input architecture; community-documented `Input tokens exceed the configured limit of 272,000`) |
| `gemini-3.1-pro-preview` | **1,048,576** tok | **65,536** tok | LIVE `GET /v1beta/models/gemini-3.1-pro-preview` (`inputTokenLimit`/`outputTokenLimit`), 2026-07-02 (`-customtools` variant: identical) |
| `grok-4.3` | **1,000,000** tok | (not published separately) | https://docs.x.ai/developers/models/grok-4.3 ; LIVE `GET /v1/language-models` exposes `long_context_threshold: 200000` (2× pricing above 200k input tokens — a **cost** tier, not a window limit) |
| `gpt-realtime-2` (voice) | 128,000 tok session (up from 32k) | 4,096 tok per response; instructions+tools ≤16,384 tok | https://developers.openai.com/api/docs/models/gpt-realtime-2 ; https://developers.openai.com/blog/realtime-api |
| `gemini-2.5-flash-native-audio-latest` (voice) | 131,072 tok | 8,192 tok | LIVE Gemini models API, 2026-07-02 |
| Grok Voice Agent (voice) | ~256k (underlying text models; not separately published for the session) | — | https://docs.x.ai/docs/guides/voice ; https://docs.x.ai/developers/model-capabilities/audio/voice-agent |
| `gemini-2.5-computer-use-preview-10-2025` (CU) | 131,072 tok | 65,536 tok | LIVE Gemini models API, 2026-07-02 |
| `gpt-5.5` (CU) | 1,050,000 tok | 128,000 tok | https://developers.openai.com/api/docs/models/gpt-5.5 |
| On-device Gemma (litertlm) | **6,144 tok engine default** (user-raisable to 16,384 hard ceiling) | shares the same window | Device engine config — see §7 reconciliation |

## 4. Live probe methodology

- **Route:** `POST /chat/stream` — OUR transport (retrieval → ToolVault injection → provider SSE relay), per the plan's intent. Chosen because recon (§1) proved it persistence-free; the raw-API fallback was not needed.
- **Payloads:** deterministic lorem-ish filler with periodic offset markers, exactly 75,000 / 210,000 / 238,000 chars, in a system message; user message: "…Do not call any tools. Reply with exactly: OK".
- **Sizes' meaning:** 75k = today's `PROVIDER_CAPS["anthropic"]` (`context_builder.py:55`); 210k = Brandon's 2026-04-25 Opus stall repro; 238k = worst-case assembled context (16 snapshots × corpus p99 ≈14.9k chars).
- **Pipeline overhead on top of the synthetic payload:** measured ≈19.4k chars (core system prompt + ToolVault instructions + 17 injected tool schemas + media-artifact list). E.g. the 238k anthropic probe delivered a 272,501-char payload, `system` = 257,375 chars (journalctl `[ANTHROPIC DEBUG]`), messages count 1 — i.e. the full synthetic document reached the provider **untruncated**.
- **Sequential**, 20s inter-probe spacing, 240s hard timeout, no retries. `est_tokens` = `Orchestrator.tokenization.estimate_tokens` (conservative chars/2 floor); `usage` = the provider's own count (truth).
- **TTFB** = wall time from request dispatch to the first provider token event (thinking or content) through the full pipeline — includes retrieval, tool selection/embedding, and provider prefill. This is the user-perceived first-token latency of a real chat turn.

## 5. Probe results (12/12 completed; 2026-07-02, run through the live orchestrator)

| Provider / model | Size (chars) | est tok (floor) | true prompt tok (provider) | TTFB (s) | Total (s) | Completed | Output |
|---|---:|---:|---:|---:|---:|---|---|
| gemini / gemini-3.1-pro-preview | 75,000 | 37,500 | 22,339 | 5.71 | 6.44 | ✅ | `OK` |
| gemini / gemini-3.1-pro-preview | 210,000 | 105,000 | 46,543 | 5.22 | 10.61 | ✅ | `OK` |
| gemini / gemini-3.1-pro-preview | 238,000 | 119,000 | 51,567 | 7.07 | 8.83 | ✅ | `OK` |
| anthropic / claude-opus-4-8 | 75,000 | 37,500 | 35,646 | 5.92 | 5.93 | ✅ | `OK` |
| anthropic / claude-opus-4-8 | **210,000** | 105,000 | 76,488 | **14.04** | 14.04 | ✅ | `OK` |
| anthropic / claude-opus-4-8 | 238,000 | 119,000 | 84,957 | 7.00 | 7.15 | ✅ | `OK` |
| openai / gpt-5.1 | 75,000 | 37,500 | 20,785 | 4.76 | 4.76 | ✅ | `OK` |
| openai / gpt-5.1 | 210,000 | 105,000 | 44,423 | 7.53 | 7.54 | ✅ | `OK` (†) |
| openai / gpt-5.1 | 238,000 | 119,000 | 49,271 | 5.30 | 5.44 | ✅ | `OK` |
| xai / grok-4.3 | 75,000 | 37,500 | 21,043 | 3.37 | 4.28 | ✅ | `OK` |
| xai / grok-4.3 | 210,000 | 105,000 | 43,850 | 3.59 | 4.82 | ✅ | `OK` |
| xai / grok-4.3 | 238,000 | 119,000 | 48,582 | 3.81 | 5.96 | ✅ | `OK` |

(†) GPT-5.1 spontaneously called the read-only `get_current_time` tool despite the no-tools instruction (one tool iteration, then `OK`). Read-only, ledger-safe — but it demonstrates the tool loop is live in probes; any future probe harness must keep the ledger guard.

**Token-math takeaways (feeds WI-11 and the M7 window guard):**
- True density of English filler through our pipeline: ~3.2 chars/token on Anthropic's tokenizer (incl. tool schemas), ~4.8–5.0 on Gemini/OpenAI/xAI. The chars/2 floor over-estimates 2.2–2.4× — safely conservative for clamping.
- **Worst-case assembled context (238k chars + 19k overhead) = 48.6k–85k true tokens.** Headroom vs verified windows: Gemini 12–20×, Anthropic/Grok ~12×, GPT-5.1 ~3.2× (vs its 272k input share). The WI-10 design claim — "count-knob budgets essentially never bind on cloud models" — is **confirmed by measurement**.
- Anthropic bills the most tokens for identical content (its tokenizer + the anthropic-format tool schemas): 84,957 vs ~49k on the other three at 238k.

## 6. Verdicts

| Provider (chat) | Verdict | Basis |
|---|---|---|
| **gemini** (`gemini-3.1-pro-preview`) | **PASS-cap-free** | 3/3 completed, TTFB 5.2–7.1s at all sizes; 1,048,576-tok window (live). No transport concern observed. |
| **xai** (`grok-4.3`) | **PASS-cap-free** | 3/3, fastest of all providers (TTFB 3.4–3.8s); 1M-tok window (docs). Cost note: input >200k tokens crosses the 2× long-context price tier (`long_context_threshold` from live discovery) — irrelevant at our worst case (~49k tok). |
| **openai** (`gpt-5.1`) | **PASS-cap-free** | 3/3, TTFB 4.8–7.5s; 400k window (272k input) comfortably fits worst case ~49k tok. Today's 100k-char cap (`PROVIDER_CAPS["openai"]`) has no measured justification. |
| **anthropic** (`claude-opus-4-8`) | **NEEDS-transport-hardening** (server transport PASSES; client legs are the risk) | 3/3 completed **through the server pipeline** — including the exact 210k stall-repro payload. But TTFB is high-variance under adaptive thinking: **14.0s observed at 210k** (5.9–7.0s at the other sizes). 14s of first-token silence exceeds Android OkHttp's 10s default read timeout — the same failure mechanism as the confirmed 2026-04-25 stall (which showed 30–60s TTFB on Opus 4.7). The provider window (1M tok, live-verified) is a non-issue; the cap is purely a transport guard and must not be removed until M7 hardens the client SSE legs. |
| Voice ×3 (`gpt-realtime-2`, `gemini-2.5-flash-native-audio-latest`, Grok Voice Agent) | **DOC-ONLY** (not probed — session/WebSocket surfaces, no cheap synthetic-payload probe) | Windows are SMALL relative to chat: 128k session / 131,072 / ~256k. `gpt-realtime-2` additionally caps instructions+tools at **16,384 tok** and responses at 4,096 tok. Voice context budgets must be sized per-model in M7, NOT inherited from chat. |
| CU ×3 (`claude-opus-4-7`, `gemini-2.5-computer-use-preview-10-2025`, `gpt-5.5`) | **DOC-ONLY** (CU turns carry screenshots; synthetic text probes unrepresentative) | Opus 4.7 = 1M/128k (live); GPT-5.5 = 1.05M/128k (docs); **Gemini CU = 131,072 tok — the tightest cloud window in the system.** Worst-case assembled context (~50k tok on Gemini tokenizer) still fits, but with screenshots in the loop M7's window guard MUST bind on this model (75k-char CU cap at `context_builder.py:56` happens to protect it today). |
| On-device Gemma | **HARD-WINDOW** | 6,144-tok engine default, device-proven GPU ceiling (see §7). Lean profile keeps its budget; delivery via WI-7a matched-chunk windowing (M8). |

## 7. Local Gemma ctx reconciliation — the true number is **6144**, the "16K" comment is stale

- **Engine (authoritative):** `AI_BlackBox_Portal_Android_MVP (2)/.../data/local/LiteRtEngine.kt:613` — `const val DEFAULT_MAX_TOKENS: Int = 6144`, the GPU-survivable default (device-proven on the Fold 2026-06-19: 6144 warmed + survived; **8192 and 16384 both OOM'd on GPU** — KV cache is pre-allocated in pinned unpageable buffers). `LiteRtEngine.kt:624` — `ABSOLUTE_MAX_TOKENS = 16384` is a user-owned sanity ceiling (CPU/high-RAM only, settings UI warns above 6144); `LiteRtEngine.kt:632` — `MIN_TOKENS = 512` floor.
- **Server catalog (mirrors engine):** `Orchestrator/local_provider/catalog.py:136,148,179` — `"max_tokens": 6144` on every curated + auto-discovered bundle.
- **The stale claim:** `Orchestrator/context_builder.py:58` — `PROVIDER_CAPS["local"] = 16000` with comment "reserves ~12K of the phone's 16K window". This conflates the 16,384 ABSOLUTE ceiling with the actual window; the real default window NOW is **6144 tokens**, and per the LiteRtEngine KDoc (lean strategy, 2026-06-30) the on-device chat path gets **no pushed fossil context at all** (`config.ini:42-43`, `local_semantic_k = 0`) — the only live consumer of `provider="local"` is `Orchestrator/routes/local_routes.py:323`.
- **Number that feeds M8:** 6,144 tokens (default, GPU-safe), 16,384 (explicit user override ceiling, OOM risk owned by user).

## 8. What M7 must do (per provider that didn't pass, + inventory additions)

- **anthropic:** Before deleting `PROVIDER_CAPS["anthropic"]` (75k, `context_builder.py:55`): (1) raise the Android OkHttp SSE read timeout and/or add server heartbeat/keepalive events on `/chat/stream` so ≥14s (observed; historically 30–60s) first-token silences survive; same review for the Portal EventSource and voice stream consumers; (2) re-run `scripts/audit_provider_windows.py --only anthropic` **through the Android client path** as the acceptance gate (the M3 server-side pass is necessary, not sufficient). The server-side leg (httpx timeout=300, `chat_routes.py:2621`) already tolerates it.
- **anthropic (new cap discovered):** the 15,000-char per-message truncation of user/assistant history on the anthropic path only (`truncate_large_content`, `chat_routes.py:2507-2519`) is a delivery cap NOT in the WI-10 inventory. M7 must decide: re-derive it from the verified window (it silently mutilates long pasted user content today) or delete it alongside the fossil caps.
- **openai:** no transport work needed; when removing `PROVIDER_CAPS["openai"]` (100k) set the window guard from 272k **input** tokens (not the headline 400k) minus response/tool headroom.
- **gemini/xai:** cap-free already (no PROVIDER_CAPS entry; global 200k char cap only). Removing `MAX_TOTAL_CONTEXT_CHARS` needs only the WI-11 token-math guard. xai guard should optionally warn at the 200k-token long-context price tier.
- **CU (gemini backend):** the 131,072-tok window makes this the one cloud surface where the window guard will actually bind — compute it per-model from live discovery (`inputTokenLimit`), never from the chat-provider default.
- **Voice routes:** size context budgets from each session model's own window (128k / 131k / ~256k) and `gpt-realtime-2`'s 16,384-tok instructions+tools cap — do not inherit chat budgets.
- **Route-default drift:** unify `/chat/stream` hardcoded fallbacks (`claude-sonnet-4-5`, `gemini-3.1-pro-preview-customtools`) onto the config defaults so the audited models are the served models.

## 9. Spend

12 probes, ~545k true input tokens billed (anthropic 197,091 · gemini 120,449 · openai 114,479 · xai 113,475), ~30 output tokens. Est. cost ≈ **$1.50** (anthropic ≈ $0.99 @ $5/M; xai = $0.134 exact from `cost_in_usd_ticks`; openai ≈ $0.14; gemini ≈ $0.25). Zero retries, zero rate-limit events, zero ledger writes.

---

## Post-verification addendum (2026-07-02, independent review)

- **Prompt-cache contamination in openai/xai probes:** the 75k payload is a byte-identical prefix
  of the 210k/238k payloads, and provider prompt caches engaged on later probes (openai 210k:
  44,288/44,423 prompt tokens ≈99.7% cached; openai 238k: 7,552 cached; xai 238k: 8,320 cached —
  see `cached_tokens` in the probes JSON). PASS verdicts stand on window headroom, but **M7 must
  not size timeouts off openai/xai TTFBs at ≥210k** — they are cache-warm, not cold-prefill.
- **Anthropic probes show zero caching** — the 14.04s TTFB at 210k is genuinely cold (n=1).
  M7 should harden for the historical 30–60s band, not 14s.
- **Re-run cautions:** the script has no confirmation gate (a bare re-run re-fires all 12 probes,
  ~$1.50); and the ledger guard hard-aborts if any legitimate future snapshot contains the probe
  marker literals — keep those strings out of dev snapshots; if the guard fires at baseline on a
  clean ledger, that is why.
