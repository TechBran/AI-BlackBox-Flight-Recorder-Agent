# Live Models Upgrade — OpenAI Realtime + Gemini Live (web + Android) — v2 (audit-revised)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan task-by-task.

**Goal:** Upgrade BlackBox's OpenAI Realtime + Gemini Live to current 2026 capabilities. Bump OpenAI to `gpt-realtime-2`, add `semantic_vad` + `idle_timeout_ms`. Add Gemini 3.1 Live as opt-in alongside `-latest` alias. Expand Gemini voices 6→30. All changes mirrored across **web Portal AND Android MVP**. Grok Live unchanged (xAI doesn't publish Realtime docs).

**Tech Stack:** Python FastAPI + websockets, vanilla JS + index.html dropdowns, Kotlin Compose + VoiceScreen.kt.

---

## Context

**Brandon's mandate (2026-05-19):** upgrade GPT Realtime + Gemini Live "across the board" — model + voices + new features. Mirror changes in both web + Android. Use full Superpowers plan + execution.

**Discovery summary** (from v1 plan, still valid):
- OpenAI: model `gpt-realtime` → `gpt-realtime-2` available; voices unchanged (10); NEW `semantic_vad` + `idle_timeout_ms` features
- Gemini: model adds `gemini-3.1-flash-live-preview` + `-latest` alias; voices 6→30 expansion available
- Grok: undocumented — out of scope

---

## Audit Findings Resolved in v2

v1 plan had 3 CRITICAL + 5 MAJOR + 5 IMPORTANT findings that fundamentally reshaped the architecture. v2 reflects the verified-in-source truth:

| # | Severity | Finding | Resolution in v2 |
|---|---|---|---|
| C1 | CRITICAL | Routes dispatch on `msg_type == "connect"`, not `"session"` (verified `realtime_routes.py:1279`, `gemini_live_routes.py:1487`). v1's "locked session-config contract" would silently no-op. | All client→server config extensions go via the EXISTING `"connect"` message OR via URL query string. No new message type. |
| C2 | CRITICAL | `phone/bridge.py` has 6 call sites of `configure_openai_session()` / `configure_gemini_session()` with positional args (lines 744, 813, 848, 1286, 1322, 1370). v1's signature changes would break the phone bridge. | All new params land as `Optional[…] = None` keyword args. Existing call sites continue working unchanged. |
| C3 | CRITICAL | OpenAI Realtime model is set via URL query param at WS connect time (`realtime_routes.py:249`: `f"{OPENAI_REALTIME_URL}?model={OPENAI_REALTIME_MODEL}"`), NOT via `session.update`. v1's "pass model into session.update payload" would be silently ignored by OpenAI. | `connect_to_openai(session, model: str = None)` accepts optional model, rebuilds the URL. Switching models = closing + reopening upstream WS. |
| M1 | MAJOR | `gpt-realtime-mini-2025-12-15` date suffix unverified in v1 | Verified via live `client.models.list()` against the customer's OpenAI key — yes, `gpt-realtime-mini-2025-12-15` is the current dated mini variant alongside `gpt-realtime-mini` alias |
| M2 | MAJOR | v1 had `GEMINI_LIVE_VOICE_DESCRIPTORS` truncated as "... 30 mapped" with no actual mapping | Full 30-name + descriptor table included verbatim in this v2 (Phase A1 spec below) |
| M3 | MAJOR | Voice list lacks verification source | Sourced from `https://ai.google.dev/gemini-api/docs/speech-generation` "Voice options" table, fetched 2026-05-19 via WebFetch |
| M4 | MAJOR | Android passes voice via URL query string (verified `VoiceClient.kt:102`: `?operator=$operator&voice=$voice`) — no JSON session-config path | Android extends the URL query: `?operator=X&voice=Y&model=Z&vad_type=...&vad_eagerness=...`. Backend route signature parses these via FastAPI Query() params. Per Brandon's explicit choice 2026-05-19. |
| M5 | MAJOR | `/realtime/status` (line 1411) + `/gemini-live/status` (lines 1618, 1645-1647) emit `model` + `voices` (single values today) | Status endpoints now emit `model_default`, `models[]` catalog, `voices[]` catalog. Android dropdown source-of-truth aligned. |
| I1 | IMPORTANT | New params lack server-side allowlist validation | Per-field allowlists in `realtime_routes.py` + `gemini_live_routes.py` (matching `grok_live_routes.py:294`'s `if voice not in GROK_LIVE_VOICES` pattern). Invalid values fall back to default with a logged warning. |
| I3 | IMPORTANT | `interrupt_response` + `create_response` available in BOTH server_vad + semantic_vad (per SDK type stubs) | Spec explicitly notes both fields apply to both VAD modes. UI exposes both as toggles regardless of vad_type. |
| I4 | IMPORTANT | `gpt-realtime-whisper` is STT-only — would silently fail in voice-conversation UI if selected | Filter `gpt-realtime-whisper` and `gpt-realtime-translate` from the chat-voice variant dropdown (they're specialized, exposed as a separate "Specialized modes" submenu if at all in v1). Default dropdown shows only conversational variants: `gpt-realtime-2`, `gpt-realtime`, `gpt-realtime-1.5`, `gpt-realtime-mini-2025-12-15`. |
| I5 | IMPORTANT | Plan implied symmetric model-injection (OpenAI = URL, Gemini = setup) — they're asymmetric | v2 spells out the asymmetry per provider — Phase A2 (OpenAI URL rebuild) vs Phase A3 (Gemini setup message extension). |
| I7 | IMPORTANT | No customer-without-API-key failure path | Status endpoints already report `enabled: false` when key missing; web/Android show "(API key not configured)" inline above dropdown. Existing UI pattern. |
| N1 | NICE | Verification step assumed pytest tests exist that don't | T1 verification adds `find Orchestrator/tests -name "*realtime*"` precheck; if no tests exist, T1 includes adding minimal ones. |
| N2 | NICE | `main.css?v=genui266` arbitrary version | Read current version first (`grep "main.css?v=" Portal/index.html`), bump to next. |
| N3 | NICE | T2 before T3 ordering note | T1 backend changes MUST land before T2 (web) and T3 (Android) — both UI tracks depend on the URL-query/JSON contract being stable. |

---

## Architecture (v2 — verified-in-source)

### Wire-format contract per provider

**OpenAI Realtime** (asymmetric to Gemini):
- **Connect-time URL param**: `model` is set via `?model=...` on `wss://api.openai.com/v1/realtime`. To switch models = new upstream WebSocket.
- **Post-connect `session.update`**: `voice`, `turn_detection` (server_vad OR semantic_vad), `idle_timeout_ms` (server_vad only), `interrupt_response`, `create_response`, `temperature`, `instructions`. These can change mid-session.
- **Client → BlackBox (web)**: JSON connect message gets new fields: `model`, `vad_type`, `vad_eagerness`, `idle_timeout_ms`, `interrupt_response`.
- **Client → BlackBox (Android)**: URL query string extended: `?operator=X&voice=Y&model=Z&vad_type=semantic_vad&vad_eagerness=medium&idle_timeout_ms=30000`.

**Gemini Live**:
- **Connect-time URL param**: None — model is in the setup message.
- **Post-connect `setup`**: `model`, `voiceConfig.prebuiltVoiceConfig.voiceName`, `realtimeInputConfig.automaticActivityDetection.{startOfSpeechSensitivity,endOfSpeechSensitivity}`, `generationConfig.thinkingConfig.thinkingLevel` (3.1 only — VERIFIED PATH per google-genai SDK 1.64.0 `types.py:ThinkingConfig` + existing `gemini_live_routes.py:367` which already builds `setup_config["generationConfig"]`).
- **Client → BlackBox (web)**: JSON connect message gets `model`, `voice`, `vad_sensitivity_start`, `vad_sensitivity_end`, `thinking_level`.
- **Client → BlackBox (Android)**: URL query string extended: `?operator=X&voice=Y&model=Z&vad_start=MEDIUM&vad_end=MEDIUM&thinking_level=STANDARD`.

**Mid-session model-switch UX (audit I4):** The `model` parameter is bound at upstream WebSocket connect time for OpenAI (URL query) and at the initial setup message for Gemini Live. Neither provider supports mid-session model changes — switching models requires closing and reopening the upstream WS, which means tearing down audio I/O and losing conversation context. Rather than silently accepting a dropdown change that does nothing (or worse, doing a hidden reconnect that drops audio mid-sentence), the UI must:

- **Web** (`gpt-realtime.js`, `gemini-live.js`): when WebSocket state transitions to `CONNECTED`, set `realtimeModelSelect.disabled = true` (and Gemini equivalent). When state transitions to `DISCONNECTED`, re-enable. Same rule for vad_type (server_vad vs semantic_vad have different upstream session schemas) but NOT for voice or eagerness (those are hot-swappable via `session.update`).
- **Android** (`VoiceScreen.kt`): wrap the model `DropdownMenu` with `enabled = sessionState != ConnectionState.Connected`. Same rule for vad_type. Voice + eagerness stay always-enabled.

This is a deliberate UX trade-off: users who want to compare models do so by Disconnect → change → Reconnect, with conversation reset implicit in that flow.

**Both providers**: status endpoints (`/realtime/status` + `/gemini-live/status`) emit the catalog so both surfaces consume from there (NO duplicated JS-side or Kotlin-side hardcoded model/voice lists — matches yesterday's T1-T3 chat-model centralization pattern). **Locked response shape:**

```jsonc
{
    "enabled": true,                     // false if API key missing
    "model_default": "gpt-realtime-2",
    "models": [                          // array of objects with id + name + default flag
        {"id": "gpt-realtime-2", "name": "GPT Realtime 2 (Newest GA)", "default": true},
        {"id": "gpt-realtime", "name": "GPT Realtime (Alias)"},
        ...
    ],
    "voice_default": "ash",
    "voices": ["alloy", "ash", "ballad", ...],  // FLAT STRING ARRAY — matches Android VOICES_GEMINI: List<String>
    "voice_descriptors": {                       // Gemini-only — maps voice name → character
        "Zephyr": "Bright", "Charon": "Informative", ...
    }
}
```

Android consumes `voices: List<String>` directly (existing shape). Web JS renders option labels from `voice_descriptors[voiceName]` if present, otherwise just the voice name. No duplicate JS-side catalog — fetch from `/realtime/status` + `/gemini-live/status` on app init, cache in sessionStorage for 5min (same as the chat-model centralization sessionStorage pattern at `state-management.js:526`).

### Source-of-truth file map

| Concern | File | Notes |
|---|---|---|
| Voice + model catalogs | `Orchestrator/config.py` | Add `OPENAI_REALTIME_MODELS`, `GEMINI_LIVE_MODELS`, expand `GEMINI_LIVE_VOICES` to 30, add `GEMINI_LIVE_VOICE_DESCRIPTORS` |
| OpenAI upstream WS URL builder | `Orchestrator/routes/realtime_routes.py` `connect_to_openai()` line 234-258 | Accept `model` param; rebuild URL with `?model={chosen}` |
| OpenAI session.update payload builder | `Orchestrator/routes/realtime_routes.py` `configure_openai_session()` line 276+ | Add kwargs: `vad_type`, `vad_eagerness`, `idle_timeout_ms`, `interrupt_response`, `create_response`. All Optional with None defaults. |
| Gemini setup message builder | `Orchestrator/routes/gemini_live_routes.py` `configure_gemini_session()` line 206+ | Add kwargs: `model`, `vad_sensitivity_start`, `vad_sensitivity_end`, `thinking_level`. All Optional with None defaults. |
| OpenAI "connect" message handler | `realtime_routes.py:1279-1340` | Read new fields from JSON; pass to `configure_openai_session()` |
| OpenAI status endpoint | `realtime_routes.py:1411` | Emit `models[]` (filtered catalog) + `voices[]` + `model_default` + `voice_default` |
| Gemini "connect" message handler | `gemini_live_routes.py:1487-1547` | Same shape as OpenAI |
| Gemini status endpoint | `gemini_live_routes.py:1618+1645-1647` | Same shape as OpenAI |
| Web Portal — realtime banner | `Portal/index.html:159-170` | Add `<select id="realtimeModelSelect">` + `<select id="realtimeVadSelect">` + `<select id="realtimeEagernessSelect">` |
| Web Portal — gemini banner | `Portal/index.html:218-225` | Add model dropdown + expand voice options 6→30 + VAD sensitivity selectors |
| Web Portal — realtime JS | `Portal/modules/gpt-realtime.js:1155-1159` connect() | Read model + vad_type + eagerness + idle_timeout from new UI; include in connect message |
| Web Portal — gemini JS | `Portal/modules/gemini-live.js:806-810` connect() | Read model + voice + sensitivity + thinking_level; include in connect message |
| Android voice catalog | `data/voice/VoiceClient.kt:102` URL builder | Extend query string with `model`, `vad_type` etc. |
| Android voice UI | `ui/voice/VoiceScreen.kt:553-560` VOICES_*+ dropdowns | Expand VOICES_GEMINI 6→30; add MODELS_GPT_REALTIME + MODELS_GEMINI_LIVE catalogs; add ModelDropdown + VadDropdown composables |
| Phone bridge (no changes needed) | `phone/bridge.py:744, 813, 848, 1286, 1322, 1370` | Verified — all 6 sites only pass positional `(session, operator)` or `(session, operator, voice, custom_role)`. Kwargs defaults protect us. |

### Complete catalogs (v2 explicit — audit M2/M3 fix)

**OpenAI Realtime models (verified via `client.models.list()` 2026-05-19):**
```python
OPENAI_REALTIME_MODELS = [
    # Conversational variants (UI dropdown)
    {"id": "gpt-realtime-2", "name": "GPT Realtime 2 (Newest GA)", "default": True, "category": "chat"},
    {"id": "gpt-realtime", "name": "GPT Realtime (Alias)", "category": "chat"},
    {"id": "gpt-realtime-1.5", "name": "GPT Realtime 1.5", "category": "chat"},
    {"id": "gpt-realtime-mini-2025-12-15", "name": "GPT Realtime Mini (Cheap)", "category": "chat"},
    # Specialized variants (NOT in main dropdown; audit I4)
    {"id": "gpt-realtime-translate", "name": "GPT Realtime Translate", "category": "translate"},
    {"id": "gpt-realtime-whisper", "name": "GPT Realtime Whisper (STT-only)", "category": "transcribe"},
]
```

Backend route filters `category == "chat"` when serving the dropdown catalog. Specialized variants accessible via direct env-var override (`OPENAI_REALTIME_MODEL=gpt-realtime-translate`) for power users.

**Gemini Live models (verified via `genai.list_models()` 2026-05-19):**
```python
GEMINI_LIVE_MODELS = [
    {"id": "gemini-2.5-flash-native-audio-latest", "name": "Gemini 2.5 Flash Live (Latest GA-track)", "default": True},
    {"id": "gemini-3.1-flash-live-preview", "name": "Gemini 3.1 Flash Live (Preview, thinkingLevel)"},
    {"id": "gemini-2.5-flash-native-audio-preview-12-2025", "name": "Gemini 2.5 Flash Live (Dec 2025 pin)"},
]
```

**Gemini Live voices — complete 30-entry catalog with character descriptors:**

```python
GEMINI_LIVE_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda",
    "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus",
    "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima",
    "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]

GEMINI_LIVE_VOICE_DESCRIPTORS = {
    "Zephyr": "Bright",          "Puck": "Upbeat",            "Charon": "Informative",
    "Kore": "Firm",              "Fenrir": "Excitable",       "Leda": "Youthful",
    "Orus": "Firm",              "Aoede": "Breezy",           "Callirrhoe": "Easy-going",
    "Autonoe": "Bright",         "Enceladus": "Breathy",      "Iapetus": "Clear",
    "Umbriel": "Easy-going",     "Algieba": "Smooth",         "Despina": "Smooth",
    "Erinome": "Clear",          "Algenib": "Gravelly",       "Rasalgethi": "Informative",
    "Laomedeia": "Upbeat",       "Achernar": "Soft",          "Alnilam": "Firm",
    "Schedar": "Even",           "Gacrux": "Mature",          "Pulcherrima": "Forward",
    "Achird": "Friendly",        "Zubenelgenubi": "Casual",   "Vindemiatrix": "Gentle",
    "Sadachbia": "Lively",       "Sadaltager": "Knowledgeable", "Sulafat": "Warm",
}
```
Source: `https://ai.google.dev/gemini-api/docs/speech-generation` "Voice options" table, fetched via WebFetch on 2026-05-19.

**Allowlist constants for validation:**
```python
OPENAI_REALTIME_VAD_TYPES = ("server_vad", "semantic_vad")
OPENAI_REALTIME_VAD_EAGERNESS = ("low", "medium", "high", "auto")
GEMINI_LIVE_VAD_SENSITIVITIES = ("LOW", "MEDIUM", "HIGH")  # Gemini VAD start/end sensitivity enum
GEMINI_LIVE_THINKING_LEVELS = ("minimal", "low", "medium", "high")  # google-genai SDK ThinkingLevel enum, lowercase (verified types.py:308 / thinking_level.py)
```

---

## Tracks

### Phase A — Backend (Orchestrator)

**T1 — config.py constants (single SoT)**
Add the catalogs + allowlists shown above. Bump defaults:
- `OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2")` (was `"gpt-realtime"`)
- `GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-2.5-flash-native-audio-latest")` (was `-preview-12-2025`)

**T2 — `realtime_routes.py`**
- Modify `connect_to_openai(session, model: Optional[str] = None)`: if model is None, use `OPENAI_REALTIME_MODEL`; validate against `OPENAI_REALTIME_MODELS` (whitelist or fall back to default); build URL with `?model={resolved}`
- Modify `configure_openai_session(session, operator, voice=..., custom_role=..., vad_type: Optional[str]=None, vad_eagerness: Optional[str]=None, idle_timeout_ms: Optional[int]=None, interrupt_response: Optional[bool]=None, create_response: Optional[bool]=None)`: validate each new field against allowlist; build the appropriate session.update payload (server_vad shape OR semantic_vad shape per docs); idle_timeout_ms only included if vad_type == "server_vad" (per SDK docstring)
- Modify `connect` message handler (line 1279): read new JSON fields; pass to `connect_to_openai` (for model) + `configure_openai_session` (for vad/timeout)
- Modify `/realtime/status` (line 1411): emit `{"enabled": bool, "model_default": str, "models": OPENAI_REALTIME_CHAT_MODELS, "voices": OPENAI_REALTIME_VOICES, "voice_default": "ash"}`
- Add URL-query reads for Android: route `@app.websocket("/ws/realtime/{session_id}")` reads via `websocket.query_params.get("model")` etc. INSIDE the handler body (NOT route signature — FastAPI's auto-Query() injection on WebSocket routes is unverified in this codebase; the proven Starlette pattern is `websocket.query_params.get(...)`). Validate each against the allowlist constants from T1.

**T3 — `gemini_live_routes.py`** (analogous)
- Modify `configure_gemini_session(session, operator, voice, custom_role="", phone_mode=False, model: Optional[str]=None, vad_sensitivity_start: Optional[str]=None, vad_sensitivity_end: Optional[str]=None, thinking_level: Optional[str]=None)`: validate against allowlists; include in BidiGenerate setup; only emit `thinkingConfig` if model is `gemini-3.1-flash-live-preview`
- Modify `connect` message handler (line 1487): read new fields, pass to `configure_gemini_session`
- Modify `/gemini-live/status` (line 1618): emit catalog same shape as OpenAI
- Add URL-query handlers for Android: `model, vad_start, vad_end, thinking_level, voice` Optional params

**T4 — Allowlist unit tests** (Orchestrator/tests/test_live_models.py — new file)
- Test: invalid `vad_type="evil"` falls back to default with logged warning, not crash
- Test: `gpt-realtime-whisper` filtered from `/realtime/status` `models[]` (category=="transcribe" excluded)
- Test: `idle_timeout_ms` ignored when `vad_type="semantic_vad"` (per SDK)
- Test: `thinkingLevel` only emitted for 3.1 model, not 2.5

### Phase B — Web Portal

**T5 — index.html — realtime banner**
At `Portal/index.html:159-170` (current realtime banner), inside `.realtime-controls`:
- ADD `<select id="realtimeModelSelect">` populated from JS-side `OPENAI_REALTIME_MODELS` catalog (filter category=="chat") — 4 entries
- ADD `<select id="realtimeVadSelect">` with 2 options: `server_vad` (default) + `semantic_vad`
- ADD `<select id="realtimeEagernessSelect">` (4 options low/medium/high/auto) — shown only when `vad_type==semantic_vad` (CSS or JS gate)
- ADD `<input type="number" id="realtimeIdleTimeout" placeholder="30000" min="5000" max="300000">` for idle_timeout_ms — shown only when `vad_type==server_vad`
- Existing voice + connect/mic/disconnect buttons unchanged

**T6 — index.html — gemini banner**
At `Portal/index.html:218-225`:
- EXPAND existing `<select id="geminiVoiceSelect">` 6→30 options. Each option label = `${name} (${descriptor})` e.g. `<option value="Zephyr">Zephyr (Bright)</option>`
- ADD `<select id="geminiModelSelect">` with 3 options from GEMINI_LIVE_MODELS
- ADD `<select id="geminiVadStartSelect">` (LOW/MEDIUM/HIGH) for start-of-speech sensitivity
- ADD `<select id="geminiVadEndSelect">` (LOW/MEDIUM/HIGH) for end-of-speech sensitivity
- ADD `<select id="geminiThinkingSelect">` (DISABLE/STANDARD/HIGH) — shown only when model is 3.1

**T7 — `gpt-realtime.js`**
In `connect()` (around line 1155), read new UI selectors via `getElementById`, append to the existing connect-message JSON before WebSocket send. Same pattern as existing voice selector.

**T8 — `gemini-live.js`** (analogous)
In `connect()` (around line 806), read model + voice + sensitivities + thinking_level; append to connect message.

**T9 — CSS + cache-buster atomicity** — find `Portal/styles/features/_realtime.css` (likely) and add styling for the new dropdowns. Match existing `.realtime-voice-select` pattern.

**Cache-buster rule (audit I3):** T5, T6, T7, T8, T9 MUST land in a single Phase B commit, OR each task that touches HTML/JS/CSS bumps its own version independently. The risk: if T5 changes index.html but the cache-buster is only bumped in T9, browsers between the two commits will fetch new HTML with stale `gpt-realtime.js` (T7) and see a runtime `getElementById('realtimeModelSelect') === null` crash. Preferred: bump `?v=genui<N>` to next value as the FIRST step of T5, so every subsequent edit in Phase B inherits the busted cache.

```bash
# At start of T5, before any HTML edit:
CURRENT=$(grep -oP 'main\.css\?v=genui\K\d+' Portal/index.html | head -1)
NEXT=$((CURRENT + 1))
sed -i "s/main\.css?v=genui${CURRENT}/main.css?v=genui${NEXT}/g; s/app-modular\.js?v=genui${CURRENT}/app-modular.js?v=genui${NEXT}/g" Portal/index.html
```

### Phase C — Android MVP

**T10 — Constants/catalogs**
In `data/voice/VoiceClient.kt` (or a new constants file under `data/voice/`):
- Add `MODELS_GPT_REALTIME: List<Pair<String, String>>` with 4 chat-category entries
- Add `MODELS_GEMINI_LIVE: List<Pair<String, String>>` with 3 entries
- Expand `VOICES_GEMINI` 6→30 (existing list at `VoiceScreen.kt:554`)
- Add `GEMINI_VOICE_DESCRIPTORS: Map<String, String>` for UI labels (Compose can render `"${name} (${desc})"`)

**T11 — VoiceClient URL builder**
Modify `VoiceClient.kt:102`:
```kotlin
val url = buildString {
    append("$baseWsUrl${backend.wsPath}/$sessionId?operator=$operator&voice=$voice")
    sessionConfig.model?.let { append("&model=").append(it) }
    sessionConfig.vadType?.let { append("&vad_type=").append(it) }
    sessionConfig.vadEagerness?.let { append("&vad_eagerness=").append(it) }
    sessionConfig.idleTimeoutMs?.let { append("&idle_timeout_ms=").append(it) }
    sessionConfig.vadStart?.let { append("&vad_start=").append(it) }
    sessionConfig.vadEnd?.let { append("&vad_end=").append(it) }
    sessionConfig.thinkingLevel?.let { append("&thinking_level=").append(it) }
}
```
New `VoiceSessionConfig` data class carries the optional fields. **File location:** `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/voice/VoiceSessionConfig.kt` (sibling of `VoiceClient.kt`, same package `com.aiblackbox.portal.data.voice`). Audit M2.

```kotlin
package com.aiblackbox.portal.data.voice

data class VoiceSessionConfig(
    val model: String? = null,
    val vadType: String? = null,           // OpenAI: "server_vad" | "semantic_vad"
    val vadEagerness: String? = null,      // OpenAI semantic_vad only: "low" | "medium" | "high" | "auto"
    val idleTimeoutMs: Int? = null,        // OpenAI server_vad only
    val vadStart: String? = null,          // Gemini: "LOW" | "MEDIUM" | "HIGH"
    val vadEnd: String? = null,            // Gemini: "LOW" | "MEDIUM" | "HIGH"
    val thinkingLevel: String? = null,     // Gemini 3.1 only: "minimal" | "low" | "medium" | "high"
)
```

**T12 — VoiceScreen.kt — UI**
Add new Compose dropdowns next to the existing voice picker per provider:
- GPT_REALTIME: model dropdown (4 options) + vad_type toggle + (eagerness OR idle_timeout based on vad_type)
- GEMINI_LIVE: model dropdown (3 options) + voice dropdown (now 30 entries with descriptors) + vad_start + vad_end + (thinkingLevel if model==3.1)
- GROK_LIVE: unchanged

UI state for new fields lives in `VoiceScreen` via `remember { mutableStateOf(...) }`, passed into `VoiceSessionConfig` when connecting.

**T13 — Gradle compile must end BUILD SUCCESSFUL**

### Phase D — Audit + Hardware Validation

**T14 — Second adversarial audit** against the diffs from T1-T13 (this v2 plan ALREADY incorporates v1 audit findings; T14 catches anything new introduced during implementation)

**T15 — Live test**
- Dev box web: connect to OpenAI Realtime → switch to `gpt-realtime-2` (already default after T1) → click Mic → speak → verify response. Switch VAD to semantic, eagerness=high, speak quickly → verify lower-latency interrupt.
- Dev box web: connect to Gemini Live → switch voice to `Zephyr` (new) → switch to `gemini-3.1-flash-live-preview` → speak → verify response
- Rebuild Android APK, install, repeat both flows
- Push to MSO2 via existing update pipeline, repeat on customer hardware

### Phase E — Commit + push + snapshot

One commit per phase (T1-4 backend, T5-9 web, T10-13 android, T14-15 validation) for bisectability. Push when all phases green. Mint snapshot via `/chat/save`.

---

## Critical Reuse

| Need | Existing pattern | File:line |
|---|---|---|
| Voice allowlist validation | `if voice not in GROK_LIVE_VOICES` | `grok_live_routes.py:294` |
| URL query string parsing | FastAPI route signature defaults | existing route param patterns |
| Status endpoint emission | `/grok-live/status` | `grok_live_routes.py:1457-1463` |
| OpenAI SDK semantic_vad shape | type stubs | `Orchestrator/venv/.../realtime_audio_input_turn_detection.py` |
| Gemini Live setup message | existing `configure_gemini_session` | `gemini_live_routes.py:206+` |
| Compose dropdown pattern | existing voice dropdown | `VoiceScreen.kt:553+` |
| Android URL builder | existing query-string construction | `VoiceClient.kt:102` |
| Web JS connect-message builder | existing voice field | `gpt-realtime.js:1155+`, `gemini-live.js:806+` |
| Cache-buster bump | `?v=genui<N>` pattern | `Portal/index.html` (current value visible via grep before bump) |
| Backend Optional kwarg defaults to protect phone bridge | universal Python pattern | n/a |

---

## Verification

**Per-task build:**
```bash
# T1-T4 (backend)
Orchestrator/venv/bin/python -m py_compile Orchestrator/routes/realtime_routes.py Orchestrator/routes/gemini_live_routes.py Orchestrator/config.py
Orchestrator/venv/bin/pytest Orchestrator/tests/test_live_models.py -v
# Verify phone bridge still imports cleanly (signature compat)
Orchestrator/venv/bin/python -c "from Orchestrator.routes.realtime_routes import configure_openai_session; from Orchestrator.routes.gemini_live_routes import configure_gemini_session; print('OK')"

# T5-T9 (web)
for f in Portal/index.html Portal/modules/gpt-realtime.js Portal/modules/gemini-live.js; do
    python3 -c "c=open('$f').read(); print(f'{\"$f\"}: braces {c.count(chr(123))}/{c.count(chr(125))}, parens {c.count(chr(40))}/{c.count(chr(41))}')"
done

# T10-T13 (android)
cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:compileDebugKotlin
```

**End-to-end (T15):**
- Dev box web Realtime: confirm `?model=gpt-realtime-2` in upstream URL when DevTools network tab inspected
- Dev box web Gemini Live: dropdown shows all 30 voices with descriptors
- Android (after APK install): tap each provider, dropdown populates from /status endpoint catalog
- MSO2 (after update pipeline): same flows on customer hardware

---

## Out of Scope (Deferred)

- **Grok Live changes** — xAI doesn't publicly document the Realtime API (confirmed via WebFetch). 5 voices + undocumented endpoint. Brandon's call: leave alone.
- **Phone bridge integration of new params** — `idle_timeout_ms` would be especially valuable for outbound calls. Phone bridge has 6 call sites that get keyword-default protection in v2 (no breakage) but doesn't actively use the new fields. Separate v2 enhancement.
- **Affective dialog / proactive audio** — 2.5-only `v1alpha` features; would require switching upstream endpoint. Defer until customer asks.
- **Gemini Live manual VAD** (`activityStart`/`End` messages) — niche power-user feature.
- **Language selection UI** for Gemini Live (97 languages) — auto-detect works fine for English-default.
- **Per-operator default voice/model persistence** — exists for chat models, separate UX design pass for live.
- **Variant dropdown for Grok Live** — single model, 5 voices, no upgrade path.

---

## Audit Resolutions Summary (v2 → audit findings)

| Finding | Where addressed in v2 |
|---|---|
| C1 (wrong message type) | "Architecture" section + Phase A T2/T3 (connect message extensions) |
| C2 (phone bridge call sites) | All new params marked `Optional[...] = None` throughout |
| C3 (OpenAI model is URL param) | Phase A T2 specifies URL rebuild in `connect_to_openai()` |
| M1 (mini date suffix) | Verified via live API call; `gpt-realtime-mini-2025-12-15` confirmed |
| M2 (descriptor table missing) | Full 30-entry `GEMINI_LIVE_VOICE_DESCRIPTORS` table in Phase A1 |
| M3 (voice list source) | Citation to ai.google.dev speech-generation page, fetched 2026-05-19 |
| M4 (Android URL query) | Phase C T11 spec explicitly uses query-string extension |
| M5 (status endpoints) | Phase A T2/T3 update both `/realtime/status` and `/gemini-live/status` to emit catalog |
| I1 (per-field allowlist validation) | Phase A T4 unit tests; explicit allowlist constants in T1 |
| I3 (interrupt + create on both VAD modes) | Phase A T2 spec notes both fields valid in both modes |
| I4 (whisper/translate filtered from dropdown) | Phase A `category` field + T2 filter to `category=="chat"` only |
| I5 (asymmetric model injection) | Architecture section spells out URL-vs-setup difference |
| I7 (API key missing UI) | Status endpoint `enabled: bool` already exists; web/Android consume it |
| N1 (pytest existence) | T4 creates `test_live_models.py` rather than assuming tests exist |
| N2 (cache buster version) | T9 reads current version before bumping |
| N3 (ordering) | T1 backend MUST land before T5+ web and T10+ android |

### Second-audit findings resolved inline (v2 revision, 2026-05-19)

| Finding | Where addressed |
|---|---|
| 2A-C2 (thinkingLevel path + enum mismatch) | `GEMINI_LIVE_THINKING_LEVELS = ("minimal","low","medium","high")` (lowercase, verified `google-genai` SDK `types.py:ThinkingConfig` + `thinking_level.py`); setup path = `setup.generationConfig.thinkingConfig.thinkingLevel` (verified existing `gemini_live_routes.py:367` already builds `setup_config["generationConfig"]`) |
| 2A-M1 (status response shape) | Locked JSON schema in Architecture section: flat `voices: string[]` + separate `voice_descriptors: {string: string}` map matches Android `VOICES_GEMINI: List<String>` |
| 2A-M2 (VoiceSessionConfig.kt path unspecified) | Explicit file path + package declaration + complete data class body in T11 |
| 2A-M3 (catalog duplication) | Architecture mandates fetch-from-status with 5min sessionStorage cache; NO JS-side or Kotlin-side hardcoded model/voice arrays |
| 2A-I1 (FastAPI WS query injection unverified) | T2/T3 use defensive `websocket.query_params.get(...)` pattern (proven Starlette API) inside the handler body, NOT route-signature injection |
| 2A-I3 (cache-buster atomicity) | T9 spec: bump `?v=genui<N>` as FIRST step of T5 so all Phase B edits share the busted cache, OR commit Phase B atomically |
| 2A-I4 (mid-session model switch) | Architecture spec: dropdown is `disabled` while CONNECTED, re-enabled on DISCONNECTED. Applies to model + vad_type (schema-changing). Voice + eagerness stay hot-swappable. |
