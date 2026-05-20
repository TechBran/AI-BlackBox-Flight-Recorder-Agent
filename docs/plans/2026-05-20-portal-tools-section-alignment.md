# Portal Tools Section + Voice/CU/CLI/Gmail Modal Alignment

> **For Claude:** Execute as one focused multi-commit chain. Push to origin/main after each track verified.

**Goal:** Align the Web Portal's hamburger menu with the Android MVP's tool layout. Extract non-generation tools out of the Generation section into a dedicated Tools section. Add 4 new tool launchers as Portal modals that mirror Android's full-page screens: Computer Use, CLI Agents, Voice Agents, Gmail.

**Read-only reference:** Android MVP — DO NOT modify any file under `AI_BlackBox_Portal_Android_MVP*/`. Read it to understand the target UX, then re-implement Portal-side as modal dialogs.

**Tech stack:** Vanilla JS + HTML + CSS Portal (`Portal/index.html`, `Portal/modules/*.js`, `Portal/styles/`). All Portal modals follow the existing `.modal.hide / .modal-card / .modal-head / .modal-body` pattern (see `geminiProTTSModal` at `index.html:1235` as a reference).

---

## Current state — Portal vs Android

### Portal hamburger menu (`index.html:605-790`)

| Section | Current contents | What Brandon wants |
|---|---|---|
| **Generation** (line 607) | image, video, music, Google SSML, Gemini Pro Audio, **Scheduler, Devices, Telephony, Cellular, SMS, Contacts** | Generation only — extract last 6 |
| Android Overlay | (Android-only, hidden on web) | unchanged |
| Running Apps | apps list | unchanged |
| Voice Preferences | TTS voice dropdown | unchanged (separate from Voice Agents tool) |
| Streaming Mode | AI reasoning toggle | unchanged |
| **Updates** (line 745) | Check / Install / View Log / Rollback | unchanged — used as pattern reference for Tools |
| System Controls | restart, etc | unchanged |
| Advanced | misc | unchanged |
| ❌ **No Tools section** | — | **NEW** — house all extracted + new tool launchers |
| ❌ **No Voice Agents launcher** | (inline banners on chat screen only) | **NEW** modal |
| ❌ **No CLI Agents launcher** | (inline banners on chat screen only) | **NEW** modal |
| ❌ **No Computer Use launcher** | (cu-drawer only — attaches to operator menu when CU provider selected) | **NEW** modal launching full-page |
| ❌ **No Gmail UI** | — | **NEW** modal (OAuth + status) |

### Android reference (`SettingsSheet.kt`)

```kotlin
// Tools section equivalents:
MenuButton("Computer Use") { onNavigate("computer_use") }   // line 248
MenuButton("CLI Agent") { onNavigate("cli_agent") }         // line 249
MenuButton("Voice Agent") { onNavigate("voice") }           // line 250
MenuButton("📧 Connect Gmail") { ... OAuth ... }            // line 391
// Plus the existing extract-targets:
MenuButton(Scheduler/Devices/Telephony/Cellular/SMS/Contacts)
```

Routes resolve to full-page composables in `NavGraph.kt` (lines 105-166):
- `Routes.COMPUTER_USE` → `ComputerUseScreen` (live view + Anthropic model selector + device dropdown + key injection)
- `Routes.CLI_AGENT` → `CliAgentScreen` (uses `AppFolderPicker` for app/working-dir + provider chooser Claude/Gemini/Codex → `TerminalScreen`)
- `Routes.VOICE` → `VoiceScreen` (provider tabs GPT/Gemini Live/Grok Live + voice + model + VAD selectors + mic)

### Portal already has (just not as launcher buttons)

- **Computer Use**: `Portal/modules/cu-drawer.js` (`cuDrawer` element, device select, controls, session id state). Currently attached to operator-menu-bubble when "computer-use" provider is selected — NOT exposed as a launcher button.
- **CLI Agents**: Only chat-provider banners at `index.html:79-122` for Claude + Gemini CLI Agent. Codex provider DOES exist in backend (`Orchestrator/routes/cli_agent_routes.py:47 SUPPORTED_PROVIDERS = ("claude", "gemini", "codex")`) but no Portal UI for it.
- **Voice Agents**: `Portal/modules/gpt-realtime.js`, `gemini-live.js`, `grok-live.js` exist as modules with inline banner controls (lines 159-279 in `index.html`). They render INLINE on the chat screen when the corresponding chat provider is selected — NOT as a unified modal.
- **Gmail**: backend has `/auth/gmail/{authorize,callback,disconnect}` + `/gmail/status/{operator}` (`Orchestrator/routes/gmail_routes.py`). Portal has zero Gmail UI.

---

## Architecture

### Tools section structure (mirrors Updates)

Same `.menu-group` block pattern as Updates section (`index.html:745`):

```html
<div class="menu-group tools-section">
  <h4>🛠️ Tools</h4>
  <div class="menu-grid">
    <!-- Extracted from Generation -->
    <button id="btnCronManager" class="btn">Scheduler</button>
    <button id="btnDeviceManager" class="btn">Devices</button>
    <button id="btnTelephonyManager" class="btn">Telephony</button>
    <button id="btnCellularManager" class="btn">Cellular</button>
    <button id="btnSMSInbox" class="btn">SMS<span id="smsBadge"></span></button>
    <button id="btnContactsManager" class="btn">Contacts</button>
    <!-- New (Track 2-5) -->
    <button id="btnComputerUse" class="btn">🖥️ Computer Use</button>
    <button id="btnCLIAgents" class="btn">💻 CLI Agents</button>
    <button id="btnVoiceAgents" class="btn">🎙️ Voice Agents</button>
    <button id="btnGmail" class="btn">📧 Gmail</button>
  </div>
</div>
```

Placement: **directly after Generation, before Android Overlay section** (so Tools is the second item the user sees in the menu, matching Brandon's "directly under Generation" ask).

### Modal pattern (mirrors `geminiProTTSModal`)

Standard structure used 5+ times in `index.html` already:

```html
<div id="<feature>Modal" class="modal hide">
  <div class="modal-card">
    <div class="modal-head">
      <h3>Title</h3>
      <button class="modal-close" data-modal="<feature>Modal">✕</button>
    </div>
    <div class="modal-body">
      <!-- per-feature content -->
    </div>
  </div>
</div>
```

Modal show/hide via existing `openModal(id)` / `closeModal(id)` helpers (already used by 5 other modals). New JS modules go in `Portal/modules/<feature>-modal.js` exporting `init<Feature>Modal()` called from `app-modular.js`.

### CSS

New `Portal/styles/features/_tools.css` for the Tools section + per-modal styling. Cache-buster `?v=genui<N>` bumped once at end of Phase B (audit I3 atomicity rule from the live-models plan applies here too — single commit for HTML+JS+CSS or version-bump first).

---

## Tracks (commit per track for bisectability)

### Track 1 — Extract tools from Generation + create empty Tools section

**Files:**
- `Portal/index.html` — remove 6 buttons from `generation-grid` (lines 615-620), insert new Tools section after Generation
- `Portal/styles/features/_tools.css` (NEW)
- Cache-buster bump `?v=genui<N>` at top of index.html

**Steps:**
1. Read current cache-buster: `grep -oP 'main\.css\?v=genui\K\d+' Portal/index.html | head -1`. Bump.
2. Cut buttons `btnCronManager`, `btnDeviceManager`, `btnTelephonyManager`, `btnCellularManager`, `btnSMSInbox`, `btnContactsManager` from generation-grid.
3. Add new `<div class="menu-group tools-section">` block after generation-section close `</div>`.
4. Paste the 6 buttons into the new tools-grid. Remove `btn-generation` class, use plain `btn` class (or new `btn-tool` class if styling differs).
5. Create `_tools.css` matching the Updates section's visual weight (subtle header, grid layout). Update CSS manifest.
6. No JS changes needed — existing button click handlers (initCronManager etc.) bind by element id, which is preserved.

**Verification:**
```bash
# All 6 buttons moved
for id in btnCronManager btnDeviceManager btnTelephonyManager btnCellularManager btnSMSInbox btnContactsManager; do
    grep -c "id=\"$id\"" Portal/index.html  # should output 1 each
done
# Tools section header present
grep -c 'tools-section' Portal/index.html  # should be ≥1
# Buttons no longer in generation-grid (count generation buttons should drop from 11→5)
grep -A 12 'generation-grid' Portal/index.html | grep -c '<button'  # should be 5
```

**Commit message:** `refactor(portal): extract tools out of Generation into dedicated Tools section`

**Smoke test:** open Portal, open hamburger menu. Verify: Generation now has 5 buttons (image/video/music/SSML/Gemini-Pro-Audio). Tools section appears below with 6 buttons (scheduler/devices/telephony/cellular/SMS/contacts). Each button still opens its existing modal.

---

### Track 2 — Computer Use launcher (provider switch, NOT a new modal)

**Decision (Brandon, 2026-05-20):** Computer Use stays with the existing cu-drawer flow exactly as it is today. The Tools button is a shortcut that switches the chat provider to `computer-use`, which auto-attaches the existing drawer to the operator-menu-bubble. Zero new modal, zero refactor to cu-drawer.js, zero new state.

**Files:**
- `Portal/index.html` — add `<button id="btnComputerUse" class="btn">🖥️ Computer Use</button>` to Tools section
- `Portal/app-modular.js` (or similar init module) — wire click handler

**Click handler logic:**
```javascript
document.getElementById('btnComputerUse').addEventListener('click', () => {
    // Switch chat provider to computer-use; existing chat-provider change
    // handlers will auto-attach the cu-drawer to the operator menu.
    const providerSelect = document.getElementById('providerSelect'); // verify actual id
    providerSelect.value = 'computer-use';
    providerSelect.dispatchEvent(new Event('change'));
    // Close hamburger menu after switching
    closeMenu();
});
```

Find the actual provider-select element id via grep before wiring. The dispatch of `change` event triggers whatever existing handler manages provider switching + drawer attachment.

**Verification:**
```bash
grep -q 'btnComputerUse' Portal/index.html && echo "✓ button present"
# Provider select element exists (find its actual id)
grep -n 'id="provider' Portal/index.html | head -5
# Click handler wired
grep -A 3 'btnComputerUse' Portal/app-modular.js Portal/modules/*.js | head -10
```

**Commit message:** `feat(portal): Computer Use Tools button (switches to computer-use provider)`

**Smoke test:** click 🖥️ Computer Use in Tools → chat provider switches to computer-use → existing cu-drawer attaches to operator-menu-bubble exactly as it does today when user picks CU from the provider dropdown directly. No behavior change to CU itself.

---

### Track 3 — CLI Agents launcher modal

**Files:**
- `Portal/index.html` — `<button id="btnCLIAgents">` + `<div id="cliAgentsModal">`
- `Portal/modules/cli-agents-modal.js` (NEW)
- `Portal/styles/features/_cli_agents_modal.css` (NEW)
- `Portal/app-modular.js` — import + init

**Modal contents (mirrors Android `CliAgentScreen` + `AppFolderPicker`):**
- App folder picker — list current apps from `/agent/apps` (already-existing endpoint)
- "New working directory" text input + "Create" button (creates folder under `Apps/<name>/`)
- Provider radio group: Claude / Gemini / Codex (backend supports all 3 per `cli_agent_routes.py:47 SUPPORTED_PROVIDERS`)
- "Launch" button — opens a terminal-like view in the modal body OR navigates to a new tab pointed at `/ws/cli-agent/<session_id>?provider=<p>&app=<slug>` (backend endpoint already exists)
- Terminal renderer: lightweight xterm.js-style live output panel inside the modal (Portal-native; doesn't try to mirror Android's full Termux integration)

**Decision (Brandon, 2026-05-20):** in-modal terminal panel with simple `<pre>` element (auto-scroll, monospace font) for output + `<textarea>` for input. NO xterm.js dependency. If the simple form feels primitive in practice, xterm.js can be a future enhancement.

**Verification:**
```bash
grep -q 'btnCLIAgents' Portal/index.html
grep -q 'cliAgentsModal' Portal/index.html
# Backend route exists for terminal WS
grep -q '/ws/cli-agent\|cli_agent_routes' Orchestrator/app.py
```

**Commit message:** `feat(portal): CLI Agents modal launcher (Claude/Gemini/Codex)`

**Smoke test:** click 💻 CLI Agents → modal opens with app list + new-dir input + provider radio. Pick an app + Claude provider → launch → terminal panel streams output. Test with each of the 3 providers.

---

### Track 4 — Voice Agents launcher modal

**Files:**
- `Portal/index.html` — `<button id="btnVoiceAgents">` + `<div id="voiceAgentsModal">`
- `Portal/modules/voice-agents-modal.js` (NEW)
- `Portal/styles/features/_voice_agents_modal.css` (NEW)
- `Portal/app-modular.js` — import + init
- **Possibly refactor:** `Portal/modules/gpt-realtime.js`, `gemini-live.js`, `grok-live.js` to export their connect/mic/disconnect functions so the modal can drive them externally

**Modal contents (mirrors Android `VoiceScreen`):**
- Provider tabs: `GPT Realtime` | `Gemini Live` | `Grok Live`
- Per tab, the existing controls from the inline banners surface inside the modal:
  - **GPT Realtime** (gpt-realtime-2 default + 4 chat models + 10 voices + vad_type + eagerness/idle_timeout)
  - **Gemini Live** (3 models + 30 voices with descriptors + vad_sensitivities + thinking_level for 3.1)
  - **Grok Live** (5 voices, single model)
- Voice row, model row, VAD row (mirrors Android's VoiceScreen.kt chip-row pattern but using HTML `<select>` dropdowns matching existing Portal style)
- Bottom row: Connect / Mic / Disconnect buttons (same controls as existing inline banners)
- Reuse all existing WS connection code in gpt-realtime.js / gemini-live.js / grok-live.js — the modal is a presentation wrapper

**Refactoring needed:**
The existing modules bind to specific DOM ids in the inline banner (e.g., `getElementById('realtimeModelSelect')`). The modal will have DIFFERENT ids (e.g., `voiceAgentModalRealtimeModelSelect`). Options:
1. **Parameterize the modules** — accept a `selectors` config object passed to each connect function. Cleanest but requires changing existing call sites.
2. **Duplicate the dropdown handling in the modal** — call WS-level functions only, do all selector reads in the modal. Less refactoring but some logic duplication.
3. **Have the modal write through to the existing inline banner ids** — hide the inline banner, copy values to the modal, write modal values back to inline before calling existing functions. Hacky.

Lean toward option 1 — minimal selector-config object is a clean refactor that improves the modules. Existing inline-banner call sites pass the current ids; modal passes its own ids. Both work.

**Decision (Brandon, 2026-05-20):** DEPRECATE the inline banners entirely. Modal-only flow. The inline banner markup at `Portal/index.html:159-279` (realtime + gemini + grok banners) gets REMOVED in this commit, along with any chat-provider-selector code that auto-shows them. The chat provider dropdown should no longer offer realtime/gemini-live/grok-live as options — those are now Tools, not chat providers. Voice agents are launched ONLY from the Tools modal.

Side-effect: the WS connection lifecycle moves into the modal entirely. If a user has a voice session active and closes the modal, the session should disconnect (no more invisible background voice connection from a previously-active banner).

**Verification:**
```bash
grep -q 'btnVoiceAgents' Portal/index.html
grep -q 'voiceAgentsModal' Portal/index.html
node --check Portal/modules/voice-agents-modal.js
# Verify modules still work after selector parameterization
node --check Portal/modules/gpt-realtime.js
node --check Portal/modules/gemini-live.js
node --check Portal/modules/grok-live.js
```

**Commit message:** `feat(portal): Voice Agents modal launcher (GPT Realtime + Gemini Live + Grok Live)`

**Smoke test:** click 🎙️ Voice Agents → modal opens. Switch between provider tabs. Each tab populates the right voice/model/VAD dropdowns. Click Connect → mic capture starts → speak → hear response (full end-to-end audio loop, mirroring what we validated yesterday for the inline banner flow).

---

### Track 5 — Gmail modal

**Files:**
- `Portal/index.html` — `<button id="btnGmail">` + `<div id="gmailModal">`
- `Portal/modules/gmail-modal.js` (NEW)
- `Portal/styles/features/_gmail_modal.css` (NEW)
- `Portal/app-modular.js` — import + init

**Modal contents (mirrors Android "Connect Gmail" button — minimal viable):**
- Connection status display — `GET /gmail/status/<operator>` shows connected email or "Not connected"
- "Connect Gmail" button → opens `/auth/gmail/authorize?operator=<op>` in a new tab (OAuth consent flow)
- "Disconnect" button → POST `/gmail/disconnect/<operator>` (visible only when connected)
- (Future enhancement note in comments: an inbox / search UI built on the `gmail_search` / `gmail_read` MCP tools — out of scope for v1)

**Why minimal:** Android also only has a Connect/Disconnect button — no inbox UI. Matching that scope avoids feature drift between platforms.

**Verification:**
```bash
grep -q 'btnGmail' Portal/index.html
grep -q 'gmailModal' Portal/index.html
node --check Portal/modules/gmail-modal.js
curl -s http://localhost:9091/gmail/status/Brandon | python3 -m json.tool | head -5
```

**Commit message:** `feat(portal): Gmail modal launcher (OAuth connect/disconnect)`

**Smoke test:** click 📧 Gmail → modal opens with current status. Click Connect → OAuth tab opens → grant access → modal status updates to connected email. Click Disconnect → status returns to Not connected.

---

### Track 6 — Polish + cache-buster bump + push

**Files:**
- `Portal/index.html` — confirm cache-buster bumped consistently (single number across all `?v=genui<N>` references)
- `Portal/app-modular.js` — verify all 4 new modal inits wired
- `Portal/styles/css_manifest.json` — if Portal uses a built manifest, rebuild

**Verification:**
```bash
# All 4 new buttons present in Tools section
for id in btnComputerUse btnCLIAgents btnVoiceAgents btnGmail; do
    grep -q "id=\"$id\"" Portal/index.html && echo "✓ $id"
done
# All 4 new modals present
for id in computerUseModal cliAgentsModal voiceAgentsModal gmailModal; do
    grep -q "id=\"$id\"" Portal/index.html && echo "✓ $id"
done
# Cache-buster unified
grep -oP 'v=genui\K\d+' Portal/index.html | sort -u  # should output single number
# All new JS modules syntax-clean
for f in Portal/modules/{computer-use,cli-agents,voice-agents,gmail}-modal.js; do
    node --check "$f" && echo "✓ $f"
done
# Push
git push origin main
git log origin/main..HEAD  # should be empty
```

**Commit message:** `chore(portal): bump cache-buster + finalize Tools section alignment`

---

## Critical reuse — don't reinvent

| Need | Existing pattern | Source |
|---|---|---|
| Modal show/hide | `openModal(id)` / `closeModal(id)` | existing app-modular.js helpers |
| Modal markup template | `geminiProTTSModal` | `Portal/index.html:1235` |
| Per-operator localStorage | `bb_cu_session_id_<op>` pattern | `Portal/modules/cu-drawer.js:26` |
| sessionStorage 5-min cache | `state-management.js:528` (chat models) + Phase B `fetchRealtimeCatalog` | already in use |
| OpenAI realtime WS connection | `gpt-realtime.js` `connect()` | reuse, parameterize selectors |
| Gemini Live WS connection | `gemini-live.js` `connect()` | reuse, parameterize selectors |
| Grok Live WS connection | `grok-live.js` `connect()` | reuse, parameterize selectors |
| Device list | `/devices/` API (used by cu-drawer) | existing |
| CU session control | `stopCUTask`, `newCUSession` exports | `Portal/modules/chat-send.js` |
| Gmail OAuth flow | `/auth/gmail/{authorize,callback,disconnect}` | `Orchestrator/routes/gmail_routes.py` |
| CLI agent WS | `/ws/cli-agent/<session>?provider=...&app=...` | `Orchestrator/routes/cli_agent_routes.py` |

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Modal stacking** — multiple modals open at once | Existing `openModal()` already closes others first (verify in app-modular.js) |
| **WS connection conflict** between inline banner and modal | Track 4 decision: deprecate inline banners OR hide-on-modal-open |
| **CSS specificity** — modal styles may not match Tools button visual weight | Style each modal from scratch using existing `_modal.css` base, override per-feature |
| **Cache-buster atomicity** | Bump as FIRST step in Track 6 if individual tracks didn't (or bump once per track if commits land days apart) |
| **CU full-page UX** — modal may not feel "full page" | Use `modal-card.modal-card-fullscreen` variant if exists, OR just full viewport `width: 100vw; height: 100vh` style |
| **CLI Agents terminal complexity** — xterm.js is a non-trivial dep | First cut: simple `<pre>` element with auto-scroll. If usable, ship. If too primitive, add xterm.js in follow-up |
| **Voice Agents selector refactor breaks existing inline use** | Run full smoke test of inline banner (chat provider → realtime → connect) before AND after Track 4 |
| **Gmail OAuth redirect URI** | Backend redirect URI may be hardcoded to a specific path — verify it matches Portal origin before testing |

---

## Out of scope (defer)

- Android-side changes — Brandon explicitly said don't touch
- Gmail inbox/search UI in Portal — match Android's Connect/Disconnect-only scope for v1
- Robotics tool button — exists in Android (`Routes.ROBOTICS`) but not requested
- Codex CLI agent backend implementation — backend declares support, but actual codex CLI may need separate setup. If `codex login` flow is needed, that's a separate plan.
- xterm.js full-fidelity terminal — start with simple output pane

---

## Verification commands (full suite, run after Track 6)

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc

# All 10 Tools-section buttons present
for id in btnCronManager btnDeviceManager btnTelephonyManager btnCellularManager btnSMSInbox btnContactsManager btnComputerUse btnCLIAgents btnVoiceAgents btnGmail; do
    grep -q "id=\"$id\"" Portal/index.html && echo "✓ $id" || echo "✗ MISSING $id"
done

# All 4 new modals present
for id in computerUseModal cliAgentsModal voiceAgentsModal gmailModal; do
    grep -q "id=\"$id\"" Portal/index.html && echo "✓ $id" || echo "✗ MISSING $id"
done

# Tools section header
grep -c 'tools-section' Portal/index.html

# Cache-buster unified
grep -oP 'v=genui\K\d+' Portal/index.html | sort -u

# All new JS modules clean
for f in Portal/modules/computer-use-modal.js Portal/modules/cli-agents-modal.js Portal/modules/voice-agents-modal.js Portal/modules/gmail-modal.js; do
    node --check "$f" 2>&1 | head -3
done

# Inline banner regression — refactored modules still work
node --check Portal/modules/gpt-realtime.js
node --check Portal/modules/gemini-live.js
node --check Portal/modules/grok-live.js

# Backend untouched
git diff --stat HEAD~6 HEAD | grep -E 'Orchestrator/' && echo "WARN: backend touched" || echo "✓ backend untouched"

# Android untouched
git diff --stat HEAD~6 HEAD | grep -E 'AI_BlackBox_Portal_Android_MVP' && echo "WARN: android touched" || echo "✓ android untouched"
```

---

## Commit map

| # | Track | Commit message | Files | Smoke test |
|---|---|---|---|---|
| 1 | Track 1 | `refactor(portal): extract tools out of Generation into dedicated Tools section` | index.html, _tools.css | Generation has 5 buttons; Tools has 6 |
| 2 | Track 2 | `feat(portal): Computer Use modal launcher` | index.html, computer-use-modal.js, _computer_use_modal.css | CU modal opens + drives existing CU machinery |
| 3 | Track 3 | `feat(portal): CLI Agents modal launcher (Claude/Gemini/Codex)` | index.html, cli-agents-modal.js, _cli_agents_modal.css | app picker + provider radio + terminal streams |
| 4 | Track 4 | `feat(portal): Voice Agents modal launcher` | index.html, voice-agents-modal.js, _voice_agents_modal.css, refactored gpt-realtime.js/gemini-live.js/grok-live.js | Full audio loop per provider through modal |
| 5 | Track 5 | `feat(portal): Gmail modal launcher (OAuth connect/disconnect)` | index.html, gmail-modal.js, _gmail_modal.css | OAuth round-trip works |
| 6 | Track 6 | `chore(portal): bump cache-buster + finalize Tools alignment` | index.html | All buttons present, cache-buster unified, pushed |

**Push to origin/main after Track 6** (or after each track if Brandon prefers tighter cadence).
