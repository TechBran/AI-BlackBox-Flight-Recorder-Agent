# Per-Operator Persona System Prompt — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task on `main`.

**Goal:** Make the persona portion of the system prompt operator-specific — each operator authors their own behavioral prompt, persisted alongside their other preferences, editable from the Portal hamburger menu and the Android settings sheet, applied across chat + voice + on-device. The functional system instructions (snapshots, tools, identity) are unchanged.

**Architecture:** Persona becomes per-request data resolved by a single `get_persona(operator, modality)` in `behavioral_core.py`, substituted into a new `{PERSONA}` placeholder in the chat prompt and into the three voice f-strings + the on-device endpoint. Storage reuses `Manifest/operator_preferences.json` (key `persona`) via existing `state` helpers. A thin `/operator/persona/{operator}` endpoint trio backs the two UIs.

**Tech Stack:** FastAPI + pytest (backend), vanilla ES-module Portal (agent-driven Chrome verification), Jetpack Compose + kotlinx.serialization + OkHttp (Android MVP).

**Design doc:** `docs/plans/2026-06-23-per-operator-persona-design.md`

**Build rules:** Directly on `main`. Explicit-path commits only (NEVER `git add -A`). Every committed step leaves the app launchable (prod serves from the working tree). Restart only at named SAFE POINTs. The stray uncommitted `Orchestrator/routes/chat_routes.py` media-task change is NOT ours — never stage it; only stage the persona lines we add.

---

## Milestone M1 — Backend resolver + lean default (no behavior change yet)

Nothing calls the resolver yet, so this milestone is pure addition — safe, fully unit-testable.

### Task 1.1: Add persona constants + resolver to `behavioral_core.py`

**Files:**
- Modify: `Orchestrator/behavioral_core.py`
- Test: `Orchestrator/tests/test_persona_resolver.py` (create)

**Step 1 — Write failing tests.** Create `test_persona_resolver.py`:

```python
import importlib
from Orchestrator import behavioral_core, state

def _reset_prefs(monkeypatch, prefs):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", prefs)

def test_get_persona_default_when_no_operator():
    assert behavioral_core.get_persona(None, "chat") == behavioral_core.DEFAULT_PERSONA_CHAT
    assert behavioral_core.get_persona("", "chat") == behavioral_core.DEFAULT_PERSONA_CHAT

def test_get_persona_default_when_operator_has_none(monkeypatch):
    _reset_prefs(monkeypatch, {})
    assert behavioral_core.get_persona("Brandon", "chat") == behavioral_core.DEFAULT_PERSONA_CHAT

def test_get_persona_returns_custom(monkeypatch):
    _reset_prefs(monkeypatch, {"Brandon": {"persona": "You are terse."}})
    assert behavioral_core.get_persona("Brandon", "chat") == "You are terse."

def test_get_persona_empty_custom_falls_back_to_default(monkeypatch):
    _reset_prefs(monkeypatch, {"Brandon": {"persona": "   "}})
    assert behavioral_core.get_persona("Brandon", "chat") == behavioral_core.DEFAULT_PERSONA_CHAT

def test_get_persona_voice_modality(monkeypatch):
    _reset_prefs(monkeypatch, {"Brandon": {"persona": "Voice me."}})
    assert behavioral_core.get_persona("Brandon", "voice") == "Voice me."

def test_default_persona_is_lean_not_old_sermon():
    # The lean default must NOT contain the retired anti-sycophancy headers.
    assert "ON SYCOPHANCY" not in behavioral_core.DEFAULT_PERSONA_CHAT
    assert len(behavioral_core.DEFAULT_PERSONA_CHAT) < 600

def test_persona_pref_key_is_persona():
    assert behavioral_core.PERSONA_PREF_KEY == "persona"
```

**Step 2 — Run, verify fail:** `Orchestrator/venv/bin/pytest Orchestrator/tests/test_persona_resolver.py -v` → fails (no `get_persona`).

**Step 3 — Implement** in `behavioral_core.py` (add after the existing constants, keep the old `BEHAVIORAL_CORE_*` for now — M3 retires them):

```python
PERSONA_PREF_KEY = "persona"

DEFAULT_PERSONA_CHAT = (
    "You are the operator's AI Black Box assistant. Be direct, clear, and "
    "grounded in what you can defend. Talk to the operator like a knowledgeable "
    "peer, and match their tone and level of formality."
)
DEFAULT_PERSONA_VOICE = DEFAULT_PERSONA_CHAT

VOICE_DELIVERY_NOTE = (
    "ON SPEECH: Short sentences. Don't read URLs, code, file paths, or markdown "
    "aloud — say \"I'll send that in text.\" Use natural prosody, not robot cadence."
)

def get_persona(operator, modality):
    """Operator's persona for a modality; falls back to the lean default.
    Empty/whitespace custom value -> default (so a cleared persona == default)."""
    default = DEFAULT_PERSONA_CHAT if modality == "chat" else DEFAULT_PERSONA_VOICE
    if not operator:
        return default
    try:
        from Orchestrator.state import get_operator_preference  # lazy: import cycle
        saved = get_operator_preference(operator, PERSONA_PREF_KEY, None)
    except Exception:
        saved = None
    if saved is not None and str(saved).strip():
        return str(saved)
    return default
```

**Step 4 — Run, verify pass.**

**Step 5 — Commit:** `git add Orchestrator/behavioral_core.py Orchestrator/tests/test_persona_resolver.py && git commit`.

---

## Milestone M2 — Chat persona slot (makes text chat per-operator)

### Task 2.1: Convert `CORE_SYSTEM_PROMPT` to a `{PERSONA}` template + add `operator` param

**Files:**
- Modify: `Orchestrator/tasks.py` (`:1934` constant, `:2015` `build_core_system_prompt`)
- Test: `Orchestrator/tests/test_core_system_prompt_persona.py` (create)

**Step 1 — Write failing tests:**

```python
from Orchestrator import tasks, behavioral_core, state

def test_persona_placeholder_present_in_template():
    assert "{PERSONA}" in tasks.CORE_SYSTEM_PROMPT
    # Old sermon must no longer be baked into the constant:
    assert "ON SYCOPHANCY" not in tasks.CORE_SYSTEM_PROMPT

def test_functional_body_unchanged_below_slot():
    out = tasks.build_core_system_prompt("TOOLS_HERE")
    for marker in ("IDENTITY:", "KNOWLEDGE HIERARCHY", "TOOL USAGE", "ARTIFACT"):
        assert marker in out
    assert "TOOLS_HERE" in out

def test_default_operator_uses_lean_default():
    out = tasks.build_core_system_prompt("x", operator=None)
    assert behavioral_core.DEFAULT_PERSONA_CHAT in out

def test_custom_operator_persona_injected(monkeypatch):
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {"Brandon": {"persona": "ZZZ-CUSTOM"}})
    out = tasks.build_core_system_prompt("x", operator="Brandon")
    assert "ZZZ-CUSTOM" in out
    assert behavioral_core.DEFAULT_PERSONA_CHAT not in out

def test_stream_excerpt_still_builds_at_module_load():
    assert isinstance(tasks.STREAM_EXCERPT, str) and len(tasks.STREAM_EXCERPT) > 100
```

**Step 2 — Run, verify fail.**

**Step 3 — Implement:**
- Change `tasks.py:1935` from `BEHAVIORAL_CORE_CHAT + "\n\n"` to `"{PERSONA}\n\n"`.
- Update `build_core_system_prompt` (`:2015`):

```python
def build_core_system_prompt(tool_instructions="", operator=None):
    """Build the core system prompt with per-operator persona + dynamic/static tools."""
    from Orchestrator.behavioral_core import get_persona
    persona = get_persona(operator, "chat")
    body = CORE_SYSTEM_PROMPT.replace("{PERSONA}", persona)
    tools = tool_instructions if tool_instructions else CORE_TOOLS_STATIC
    return body.replace("{TOOL_INSTRUCTIONS}", tools)
```

- The `tasks.py:35 from ... import BEHAVIORAL_CORE_CHAT` becomes unused here — leave the import until M3 (which retires it) to keep this commit minimal, OR remove now if no other use in tasks.py (grep first).

**Step 4 — Run, verify pass.**

**Step 5 — Commit.**

### Task 2.2: Thread `operator` through both chat callers

**Files:** Modify `Orchestrator/tasks.py:1417`, `Orchestrator/routes/chat_routes.py:5818`

**Step 1 — Change the primary calls:**
- `tasks.py:1417`: `build_core_system_prompt(tool_instructions)` → `build_core_system_prompt(tool_instructions, operator=active_operator)`.
- `chat_routes.py:5818`: `build_core_system_prompt(tool_instructions)` → `build_core_system_prompt(tool_instructions, operator=operator)`.

**Step 1b — Fix the `STREAM_EXCERPT` fallback branches (review finding #10).** Both chat paths fall back to the module-level `STREAM_EXCERPT` (built at import with `operator=None` → **default persona**) when `TOOLVAULT_ENABLED` is false OR no user message is extractable: `tasks.py:1419` + `:1421`, `chat_routes.py:5821` + `:5823`. Without this step, those branches silently ignore the operator's custom persona. The operator IS in scope at each, so replace the `STREAM_EXCERPT` fallback with a per-operator build:
- `tasks.py:1419/1421` fallback → `build_core_system_prompt("", operator=active_operator)` (static tools + operator persona).
- `chat_routes.py:5821/5823` fallback → `build_core_system_prompt("", operator=operator)`.
- Keep the module-level `STREAM_EXCERPT = build_core_system_prompt()` as-is (it's the no-operator import-time constant; only the per-request *fallback usages* change). Confirm by grep that no other importer of `STREAM_EXCERPT` relies on it being operator-agnostic.

**Step 2 — Guard test (extend the M2 test file or add to an integration test):** assert that when a custom persona is set for an operator, a built streaming/non-stream system prompt contains it — including in the no-tool-instructions fallback path. (If a full call is heavy, a focused unit test asserting the two call sites pass `operator` via monkeypatched `build_core_system_prompt` spy is acceptable.)

**Step 3 — Run backend suite:** `Orchestrator/venv/bin/pytest Orchestrator/tests/ -q`.

**Step 4 — Commit.**

### Task 2.3: Exclude `persona` from the CU preferences dump

**Files:** Modify `Orchestrator/routes/chat_routes.py:5732-5739` (the `for key, val in prefs.items()` loop inside `build_cu_context`)
- Test: add to a CU-context test or `test_core_system_prompt_persona.py`.

**Step 1 — Failing test:** with `OPERATOR_PREFERENCES = {"Brandon": {"tts_voice":"x","persona":"SECRET"}}`, `build_cu_context("hi","Brandon")[0]` must contain `tts_voice` but NOT `SECRET`.

**Step 2 — Implement:** in the `for key, val in prefs.items()` loop, `if key == behavioral_core.PERSONA_PREF_KEY: continue` (import the constant).

**Step 3 — Run, verify pass. Commit.**

**SAFE POINT — restart + live smoke:** `sudo systemctl restart blackbox.service` (pre-authorized). After warm-up, send a chat as an operator with no custom persona → confirm it works and tone is the lean default (no behavior break). Tail `journalctl` for clean prompt build.

---

## Milestone M3 — Voice + on-device read sites; retire old constants

### Task 3.1: Voice routes use `get_persona` + functional delivery note

**Files:** Modify `realtime_routes.py:410`, `grok_live_routes.py:354`, `gemini_live_routes.py:331`

**Step 1 — For each of the 3 sites:** replace the leading `{BEHAVIORAL_CORE_VOICE}` interpolation in `system_instructions` with a resolved value computed just above the f-string:

```python
from Orchestrator.behavioral_core import get_persona, VOICE_DELIVERY_NOTE
voice_persona = get_persona(operator, "voice") + "\n\n" + VOICE_DELIVERY_NOTE
```

and interpolate `{voice_persona}` instead of `{BEHAVIORAL_CORE_VOICE}`. Remove each route's now-unused `from ... import BEHAVIORAL_CORE_VOICE`.

**Step 2 — Test:** a unit test importing each route module and asserting `BEHAVIORAL_CORE_VOICE` is no longer referenced (grep-style) is brittle; instead add a `test_voice_persona_helper` that asserts `get_persona("X","voice") + VOICE_DELIVERY_NOTE` contains the delivery note and the persona. (Full live-socket tests are out of scope; voice is verified live at the SAFE POINT.)

**Step 3 — `node`-free check:** `Orchestrator/venv/bin/python -c "import Orchestrator.routes.realtime_routes, Orchestrator.routes.grok_live_routes, Orchestrator.routes.gemini_live_routes"` → imports cleanly.

**Step 4 — Commit.**

### Task 3.2: On-device endpoint returns per-operator persona

**Files:** Modify `Orchestrator/routes/local_routes.py:531` (+ docstring `:520-525`)

**Step 1 — Change** `prompt = get_behavioral_core("chat")` → `from Orchestrator.behavioral_core import get_persona; prompt = get_persona(operator, "chat")`. Update the docstring (it currently claims persona is operator-independent — now it IS per-operator). The existing `version = sha256(prompt)[:12]` now varies per operator (correct — the Android `PersonaCache` is operator-keyed).

**Step 2 — Test:** `GET /local/system-prompt?operator=Brandon` with a custom persona returns it; with none returns the lean default. (FastAPI TestClient or a direct function call.)

**Step 3 — Repoint the two existing local-route tests this breaks (review finding #11):**
- `tests/test_local_routes.py:409-413` `test_system_prompt_is_behavioral_core_chat` asserts the endpoint returns `get_behavioral_core("chat")` (the OLD sermon). Update it to assert the lean default (`get_persona(None,"chat")` / `DEFAULT_PERSONA_CHAT`), and add a case asserting a custom persona is returned for an operator that has one. Rename to `test_system_prompt_is_operator_persona`.
- `tests/test_local_turn_prepare.py:35,62,78,109,149` monkeypatch `get_behavioral_core` **in the `local_routes` namespace**. After Step 1's import swap, `local_routes` uses `get_persona` → those patches target a now-unused symbol and silently no-op. Repoint each patch to `get_persona` (and adjust the patched return shape if needed).

**Step 4 — Verify Android PersonaCache key includes operator** (read-only check of `PersonaCache.kt:77-78,98-99,105-106` — keys are `"prompt:"+operator` / `"version:"+operator`, confirmed by review). Note in commit message.

**Step 5 — Commit.**

### Task 3.3: Retire old constants + handle `config.build_output_spec`

**Files:** Modify `Orchestrator/behavioral_core.py`, `Orchestrator/config.py`, `Orchestrator/tasks.py` (import cleanup)

**Step 1 — Grep all callers:** `grep -rn "BEHAVIORAL_CORE_CHAT\|BEHAVIORAL_CORE_VOICE\|get_behavioral_core\|build_output_spec\|OUTPUT_SPEC" Orchestrator/`. (Review #6 confirmed `build_output_spec`/`OUTPUT_SPEC` has **zero live callers** — only the `config.py` definition, a `tasks.py:1405` comment, and `tests/test_phase2_cutover.py:76-84` import it. So it is dead for chat but the test imports it.)
**Step 2 — Decide per caller (keep the test suite importable, strip the old sermon):**
- `config.py` `build_output_spec`/`OUTPUT_SPEC` (`:323-348`): **do NOT delete it** (a test imports it). Instead repoint its `BEHAVIORAL_CORE_CHAT` references (`:326,345`) to `DEFAULT_PERSONA_CHAT` so the retired sermon is gone but `OUTPUT_SPEC`/`OUTPUT_SPEC_CORE` stay importable. Then check `tests/test_phase2_cutover.py:76-84`: if it only imports/asserts existence, no change; if it asserts the old text, update it to the lean default.
- `config.py:87-89`: the `from Orchestrator.behavioral_core import BEHAVIORAL_CORE_CHAT` import → switch to `DEFAULT_PERSONA_CHAT` (or drop if `build_output_spec` no longer needs it after repoint).
- `tasks.py:35`: remove the now-unused `BEHAVIORAL_CORE_CHAT` import.
- `get_behavioral_core(modality)`: after Task 3.2 repoints `local_routes` to `get_persona`, grep for any remaining caller. If none, delete it; if any remain, repoint it to return `DEFAULT_PERSONA_CHAT`/`DEFAULT_PERSONA_VOICE`.
- Delete the `BEHAVIORAL_CORE_CHAT` / `BEHAVIORAL_CORE_VOICE` constants once no importer references them (the grep in Step 1 is the gate).

**Step 3 — Full import + test sweep:** `Orchestrator/venv/bin/python -c "import Orchestrator.tasks, Orchestrator.config, Orchestrator.routes.local_routes"` and `Orchestrator/venv/bin/pytest Orchestrator/tests/ -q`. **All green** — the M3 test repoints (Task 3.2 Step 3) + this OUTPUT_SPEC decision must leave zero failures, not discover them.

**Step 4 — Commit.**

**SAFE POINT — restart + live voice smoke:** restart; start a voice session (one provider) → confirm persona + that it doesn't read URLs aloud (delivery note intact).

---

## Milestone M4 — Persona API endpoints

### Task 4.1: `persona_routes.py` GET/PUT/DELETE

**Files:**
- Create: `Orchestrator/routes/persona_routes.py`
- Modify: `Orchestrator/app.py` — add `from Orchestrator.routes.persona_routes import *` to the route-module `import *` block at **`app.py:77-97`** (alongside `admin_routes`, `local_routes`, `tts_routes`, etc.)
- Test: `Orchestrator/tests/test_persona_routes.py` (create)

> **Registration pattern (review finding #7 — do NOT use `APIRouter`/`include_router`).** The dominant sibling pattern is `from Orchestrator.checkpoint import app` + bare `@app.get(...)` decorators, pulled in via the `from Orchestrator.routes.<mod> import *` block at `app.py:77-97`. Copy `tts_routes.py`'s header verbatim to get the exact `app` import source (it's `from Orchestrator.checkpoint import app`) and confirm by reading the top of `tts_routes.py`. Endpoints register simply by being imported — no `include_router` line.

**Step 1 — Failing tests** (FastAPI TestClient): GET returns `{operator, persona, is_custom:false, default}` when unset; PUT `{persona:"X"}` persists (`get_operator_preference` == "X"); GET then `is_custom:true, persona:"X"`; DELETE removes the key, GET `is_custom:false, persona==default`. Names with spaces (e.g. `Anna 2`) work URL-encoded.

**Step 2 — Implement** (thin wrappers; reuse `state` + `behavioral_core`):

```python
# Orchestrator/routes/persona_routes.py
from pydantic import BaseModel
from Orchestrator.checkpoint import app   # shared FastAPI app — SAME source as tts_routes/admin_routes (confirm by reading tts_routes.py header)
from Orchestrator.behavioral_core import get_persona, DEFAULT_PERSONA_CHAT, PERSONA_PREF_KEY
from Orchestrator.state import (
    get_operator_preference, set_operator_preference,
    OPERATOR_PREFERENCES, save_operator_preferences,
)

class PersonaBody(BaseModel):
    persona: str = ""

@app.get("/operator/persona/{operator}")
def get_op_persona(operator: str):
    custom = get_operator_preference(operator, PERSONA_PREF_KEY, None)
    return {"operator": operator, "persona": get_persona(operator, "chat"),
            "is_custom": bool(custom and str(custom).strip()),
            "default": DEFAULT_PERSONA_CHAT}

@app.put("/operator/persona/{operator}")
def put_op_persona(operator: str, body: PersonaBody):
    set_operator_preference(operator, PERSONA_PREF_KEY, body.persona)
    return {"status": "ok", "operator": operator,
            "persona": get_persona(operator, "chat"), "is_custom": bool(body.persona.strip())}

@app.delete("/operator/persona/{operator}")
def delete_op_persona(operator: str):
    if operator in OPERATOR_PREFERENCES:
        OPERATOR_PREFERENCES[operator].pop(PERSONA_PREF_KEY, None)
        save_operator_preferences()
    return {"status": "ok", "operator": operator, "persona": DEFAULT_PERSONA_CHAT, "is_custom": False}
```

**Step 3 — Register:** add `from Orchestrator.routes.persona_routes import *` to the `import *` block at `app.py:77-97`. (Bare `@app.*` endpoints register on import — no `include_router`.) **Step 4 — Run tests, verify pass. Step 5 — Commit.**

**SAFE POINT — restart + curl smoke:** `curl -s localhost:9091/operator/persona/Brandon`, PUT a value, GET, DELETE; confirm `Manifest/operator_preferences.json` shows/loses the `persona` key.

---

## Milestone M5 — Portal web UI

> Frontend verification = agent-driven Claude-in-Chrome (no JS test harness).

### Task 5.1: Markup + CSS (collapsible above Voice Preferences)

**Files:** Modify `Portal/index.html` (insert before `:478`; bump `main.css ?v=`), `Portal/styles/features/_settings.css`

- Add the `<div class="menu-group system-prompt-section">` collapsible (toggle button `#btnToggleSystemPrompt`, content `#systemPromptContent.section-content.collapsed`, `<textarea id="systemPromptTextarea">`, `<button id="btnSaveSystemPrompt">`, a `#btnResetSystemPrompt` link, hint text). Copy structure from the Streaming/Reasoning collapsible (`:567-583`).
- Add `.system-prompt-section` divider/header CSS (copy `.voice-preferences-section`).

**Verify (Chrome):** navigate `/ui`, open hamburger → section renders above Voice Preferences, collapses/expands. Commit.

### Task 5.2: Load/Save/Reset logic + operator-switch reload

**Files:** Modify `Portal/modules/tts-stt.js` (new exported `initSystemPromptSection`, `syncSystemPromptTextarea`, `saveSystemPrompt`, `resetSystemPrompt`), `Portal/modules/ui-setup.js:854` (toggle wiring), `Portal/modules/app-init.js:619` (boot call + import), `Portal/modules/state-management.js:708` (per-operator reload in the real-switch branch)

- `syncSystemPromptTextarea()`: `GET /operator/persona/{op}` → set `textarea.value = data.persona`; tag `textarea.dataset.isCustom`.
- `saveSystemPrompt()`: `PUT` with `{persona: textarea.value}`, **await + toast** success/failure, refresh.
- `resetSystemPrompt()`: `DELETE` → reload textarea with returned default; toast.
- `state-management.js:708`: add `import('./tts-stt.js').then(m => m.syncSystemPromptTextarea())` in the else (real switch) branch.

**Verify (Chrome):** load shows effective persona; Save persists across reload; switch operator → textarea swaps to that operator's persona; Reset restores default; send a chat and confirm the persona takes effect (e.g. a distinctive instruction in the persona changes the reply tone). Commit.

---

## Milestone M6 — Android Kotlin MVP UI

App root: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/` (quote the path).

### Task 6.1: ViewModel persona load/save

**Files:** Modify `.../ui/settings/SettingsViewModel.kt`

- Add `private val _persona = MutableStateFlow("")` (+ public), `loadOperatorPersona(operator)` (GET `/operator/persona/$operator`, parse `persona`, push to flow), `setOperatorPersona(operator, text)` (PUT, body via `Json.encodeToString` of a `@Serializable` `PersonaBody(persona)`), optional `resetOperatorPersona(operator)` (DELETE). Mirror `loadVoiceCatalog`/`setOperatorVoice`. Guard `api ?: return`.

**Step — Compile:** `cd` into the app root and `./gradlew :app:compileDebugKotlin` (or the project's build command). Commit.

### Task 6.2: System Prompt section in the sheet

**Files:** Modify `.../ui/settings/SettingsSheet.kt`

- Insert between `:657` and `SectionHeader("Operator", BbxDim)` (`:662`) a `CollapsibleSection("🧬 System Prompt", accent=BbxAccent) { ... }` containing a multi-line `OutlinedTextField` bound to `rememberSaveable` state seeded from `viewModel.persona`, a `FullWidthMenuButton("Save")` → `viewModel.setOperatorPersona(operator, text)` + Toast, and a "Reset to default" button.
- Seed/refresh on switch: `LaunchedEffect(operator) { viewModel.loadOperatorPersona(operator) }`.

**Step — Compile.** On-device UI verification is the operator's device test. Commit.

---

## Milestone M7 — Verification + snapshot

### Task 7.1: Full backend suite + cross-surface live matrix
- `Orchestrator/venv/bin/pytest Orchestrator/tests/ -q` all green.
- Live (after final restart): chat persona (default + custom), one voice provider (persona + delivery note), `/local/system-prompt?operator=` per-operator, persona endpoint trio, Portal edit→save→switch→reset, `operator_preferences.json` integrity.

### Task 7.2: Fresh-box / portability verification (build gate — see memory)
This feature ships to customers who boot a clean box; verify the empty-state path, not just our populated box.
- **pytest:** `get_persona` against `OPERATOR_PREFERENCES = {}` (no file) → lean default for any operator name (e.g. `"Dana"`, `"Operator 1"`), no crash. `build_core_system_prompt("x", operator="Dana")` with empty store → contains the lean default. Persona endpoints against an operator with no record → GET `is_custom:false` + default, PUT lazily creates the record.
- **Assert the default is generic:** `DEFAULT_PERSONA_CHAT` contains no hardcoded operator name (no `"Brandon"`), no host/URL. (Reinforce the M1 lean-default test.)
- **Manual:** add a brand-new operator (via the onboarding/admin operator flow) on this box → open the System Prompt editor for it → confirm it pre-seeds the lean default, Save round-trips, switching to it in chat uses the persona. This proves the "new operator → fill it in → saved" path end-to-end.
- **Migration-safe:** confirm an existing `operator_preferences.json` record holding only `tts_voice` still loads and the operator gets the default persona (additive key, no migration).

### Task 7.3: Final code review + dev snapshot
- Dispatch the final code-reviewer over the whole diff.
- `/snapshot-dev` (operator resolved dynamically) documenting the feature, commits, and the design/plan paths.

---

## Risk register
- **Live chat prompt is touched (M2).** Mitigate: TDD asserting functional body byte-identical below the slot + module-load `STREAM_EXCERPT` builds; SAFE-POINT live smoke before proceeding.
- **`STREAM_EXCERPT` fallback branches ignore operator persona (review #10).** Mitigate: Task 2.2 Step 1b rebuilds them per-operator (`build_core_system_prompt("", operator=…)`).
- **3 voice sites, no shared chokepoint.** Mitigate: identical edit per site + an import-sweep check; the import-removal of `BEHAVIORAL_CORE_VOICE` makes a missed site a hard NameError, not a silent miss. (Intentional tripwire.)
- **`config.build_output_spec` parallel assembly (review #6).** Confirmed dead for chat (zero live callers) but a test imports it. Mitigate: Task 3.3 keeps `OUTPUT_SPEC` importable, repointed to the lean default — never delete it out from under `test_phase2_cutover`.
- **Three existing tests break (review #11).** `test_local_routes.py:409`, `test_phase2_cutover.py:76`, `test_local_turn_prepare.py` patches. Mitigate: Task 3.2 Step 3 + Task 3.3 Step 2 pre-empt them; M3's test sweep must be green, not a discovery.
- **New endpoint registration (review #7).** Use the bare `@app.*` + `import *` pattern (Task 4.1), NOT `APIRouter`/`include_router` — the latter would need an extra `include_router` line the plan originally omitted.
- **Portability / fresh box.** Mitigate: Task 7.2 verifies the empty-store + new-operator path; generic default asserted name/host-free.
- **Android multi-line JSON body.** Mitigate: kotlinx.serialization encode, never string interpolation.
- **Stray `chat_routes.py` media change in the tree.** Mitigate: stage only persona lines; never `git add -A`.
