# Hamburger Menu Polish — Collapsible Sections + Tailscale Copy Button (both surfaces)

> **For Claude:** Execute as one focused commit chain across Android + Portal. Push to origin/main when verified.

**Goal:** Pure polish pass on both the Android MVP front end and the Web Portal hamburger menu. Collapse most sections behind clickable headers (narrow-screen friendly, less visual noise) and add a copy button to the Tailscale paired-server URL on both surfaces.

**This is polish — NOT new features.** Existing buttons, behaviors, and modal launchers from yesterday's Tools restructure (commits `e02bffd..1245783`) are preserved verbatim. Only the section CONTAINERS change shape; their CONTENTS are unchanged.

---

## Per-section disposition (locked from Brandon, 2026-05-20)

### Android MVP (`SettingsSheet.kt`)

| Section | Current | After |
|---|---|---|
| Generation (line 156) | Always-expanded with `FlowRow` (side-by-side wrap) | **Collapsible** under tappable header. Content reflows to **vertical Column (one item per line)** for narrow-screen friendliness. |
| Floating Overlay (line 176) | Always-expanded header + description + 1 button | **Collapsible** behind header; description and button hide until tapped. |
| Tools (line 239) | Always-expanded `FlowRow` with 10 buttons | **Collapsible**. Content reflows to **vertical Column (one button per line)**. (Brandon: "Let's try putting the tools behind the dropdown… if we don't like it, we can change it later.") |
| Running Apps (line 268) | Always-expanded list | **Collapsible** behind header. Apps already one-per-line; perfect for the dropdown content. |
| Gmail (line 325) | Always-expanded with status + 2 buttons | **Collapsible**. The header shows `📧 Gmail · <connected email>` (or "Not connected"); tap reveals Connect/Disconnect buttons. |
| Voice (line 410) | Already a dropdown menu pattern | **LEAVE ALONE** — already correct. |
| Provider (line 467) | Standard dropdown | **LEAVE ALONE**. |
| Model (line 517) | Standard dropdown | **LEAVE ALONE**. |
| Operator (line 548) | Standard | **LEAVE ALONE**. |
| System (line 654) | Includes paired-server display + action buttons | **Add copy button** to the Paired Server display at line 666-676. No collapse. |

### Web Portal (`Portal/index.html`)

| Section | Current | After |
|---|---|---|
| Generation (line 406) | Always-expanded `.menu-grid` (2-column) | **Collapsible** behind header button. Content reflows to **single column**. |
| Tools (line 418) | Always-expanded `.menu-grid` (2-column) | **Collapsible**. Single column when expanded. |
| Running Apps (line 445) | Always-expanded `.menu-apps-list` | **Collapsible**. Apps remain one-per-line. |
| Voice Preferences (line 453) | Always-expanded TTS voice dropdown | **LEAVE ALONE** — already useful at-a-glance. |
| AI Reasoning Mode (line 542) | Always-expanded toggle | **Collapsible** behind header. (Brandon: "we don't need this exposed".) |
| Updates (line 555) | Always-expanded status + buttons | **LEAVE ALONE**. |
| System Controls (line 574) | Always-expanded action buttons | **LEAVE ALONE**. **Add Tailscale Address row** at the top of this section with a copy button. |
| Advanced Settings (line 586) | **Already collapsible** via `.advanced-section` toggle pattern | **LEAVE ALONE — use as the pattern reference for the 4 new collapsibles.** |

---

## Architecture

### Android — `CollapsibleSection` reusable Composable

Currently each section uses a free-standing `SectionHeader(text, color)` call followed by content. Refactor to a `CollapsibleSection` wrapper:

```kotlin
@Composable
private fun CollapsibleSection(
    title: String,
    accent: Color = BbxAccent,
    subtitle: String? = null,   // e.g. Gmail's connected-email tail
    initiallyExpanded: Boolean = false,
    content: @Composable () -> Unit,
) {
    var expanded by rememberSaveable { mutableStateOf(initiallyExpanded) }
    Column(modifier = Modifier.fillMaxWidth()) {
        // Tappable header row — chevron rotates 180° on expanded
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable { expanded = !expanded }
                .padding(vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = title,
                style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                color = accent,
                modifier = Modifier.weight(1f),
            )
            subtitle?.let {
                Text(
                    text = it,
                    style = MaterialTheme.typography.bodySmall,
                    color = Neutral500,
                    modifier = Modifier.padding(end = 8.dp),
                )
            }
            val rotation by animateFloatAsState(if (expanded) 180f else 0f, label = "chevron")
            Icon(
                Icons.Default.ExpandMore,
                contentDescription = if (expanded) "Collapse" else "Expand",
                modifier = Modifier.rotate(rotation),
                tint = accent,
            )
        }
        AnimatedVisibility(visible = expanded) {
            Column(modifier = Modifier.padding(top = 4.dp, bottom = 8.dp)) {
                content()
            }
        }
    }
}
```

Use `rememberSaveable` so the expand/collapse state survives configuration changes (rotation, theme switch). NO cross-session persistence in v1 — Brandon didn't ask, and DataStore plumbing would inflate scope.

### Android — vertical-stack content for Generation + Tools

Currently both sections wrap their `MenuButton`s in `FlowRow` which fills horizontally then wraps to next row. For Brandon's "one line at a time" UX, replace with a plain `Column` whose children are full-width `MenuButton`s:

```kotlin
// OLD:
FlowRow(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
    MenuButton("Generate Image") { ... }
    MenuButton("Generate Video") { ... }
    // ...
}

// NEW:
Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
    FullWidthMenuButton("Generate Image") { ... }
    FullWidthMenuButton("Generate Video") { ... }
    // ...
}
```

Add a `FullWidthMenuButton(text, onClick)` helper that's just `MenuButton` with `Modifier.fillMaxWidth()` applied. Don't break the existing `MenuButton` signature (other code paths use it).

### Android — Paired Server copy button

Refactor the Box at line 660-676. Add a horizontally-arranged Row inside the Column with the address text + a copy IconButton:

```kotlin
Box(
    Modifier
        .fillMaxWidth()
        .clip(RoundedCornerShape(RadiusMd))
        .background(Neutral200)
        .padding(12.dp)
) {
    Column {
        Text("Paired Server", style = MaterialTheme.typography.labelSmall, color = Neutral500)
        Spacer(Modifier.height(4.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                origin.ifBlank { "Not paired" },
                style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                color = if (origin.isNotBlank()) SolidGreen else BbxAccent,
                modifier = Modifier.weight(1f),
            )
            if (origin.isNotBlank()) {
                IconButton(
                    onClick = {
                        val clip = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                        clip.setPrimaryClip(ClipData.newPlainText("Paired Server", origin))
                        view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                        Toast.makeText(context, "Copied $origin", Toast.LENGTH_SHORT).show()
                    },
                    modifier = Modifier.size(32.dp),
                ) {
                    Icon(
                        Icons.Default.ContentCopy,
                        contentDescription = "Copy paired server URL",
                        tint = Neutral500,
                    )
                }
            }
        }
    }
}
```

Reuse `context` and `view` which already exist in scope (verified by reading line 188+ where the screenCaptureLauncher block uses both).

### Portal — collapsible section pattern (reuse `.advanced-section` style)

The existing implementation at `Portal/index.html:586+` already has a working collapse pattern:

```html
<div class="menu-group advanced-section">
  <button id="btnToggleAdvanced" class="btn-advanced-toggle">
    🔧 Advanced Settings
    <span class="toggle-icon">▼</span>
  </button>
  <div id="advancedSettings" class="advanced-content collapsed">
    <div class="menu-grid"> ... </div>
  </div>
</div>
```

The toggle is wired in a JS module (probably `Portal/modules/ui-setup.js` or `app-init.js` — verify via grep). The pattern:
1. Header → `<button class="btn-toggle">` with `▼` chevron span
2. Content → `<div class="content collapsed">` (CSS: `.collapsed { display: none; }` or `max-height: 0`)
3. Click handler → toggles `collapsed` class + rotates chevron

Apply this exact pattern to 4 new sections. Reuse the existing `.btn-advanced-toggle` and `.advanced-content` CSS rules (rename to generic `.btn-section-toggle` / `.section-content-collapsed` if you want, OR just reuse as-is for v1 — naming cleanup is future polish).

For Generation + Tools, ALSO change the inner `.menu-grid` to single-column when inside a collapsed section. CSS:

```css
.section-content-collapsed.expanded .menu-grid {
    grid-template-columns: 1fr;  /* single column when expanded */
}
```

OR simpler: add a new `.menu-grid-vertical` class for the new collapsibles, keep existing `.menu-grid` for the always-expanded sections (Voice Preferences, Updates, System Controls — those use 2-column and shouldn't change).

### Portal — Tailscale Address display + copy button (NEW)

Portal currently has NO Tailscale address display in the hamburger menu (verified by grep — only mentions in device-manager and pairing flows, not in the menu). Brandon's ask: ADD it with a copy button.

Backend source: `BLACKBOX_TAILNET_HOSTNAME` env var, exposed via `pairing_routes.py:296-297` as `https://{BLACKBOX_TAILNET_HOSTNAME}`. The existing onboarding settings endpoint also returns it. Find the right Portal-side fetch point:

```bash
grep -rnE 'default_origin|pairing.*origin|tailnet' Portal/modules/ | head -5
```

Likely `ui-setup.js:818` already has `pairing.default_origin || (window.location.origin + "/ui")` — that gives us the value we want.

Placement: top of System Controls section, BEFORE the action buttons. Markup:

```html
<div class="menu-group system-controls-section">
  <h4>System Controls</h4>

  <!-- NEW: Tailscale address with copy button -->
  <div class="tailscale-address-row">
    <span class="tailscale-label">Paired Server:</span>
    <code class="tailscale-value" id="tailscaleAddressValue">Loading…</code>
    <button id="btnCopyTailscale" class="btn-icon" title="Copy address to clipboard" type="button">📋</button>
  </div>

  <div class="menu-grid">
    <!-- existing buttons unchanged -->
  </div>
  <p class="checkpoint-hint">...</p>
</div>
```

JS module: fetch the address (reuse `ui-setup.js` pairing logic OR fetch `/pairing/origin` or equivalent — verify backend endpoint). Wire copy via `navigator.clipboard.writeText(value)` with a toast confirmation.

---

## Tracks (commit per track for bisectability)

### Track 1 — Android collapsible sections (Generation, Floating Overlay, Tools, Running Apps, Gmail)

**Files:**
- `AI_BlackBox_Portal_Android_MVP*/SettingsSheet.kt` — refactor 5 sections to use new `CollapsibleSection` helper + vertical Column for Gen/Tools content
- Add `FullWidthMenuButton` helper near `MenuButton`

**Steps:**
1. Add the `CollapsibleSection` and `FullWidthMenuButton` Composables near the existing `SectionHeader` helper (line 800+)
2. Wrap Generation section content in `CollapsibleSection(title = "🎨 Generation", accent = BbxAccent)` — replace inner FlowRow with vertical Column of FullWidthMenuButtons
3. Wrap Floating Overlay section similarly — keep description visible inside the expanded content
4. Wrap Tools section — same FlowRow→Column transformation
5. Wrap Running Apps section — content already vertical, just wrap
6. Wrap Gmail section — pass `subtitle = connectedEmail.ifBlank { null }` so the header shows email at-a-glance

Compile + verify with `./gradlew :app:compileDebugKotlin`.

**Commit:** `feat(android): collapsible Generation/Overlay/Tools/Apps/Gmail sections`

### Track 2 — Android Paired Server copy button

**Files:**
- `SettingsSheet.kt` — update the Box at line 660-676 to add a copy IconButton

**Steps:**
1. Add imports: `ClipboardManager`, `ClipData`, `Context`, `Icons.Default.ContentCopy`, `IconButton`
2. Refactor the Column inside Box to use a Row for the address + copy button
3. Wire copy logic with haptic + Toast
4. Verify on emulator/device — tap copy button, paste into another field, confirm value

**Commit:** `feat(android): copy button on Paired Server URL display`

### Track 3 — Portal collapsible sections (Generation, Tools, Running Apps, AI Reasoning Mode)

**Files:**
- `Portal/index.html` — refactor 4 sections to use the existing `.advanced-section`-style toggle pattern
- `Portal/styles/features/_tools.css` or a new `_collapsibles.css` — add styling for the section-toggle button + chevron rotation
- `Portal/modules/app-init.js` (or wherever advanced-section toggle is wired) — extend the toggle JS to handle the 4 new toggles

**Steps:**
1. Find the existing Advanced Settings toggle JS (`grep -rn btnToggleAdvanced Portal/modules/`). Read its structure.
2. Generalize the JS: factor out a `wireCollapsibleSection(toggleBtnId, contentDivId)` helper, or extend the existing handler to also bind to 4 new toggle button ids
3. Refactor index.html section by section:
   - Generation: wrap content in `<div class="section-content collapsed">`, convert `<h4>` to `<button class="btn-section-toggle">🎨 Generation <span class="toggle-icon">▼</span></button>`
   - Same for Tools, Running Apps, AI Reasoning Mode
4. CSS:
   - `.section-content.collapsed { display: none; }` (or `max-height: 0` with transition for animated)
   - `.btn-section-toggle` styled like the existing `.btn-advanced-toggle`
   - `.btn-section-toggle .toggle-icon` rotates when content is expanded
   - For Generation + Tools specifically: `.menu-grid` inside their content becomes `grid-template-columns: 1fr` (single column)
5. Wire init in `app-init.js` for all 4 new toggle buttons

**Commit:** `refactor(portal): collapse Generation/Tools/Apps/Reasoning sections behind toggle headers`

### Track 4 — Portal Tailscale Address display + copy button

**Files:**
- `Portal/index.html` — add Tailscale address row at top of System Controls section
- `Portal/modules/app-init.js` (or a new `tailscale-display.js` module) — fetch + render + wire copy button
- `Portal/styles/features/_system_controls.css` (or wherever System Controls is styled) — add `.tailscale-address-row` styling

**Steps:**
1. Find the existing pairing-origin fetch in `ui-setup.js:818` and confirm it exposes the right value
2. Add markup at top of System Controls section
3. JS: on app-init, fetch the origin (reuse pairing logic) → set `#tailscaleAddressValue` textContent → display "Not paired" with greyed style if blank
4. Copy button click: `navigator.clipboard.writeText(value).then(() => toast('Copied'))`. Fallback to `document.execCommand('copy')` for older browsers (unlikely needed on modern Portal users but cheap)
5. CSS: row layout (label + value + copy button, value in monospace, copy button as compact icon-style)

**Commit:** `feat(portal): Tailscale paired-server address display + copy button`

### Track 5 — Polish + cache-buster bump + push

**Files:** `Portal/index.html` cache-buster

**Steps:**
1. Bump cache-buster genui268 → genui269
2. Full verification suite
3. Commit + push all 4 tracks to origin/main

**Commit:** `chore(portal): cache-buster bump + finalize hamburger polish`

---

## Critical reuse

| Need | Existing pattern | Source |
|---|---|---|
| Portal section collapse | `.advanced-section` toggle | `Portal/index.html:586+`, JS in `app-init.js` |
| Android Composable section header | `SectionHeader(text, color)` | `SettingsSheet.kt:800+` |
| Android `MenuButton` | existing helper | `SettingsSheet.kt` |
| Android haptic feedback | `view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)` | used throughout SettingsSheet.kt |
| Portal toast | `toast()` / `toastSuccess()` from `core-utils.js` | per CLAUDE.md memory |
| Tailscale hostname source | `BLACKBOX_TAILNET_HOSTNAME` env var, exposed via pairing | `config.py:341`, `pairing_routes.py:296-297` |
| Portal pairing origin fetch | `ui-setup.js:818` `pairing.default_origin` | existing |
| Clipboard API (Portal) | `navigator.clipboard.writeText()` | browser-native |
| Clipboard API (Android) | `ClipboardManager.setPrimaryClip()` | Android-native |

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Cross-session state**: Brandon may want collapsed state to persist across browser/app restarts. | v1: `rememberSaveable` (Android) and in-memory only (Portal). If Brandon wants persistence after smoke test, add localStorage/DataStore in a follow-up. Don't add now. |
| **Default expanded state**: confusing first impression if everything's collapsed. | Default to **collapsed** for the 5 Android + 4 Portal collapsibles per Brandon's "hide this behind a dropdown button" framing. User taps each to open. Smoke test will reveal if this is annoying. |
| **Existing-section visual drift**: changing the menu-grid to single-column might break Voice Preferences / Updates which also use `.menu-grid`. | Use new `.menu-grid-vertical` class scoped only to the new collapsibles. Don't touch the existing `.menu-grid` rule. |
| **Animation performance**: Compose `AnimatedVisibility` can be janky on heavy content. | Keep animations short (200ms). If smoke test shows jank, swap to instant expand/collapse with no animation. |
| **Clipboard permission**: `navigator.clipboard.writeText()` requires HTTPS or localhost. | Portal runs on Tailscale (HTTPS) or localhost (always allowed). Should work. If not, fallback to `document.execCommand('copy')` with a hidden textarea. |
| **Android Paired Server unset**: when `origin` is blank, the copy button shouldn't render. | Guard: only render `IconButton` when `origin.isNotBlank()`. |
| **Refactor scale on Android**: wrapping 5 sections is substantial markup churn in SettingsSheet.kt (1027-line file). | Track 1 is self-contained — refactors are mechanical (each section becomes `CollapsibleSection { existing-content }`). Diff will be large but each section change is local. |

---

## Out of scope (defer)

- Cross-session collapse-state persistence (localStorage / DataStore)
- Generic "collapse all" / "expand all" buttons
- Drag-to-reorder sections
- Animation tuning beyond defaults
- Voice Preferences / Updates / System Controls visual changes (Brandon: leave alone)
- Provider/Model/Operator section changes (Brandon: leave alone)
- Cleanup of unused `.advanced-section` CSS rule once `.btn-section-toggle` generalizes it — defer to a future cleanup commit

---

## Verification commands (full suite, run after Track 5)

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc

# Android compiles
cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal"
./gradlew :app:compileDebugKotlin 2>&1 | tail -3
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc

# Android: CollapsibleSection helper exists
grep -c 'fun CollapsibleSection' "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsSheet.kt"
# Should be 1

# Android: 5 sections wrapped
for title in '"🎨 Generation"' '"📱 Floating Overlay"' '"🚀 Tools"' '"🚀 Running Apps"' '"📧 Gmail"'; do
    grep -q "CollapsibleSection.*$title\|CollapsibleSection(.*title.*$title" "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsSheet.kt" && echo "✓ $title" || echo "✗ MISSING $title (check actual emoji bytes)"
done

# Android: copy button on Paired Server (look for ContentCopy import + ClipboardManager usage)
grep -q 'ContentCopy' "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsSheet.kt" && echo "✓ ContentCopy import"
grep -q 'ClipboardManager' "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsSheet.kt" && echo "✓ ClipboardManager"

# Portal: 4 new toggle buttons
for id in btnToggleGeneration btnToggleTools btnToggleApps btnToggleReasoning; do
    grep -q "id=\"$id\"" Portal/index.html && echo "✓ $id" || echo "ℹ (id naming may differ — verify) $id"
done

# Portal: Tailscale row present
grep -q 'tailscaleAddressValue\|btnCopyTailscale' Portal/index.html && echo "✓ Tailscale display"

# Portal: cache-buster bumped
grep -oP 'v=genui\K\d+' Portal/index.html | sort -u
# Should output 269 (or whatever Track 5 picked)

# JS modules syntax
node --check Portal/modules/app-init.js && echo "✓ app-init"

# Push
git push origin main
git log origin/main..HEAD  # should be empty
```

---

## Commit map

| # | Track | Commit message | Files | Smoke test |
|---|---|---|---|---|
| 1 | Track 1 | `feat(android): collapsible Generation/Overlay/Tools/Apps/Gmail sections` | SettingsSheet.kt | All 5 sections collapse/expand cleanly; Gen + Tools show one button per line when expanded |
| 2 | Track 2 | `feat(android): copy button on Paired Server URL display` | SettingsSheet.kt | Tap copy → paste elsewhere → confirms URL on clipboard; haptic + Toast fire |
| 3 | Track 3 | `refactor(portal): collapse Gen/Tools/Apps/Reasoning sections behind toggle headers` | index.html, app-init.js, CSS | All 4 sections collapse/expand; Gen + Tools single-column when expanded; existing Voice Pref / Updates / System unchanged |
| 4 | Track 4 | `feat(portal): Tailscale paired-server address display + copy button` | index.html, JS, CSS | Address renders at top of System Controls; copy button copies to clipboard with toast |
| 5 | Track 5 | `chore(portal): cache-buster bump + finalize hamburger polish` | index.html | All previous tracks visible after hard-refresh; nothing regressed |

**Push to origin/main after Track 5 verification.**
