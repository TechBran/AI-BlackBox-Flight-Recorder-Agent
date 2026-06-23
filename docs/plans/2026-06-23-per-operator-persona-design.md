# Per-Operator Persona System Prompt — Design

**Date:** 2026-06-23
**Status:** Design locked, ready for implementation plan
**Investigation source:** parallel four-surface read (backend persona, operator-preference store, Portal menu, Android menu), 2026-06-23

---

## Goal

Make the **persona portion** of the system prompt operator-specific. Each operator
authors their own behavioral/persona prompt; it persists alongside their other
preferences (voice, etc.) in the one existing store, rides along automatically for
new operators, and is editable from a new "System Prompt" section in both the
Portal web hamburger menu and the Android MVP settings sheet.

The **functional** system instructions (identity, the snapshot knowledge hierarchy,
tool usage, artifact rules, temporal awareness) are NOT persona and do **not**
change — they stay code, identical for every operator.

---

## Locked decisions

1. **Replace semantics.** The operator's persona *replaces* the old behavioral
   core (anti-sycophancy / calibration / pushback / tone / limits) entirely. Those
   nudges were Anthropic-era prompt crutches that a model like Opus 4.8 no longer
   needs as a baked-in default. They become *whatever the operator writes*.
2. **Functional body is untouched.** "How to use snapshots, what they are" and all
   other functional instructions stay exactly as they are, for all operators.
3. **All surfaces read the persona:** text chat (both paths), all three voice routes
   (OpenAI Realtime, Grok Live, Gemini Live), and the on-device `/local/system-prompt`.
   One resolver feeds all six sites.
4. **Lean default.** The shipped default persona (what a non-customizing operator
   gets, and what the editor pre-seeds) is a short, neutral 2–3 sentence persona —
   NOT the old ~40-line behavioral sermon.
5. **Editor pre-seeds with the effective persona.** Opening the editor shows the
   operator's saved persona if any, else the lean default — a real starting point,
   never a blank box. A "Reset to default" affordance clears the custom value.

### Derived engineering calls (made from the above)

- **One persona field per operator**, applied across all modalities (one textarea,
  one stored string). Not separate chat/voice personas.
- **Voice keeps a tiny always-on *functional* delivery note** ("short sentences;
  don't read URLs/code/markdown aloud") — this is functional, like the snapshot
  rules, not persona. It is appended at the voice sites regardless of the operator's
  persona, so voice never regresses into reading file paths aloud.
- **Persona is promoted into the real `{PERSONA}` slot**, and the `persona` key is
  **excluded** from the only generic preference dump that exists
  (`build_cu_context`), so it can never appear as a junk context line.
- **Android POSTs persona to the backend** (unlike how Android stores voice, which
  is on-device DataStore only) — because the chat backend must *read* it and it
  should follow the operator across devices. Persona is server-authoritative.

---

## Architecture

### Persona becomes data, not a frozen constant

Today the chat persona is concatenated into a module-level constant **at import
time** (`tasks.py:1934`):

```python
CORE_SYSTEM_PROMPT = ( BEHAVIORAL_CORE_CHAT + "\n\n" + "IDENTITY:..." + "{TOOL_INSTRUCTIONS}" + ... )
```

We convert the leading persona into a `{PERSONA}` placeholder — exactly the same
shape as the existing `{TOOL_INSTRUCTIONS}` placeholder — resolved per request:

```
CHAT SYSTEM PROMPT  (build_core_system_prompt(tool_instructions, operator))
├─ {PERSONA}            ◄── get_persona(operator, "chat")  — operator-authored or lean default
├─ IDENTITY             ─┐
├─ TEMPORAL AWARENESS    │
├─ KNOWLEDGE HIERARCHY   ├─ FUNCTIONAL — unchanged, not editable
│   (snapshots: what they│
│    are, search first)  │
├─ MULTIMODAL MEDIA      │
├─ TOOL USAGE + {TOOL_INSTRUCTIONS}  │
└─ ARTIFACT GENERATION  ─┘
```

### The single resolver (single source of truth)

A new accessor in `behavioral_core.py` is the *only* place persona is resolved.
Every read site calls it; the override logic lives in exactly one function.

```python
# behavioral_core.py
PERSONA_PREF_KEY = "persona"

DEFAULT_PERSONA_CHAT = (
    "You are the operator's AI Black Box assistant. Be direct, clear, and "
    "grounded in what you can defend. Talk to the operator like a knowledgeable "
    "peer, and match their tone and level of formality."
)
DEFAULT_PERSONA_VOICE = DEFAULT_PERSONA_CHAT  # same lean default; voice delivery note is separate/functional

# Functional voice-delivery mechanics — NOT persona, always appended for voice.
VOICE_DELIVERY_NOTE = (
    "ON SPEECH: Short sentences. Don't read URLs, code, file paths, or markdown "
    "aloud — say \"I'll send that in text.\" Use natural prosody, not robot cadence."
)

def get_persona(operator: str | None, modality: str) -> str:
    """Operator's persona for a modality, falling back to the lean default.
    Empty/whitespace custom value falls back to default (so 'cleared' == default)."""
    default = DEFAULT_PERSONA_CHAT if modality == "chat" else DEFAULT_PERSONA_VOICE
    if not operator:
        return default
    try:
        from Orchestrator.state import get_operator_preference  # lazy: avoid import cycle
        saved = get_operator_preference(operator, PERSONA_PREF_KEY, None)
    except Exception:
        saved = None
    if saved is not None and str(saved).strip():
        return str(saved)
    return default
```

**Import-cycle note:** `behavioral_core` is imported by `config` → `state` imports
`config`. The `from Orchestrator.state import ...` MUST be lazy (inside the function)
or it deadlocks at import. `get_persona` is therefore safe to call from anywhere.

### Read sites (all six)

| Site | File / line | Change |
|---|---|---|
| Chat chokepoint (both paths) | `tasks.py:2015` `build_core_system_prompt` | add `operator=None` param; resolve `{PERSONA}` via `get_persona(operator,"chat")` |
| Non-stream caller | `tasks.py:1417` | pass `active_operator` |
| Streaming caller | `chat_routes.py:5818` | pass `operator` |
| Voice — OpenAI Realtime | `realtime_routes.py:410` | `{BEHAVIORAL_CORE_VOICE}` → `get_persona(operator,"voice")` + `VOICE_DELIVERY_NOTE` |
| Voice — Grok Live | `grok_live_routes.py:354` | same |
| Voice — Gemini Live | `gemini_live_routes.py:331` | same |
| On-device | `local_routes.py:531` | `get_behavioral_core("chat")` → `get_persona(operator,"chat")` |

**Defensive guard (one line):** `build_cu_context` (`chat_routes.py:5733-5739`)
loops every preference into a `=== OPERATOR PREFERENCES ===` block. Skip the
`persona` key in that loop so persona never leaks as a CU context line. (Main chat
context — `context_builder.build_fossil_context` — does NOT dump prefs, so no change
needed there.)

**Module-load safety:** `STREAM_EXCERPT = build_core_system_prompt()` runs at import
with no operator → resolves to the lean default. The new param MUST default to
`None`. Keep this working.

### Retiring the old constants

`BEHAVIORAL_CORE_CHAT` / `BEHAVIORAL_CORE_VOICE` (the ~40-line sermons) are no longer
the default. Plan must grep every importer and repoint:
- `tasks.py:35` import + `:1935` usage → replaced by `{PERSONA}` slot.
- 3 voice routes → replaced by `get_persona` + `VOICE_DELIVERY_NOTE`.
- `config.py:87-89,326,345` `build_output_spec` → **verify it is dead for the live
  chat path** (investigation says it is); if dead, leave or repoint to
  `get_persona(None,"chat")`. Do NOT let it silently keep old text alive.
- `get_behavioral_core(modality)` (`behavioral_core.py:119`) → keep as a thin
  back-compat wrapper returning the lean `DEFAULT_PERSONA_*` (so any straggler
  caller gets the new default, not a crash), or delete if no callers remain after
  the repoint. Decide by grep.

---

## Storage

**No new store, no schema migration.** Reuse `Manifest/operator_preferences.json`
(the same file voice selection uses), via the existing
`state.get_operator_preference` / `set_operator_preference` (auto-create on write,
auto-persist). New key: **`persona`**.

```json
{ "Brandon": { "tts_voice": "openai:onyx", "persona": "You are terse and technical..." } }
```

- New operators: a persona record is created lazily on first save — already the
  desired "fill it in → saved" behavior, no change.
- Operator names contain spaces (e.g. "Anna 2") and are used as JSON keys / URL path
  params → always `encodeURIComponent` / URL-encode.

---

## API

Storage is the generic prefs store, but the editor needs three things the generic
endpoint doesn't give cleanly: the **effective** persona, whether it's **custom**,
and the **default** (for pre-seed + reset). So add a thin, typed persona endpoint
trio (wrappers over `state` helpers — zero new persistence code):

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/operator/persona/{operator}` | `{operator, persona, is_custom, default}` — `persona` = effective (custom or default) |
| `PUT` | `/operator/persona/{operator}` | body `{persona}` → `set_operator_preference(op,"persona",text)`; returns saved |
| `DELETE` | `/operator/persona/{operator}` | reset: remove the `persona` key, persist; returns the default |

`GET /local/system-prompt?operator=` is *repointed* to `get_persona(operator,"chat")`
so the on-device path already returns the per-operator persona. The on-device
Android `PersonaCache` is operator-keyed (`prompt:<op>`/`version:<op>`) — verify, so
two operators don't collide.

New route module: `Orchestrator/routes/persona_routes.py` (registered in the app
router), or fold into `admin_routes.py` near `GET /operators`. Prefer a dedicated
module for clarity.

---

## Portal web UI

- **Insertion point:** `Portal/index.html:478` — a new `<div class="menu-group
  system-prompt-section">` collapsible **immediately above** the Voice Preferences
  section. Template = the Streaming/Reasoning collapsible (`index.html:567-583`):
  `button#btnToggleSystemPrompt.btn-section-toggle` + `div#systemPromptContent.section-content.collapsed`
  containing a `<textarea id="systemPromptTextarea">`, a Save button, a "Reset to
  default" link, and a hint ("Saved per operator").
- **Toggle wiring:** add `wireCollapsibleSection("btnToggleSystemPrompt","systemPromptContent")`
  at `ui-setup.js:854-857`.
- **Logic (new functions in `Portal/modules/tts-stt.js`, mirroring the voice
  pattern):** `initSystemPromptSection()` (wire Save/Reset, initial load),
  `syncSystemPromptTextarea()` (load the active operator's effective persona via
  `GET /operator/persona/{op}`, set textarea), `saveSystemPrompt()` (`PUT`),
  `resetSystemPrompt()` (`DELETE` → reload). **Save awaits + toasts success/failure**
  (do NOT copy voice's silent fire-and-forget — this is an explicit Save button).
- **Boot:** call `initSystemPromptSection()` in `app-init.js` Phase 3 right after
  `initVoiceSelector()` (`:619`).
- **Per-operator reload hook (must-have):** `state-management.js:708`, in the
  operator `onchange` handler's real-switch branch (after the `__add__` sentinel
  check), add a sibling dynamic import calling `syncSystemPromptTextarea()` so the
  textarea reflects the newly selected operator.
- **CSS:** add `.system-prompt-section` to `Portal/styles/features/_settings.css`
  (copy the `.voice-preferences-section` divider/header); collapsible visuals are
  already covered. Bump `index.html` `main.css` `?v=genuiXX`.
- **Edit `Portal/index.html` only** (the served file), NOT `index-modular.html`.

---

## Android Kotlin MVP UI

App root (note the spaces/parens in the path — quote it):
`AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/`

- **Insertion point:** `SettingsSheet.kt` between the end of the On-Device Model
  block (`:657`) and `SectionHeader("Operator", BbxDim)` (`:662`) — directly above
  the Operator selector. Wrap in the existing `CollapsibleSection("🧬 System Prompt",
  accent=BbxAccent) { ... }` (`:979-1028`).
- **Field:** multi-line `androidx.compose.material3.OutlinedTextField` (copy colors
  from the Add-Operator dialog `:709-721`, `singleLine=false`, `minLines`), state
  seeded from the operator's persona; a `FullWidthMenuButton("Save")` (`:973`) +
  a "Reset to default" text button.
- **Operator reactivity:** `operator` is `collectAsState` from `store.operator`
  (`:128`) → the sheet recomposes on switch. Load persona in
  `LaunchedEffect(operator) { viewModel.loadOperatorPersona(operator) }` so the
  field follows the selected operator automatically.
- **ViewModel (`SettingsViewModel.kt`):** add `loadOperatorPersona(operator)` (GET
  `/operator/persona/$operator`, parse, push to a StateFlow) and
  `setOperatorPersona(operator, text)` (PUT) mirroring `setOperatorVoice`
  (`:176-178`) + `loadVoiceCatalog` (`:57-64`). **Build the JSON body with
  kotlinx.serialization** (`Json.encodeToString`), NOT string interpolation —
  persona is multi-line/quoted and would break naïve interpolation (the bug latent
  in `addOperator` `:184`).
- **Network:** existing hand-rolled `BlackBoxApi.get/put` (`:51-77`). No Retrofit.
- **Cross-device:** persona goes to the backend (divergence from voice's local-only
  DataStore) so chat can read it. A local DataStore cache is optional, not required.

---

## The lean default persona (shipped content)

```
You are the operator's AI Black Box assistant. Be direct, clear, and grounded in
what you can defend. Talk to the operator like a knowledgeable peer, and match
their tone and level of formality.
```

Single source: `behavioral_core.DEFAULT_PERSONA_CHAT`. Surfaced to the editor via
`GET /operator/persona/{op}` (`default` field) — the frontend never hardcodes it.

---

## Testing

- **Backend (pytest):** `get_persona` (default fallback, custom override,
  empty-string→default, unknown operator, modality split); `build_core_system_prompt`
  with/without operator (`{PERSONA}` substituted, functional body byte-identical
  below the slot, module-load `STREAM_EXCERPT` still builds); persona endpoint trio
  (GET effective+is_custom+default, PUT persists, DELETE resets); `build_cu_context`
  omits the `persona` key; voice resolution helper. Run via
  `Orchestrator/venv/bin/pytest`.
- **Portal (agent-driven Chrome):** open menu → System Prompt section above Voice
  Preferences; load shows effective persona; edit+Save persists (reload); switch
  operator → textarea swaps; Reset restores default; live chat reflects the persona.
- **Android:** compile/build check in this session; on-device UI verification is the
  operator's device test (per the established MVP workflow).
- **Three-surfaces note:** Portal + Android are both in scope here; WebView wrappers
  wrap the Portal and inherit it for free.

---

## Out of scope (deliberate)

- **Computer Use / phone / SMS persona.** These deliberately skip `behavioral_core`
  today; we keep that. (We only add the defensive `persona`-key exclusion to the CU
  prefs dump.)
- **Separate chat vs voice personas per operator.** One field for now; a second
  field is a clean future addition (the resolver already takes `modality`).
- **Persona versioning / history.** Last-write-wins, like the rest of the prefs store.
- **Length caps / injection sanitization.** Operator-authored free text is trusted
  (the operator IS the trust boundary — Tailscale perimeter). Note for future if
  multi-tenant ever changes that.
