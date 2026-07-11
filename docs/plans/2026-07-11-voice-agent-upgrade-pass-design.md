# Voice Agent Pipeline Upgrade Pass — Design

**Date:** 2026-07-11
**Status:** Approved by Brandon (brainstorming session, section-by-section)
**Scope:** All three realtime voice providers (OpenAI, Gemini, Grok/xAI) across backend + Portal + Android + phone bridge. Maximal pass: newest models, all provider features, xAI agent presets + provisioned phone number, translation mode, affective dialog, voice cloning.

## Context: what the recon found (2026-07-11, 10-agent workflow)

### Gemini silent failure — ROOT CAUSE CONFIRMED (live reproduction)

- `ToolVault/tools/update_sheet_values/schema.json` declares `values: {"type":"array","items":{"type":"array"}}` — inner array has **no `items`**. Google's BidiGenerateContent setup validator rejects the entire 56-tool setup with WS close **1007** (`function_declarations[N].parameters.properties[values].items.items: missing fi[eld]`).
- Timeline fits exactly: tool landed 2026-06-20 (`ff43d8b`); last working Gemini Live session Jun 14; every attempt since Jun 26 died in a 1007 storm (~150 lines/session in journalctl). Model-independent (3.1-preview AND the 2.5 fallback fail identically).
- Live probe: bare session on `gemini-3.1-flash-live-preview` returns 32,642 bytes of audio; same session + backend's exact tools reproduces the identical 1007 (index 53 today = update_sheet_values).
- **Silence layer 1 (backend):** `gemini_live_routes.py` labels the 1007 "Handler error (non-fatal)", reconnects with the same broken setup 5×, never forwards the close code, keeps answering client pings.
- **Silence layer 2 (Android):** `VoiceClient.parseMessage` has no cases for `disconnected`/`reconnecting`/`reconnected`/`status` — UI shows "Connected — listening" forever.
- **Second latent killer:** `gemini_reconnect` (gemini_live_routes.py:1339-1419) never respawns `gemini_listener` (only spawn site: line 1665). After ANY goAway/staleness reconnect the session is a permanently mute one-way pipe reporting "reconnected"; reconnect_count resets on every "success" so it loops forever. Google cuts Live connections at ~10 min, so every long session hits this. The phone bridge fixed this for itself (`phone/bridge.py:1322-1400` listener-respawning loop); the Portal/Android path never got it. This explains months of "Gemini is the flakiest."

### Provider state (July 2026, research-confirmed from official docs)

**OpenAI:** newest API models = `gpt-realtime-2.1` / `gpt-realtime-2.1-mini` (GA 2026-07-06, same price as gen-2, better ASR/interruptions/noise, ≥25% p95 latency cut). No gpt-realtime-3. **GPT-Live-1 (2026-07-08) is ChatGPT-only** — replaced Advanced Voice Mode; no API model ID exists in docs/pricing. Brandon asked for GPT-Live-1: plan includes an empirical WS probe of gpt-live IDs (per the "wire probes beat documentation" rule); if it ever lands in the API, the live-fetched catalog picks it up. Beta protocol removed 2026-05-12 (we already migrated). `gpt-realtime-mini-2025-10-06` shuts down 2026-07-23 (we don't list it). New features: noise_reduction (near_field/far_field), marin/cedar voices, MCP-in-session, SIP GA, image input, prompt objects, tracing, gpt-realtime-whisper `delay` knob. Docs claim `gpt-realtime-2025-08-28` is valid — contradicts our May close-4000 test; re-probe.

**Gemini:** `gemini-3.1-flash-live-preview` is alive and THE recommended Live model (not retired; probe-confirmed). `gemini-2.5-flash-native-audio-preview-12-2025` deprecated (no date); half-cascade models shut down 2025-12-09. 3.1 supports thinkingLevel, search grounding, native input/output transcription, session resumption, context compression — but NOT affective dialog / proactive audio (2.5-native-audio only, **v1alpha** only). Server default (config.py:540 = 2.5-latest) contradicts the recorded "default = 3.1 preview deliberate" decision and Android's default — align to 3.1 everywhere.

**xAI:** current models `grok-voice-latest` (alias) → `grok-voice-think-fast-1.0` (flagship, background reasoning, `reasoning.effort` high|none); `grok-voice-fast-1.0` deprecated. **Our code never sends a model at all** — no `?model=` on the WS URL, no session.model; `"grok-voice-agent"` (grok_live_routes.py:1403) is a cosmetic label. Voice Agent Builder (beta 2026-07-01) agent CRUD is **console-only**; but phone provisioning is API: `POST /v2/phone-numbers` (one FREE number/account, +$0.01/min; signing secret returned once), signed `realtime.call.incoming` webhook, attach via `wss://api.x.ai/v1/realtime?call_id={id}`, `/refer` + `/hangup` call control, BYO SIP `sip:{number}@sip.voice.x.ai`. Also: session resumption (`resumption.enabled` + `?conversation_id=`), `replace` pronunciation map, `keyterms` (≤100), `language_hint`, custom voices `/v1/custom-voices` (clone from ≤120s audio), ephemeral client secrets, built-in server-side tools (web_search, x_search, file_search, MCP). Realtime $0.05/min. Docs don't list server_vad threshold/padding knobs the code sends — may be ignored (probe).

### Other confirmed defects (recon)

- Android captures Grok mic at 16 kHz but backend declares 24 kHz to xAI (VoiceScreen.kt:288-291 vs grok_live_routes.py:448-452) — silent quality degradation.
- Tool-dispatch in ALL THREE routes has no per-tool try/except: an exception dangles the model's tool call → silent dead turn (gemini_live_routes.py:937-1290, grok_live_routes.py:613-1016, realtime equivalent).
- Grok: transcript cleared even after a FAILED save (grok_live_routes.py:161); input transcription never configured in session.update (user turns may be missing from saved transcripts).
- All three routes freeze tool lists at import time (`GEMINI_LIVE_TOOLS` line 98, `GROK_LIVE_TOOLS` line 88, `REALTIME_TOOLS`) — `/toolvault/reload` never reaches live voice; restart required. This is why a June 20 tool addition detonated invisibly.
- Session save uses `POST /chat` (full LLM round-trip) instead of `/chat/save` (~400× cheaper; CLAUDE.md mandate) in all three routes.
- Android: model/voice lists hardcoded in Constants.kt (violates provider-API-as-SoT); statusPath dead code (no preflight); no CONNECTING timeout; no client reconnect (SttStreamClient has the proven pattern); no barge-in UI (`interrupt` never sent; mic client-muted during AI speech); tool calls invisible; settings don't persist; `else`-less parseMessage.
- Prompt/tool mismatch: system prompt mandates `get_recent_snapshots` but the declared group ships `list_recent_snapshots`.
- Stale docs: tool_registry.py:18 says "~21 tools" (actual: 56); realtime module header says "GPT-4o Realtime" (line shut down 2026-05-07).

## Decisions (Brandon, 2026-07-11)

1. **Gemini fix bundled with the pass** (no immediate hotfix).
2. **xAI full scope**: local agent presets + provisioned phone number.
3. **Keep the server-side relay** — no client-direct/ephemeral-token path. Keys stay on the box; tools + transcripts stay server-side (exosuit + audit-trail architecture).
4. **All extras in scope**: translation voice mode, Gemini affective dialog + proactive audio (2.5/v1alpha-gated), xAI custom voice cloning.
5. **OpenAI "newest layer" = gpt-realtime-2.1 family** (GPT-Live-1 is ChatGPT-only; probe + catalog-liveness covers its possible API arrival).

## Design

### Workstream 1 — Gemini rescue

1. Fix `update_sheet_values` schema (inner `items: {"type": "string"}` — cell values; verify against actual sheets usage).
2. New ToolVault validator rule (`Orchestrator/toolvault/validate.py`): recursively reject any `array` type lacking `items`. Applies to every module; CI gate.
3. Un-freeze tool snapshots in all three routes: read `get_*_tools(group)` at session-configure time. `/toolvault/reload` then reaches voice sessions.
4. Reconnect rebuild: port phone bridge's listener-respawning loop; persist `model`, `vad_*`, `thinking_level`, `custom_role`, `phone_mode` on `GeminiLiveSession` (models.py:111-138 has no fields today) so reconfigure stops reverting; honor `goAway.timeLeft` (graceful pre-migration); stop resetting reconnect_count to defeat max_reconnects.
5. Silence kill: forward WS close code/reason as client `error` events; terminal `disconnected` also CLOSES the portal WS; check `_safe_ws_send` returns for critical frames.
6. Native transcription: enable `inputAudioTranscription`/`outputAudioTranscription` in setup; parse the real field shapes (objects with `.text`); retire post-hoc Whisper hop (keep as fallback only); removes the `/stt/json` quota dependency from the voice path.
7. Default model = `gemini-3.1-flash-live-preview` in config.py + /gemini-live/status + Constants.kt (already there) — one canonical default.
8. Post-fix probe: full 56-tool setup must pass on BOTH 3.1-preview and 2.5-latest (a second latent schema violation would fail at the next index).

### Workstream 2 — OpenAI + Grok modernization

**OpenAI:**
- Catalog += `gpt-realtime-2.1` (default), `gpt-realtime-2.1-mini`; WS-probe each before listing; re-probe `gpt-realtime-2025-08-28`; probe gpt-live IDs.
- Wire noise_reduction (near_field default on phone bridge; configurable Portal/Android); ensure marin/cedar in voice catalog; transcription `delay` knob.
- Clean stale header/docstrings.

**Grok:**
- Send `?model=` on WS URL; catalog `grok-voice-latest` (default) + `grok-voice-think-fast-1.0` (pin), new `models`/`model_default` fields on `/grok-live/status`; kill the cosmetic label.
- Expose `reasoning.effort` (high|none) in session config + UI.
- Explicitly configure input transcription in session.update.
- Session resumption: `resumption.enabled: true`, store `conversation.id` from `conversation.created`, reconnect with `?conversation_id=` instead of full context rebuild.
- `replace` pronunciation map + `keyterms` (seed from operator contacts) + `language_hint`.
- Fix sample-rate mismatch: probe which rate xAI prefers, then either declare 16k or capture 24k — one truth across Android/backend/asterisk map.
- Live probes FIRST: no-model default resolution; transcription default on/off; server_vad knob honoring (phone bridge retune may be a no-op).

### Workstream 3 — Voice Agent Presets + xAI phone number

**Presets (provider-agnostic local "agent builder"):**
- Preset = `{id, name, provider, model, voice, instructions, tool_group_override?, greeting?, language?, keyterms?, created_by}`.
- Registry JSON following `Orchestrator/onboarding/custom_servers.py` conventions: fresh-read per request, atomic writes, corrupt quarantine, gitignored.
- CRUD: `GET/POST /voice-agents`, `PATCH/DELETE /voice-agents/{id}`.
- `?agent=<id>` on all three `/ws/*-live/` endpoints; precedence: explicit params > preset > defaults.
- Selectable in Portal voice panel, Android voice screen, `make_phone_call` role param, cron calls.

**Phone number (sovereign line — no Twilio hop):**
- Provision via `POST /v2/phone-numbers` with webhook attach (not console agent_id); signing secret → `credentials/`, 0600.
- `POST /xai/voice/incoming`: verify signed webhook (webhook-id/timestamp/signature), open `wss://api.x.ai/v1/realtime?call_id=`, run through existing Grok bridge machinery (config, tools, transcript-to-ledger, reaper). Inbound call = Grok voice session with a different transport origin.
- Public exposure via Tailscale Funnel (MCP-remote pattern).
- `/refer` + `/hangup` wired as agent-invocable tools.
- Default preset per line; Twilio stays as second line (additive).

### Workstream 4 — Android uplift (+ Portal parity, 3-surface rule)

- Handle `disconnected`/`reconnecting`/`reconnected`/`status`; `else` branch logs unknown types; CONNECTING timeout; mic-loop send-failure detection; reconnect-with-resume ported from SttStreamClient.
- Catalog-driven models/voices/presets from `/status` endpoints at screen open; Constants.kt lists become fallbacks; settings persist.
- Barge-in: tap-to-interrupt sends `interrupt`; investigate mic-open-during-AI-speech with AEC stack (Gemini `START_OF_ACTIVITY_INTERRUPTS`).
- Tool-call chips in transcript (tool_call/tool_result/image_task/video_task/music_task).
- Text input UI during voice sessions; Grok config UI (model + reasoning).
- Fix hardcoded `"Brandon"` operator fallback (VoiceScreen.kt:144).
- Portal JS modules (gpt-realtime.js, gemini-live.js, grok-live.js) get equivalent additive changes.

### Workstream 5 — Extras

- **Translation mode**: 4th voice mode; `gpt-realtime-translate` + `gemini-3.5-live-translate-preview` behind target-language picker; Grok greyed out. Empirical probe of both session shapes first.
- **Affective dialog + proactive audio (Gemini)**: `enableAffectiveDialog` + `proactivity.proactiveAudio`, gated to 2.5-native-audio models AND v1alpha endpoint (per-session URL version selection; 3.1 rejects these fields). UI labels "2.5 only (deprecated line)".
- **xAI voice cloning**: `/v1/custom-voices` CRUD in Voice Lab (Portal + Android), consent-gated like `elevenlabs_clone_voice` (`confirm_consent=true`); cloned voice ids usable in Grok sessions + phone line.

### Workstream 6 — Cross-cutting hardening + testing

- Tool dispatch wrapped in all three routes → error `function_call_output`/`functionResponse`, never dangle.
- Transcript persistence → `/chat/save`; clear conversation only after confirmed save.
- Listener-respawn parity audit on OpenAI + Grok reconnect paths.
- `get_recent_snapshots`/`list_recent_snapshots` prompt-tool mismatch fixed; stale comments corrected.
- **WS-probe harness** in `diagnostics/`: per-provider, per-model connect + setup assert; run before any catalog change; doubles as smoke test (replaces the voice-blind test_grok.sh).
- Unit tests: validator rule; reconnect respawn (fake WS); preset registry (fresh-read/corrupt/empty); webhook signature; Android VoiceClient parse + state machine.
- Fresh-box gate: empty registry, no keys, no hardcoded operator — graceful degradation everywhere.

## Rollout (each phase independently shippable; service runs live from working tree)

- **P0** Live probes (xAI model/transcription/VAD, OpenAI 2.1/gpt-live/2025-08-28, Gemini post-fix 56-tool, translate shapes)
- **P1** Gemini rescue + cross-route hardening
- **P2** OpenAI/Grok modernization + catalogs
- **P3** Android uplift + Portal parity
- **P4** Agent presets
- **P5** xAI phone number
- **P6** Extras

## Key risks

- Affective/proactive requires v1alpha on a deprecated model line — feature may outlive its model; UI must label clearly.
- xAI webhook needs Funnel exposure — verify signed-webhook verification before exposing.
- `-latest` alias semantics for gemini-2.5-native-audio unknown (kept in catalog, not default).
- Translation models' wire shapes unverified — P0 probes gate the feature.
- 56 tool schemas ride every session setup; nobody has measured their latency/token cost (flagged; possible follow-up trim).
