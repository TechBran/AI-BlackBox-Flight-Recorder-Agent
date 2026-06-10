# Live API GA Migration — OpenAI Realtime Beta→GA + Gemini Live mediaChunks→audio

> **For Claude:** Execute as ONE focused commit chain. Push to origin/main when done.

**Goal:** Restore both OpenAI Realtime and Gemini Live to working state by migrating from deprecated wire formats to current GA shapes. Preserve every Phase A–D feature already delivered (model selection, VAD modes, voices, thinkingLevel, reconnect-state, audit-clamp).

**Why now:** Hardware-validation T15 revealed two independent deprecations:
- OpenAI: `OpenAI-Beta: realtime=v1` header routes to a sunsetting Beta endpoint. New GA models (`gpt-realtime-2`) reject with *"only available on the GA API"*; the alias `gpt-realtime` is itself being rejected with *"The Realtime Beta API is no longer supported."* Both old and new IDs work on GA without the header.
- Gemini: `realtimeInput.mediaChunks` was deprecated. Every audio frame closes with code 1007 *"realtime_input.media_chunks is deprecated. Use audio, video, or text instead."* This affects ALL Gemini Live models (2.5 + 3.1) — Brandon's "3.1 silent" symptom is actually mediaChunks rejection on the 2.5 default.

**Tech Stack:** Python FastAPI (`Orchestrator/routes/realtime_routes.py`, `gemini_live_routes.py`), `websockets` library, OpenAI Realtime GA, Google Generative Language Live GA.

---

## Empirical-verified wire-format diff

### OpenAI Beta → GA

| Field | Beta (current) | GA (target) |
|---|---|---|
| Header | `OpenAI-Beta: realtime=v1` | **REMOVED** |
| URL | `wss://api.openai.com/v1/realtime?model=X` | same |
| `session.type` | (absent) | `"realtime"` (required) |
| `session.modalities` | `["text", "audio"]` | renamed `output_modalities: ["audio"]` (audio only on GA — text-only mode is separate) |
| `session.voice` (flat) | `"ash"` | nested: `session.audio.output.voice: "ash"` |
| `session.input_audio_format` (string) | `"pcm16"` | nested struct: `session.audio.input.format: {"type": "audio/pcm", "rate": 24000}` |
| `session.output_audio_format` (string) | `"pcm16"` | nested struct: `session.audio.output.format: {"type": "audio/pcm", "rate": 24000}` |
| `session.input_audio_transcription` (flat) | `{"model": "whisper-1"}` | nested: `session.audio.input.transcription: {"model": "whisper-1"}` |
| `session.turn_detection` (flat) | `{...}` | nested: `session.audio.input.turn_detection: {...}` |
| `session.instructions` | unchanged | unchanged |
| `session.tools` | unchanged | unchanged |
| `session.tool_choice` | unchanged | unchanged |
| `session.max_output_tokens` | unchanged | unchanged |
| `session.audio.output.speed` (new) | — | `1.0` (optional) |
| `session.tracing` (new) | — | `null` (optional) |
| `session.truncation` (existing) | unchanged | unchanged |

**Probed empirically** by sending a GA-shape `session.update` and observing the `session.updated` echo include our exact values (`voice: "ash"`, `turn_detection.threshold: 0.7`, `idle_timeout_ms: 30000`, `transcription.model: "whisper-1"`). No errors. GA accepts the shape.

**Event types** going both directions are documented as unchanged in name (`session.update`, `session.updated`, `input_audio_buffer.append`, `input_audio_buffer.commit`, `input_audio_buffer.speech_started/stopped`, `conversation.item.create`, `conversation.item.input_audio_transcription.completed`, `response.create`, `response.audio.delta`, `response.audio_transcript.delta`, `response.done`, tool/function call events). To be smoke-tested but expected to JustWork.

### Gemini Live mediaChunks → realtime_input.audio

| Frame type | Old (deprecated) | New (GA) |
|---|---|---|
| Audio | `{"realtimeInput": {"mediaChunks": [{"mimeType": "audio/pcm;rate=16000", "data": "<b64>"}]}}` | `{"realtime_input": {"audio": {"mimeType": "audio/pcm;rate=16000", "data": "<b64>"}}}` |

**Verified** against the upstream `google-genai` SDK at `Orchestrator/venv/lib/python3.12/site-packages/google/genai/live.py:241+`. The `send_realtime_input()` method serializes to `{'realtime_input': {audio|video|text}}` format (line 344).

Note: snake_case `realtime_input` vs camelCase `realtimeInput` — Google's WS gateway accepts both, but the SDK uses snake_case. We'll match the SDK convention.

Also: per the error message *"Use audio, video, or text instead"*, only audio frames currently use mediaChunks in our codebase (3 sites). Text inputs use a different path (`clientContent.turns[].parts[].text`) which appears unaffected.

---

## File-by-file changes

### Track 1 — OpenAI Beta → GA (`Orchestrator/routes/realtime_routes.py`)

**1A — `connect_to_openai()` (line 269-286):** drop the `OpenAI-Beta: realtime=v1` header from the `headers` dict. URL build unchanged.

**1B — `configure_openai_session()` (line 297-507):** restructure the `config_event["session"]` payload from flat to nested per the diff table above. Specifically:

```python
# BEFORE (Beta shape, current line ~506-548):
config_event = {
    "type": "session.update",
    "session": {
        "modalities": ["text", "audio"],
        "instructions": system_instructions,
        "voice": voice,
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "input_audio_transcription": {"model": "whisper-1"},
        "turn_detection": turn_detection,
        "tools": [...],
        "tool_choice": "auto",
        "temperature": 0.8,
        ...
    }
}

# AFTER (GA shape):
config_event = {
    "type": "session.update",
    "session": {
        "type": "realtime",                          # NEW required field
        "output_modalities": ["audio"],              # renamed from modalities
        "instructions": system_instructions,         # unchanged
        "audio": {                                   # NEW nesting
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": "whisper-1"},
                "turn_detection": turn_detection,    # already-built object
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": voice,
                "speed": 1.0,
            },
        },
        "tools": [...],                              # unchanged
        "tool_choice": "auto",                       # unchanged
        # temperature is removed in GA (no temperature field in the response shape)
        ...
    }
}
```

The `turn_detection` build block (lines 482-503) stays unchanged — its shape is identical between Beta and GA. It just lives at a different path in the outer payload.

**1C — Verify event-type handlers (lines 633-1107):** these are likely unchanged but smoke-test for any `KeyError` after migration. Specifically inspect:
- `response.audio.delta` (line 644)
- `response.audio_transcript.delta` (line 653)
- `conversation.item.input_audio_transcription.completed` (line 1079)
- `input_audio_buffer.speech_started` (line 1100)
- `input_audio_buffer.speech_stopped` (line 1107)

If any payload field has moved (e.g., `delta` content path), patch.

**1D — Catalog revert:** undo my T15 over-correction. `Orchestrator/config.py:360` set default back to `"gpt-realtime-2"` (correct GA default). `OPENAI_REALTIME_MODELS` list should have `gpt-realtime-2` first with `"default": True`, followed by `gpt-realtime`, `gpt-realtime-1.5`, `gpt-realtime-mini`, `gpt-realtime-mini-2025-12-15` as the 5-entry chat list. Mirror in `Constants.kt` `MODEL_CONFIG["realtime"]` + `LIVE_MODEL_DEFAULTS["realtime"]`.

**1E — Test updates:** `Orchestrator/tests/test_live_models.py`
- `test_idle_timeout_ignored_under_semantic_vad` / `test_idle_timeout_honored_under_server_vad` / `test_idle_timeout_out_of_range_rejected` / `test_invalid_vad_type_falls_back_to_server_vad` all inspect `payload["session"]["turn_detection"]`. Update to `payload["session"]["audio"]["input"]["turn_detection"]`.
- `test_realtime_status_filters_non_chat_categories`: revert default-id assertion back to `gpt-realtime-2`. Update the "rejected ids" guard set to exclude `gpt-realtime-2` (it's now valid) and add only `gpt-realtime-2025-08-28` if we want to keep a guard.
- `test_allowlist_casing_precision`: same anchor update.

### Track 2 — Gemini Live mediaChunks → realtime_input.audio (`Orchestrator/routes/gemini_live_routes.py`)

Three call sites to migrate (lines 645, 726, 1441). All emit the same shape — replace as a unit:

```python
# BEFORE:
realtime_input = {
    "realtimeInput": {
        "mediaChunks": [{
            "mimeType": "audio/pcm;rate=16000",
            "data": base64_audio,
        }]
    }
}

# AFTER:
realtime_input = {
    "realtime_input": {
        "audio": {
            "mimeType": "audio/pcm;rate=16000",
            "data": base64_audio,
        }
    }
}
```

Two-name changes: outer `realtimeInput` → `realtime_input` (snake_case per SDK), inner `mediaChunks: [...]` → `audio: {...}` (single Blob, not a list).

### Track 3 — Investigate model-propagation bug

Brandon's UI selected `gemini-3.1-flash-live-preview` but the backend log showed `model=gemini-2.5-flash-native-audio-latest`. Possibilities:
- JS connect message omits `model` field (only sends it on first connect, not on the reconnect that happened due to mediaChunks errors)
- Backend `data.get("model", url_model)` resolves to URL fallback if JSON doesn't include it
- T14 F1 fix (`currentGeminiModel` module state) captures the value AT first-connect — if user changed dropdown AFTER connecting, the change wouldn't propagate

Trace: read `gemini-live.js` connect path + `gemini_live_routes.py` line 1583-1599 connect-message handler. If user changes a dropdown while connected, does the next reconnect pick up the new value or the stashed value?

Likely fix: the dropdown change events should also update the `current*` module state so reconnect always uses the latest user-selected value. One-line addition in each change handler.

This is small and self-contained — defer to a third commit only if a real bug is found. If the logs were just from an old session before Brandon switched, no fix needed; document and move on.

### Track 4 — Validation + GitHub push

- Run 8 unit tests (will need updates from 1E first)
- Restart backend, curl `/realtime/status` + `/gemini-live/status` to confirm shape unchanged for the status endpoints (they only emit the catalog, not session config)
- Smoke-test: actual WebSocket connect via the Portal — both providers should now connect cleanly. Brandon does live audio test.
- `git push origin main` — Brandon noted dev box has commits ahead of origin

---

## Commits (one per logical change for bisectability)

| # | Commit | Files | Smoke-test before next |
|---|---|---|---|
| 1 | `feat(realtime): migrate to OpenAI Realtime GA API (Beta deprecation)` | realtime_routes.py + config.py + Constants.kt + test_live_models.py | curl /realtime/status; Brandon verifies WS connect works on dev box |
| 2 | `fix(gemini-live): migrate realtimeInput.mediaChunks to realtime_input.audio` | gemini_live_routes.py (3 call sites) | Brandon verifies WS audio flow works on dev box |
| 3 | `fix(live-models): model dropdown change updates reconnect state` | gpt-realtime.js + gemini-live.js (if Track 3 surfaces a real bug; otherwise omit) | Brandon switches dropdown mid-session, verifies takes effect on reconnect |
| 4 | `git push origin main` (no commit — just push) | — | confirm `git log origin/main..HEAD` empty |

---

## Risks + rollback

- **Risk 1 — event-type drift.** GA may have renamed some `response.*` events. Mitigation: smoke-test events come back after Brandon's first audio frame; if any handler errors, patch as we discover. Each event handler is ~10 lines.
- **Risk 2 — voice rejection.** GA may not accept all 10 voices we have catalogued. Mitigation: live test with default `ash` first; if other voices fail, narrow the catalog (low-likelihood — `session.created` showed `voice: "marin"` as default, suggesting voices are unchanged).
- **Risk 3 — temperature field absence.** GA shape removed `temperature` (visible in `session.created` response — not present). We currently send `temperature: 0.8` in the Beta payload. GA may either accept-and-ignore OR reject. Mitigation: remove `temperature` from the new payload; if behavior degrades, investigate where the equivalent knob moved.
- **Rollback:** each commit is self-bisectable. `git revert` of commit 1 restores Beta-only working state for non-new-model use; revert of commit 2 puts mediaChunks back; revert of commit 3 is no-op if not landed.

---

## Out of scope (defer)

- WebRTC connection mode (GA also supports WebRTC, but our WebSocket path is sufficient and lower-complexity).
- `noise_reduction` field tuning (visible in GA response; we don't currently set it; defaults are fine).
- `tracing` field opt-in (visible in GA; we don't need it for first-pass migration).
- Affective dialog / proactive audio in Gemini 2.5 (v1alpha-only; separate endpoint).

---

## Verification commands

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc

# After Track 1
Orchestrator/venv/bin/python -m py_compile Orchestrator/routes/realtime_routes.py Orchestrator/config.py
Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_live_models.py -v
echo '<REDACTED-SECRET>' | sudo -S systemctl restart blackbox.service && sleep 20
curl -s http://localhost:9091/realtime/status | python3 -m json.tool | head -30
# Brandon: open Portal, connect to OpenAI Realtime, speak, verify response

# After Track 2
Orchestrator/venv/bin/python -m py_compile Orchestrator/routes/gemini_live_routes.py
echo '<REDACTED-SECRET>' | sudo -S systemctl restart blackbox.service && sleep 20
echo '<REDACTED-SECRET>' | sudo -S journalctl -u blackbox.service --since="1 minute ago" -o cat | grep -iE 'gemini|1007' | tail -10
# Brandon: open Portal, connect to Gemini Live, speak, verify response (no close-1007)

# After Track 3 (if needed)
# Brandon: switch model dropdown mid-session, reconnect, verify new model takes effect

# After Track 4 (push)
git push origin main
git log origin/main..HEAD  # should be empty
```
