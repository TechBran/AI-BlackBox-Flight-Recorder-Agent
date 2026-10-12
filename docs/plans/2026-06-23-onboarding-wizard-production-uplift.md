# Onboarding Wizard Production-Quality Uplift — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn the linear-only onboarding wizard into a hub-first, status-driven console — random-access navigation to any section, system state surfaced at a glance, production-grade look — without removing any existing functionality.

**Architecture:** Two surfaces over the existing shell: the untouched linear first-run flow, plus a new `renderHub()` console (`?mode=manage`, the post-completion home) that reuses the working `?step=` revisit plumbing and the `done.js` summary model. A new backend `/onboarding/status` rollup (+ SSE `/onboarding/status/stream` for live re-validation) canonicalizes per-section status server-side; the frontend stays presentational. Built directly on `main` across 6 shippable milestones; every commit leaves the app launchable.

**Tech Stack:** FastAPI (Python) backend with pytest TDD; vanilla ES-module frontend (no bundler/framework) verified via Claude-in-Chrome; CSS custom-property design tokens.

---

**Design doc:** `docs/plans/2026-06-23-onboarding-wizard-production-uplift-design.md`
**Investigation:** `docs/onboarding/2026-06-22-wizard-production-uplift-investigation.md`

**Global discipline (read before any task):**
- Build directly on `main`. Commit frequently. Every committed step must leave the app launchable (prod serves the wizard from this working tree; JS/CSS edits are live on hard-refresh, no restart needed).
- Restart `sudo systemctl restart blackbox.service` ONLY at the SAFE RESTART POINTs each milestone names — never mid-broken-state.
- Backend tasks are pytest-TDD (`Orchestrator/venv/bin/pytest` from repo root). Frontend tasks are verified via Claude-in-Chrome (navigate the live wizard, screenshot, assert a concrete condition) — requires the extension connected.
- Reference: @superpowers:test-driven-development (backend), @superpowers:executing-plans (task-by-task execution), @superpowers:verification-before-completion (before any "PASS" claim).

**Canonical section order (used identically by M1 backend, M4 status.js, the left-rail; = ALL_STEPS minus welcome/done):**
`tailscale, api_keys, embeddings, optional_integrations, transcription, web_search, image, pair_phone, cli_agents, operator`

**Milestone map (full sweep, directions A–H):**
| Milestone | Directions | Ships |
|---|---|---|
| M0 | H + D/E | Amber + `--ob-state-*`/`--ob-radius-*` tokens, normalize off-token CSS, correctness sweep |
| M1 | F | Backend `/onboarding/status` rollup + SSE stream (pytest TDD) |
| M2 | A | Clickable `done.js` summary + missing rows + global attention (validates M1) |
| M3 | B + D | The console hub: `renderHub()` + `.ob-section-card` + hub-first routing |
| M4 | C + G | Left-rail navigator + visual elevation |
| M5 | E + G | CSS colocation split + restyle finish |
| M6 | — | Verification matrix + dev snapshot |

> **Cross-milestone contracts (frozen):** M0 is the SOLE creator of `util.js` `cssEscape()`, `.ob-card-selected`, and all `--ob-*` tokens (others verify-only). Hub interface (M3 authoritative): `hub.js` exports `applySectionEvent(root, ev)`; DOM = `.ob-hub` / `#ob-hub-groups` / `#ob-hub-attention`; loaded via `await import('./hub.js')` so a hub failure can't white-screen first-run. `set_current()` stays unexposed.

---

---

## Milestone M0: Foundation & Correctness (tokens + normalize CSS + correctness sweep)

This milestone lays the design-system and correctness foundation every later milestone builds on, with **zero behavior loss**. It (1) adds the amber `--ob-attention` + `--ob-state-*` tokens and a minimal radius scale (`--ob-radius-*`) to `onboarding-tokens.css`, repointing `--ob-warning` to amber; (2) normalizes the post-line-1865 off-token CSS (`var(--accent, #4a9eff)` blue bleed → `--ob-accent`, raw `rgba` amber literals → `--ob-attention`); (3) de-dupes the doubled `@keyframes ob-spin`; (4) extracts `.ob-card-selected` (replacing the inline `SELECTED_STYLE` literal in `embeddings.js` + `transcription.js`) and a shared `Portal/onboarding/util.js` `cssEscape()`; and (5) runs the correctness sweep: wire the dead `renderAlreadyPaired()` into `pair_phone.render()`, derive sigil numbers + "Back to <x>" labels from the `STEPS`/`STEP_LABELS` arrays instead of hardcoded literals, scope the leaky module-level singletons per-render, fix tailscale's brittle container reconstruction, and tighten the Branch-C catch-all mislabel. M0 is almost entirely frontend — verified live in Chrome — plus two source-text guard tests in the established `test_onboarding_steps_parity.py` style. Use @superpowers:test-driven-development for the guard tests and @superpowers:verification-before-completion before every "PASS" claim.

> **SOLE-CREATOR OWNERSHIP (uplift Decision D3):** M0 is the *sole creator* of (a) `Portal/onboarding/util.js` `cssEscape()` (Task 6), (b) the `.ob-card-selected` rule using the **combined selector** `.ob-cli-agent-card.ob-card-selected, .ob-section-card.ob-card-selected` (Task 5), and (c) **all** new tokens — `--ob-attention`, `--ob-attention-soft`, `--ob-state-*`, the `--ob-warning`→amber repoint, and the `--ob-radius-*` scale (Task 1). Later milestones (M2/M3/M5) are **import-only / verify-only** for these — they MUST NOT re-add or redefine them; they grep for M0's version and import it.

> **RADIUS TOKEN NAMING (uplift Decision D1):** the radius scale is named `--ob-radius-{sm,md,lg,full}` (NOT `--radius-*`). M0 defines `--ob-radius-*` and does NOT define `--radius-*`. The existing inline `var(--radius-md, 8px)` refs already sprinkled through `onboarding.css` are left untouched in M0 and keep resolving to their **literal fallbacks** (`8px`/`6px`/`12px` — identical values, so no visual change); M5 owns rewriting those inline refs to `var(--ob-radius-*)`.

**Pre-flight (every task assumes):** working tree on `main`, blackbox.service running (prod serves the wizard from the working tree, so frontend edits are live on hard-refresh — NO restart needed for JS/CSS). Pytest is run as `Orchestrator/venv/bin/pytest` from repo root `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc`. Chrome verification uses `http://localhost:9091/onboarding/?step=<name>` (revisit mode bypasses the completed→/ui redirect, so any step is reachable regardless of onboarding state). Before any Chrome task, ensure the claude-in-chrome tools are loaded (one `ToolSearch` batch) and the extension is connected.

---

### Task 1: Add amber + state tokens + `--ob-radius-*` scale to `onboarding-tokens.css`

**Files:** `Portal/onboarding/onboarding-tokens.css:107-117` (accent/state block), `:140-146` (layout constraints block — append radius after).

> **D3:** M0 is the SOLE creator of `--ob-attention`, `--ob-attention-soft`, `--ob-attention-glow`, the `--ob-state-*` quartet, the `--ob-warning`→amber repoint, and the `--ob-radius-*` scale. M2/M3/M5 verify-only (grep these exist), never redefine.

The wizard's `index.html` loads ONLY `onboarding-tokens.css` + `onboarding.css` — it does NOT import `Portal/styles/_variables.css` — so `--accent` is currently undefined here and resolves to its inline CSS fallback (`#4a9eff` blue), and `var(--radius-md, 8px)` resolves to its inline `8px` fallback. This task makes the new amber/state/`--ob-radius-*` tokens real. (Per D1, M0 does NOT define `--radius-*`; the inline `--radius-*` fallbacks keep their literal values until M5 migrates them to `--ob-radius-*`.)

1. In `onboarding-tokens.css`, replace the warning aliases (currently lines 114-115) and add the attention + state tokens. Change:
```css
    --ob-warning: var(--ob-accent);            /* red — single attention signal matching Portal's identity */
    --ob-warning-soft: var(--ob-accent-soft);  /* red — single attention signal matching Portal's identity */
```
to:
```css
    /* ── Attention (amber) — distinct status state, adopted 2026-06-23
     * (uplift design Decision #2). Reverses the earlier "collapse warning
     * into red" choice: the console hub needs amber to mean "configured-
     * but-wrong / required-unsatisfied" as a third signal alongside
     * green=ready and red=error. ── */
    --ob-attention: #f5a623;
    --ob-attention-soft: rgba(245, 166, 35, 0.12);
    --ob-attention-glow: 0 0 24px rgba(245, 166, 35, 0.20);
    --ob-warning: var(--ob-attention);            /* repointed red→amber: warning IS the attention signal */
    --ob-warning-soft: var(--ob-attention-soft);

    /* ── Canonical status-state tokens (uplift). The .ob-section-card and
     * every status pip read these four — one vocabulary, not the 4-5
     * competing ad-hoc rgba sets scattered through the late-era CSS. ── */
    --ob-state-ready: var(--ob-success);
    --ob-state-attention: var(--ob-attention);
    --ob-state-optional: var(--ob-text-tertiary);
    --ob-state-checking: var(--ob-text-tertiary);
```

2. After the `--ob-header-height: 72px;` line (line 145) and before the closing `}` of `:root` (line 146), add a minimal radius scale named with the `--ob-` prefix (per D1). Values mirror `Portal/styles/_variables.css:61-66` so they are identical to the literal fallbacks the inline `var(--radius-md, 8px)` refs currently use (M5 will rewrite those inline refs to `var(--ob-radius-md)` — until then they keep their identical-valued literal fallback, so no visual change at the M0/M5 boundary):
```css

    /* ── Radius scale (--ob-radius-*) ── the wizard's index.html does NOT
     * load Portal's _variables.css. M0 defines the OWN-NAMESPACE radius
     * scale (uplift Decision D1: --ob-radius-*, NOT --radius-*). Values
     * match Portal/styles/_variables.css. The existing inline
     * `var(--radius-md, 8px)` refs in onboarding.css are NOT migrated here —
     * they keep their identical-valued literal fallback until M5 rewrites
     * them to var(--ob-radius-md). ── */
    --ob-radius-sm: 6px;
    --ob-radius-md: 8px;
    --ob-radius-lg: 12px;
    --ob-radius-full: 9999px;
```

3. Verify the file parses (no syntax tool, so grep-confirm the tokens landed):
```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && grep -c "ob-attention\|ob-state-ready\|ob-state-attention\|ob-state-optional\|ob-state-checking\|--ob-radius-md" Portal/onboarding/onboarding-tokens.css
```
Expected output: `9` (or higher — `--ob-attention` appears in 3 lines + 1 warning repoint, `--ob-state-*` 4 lines, `--ob-radius-md` 1 line). Also confirm M0 did NOT define a `--radius-*` token (those stay as inline fallbacks until M5):
```bash
grep -n "^\s*--radius-" Portal/onboarding/onboarding-tokens.css
```
Expected output: (empty — no `--radius-*` definitions; only `--ob-radius-*` exist).

4. Commit:
```bash
git add Portal/onboarding/onboarding-tokens.css && git commit -m "$(cat <<'EOF'
feat(onboarding): add amber --ob-attention + --ob-state-* tokens + --ob-radius-* scale

Adopts amber officially as a distinct status state (uplift Decision #2),
repoints --ob-warning red→amber, and defines the OWN-NAMESPACE radius scale
--ob-radius-* (Decision D1: NOT --radius-*) the wizard's index.html was
missing (it never loaded Portal/styles/_variables.css). Existing inline
var(--radius-md, 8px) refs keep their identical-valued literal fallback
until M5 migrates them to var(--ob-radius-md).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

App remains launchable (additive CSS tokens only).

---

### Task 2: Normalize off-token `--accent`→blue bleed + raw amber rgba in `onboarding.css`

**Files:** `Portal/onboarding/onboarding.css` — the `var(--accent, #4a9eff)` occurrences at lines 1871, 1893, 1918, 1942, 2011, 2306, 2366; the raw amber `rgba(255, 180, 50, …)` literals in the `.ob-banner-warn`, `.ob-auth-hint`, `.ob-cli-agent-*` blocks (lines ~1886-1887, 1905-1906, 2410, 2449, 2512-2517).

`--accent` is NOT defined in the wizard's loaded stylesheets, so every `var(--accent, #4a9eff)` renders **blue** (`#4a9eff`) — a visible identity break against the wizard's red. Repoint all of them to `--ob-accent`, and collapse the ad-hoc amber `rgba(255,180,50,…)` literals onto `--ob-attention` (now that it exists).

> Note (D1): this task does NOT touch the inline `var(--radius-*, …)` refs — those stay as-is in M0 (they keep their literal fallback) and are M5's migration. M0 only repoints `--accent` and the banner/hint amber.

1. Repoint all blue-bleed accent refs in one sweep (these are the `var(--accent, #4a9eff)` form — replace with the wizard token):
```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && sed -i 's/var(--accent, #4a9eff)/var(--ob-accent)/g' Portal/onboarding/onboarding.css
```

2. Verify zero blue-bleed refs remain:
```bash
grep -n "var(--accent, #4a9eff)\|#4a9eff" Portal/onboarding/onboarding.css
```
Expected output: (empty — no matches).

3. Normalize the raw amber literals used for *attention* semantics (banners/hints) to the token. Use Edit for each block so we don't over-replace decorative amber. For `.ob-auth-hint` (lines 1886-1887):
```css
    background: rgba(255, 180, 50, 0.06);
    border-left: 3px solid rgba(255, 180, 50, 0.5);
```
→
```css
    background: var(--ob-attention-soft);
    border-left: 3px solid var(--ob-attention);
```
For `.ob-banner-warn` (lines 1905-1906):
```css
    background: rgba(255, 180, 50, 0.08);
    border-left: 3px solid rgba(255, 180, 50, 0.6);
```
→
```css
    background: var(--ob-attention-soft);
    border-left: 3px solid var(--ob-attention);
```

> ASSUMPTION: the `.ob-cli-agent-card[data-state="needs-auth"]` / badge / action-auth amber rgba (lines 2410, 2449, 2512-2517) are left AS-IS in M0 — they are the agent-card "needs-auth" treatment that M5's restyle pass owns, and changing the action-button amber here risks contrast regressions on the existing buttons. M0 only normalizes the *banner/hint attention* literals. Note this in the commit so M5 knows the card amber is intentionally deferred.

4. Verify the banner/hint blocks now reference the token:
```bash
grep -n "ob-attention" Portal/onboarding/onboarding.css
```
Expected output: 4 lines (2 in `.ob-auth-hint`, 2 in `.ob-banner-warn`).

5. Commit:
```bash
git add Portal/onboarding/onboarding.css && git commit -m "$(cat <<'EOF'
fix(onboarding): kill --accent blue bleed + normalize attention amber onto tokens

7 var(--accent,#4a9eff) refs were rendering BLUE (wizard never defines
--accent) — repointed to --ob-accent. Banner/hint amber rgba literals
collapsed onto --ob-attention. Agent-card needs-auth amber deferred to M5
restyle (contrast-sensitive). Inline var(--radius-*) refs untouched (M5).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

---

### Task 3: Chrome-verify the blue bleed is gone (tailscale Branch-A banners)

**Files:** none (verification only).

The clearest surface for the old blue bleed is the tailscale step's MagicDNS/HTTPS banners (`.ob-banner-link-primary`, line 1942) and auth links. Verify they now render red/amber, not blue.

1. Load Chrome tools (one batch): `ToolSearch` query `select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__javascript_tool`.
2. Navigate to `http://localhost:9091/onboarding/?step=tailscale`.
3. Hard-refresh (the CSS may be cached). Take a screenshot of the rendered step.
4. Use `javascript_tool` to assert the resolved accent token is red, not blue:
```js
getComputedStyle(document.documentElement).getPropertyValue('--ob-accent').trim()
```
Expected: `#cc0000` (NOT `#4a9eff` / blue).
5. Assert no element resolves a blue link color — query any `.ob-banner-link-primary` or `.ob-auth-link` if present; if the step is in Branch A with a banner, confirm `getComputedStyle(el).backgroundColor` is `rgb(204, 0, 0)` not `rgb(74, 158, 255)`. If no banner is on screen (Branch A with magicdns already enabled), this is satisfied by the token assertion above.
6. Commit (verification-only marker — no code change, so amend-free empty doc commit is unnecessary; instead capture the screenshot evidence in the task notes and proceed). **No commit for this task** — it gates Tasks 1-2. If the assertion FAILS, return to Task 2.

---

### Task 4: De-dupe the doubled `@keyframes ob-spin`

**Files:** `Portal/onboarding/onboarding.css:1978` (first def), `:2557-2561` (second def).

There are two identical-purpose `@keyframes ob-spin` blocks (line 1978 one-liner, lines 2558-2561 expanded). Keep one, delete the other.

1. Delete the redundant one-liner at line 1978 (the expanded version at 2558 with its explanatory comment is the keeper). Edit removes:
```css
@keyframes ob-spin { to { transform: rotate(360deg); } }
```
(the line 1978 occurrence — match it with its surrounding `.ob-banner-spinner-hint` block context to keep it unique, since the string is otherwise unique at line 1978).

2. Verify exactly one `@keyframes ob-spin` remains:
```bash
grep -c "@keyframes ob-spin" Portal/onboarding/onboarding.css
```
Expected output: `1`.

3. Chrome-verify the spinner still animates: navigate `http://localhost:9091/onboarding/?step=embeddings` (the validating pill / job panel uses spin) OR `?step=tailscale` (`.ob-banner-spinner`), screenshot, confirm no console error and the `@keyframes ob-spin` rule is still applied:
```js
[...document.styleSheets].flatMap(s=>{try{return[...s.cssRules]}catch{return[]}}).filter(r=>r.name==='ob-spin').length
```
Expected: `1`.

4. Commit:
```bash
git add Portal/onboarding/onboarding.css && git commit -m "$(cat <<'EOF'
refactor(onboarding): de-dupe doubled @keyframes ob-spin

Two identical ob-spin keyframe blocks existed (L1978 + L2558); kept the
documented expanded form.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

---

### Task 5: Extract `.ob-card-selected`, replacing the inline `SELECTED_STYLE` literal

**Files:** `Portal/onboarding/onboarding.css` (append a new rule after the `.ob-cli-agent-card[data-state=...]` block at line 2411), `Portal/onboarding/steps/embeddings.js:43-46` + `:335`, `Portal/onboarding/steps/transcription.js:53-56` + `:193`.

> **D3:** M0 is the SOLE creator of the `.ob-card-selected` rule, and it uses the **combined selector** `.ob-cli-agent-card.ob-card-selected, .ob-section-card.ob-card-selected` so the same accent-ring treatment applies to BOTH the existing radio-cards AND the M3 hub section-cards. M3/M5 verify-only (grep this rule exists), never redefine it.

The identical `SELECTED_STYLE` inline literal is duplicated in `embeddings.js` and `transcription.js` and uses raw `rgba(204,0,0,…)` accent literals. Replace with a `.ob-card-selected` class.

1. Add the CSS rule after line 2411 (after the `[data-state="ready"]` line). Use the **combined selector** (D3) so the future M3 `.ob-section-card` inherits the same "you chose this" ring without a second rule:
```css

/* User-picked card highlight (radio-card selection). SOLE creator: M0
 * (uplift Decision D3) — M3/M5 import/verify only, never redefine. Replaces
 * the inline SELECTED_STYLE literal that was duplicated in embeddings.js +
 * transcription.js. Combined selector covers BOTH the radio-cards and the
 * M3 hub section-cards. data-state colors the LEFT border (capability
 * state); this accent ring means "you chose this one". */
.ob-cli-agent-card.ob-card-selected,
.ob-section-card.ob-card-selected {
    border-color: var(--ob-accent);
    box-shadow: 0 0 0 1px var(--ob-accent), var(--ob-accent-glow);
    background: var(--ob-accent-soft);
}
```

2. In `embeddings.js`, delete the `SELECTED_STYLE` constant (lines 43-46) and change the card render. At line 335 replace:
```js
    const styleAttr = isSelected ? ` style="${SELECTED_STYLE}"` : "";
```
with:
```js
    const selectedClass = isSelected ? " ob-card-selected" : "";
```
Then at line 338 change the card opening tag class + remove `${styleAttr}` at line 340:
```js
        <div class="ob-cli-agent-card${selectedClass}" data-slug="${escapeHtml(m.slug)}"
             data-state="${dataState}" role="radio"
             aria-checked="${isSelected ? "true" : "false"}" tabindex="0">
```

3. In `transcription.js`, delete the `SELECTED_STYLE` constant (lines 53-56) and change the card render. At line 193 replace:
```js
    const styleAttr = isSelected ? ` style="${SELECTED_STYLE}"` : "";
```
with:
```js
    const selectedClass = isSelected ? " ob-card-selected" : "";
```
Then at lines 196-198 change the class + drop `${styleAttr}`:
```js
        <div class="ob-cli-agent-card${selectedClass}" data-provider="${escapeHtml(id)}"
             data-state="${dataState}" role="radio"
             aria-checked="${isSelected ? "true" : "false"}" tabindex="0">
```

4. Verify no `SELECTED_STYLE` references remain:
```bash
grep -rn "SELECTED_STYLE" Portal/onboarding/steps/
```
Expected output: (empty).

5. Chrome-verify selection still highlights. Navigate `http://localhost:9091/onboarding/?step=transcription`, click a Ready provider card, screenshot, and assert:
```js
document.querySelectorAll('.ob-cli-agent-card.ob-card-selected').length
```
Expected: `1` after a click; the selected card shows the red ring. Repeat for `?step=embeddings` (click a non-active READY model card).

6. Commit:
```bash
git add Portal/onboarding/onboarding.css Portal/onboarding/steps/embeddings.js Portal/onboarding/steps/transcription.js && git commit -m "$(cat <<'EOF'
refactor(onboarding): extract .ob-card-selected, drop duplicated SELECTED_STYLE

The inline SELECTED_STYLE literal (raw rgba accent) was copy-pasted in
embeddings.js + transcription.js. One token-backed class now owns it, using
the combined .ob-cli-agent-card/.ob-section-card selector (uplift D3) so the
M3 hub section-cards inherit the same selection ring. M0 is the SOLE creator;
M3/M5 import/verify only.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

---

### Task 6: Create `Portal/onboarding/util.js` with shared `cssEscape()`

**Files:** new `Portal/onboarding/util.js`, `Portal/onboarding/steps/web_search.js:311-313`, `Portal/onboarding/steps/image.js` (its `cssEscape` def).

> **D3:** M0 is the SOLE creator of `Portal/onboarding/util.js` and its `cssEscape()` (the regex-replace form below). M2/M3/M5 import it (`import { cssEscape } from "../util.js"`) and verify-only (grep it exists) — they MUST NOT re-create util.js or redefine `cssEscape`.

`cssEscape()` is duplicated verbatim in `web_search.js` and `image.js` (the milestone says "3 copies" — only 2 currently exist in the tree; the shared module still de-dupes them and gives future steps + the hub a single import). Extract to `util.js`.

1. Create `Portal/onboarding/util.js`:
```js
// Shared onboarding-wizard utilities. Keep this dependency-free and
// side-effect-free (pure functions only) so any step module — or the
// future hub/status modules — can import it without ordering concerns.
//
// SOLE creator: M0 (uplift Decision D3). M2/M3/M5 import + verify only; they
// must NOT re-create this file or redefine cssEscape.

// Escape an arbitrary string for safe interpolation into a CSS attribute
// selector (e.g. `[data-slug="${cssEscape(slug)}"]`). Backslash-escapes
// every char outside [A-Za-z0-9_-]. Sufficient for the wizard's slug/id
// values; not a full CSS.escape polyfill (we don't need leading-digit or
// control-char handling here).
export function cssEscape(s) {
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}
```

2. In `web_search.js`, delete the local `function cssEscape(...)` (lines 311-313) and add the import at the top of the file (after the leading comment block, before the first `const`):
```js
import { cssEscape } from "../util.js";
```

3. In `image.js`, do the same — delete its local `cssEscape` definition and add `import { cssEscape } from "../util.js";` at the top.

4. Verify exactly one definition remains and both steps import it:
```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && echo "defs:"; grep -rc "function cssEscape" Portal/onboarding/util.js Portal/onboarding/steps/web_search.js Portal/onboarding/steps/image.js; echo "imports:"; grep -rn 'import { cssEscape } from "../util.js"' Portal/onboarding/steps/
```
Expected: `util.js:1`, `web_search.js:0`, `image.js:0`; two import lines.

5. Chrome-verify the importing steps still render (a broken import path would blank the step). Navigate `http://localhost:9091/onboarding/?step=web_search` and `?step=image`, screenshot each, and assert no console error and the card grid rendered:
```js
document.querySelectorAll('.ob-cli-agent-card').length > 0
```
Expected: `true` for both.

6. Commit:
```bash
git add Portal/onboarding/util.js Portal/onboarding/steps/web_search.js Portal/onboarding/steps/image.js && git commit -m "$(cat <<'EOF'
refactor(onboarding): shared Portal/onboarding/util.js cssEscape()

De-dupes the cssEscape copies in web_search.js + image.js into one
pure-function module the future hub/status modules can also import. M0 is the
SOLE creator (uplift D3); M2/M3/M5 import + verify only.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

---

### Task 7 — SAFE RESTART POINT: hard-refresh sweep of CSS-touching steps

**Files:** none (verification + restart).

Tasks 1-6 touched tokens, the monolith CSS, and two step modules' import graph. Before the correctness-sweep tasks, confirm the whole CSS/import foundation is healthy on a clean service boot.

1. Restart the service (pre-authorized per project memory — don't ask): `sudo systemctl restart blackbox.service`.
2. Wait for warm-up (60-90s; index rebuild). Poll readiness:
```bash
until curl -sf http://localhost:9091/health >/dev/null 2>&1; do sleep 3; done; echo "up"
```
Expected: `up`.
3. Chrome: navigate each touched step in turn and confirm no console errors + the step body renders: `?step=tailscale`, `?step=embeddings`, `?step=transcription`, `?step=web_search`, `?step=image`. For each, screenshot and assert `document.querySelector('.ob-step') !== null`.
4. No commit (verification gate). If any step is blank/errored, fix before proceeding.

---

### Task 8: Wire the dead `renderAlreadyPaired()` into `pair_phone.render()`

**Files:** `Portal/onboarding/steps/pair_phone.js:30-116` (render entry + mintAndPoll call), `:239-287` (the existing dead `renderAlreadyPaired`).

`renderAlreadyPaired()` is defined but **never called** — so revisiting the pair step always shows a fresh QR as if unpaired, even when devices exist. The file's own header (lines 4-7) documents the intended behavior: GET `/onboarding/current-config`, and if `paired_devices` exists, render the already-paired view instead of minting. Wire it.

1. In `render()`, after `stopPolling();` (line 32) and BEFORE setting `container.innerHTML`, the structure stays — but the gating happens AFTER the static shell is rendered and handlers wired, replacing the final `await mintAndPoll(...)` (line 115). Change the tail of `render()` (line 114-116) from:
```js
    // Mint a fresh pairing token + start the QR + poll cycle.
    await mintAndPoll(container, { next });
}
```
to:
```js
    // E13: if devices are already paired (persistent registry), show the
    // already-paired success view instead of a fresh QR — the phone won't
    // re-claim a new token once it holds stored credentials, so polling
    // alone would spin forever on revisit. Best-effort: any failure falls
    // through to the normal mint/poll flow.
    const stage = container.querySelector("#ob-pair-stage");
    let pairedDevices = [];
    try {
        const r = await fetch("/onboarding/current-config");
        if (r.ok) {
            const cfg = await r.json();
            pairedDevices = (cfg && cfg.paired_devices) || [];
        }
    } catch (_) { /* non-fatal — fall through to mint */ }

    if (pairedDevices.length > 0) {
        renderAlreadyPaired(stage, pairedDevices, {
            next,
            pairAnother: () => mintAndPoll(container, { next }),
        });
        return;
    }

    // No devices on record — mint a fresh pairing token + start QR/poll cycle.
    await mintAndPoll(container, { next });
}
```
(`renderAlreadyPaired` already takes `(stage, devices, { next, pairAnother })` per its signature at line 239 — `pairAnother` re-enters the mint flow, exactly matching its "Pair another device" button at line 286.)

2. Verify the function is now referenced:
```bash
grep -n "renderAlreadyPaired" Portal/onboarding/steps/pair_phone.js
```
Expected: 2 matches (the definition at ~239 AND the new call site).

3. Chrome-verify both branches:
   - **Paired branch:** This requires `current-config` to report `paired_devices`. Probe the live state first:
     ```bash
     curl -s http://localhost:9091/onboarding/current-config | python3 -c "import sys,json;d=json.load(sys.stdin);print('paired:',len(d.get('paired_devices',[])))"
     ```
     If `paired: 0`, simulate the paired view in Chrome WITHOUT mutating real state by stubbing the fetch before navigation: open `?step=pair_phone`, then in `javascript_tool` override `window.fetch` to return a fake `paired_devices` for `/onboarding/current-config` and call the module's render again is not directly reachable — instead assert the wiring by confirming that when `paired_devices` is non-empty the already-paired DOM appears. Concretely, navigate `?step=pair_phone`, and in the console run a fetch-mock + re-import:
     ```js
     const origFetch = window.fetch;
     window.fetch = (u, o) => u.includes('/onboarding/current-config')
       ? Promise.resolve(new Response(JSON.stringify({paired_devices:[{device_name:'Pixel 8',device_kind:'phone',claimed_at:1}]}),{status:200,headers:{'Content-Type':'application/json'}}))
       : origFetch(u, o);
     const m = await import('/onboarding/steps/pair_phone.js?cachebust=' + Date.now());
     const c = document.getElementById('ob-step-container');
     await m.render(c, { next(){}, back(){}, skip(){} });
     document.querySelector('.ob-pair-claimed-title')?.textContent
     ```
     Expected: contains `Paired with Pixel 8`. Restore `window.fetch = origFetch;` afterward. Screenshot the already-paired view.
   - **Unpaired branch:** restore fetch, re-navigate `?step=pair_phone` fresh; with `paired: 0` it should mint a QR. Assert `document.querySelector('.ob-qr-img') !== null`. Screenshot.

4. Commit:
```bash
git add Portal/onboarding/steps/pair_phone.js && git commit -m "$(cat <<'EOF'
fix(onboarding): wire dead renderAlreadyPaired() into pair_phone revisit

Revisiting the pair step always showed a fresh QR even with devices already
paired — the phone won't re-claim a new token once it holds credentials, so
polling spun forever. Now GET /onboarding/current-config and render the
already-paired view when paired_devices exists (its "Pair another" button
falls through to the normal mint/poll flow).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

---

### Task 9: Derive sigil numbers + "Back to <x>" labels from `STEPS`/`STEP_LABELS`

**Files:** `Portal/onboarding/onboarding.js:150-172` (`renderStep` — the `mod.render` call site at 156), each step module's hardcoded `<em>NN</em>` sigil + "Back to <x>" literal (cli_agents:78/108, api_keys:94/122, embeddings:82/109, transcription:66/94, web_search:45/77, image:43/74, pair_phone:37/73, optional_integrations:118/147, operator:60/89, tailscale:38/671, done:17/53, welcome:16).

Hardcoded sigils are badly drifted: `STEPS` order makes embeddings=4 but optional_integrations is also "04"; transcription AND pair_phone both show "05"; web_search AND cli_agents both show "06"; image AND operator both show "07"; done shows "08" with denominator `/08` while `STEPS` has 12 entries. Derive these from the source of truth and pass them into each step's `render()`.

> **D6 (render() ARITY CHANGE — grep ALL callers):** widening every step `render()` to accept a derived `sigil` is an arity change. Per project memory ("grep ALL callers on any arity change"), EVERY internal `render(` caller must be updated to thread `sigil` — *including the self re-render paths inside `tailscale.js`* (the `guardedRecheck` closure at line 84 and the `wireRecheckBtn` re-render at line 349; see step 3 below). A self re-render that drops `sigil` would fall back to the literal — and the source-text guard test (step 4) is **scoped** so it does NOT flag those derived re-render paths, but they must still pass the real sigil so the live re-render shows the correct number.

1. In `onboarding.js`, add a pure helper after the `STEP_LABELS` map (after line 28). It computes the per-step sigil context from `STEPS`/`STEP_LABELS`:
```js
// Sigil + nav context derived from STEPS order — single source of truth.
// Kills the drifted hardcoded "<em>05</em>" literals (two steps showed 05,
// two showed 06, etc.) and the stale "/08" denominator. Returns the 1-based
// 2-digit sigil number, the total, the step's own label, and the previous
// step's label for the "Back to <x>" affordance.
export function stepSigilContext(stepName) {
    const idx = STEPS.indexOf(stepName);
    const num = idx >= 0 ? String(idx + 1).padStart(2, "0") : "00";
    const total = String(STEPS.length).padStart(2, "0");
    const prev = idx > 0 ? STEPS[idx - 1] : null;
    const backLabel = prev ? (STEP_LABELS[prev] || prev) : null;
    return { num, total, label: STEP_LABELS[stepName] || stepName, backLabel };
}
```

2. Pass it into every step via the existing `mod.render` call. In `renderStep()` (line 156) change:
```js
        const mod = await import(`./steps/${stepName}.js`);
        await mod.render(container, { state, next, back, skip, mode: MODE });
```
to:
```js
        const mod = await import(`./steps/${stepName}.js`);
        const sigil = stepSigilContext(stepName);
        await mod.render(container, { state, next, back, skip, mode: MODE, sigil });
```

3. In EACH step module, consume `sigil` instead of the hardcoded literals. Pattern (apply per file — example for `transcription.js`): change the destructure at its `render` signature (line 62) from `{ next, back, skip }` to `{ next, back, skip, sigil }`, then in the template replace the sigil num + back label. For `transcription.js`:
   - Line 66 `<em>05</em>` → `<em>${sigil ? sigil.num : "05"}</em>`
   - Line 68 `MEMORY`/label stays as the step's display label (these are deliberately custom-cased per `STEP_LABELS`; keep the existing `SPEECH`).
   - Line 94 `Back to extras` → `Back to ${sigil && sigil.backLabel ? sigil.backLabel.toLowerCase() : "extras"}`
   
   Apply the same two substitutions (sigil num + back label) to: `cli_agents.js` (78/108), `api_keys.js` (94/122), `embeddings.js` (82/109), `web_search.js` (45/77), `image.js` (43/74), `pair_phone.js` (37/73), `optional_integrations.js` (118/147), `operator.js` (60/89), `done.js` (17/53). `welcome.js` (16) has no back label (first step) — substitute only its sigil num at line 16 (`<em>01</em>` → `<em>${sigil ? sigil.num : "01"}</em>`); leave its four `ob-feature-num` `<em>01..04</em>` pillar cards (lines 36/48/60/72) ALONE — those are decorative feature numerals, NOT a step sigil.

   **tailscale.js — D6 self re-render (DO NOT drop the sigil):** widen `render()`'s destructure (line 29) to include `sigil`, substitute line 38 `<em>02</em>` → `<em>${sigil ? sigil.num : "02"}</em>`, and CRUCIALLY thread `sigil` through BOTH internal self re-render paths so the re-render shows the correct number (not a hardcoded `"NN"`/`"02"` fallback):
   - The `guardedRecheck` closure (line 84): change `await render(container, { next, back, skip });` → `await render(container, { next, back, skip, sigil });`
   - `wireRecheckBtn` re-render (line 349, threaded via `rs.container` after Task 12): the re-render must also pass `sigil`. Capture `sigil` into the per-render state in Task 12 (`rs.sigil = sigil`) and call `await render(rs.container, { ...navCallbacks, sigil: rs.sigil });` — i.e. pass `sigil` through `navCallbacks` so the re-render keeps the derived number. (Task 12 owns the `rs` plumbing; this step only requires that `sigil` survive the self re-render — never a hardcoded `"NN"`.)

   tailscale's back-button text is built in `renderStepNav` at line 671 — pass `sigil.backLabel` through (or substitute inline there) the same way the other steps do.

   > ASSUMPTION: each step's `render(container, { ... })` destructure is widened to include `sigil`, and the fallback `sigil ? sigil.num : "<old>"` keeps the step renderable if ever mounted without context (e.g. future hub mount). The custom-cased `STEP_LABELS` (e.g. "MEMORY & SEARCH") are preserved as the sigil *label* source so casing intent survives. Per D6, the self re-render paths in tailscale MUST forward the real `sigil` — a re-render is never allowed to silently fall back to the literal.

4. Add a source-text guard test (matches the established `test_onboarding_steps_parity.py` regex-over-JS style) asserting NO step module hardcodes a `<em>NN</em>` **sigil** literal. The regex is **scoped to the `ob-step-sigil-num"><em>NN` shape** so it does NOT false-positive welcome.js's four `ob-feature-num` `<em>01..04</em>` decorative pillar cards (D6). Append to `Orchestrator/tests/test_onboarding_steps_parity.py`:
```python
def test_no_step_hardcodes_sigil_number():
    """Sigil numbers must derive from STEPS order (stepSigilContext), never a
    hardcoded literal — two steps previously both showed '05' and the denom
    was a stale '/08'. Guard: no steps/<x>.js contains a hardcoded sigil digit
    literal in the `<div class="ob-step-sigil-num"><em>NN</em>` shape (the
    dynamic form is `...><em>${sigil ... }</em>`). The pattern is SCOPED to the
    ob-step-sigil-num wrapper so it does NOT flag welcome.js's decorative
    `ob-feature-num` <em>01..04</em> pillar cards, and does NOT flag the
    derived re-render paths in tailscale.js (which carry `${sigil...}`)."""
    offenders = []
    for js in sorted(STEPS_DIR.glob("*.js")):
        src = js.read_text(encoding="utf-8")
        # Scoped: only the step-sigil wrapper followed immediately by <em>NN.
        if re.search(r'ob-step-sigil-num"><em>\s*\d{2}', src):
            offenders.append(js.name)
    assert not offenders, (
        "These step modules still hardcode a <em>NN</em> sigil number (in the "
        "ob-step-sigil-num wrapper) instead of deriving it from STEPS via "
        f"stepSigilContext: {offenders}"
    )
```

5. Run the guard test — confirm it FAILS first if you run it before completing step 3, then PASSES after:
```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_steps_parity.py -q
```
Expected (after step 3 complete): all tests pass, e.g. `4 passed`.

6. Chrome-verify the live sigils are now correct + unique. Navigate the wizard linearly from `?step=welcome` (or hit each `?step=` directly) and assert the rendered sigil matches `STEPS` index. Spot-check the two former collisions:
```js
// pair_phone should now be 09, NOT 05
(await (async()=>{location.href='/onboarding/?step=pair_phone';})());
```
Practically: navigate `?step=pair_phone`, screenshot, assert `document.querySelector('.ob-step-sigil-num em').textContent.trim() === '09'`. Navigate `?step=transcription`, assert sigil `=== '06'`. Navigate `?step=done`, assert sigil `=== '12'` and that its denominator in the top progress chrome reads `/12` (from `updateProgress`, line 182 already uses `STEPS.length`). Then navigate `?step=tailscale` and click "Re-check" (if any banner) — confirm the re-rendered step STILL shows sigil `=== '02'` (proves the self re-render threaded `sigil`, not a literal fallback). Also confirm `?step=welcome` shows the step sigil `=== '01'` AND its four `ob-feature-num` pillar cards still read 01/02/03/04 unchanged.

7. Commit:
```bash
git add Portal/onboarding/onboarding.js Portal/onboarding/steps/*.js Orchestrator/tests/test_onboarding_steps_parity.py && git commit -m "$(cat <<'EOF'
fix(onboarding): derive sigil numbers + Back-to labels from STEPS order

Hardcoded sigils had drifted badly (two steps showed 05, two showed 06,
done showed 08 with a /08 denom vs 12 real steps). onboarding.js now derives
{num,total,label,backLabel} via stepSigilContext(stepName) and passes it into
every step render() (arity change — ALL callers updated, incl. tailscale's
two self re-render paths which thread the derived sigil, never a literal).
Source-text guard test forbids a hardcoded <em>NN</em> in the
ob-step-sigil-num wrapper, scoped so welcome.js's ob-feature-num pillar
cards (01..04) do NOT trip it.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

---

### Task 10: Scope leaky module-level singletons per-render — `pair_phone.js`

**Files:** `Portal/onboarding/steps/pair_phone.js:19-28` (`pollRef`, `activeToken`, `stopPolling`).

`pollRef` and `activeToken` are module-level globals. When the hub mounts steps non-linearly (M3), two mounts of the same module share these and race. Scope them to a per-render closure object while preserving the existing zombie-poller cleanup contract.

1. Replace the module-level declarations (lines 19-28) with a per-render state factory + a module-level "current render" pointer used only for cross-render cleanup:
```js
// Per-render poll state. Module-level singletons (pollRef/activeToken) leaked
// across non-linear mounts; scope them per render() instead. `_active` points
// at the most recent render's state so a NEW render can stop the OLD poller.
let _active = null;

function stopPolling(s) {
    const st = s || _active;
    if (st && st.pollRef !== null) {
        clearInterval(st.pollRef);
        st.pollRef = null;
    }
    if (st) st.activeToken = null;
}
```

2. In `render()` (line 30), create the per-render state and supersede the previous one. At the top of `render()` replace `stopPolling();` (line 32) with:
```js
    // Supersede any prior render's poller, then install fresh per-render state.
    stopPolling(_active);
    const rs = { pollRef: null, activeToken: null };
    _active = rs;
```
Then thread `rs` through `mintAndPoll`. Change every `stopPolling()` call inside `render()` (the back/skip/mark-done handlers, lines 87, 91, 102) to `stopPolling(rs)`, and change the `mintAndPoll(container, { next })` calls to `mintAndPoll(container, { next }, rs)`.

3. In `mintAndPoll(container, { next })` (line 118) widen the signature to `mintAndPoll(container, { next }, rs)`, and replace the three module-global touches:
   - line 139 `activeToken = token;` → `rs.activeToken = token;`
   - line 143 `pollRef = setInterval(...)` → `rs.pollRef = setInterval(...)`
   - line 145 guard `if (activeToken !== token) return;` → `if (rs.activeToken !== token) return;`
   - lines 151, 158, 162 `stopPolling();` → `stopPolling(rs);`
   - the `renderExpired(stage, () => mintAndPoll(container, { next }))` retry callbacks (lines 151, 162, 134) → `() => mintAndPoll(container, { next }, rs)`

   And the `renderAlreadyPaired` `pairAnother` from Task 8 → `pairAnother: () => mintAndPoll(container, { next }, rs)`.

4. Verify no bare module-global poll references remain:
```bash
grep -n "pollRef\|activeToken" Portal/onboarding/steps/pair_phone.js | grep -v "rs\.\|st\.\|_active\|st &&\|let _active\|function stopPolling"
```
Expected: (empty — every reference is now `rs.`/`st.`/`_active`).

5. Chrome-verify the QR poll cycle still works and re-render doesn't double-poll. Navigate `?step=pair_phone` (unpaired), confirm QR appears and the countdown ticks (screenshot showing `.ob-pair-countdown` text changing across two reads ~3s apart). Then trigger a re-render (click Back then re-navigate) and assert there is no console error and only one interval is active (no zombie — the countdown still ticks exactly once per second, not doubled).

6. Commit:
```bash
git add Portal/onboarding/steps/pair_phone.js && git commit -m "$(cat <<'EOF'
fix(onboarding): scope pair_phone poll singletons per-render

pollRef/activeToken were module globals that would race across non-linear
hub mounts. Now per-render state objects with a module pointer used only to
supersede the previous render's poller. Same zombie-cleanup contract.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

---

### Task 11: Scope `done.js` singletons (`busy`, `restartBusy`) per-render

**Files:** `Portal/onboarding/steps/done.js:11` (`busy`), `:310` (`restartBusy`), `:267-297` (`completeAndOpen`), `:370-443` (`doRestart`).

`busy` and `restartBusy` are module globals. If the done/summary view is ever mounted twice (hub revisit, M3), a stuck flag from a prior mount silently no-ops all future clicks (the file's own E9 comment at line 374 documents exactly this failure mode). Scope to a per-render object.

1. Replace the two module-level flags (lines 11 and 310) with a single per-render state object created in `render()`. At line 11 change `let busy = false;` to:
```js
// Per-render busy state — module globals would survive across remounts and
// silently dead-lock the buttons (see E9 note in doRestart). Reset each render.
let rs = { busy: false, restartBusy: false };
```
Delete `let restartBusy = false;` at line 310.

2. At the top of `render()` (line 13, first line of the function body) reset the state:
```js
    rs = { busy: false, restartBusy: false };
```

3. In `completeAndOpen` (line 267), replace `busy` references: `if (busy) return;` → `if (rs.busy) return;`, `busy = true;` → `rs.busy = true;`, the `finally { busy = false; }` → `rs.busy = false;`.

4. In `doRestart` (line 370), replace `restartBusy` references: `if (restartBusy) return;` → `if (rs.restartBusy) return;`, `restartBusy = true;` → `rs.restartBusy = true;`, `finally { restartBusy = false; }` → `rs.restartBusy = false;`.

5. Verify:
```bash
grep -n "\bbusy\b\|restartBusy" Portal/onboarding/steps/done.js | grep -v "rs\.\|rs = {\|keepAliveBusy\|restartBusy: false\|busy: false"
```
Expected: (empty — all guarded through `rs.`).

6. Chrome-verify the Restart button + Open Portal still function (without actually completing onboarding — DON'T click Open Portal, which writes the sentinel). Navigate `?step=done`, screenshot the summary + restart status row, assert `document.getElementById('ob-done-restart')` exists and is not stuck disabled, and assert no console error. (The summary rows are still the old `done.js` content — M2 reworks them; M0 only scopes state.)

7. Commit:
```bash
git add Portal/onboarding/steps/done.js && git commit -m "$(cat <<'EOF'
fix(onboarding): scope done.js busy/restartBusy per-render

Module-global busy flags would survive remounts and dead-lock Open Portal /
Restart on the second mount (the exact E9 failure mode the code comments).
Now reset per render() into a per-render state object.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

---

### Task 12: Scope tailscale singletons + fix brittle container reconstruction + Branch-C mislabel

**Files:** `Portal/onboarding/steps/tailscale.js:24-27` (`recheckBusy`, `_currentAuthAbort`, `_authInFlight`, `_branchAGen`), `:137,145,345` (`window.__ob_tailscale_serve_done`), `:348` (brittle container reconstruction), `:90-98` (Branch dispatch), `:454-455` (Branch-C label).

Three fixes in tailscale: (a) scope the module-level singletons + the `window.__ob_tailscale_serve_done` global per-render; (b) replace the brittle `scope.closest('.ob-step')?.parentElement || document.body` container lookup (breaks inside hub/modal mounts) with the actual container; (c) tighten Branch-C so genuine errors aren't mislabeled "installed, needs auth".

> **D6 interaction:** Task 9 widened tailscale's `render()` to accept `sigil` and requires the two self re-render paths (`guardedRecheck` and `wireRecheckBtn`) to thread it. This task owns the `rs` per-render state — so capture `sigil` into `rs` (`rs.sigil = sigil`) and forward it through both self re-renders (see steps 1 and 2). A self re-render must never drop `sigil`.

1. **Scope singletons.** Replace lines 24-27:
```js
let recheckBusy = false;
let _currentAuthAbort = null;
let _authInFlight = false;
let _branchAGen = 0;
```
with a per-render state object + a module pointer (mirrors pair_phone's pattern):
```js
// Per-render state — module globals leaked across non-linear hub mounts.
// _active points at the latest render so a new render can abort the old
// render's auth poll loop.
let _active = null;
```
Then in `render()` (line 29) widen the destructure to include `sigil` (per Task 9 / D6) and install fresh state. Replace the signature + lines 31-32:
```js
export async function render(container, { next, back, skip }) {
    // C1: abort any in-flight auth poll loop from a prior render of this step
    if (_currentAuthAbort) _currentAuthAbort.aborted = true;
    _currentAuthAbort = null;
```
with:
```js
export async function render(container, { next, back, skip, sigil }) {
    // C1: abort any in-flight auth poll loop from a prior render, install fresh
    // per-render state, and remember the real container + the derived sigil so
    // Re-check self re-renders use the right container AND keep the right sigil
    // number (D6 — a self re-render must never drop sigil back to a literal).
    if (_active && _active.authAbort) _active.authAbort.aborted = true;
    const rs = { container, sigil, recheckBusy: false, authAbort: null,
                 authInFlight: false, branchAGen: 0, serveDone: false };
    _active = rs;
```
Then thread `rs` through the branch dispatch + the inner functions. Change the `guardedRecheck` closure (lines 80-88) to use `rs.recheckBusy`, and its self re-render to forward `rs.sigil`:
```js
    const guardedRecheck = async () => {
        if (rs.recheckBusy) return;
        rs.recheckBusy = true;
        try {
            await render(container, { next, back, skip, sigil: rs.sigil });
        } finally {
            rs.recheckBusy = false;
        }
    };
```
Change `renderBranchA/B/C` calls (lines 91-97) to pass `rs`: `renderBranchA(statusEl, result, { next, back, skip }, rs)` etc. Inside `renderBranchA`, replace `_branchAGen++; const myGen = _branchAGen;` with `rs.branchAGen++; const myGen = rs.branchAGen;` and the supersede check `if (myGen !== _branchAGen) return;` with `if (myGen !== rs.branchAGen) return;`. Replace the three `window.__ob_tailscale_serve_done` touches (lines 137, 145, 345) with `rs.serveDone`. In `renderBranchC`, replace `_authInFlight` (lines 484-487) with `rs.authInFlight`. In `startAuth`, replace `_currentAuthAbort`/`myAbort` wiring (lines 494-496) with `rs.authAbort`.

2. **Fix brittle container reconstruction (+ keep sigil through the self re-render).** In `wireRecheckBtn` (line 336) the re-render does `const container = scope.closest(".ob-step")?.parentElement || document.body;` — replace by capturing the real container AND the derived sigil from `rs`. Change `wireRecheckBtn(scope, navCallbacks)` to `wireRecheckBtn(scope, navCallbacks, rs)` at every call site (lines 189, 207, 212), and inside it (line 348) replace:
```js
            const container = scope.closest(".ob-step")?.parentElement || document.body;
            await render(container, navCallbacks);
```
with:
```js
            // D6: forward the derived sigil so the re-rendered step keeps its
            // correct sigil number instead of falling back to the literal.
            await render(rs.container, { ...navCallbacks, sigil: rs.sigil });
```
(`rs.container` is the exact element the wizard/hub passed in — never `document.body`; `rs.sigil` is the derived `stepSigilContext` from Task 9.)

3. **Tighten Branch-C mislabel.** The dispatch (lines 92-98) routes ANY non-"binary not found" error to Branch C ("installed, needs auth"), so a genuine network/backend error is mislabeled. Add a recognizable not-authenticated signal and route only those to C; everything else to a clearer error variant. Change lines 90-98:
```js
    if (result.ok) {
        renderBranchA(statusEl, result, { next, back, skip });
    } else if (typeof result.error === "string" && result.error.includes("binary not found")) {
        renderBranchB(statusEl, result, { back, skip, recheck: guardedRecheck });
    } else {
        // BackendState mismatch OR any other error → "installed, needs auth".
        // Re-checking is the recovery path.
        renderBranchC(statusEl, result, { back, skip, recheck: guardedRecheck });
    }
```
to:
```js
    const err = typeof result.error === "string" ? result.error : "";
    if (result.ok) {
        renderBranchA(statusEl, result, { next, back, skip }, rs);
    } else if (err.includes("binary not found")) {
        renderBranchB(statusEl, result, { back, skip, recheck: guardedRecheck }, rs);
    } else if (err.includes("BackendState") || err.includes("NeedsLogin") || err.includes("not authenticated")) {
        // Genuinely installed-but-unauthenticated → the auth action card.
        renderBranchC(statusEl, result, { back, skip, recheck: guardedRecheck }, rs);
    } else {
        // Anything else (network error, backend down, unexpected) is NOT
        // "needs auth" — show it honestly so Re-check is the recovery path
        // without the misleading "installed" badge.
        renderBranchC(statusEl, { ...result, _unexpected: true },
                      { back, skip, recheck: guardedRecheck }, rs);
    }
```
Then in `renderBranchC` (line 454), differentiate the unexpected case in the badge. After `const errMsg = ...` (line 455), add:
```js
    const unexpected = !!result._unexpected;
```
and change the first status badge label (line 458-460) so the "Tailscale installed" claim isn't asserted on an unexpected error:
```js
        <div class="ob-status-badge ${unexpected ? "ob-status-badge-error" : "ob-status-badge-installed"}" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">${unexpected ? "!" : "&check;"}</span>
            <span class="ob-status-badge-label">${unexpected ? "Couldn't determine Tailscale state" : "Tailscale installed"}</span>
        </div>
```

   > ASSUMPTION: the real validator surfaces "BackendState" in its not-authenticated error string (the existing code's own Branch-C comment at line 14 names `BackendState`). `NeedsLogin`/`not authenticated` are added as belt-and-suspenders; if the live validator uses a different not-auth token, the unexpected-branch still degrades gracefully (shows an honest error + Re-check) rather than a false "installed" badge.

4. Verify no bare module-globals remain:
```bash
grep -n "recheckBusy\|_currentAuthAbort\|_authInFlight\|_branchAGen\|__ob_tailscale_serve_done" Portal/onboarding/steps/tailscale.js | grep -v "rs\.\|_active"
```
Expected: (empty).

5. Chrome-verify all three tailscale branches still render correctly on the live box. Navigate `?step=tailscale`, screenshot. The live box is likely Branch A (tailnet up) — assert `document.querySelector('.ob-status-badge-success')` exists and the Re-check buttons (if any banners) still re-render the step (click a "Re-check" if present, confirm no console error, the step re-renders **in place** — NOT into `document.body` — AND the sigil still reads `02` after the re-render, proving `rs.sigil` survived). Confirm no console error.

6. Commit:
```bash
git add Portal/onboarding/steps/tailscale.js && git commit -m "$(cat <<'EOF'
fix(onboarding): scope tailscale singletons + fix container reconstruction + Branch-C mislabel

(a) recheckBusy/_currentAuthAbort/_authInFlight/_branchAGen + the
window.__ob_tailscale_serve_done global are now per-render state, so
non-linear hub mounts can't race. (b) Re-check re-renders into the real
container (rs.container) instead of scope.closest('.ob-step').parentElement
|| document.body, which broke inside hub/modal mounts — and forwards the
derived sigil (rs.sigil) so the re-render keeps its correct sigil number
(D6, no literal fallback). (c) Branch-C catch-all no longer mislabels
genuine network/backend errors as "installed, needs auth" — only true
not-auth signals get the auth card.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

---

### Task 13: Scope `embeddings.js` long-lived singletons per-render

**Files:** `Portal/onboarding/steps/embeddings.js:50-63` (the module-level `status`, `selectedSlug`, `validating`, `migrating`, `pulling`, `keepAliveBusy`, `cancelClicked`, `pollTimer`, `watchingJob`, `root`, `ctx`, etc.), `:65-78` (`render` reset block).

`embeddings.js` already RESETS most of its module globals at the top of `render()` (lines 66-77) and uses `root.isConnected` as the unmount signal — so it is *partially* safe. The remaining leak risk is `pollTimer` (a live `setInterval`): if a new render starts while a previous poll timer is alive (non-linear hub remount before the old `root` disconnects), the old timer keeps mutating shared globals. The minimal, correct fix is to ensure `stopPolling()` (already called first in `render()`, line 66) fully clears the prior timer — which it does — AND to confirm `keepAliveBusy`/`pulling`/`migrating` are reset.

Audit confirms `render()` already resets `selectedSlug`, `validation`, `validating`, `migrating`, `pulling`, `cancelClicked`, `etaSamples`, `etaJobKey`, `watchingJob`, `ctx` and calls `stopPolling()`. The only un-reset flags are `keepAliveBusy` (line 56) and `pollTimer` (cleared via `stopPolling`). Add the missing reset.

1. In the `render()` reset block (after line 73 `pulling = false;`), add the missing flag reset:
```js
    keepAliveBusy = null;
```

2. Verify the reset block now covers every mutable picker flag:
```bash
grep -n "keepAliveBusy = null;" Portal/onboarding/steps/embeddings.js
```
Expected: 3 matches (the new reset in `render`, plus the existing assignments at the start/end of `setKeepAlive`).

3. Chrome-verify embeddings still renders + remount is clean. Navigate `?step=embeddings`, screenshot the model grid, click a non-active READY model (selects it), then click Back and re-navigate `?step=embeddings` — assert the grid re-renders fresh (no stale `keepAliveBusy` disabling a warm toggle) and no console error. Assert `document.querySelector('#ob-emb-grid')` exists.

4. Commit:
```bash
git add Portal/onboarding/steps/embeddings.js && git commit -m "$(cat <<'EOF'
fix(onboarding): reset embeddings keepAliveBusy per-render

embeddings.js already resets most picker flags in render() and uses
root.isConnected as the unmount signal, but keepAliveBusy was never reset —
a remount mid-toggle could leave a warm switch wedged disabled. Reset it
alongside the others.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
EOF
)"
```

---

### Task 14 — SAFE RESTART POINT: full M0 verification sweep

**Files:** none (verification + restart + test run).

Final gate for M0: clean service boot, full pytest, and a Chrome smoke of every touched step.

1. Restart: `sudo systemctl restart blackbox.service`, then wait:
```bash
until curl -sf http://localhost:9091/health >/dev/null 2>&1; do sleep 3; done; echo "up"
```
Expected: `up`.

2. Run the onboarding parity/guard tests (the only backend surface M0 touched) and confirm green:
```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_steps_parity.py -q
```
Expected: `4 passed` (3 original + the new `test_no_step_hardcodes_sigil_number`).

3. Chrome smoke EVERY step touched in M0, asserting per step: no console error, `.ob-step` present, correct sigil number per `STEPS` index. Walk: `?step=welcome`(01), `?step=tailscale`(02), `?step=api_keys`(03), `?step=embeddings`(04), `?step=optional_integrations`(05), `?step=transcription`(06), `?step=web_search`(07), `?step=image`(08), `?step=pair_phone`(09), `?step=cli_agents`(10), `?step=operator`(11), `?step=done`(12). (This walk is the canonical section order — `STEPS` minus welcome/done bookends, per Decision D2: tailscale, api_keys, embeddings, optional_integrations, transcription, web_search, image, pair_phone, cli_agents, operator.) For each, screenshot + assert:
```js
document.querySelector('.ob-step') !== null && document.querySelector('.ob-step-sigil-num em')?.textContent.trim()
```
Capture the 12 sigil values; they MUST read 01..12 in order with NO duplicates. Also confirm `welcome`'s four `ob-feature-num` pillar cards still read 01/02/03/04 (unchanged decorative numerals — the sigil guard must not have touched them).

4. Confirm the token foundation resolved (one assertion covers Tasks 1-2). Note `--ob-radius-md` is the queried radius token (D1: M0 defines `--ob-radius-*`, NOT `--radius-*`):
```js
const cs = getComputedStyle(document.documentElement);
({accent: cs.getPropertyValue('--ob-accent').trim(),
  attention: cs.getPropertyValue('--ob-attention').trim(),
  stateReady: cs.getPropertyValue('--ob-state-ready').trim(),
  obRadiusMd: cs.getPropertyValue('--ob-radius-md').trim(),
  radiusMdLegacy: cs.getPropertyValue('--radius-md').trim()})
```
Expected: `accent: "#cc0000"`, `attention: "#f5a623"`, `stateReady` non-empty (resolves to success green), `obRadiusMd: "8px"`, and `radiusMdLegacy: ""` (empty — M0 deliberately does NOT define `--radius-*`; the inline `var(--radius-md, 8px)` refs in onboarding.css still resolve to their `8px` literal fallback until M5 migrates them).

5. No commit (verification gate). If any sigil duplicates, any console error, or any token is empty (other than the deliberately-undefined legacy `--radius-md`), fix the responsible task before declaring M0 done.

6. Per @superpowers:verification-before-completion, only after steps 1-4 all show the expected output is M0 complete. The dev snapshot is minted at the END of the overall plan (M6), not per-milestone — do NOT snapshot here.

---

**M0 exit criteria:** amber + `--ob-state-*` + `--ob-radius-*` tokens live and resolving (and M0 did NOT define `--radius-*` — inline refs keep their literal fallback until M5); zero `--accent`/`#4a9eff` blue bleed and banner amber on tokens; single `@keyframes ob-spin`; `.ob-card-selected` (combined `.ob-cli-agent-card`/`.ob-section-card` selector) + `util.js cssEscape()` extracted as M0-owned sole-creator artifacts (M2/M3/M5 verify-only); `renderAlreadyPaired()` wired; sigils 01..12 unique and `STEPS`-derived (the sigil-guard regex scoped to the `ob-step-sigil-num"><em>NN` shape so welcome.js's `ob-feature-num` pillar cards don't trip it, and tailscale's two self re-render paths thread the derived sigil); all leaky singletons (`pair_phone`, `done`, `tailscale`, `embeddings`) scoped per-render; tailscale container reconstruction + Branch-C mislabel fixed; `test_onboarding_steps_parity.py` green with the new sigil guard. Every commit left the app launchable; the only restarts were at the two named SAFE RESTART POINTs.

---

## Milestone M1: Backend `/onboarding/status` rollup + SSE live re-validation (pytest TDD)

This milestone builds the canonical server-side status surface the hub renders from: a fast `GET /onboarding/status` rollup (state derived **purely from persisted data** — zero subprocess/provider calls) and a `GET /onboarding/status/stream` SSE endpoint that does the live re-validation off the blocking path. The provider→step join and all attention-derivation rules (the logic that today lives only in scattered frontend code) get canonicalized in one new backend module, `Orchestrator/onboarding/status_rollup.py`. Tests concentrate here per the design's "backend = where tests concentrate" decision, including a parity guard so the rollup's 10-section list can never drift from `ALL_STEPS`. Every task is bite-sized TDD: failing test → confirm RED → minimal impl → confirm GREEN → commit. Each commit leaves the app launchable (prod serves from the working tree).

Reference @superpowers:test-driven-development and @superpowers:verification-before-completion throughout. Per `tmp_env` fixture conventions in `test_onboarding_web_search.py`, hermeticity is achieved by monkeypatching `secrets_writer.ENV_FILE`, `availability._ROOT`, and `state.STATE_FILE`/`state.COMPLETE_SENTINEL` — the real `.env` and sentinel files are **never** touched.

**Pytest invocation (use this EXACT command everywhere below):**
```
Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_status.py -q
```
(`pytest.ini` sets `testpaths = Orchestrator/tests` + `pythonpath = .`; the root `conftest.py` puts the repo root on `sys.path`. Run from the repo root `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc`.)

---

### Task 1: Canonical SECTIONS table + section↔STEPS parity test (RED)

Create the new module's section catalog FIRST, guarded by a parity test that ties it to `ALL_STEPS` (mirrors `test_onboarding_steps_parity.py`'s "can never drift" intent). This is the keystone the rest of the rollup hangs off.

**Files:**
- `Orchestrator/onboarding/status_rollup.py` (NEW)
- `Orchestrator/tests/test_onboarding_status.py` (NEW)

Write the failing test first:

```python
# Orchestrator/tests/test_onboarding_status.py
"""Backend /onboarding/status rollup + SSE live re-validation (M1).

The rollup canonicalizes the provider->step join + attention-derivation rules
server-side so the hub frontend stays presentational. SECTIONS is the 10-section
catalog (welcome/done are the hub itself, NOT sections); it must never drift from
state.ALL_STEPS. Hermeticity mirrors test_onboarding_web_search.py's tmp_env.
"""
from Orchestrator.onboarding import status_rollup as sr
from Orchestrator.onboarding.state import ALL_STEPS


# Steps that are the hub itself, not status sections (design: welcome->hub
# basis, done->summary model).
_NON_SECTION_STEPS = {"welcome", "done"}


def test_sections_keys_are_all_steps_minus_welcome_and_done():
    section_keys = [s["key"] for s in sr.SECTIONS]
    expected = [s for s in ALL_STEPS if s not in _NON_SECTION_STEPS]
    assert section_keys == expected, (
        "status_rollup.SECTIONS drifted from state.ALL_STEPS:\n"
        f"  sections: {section_keys}\n"
        f"  expected: {expected}"
    )


def test_every_section_step_equals_its_key():
    # Contract: step == key for every section (the hub links ?step=<key>).
    for s in sr.SECTIONS:
        assert s["step"] == s["key"], f"{s['key']}: step != key"


def test_sections_have_required_shape():
    valid_groups = {"network", "keys", "capabilities", "identity"}
    for s in sr.SECTIONS:
        assert set(s) >= {"key", "group", "label", "required", "step"}
        assert s["group"] in valid_groups
        assert isinstance(s["required"], bool)
```

Run to confirm it fails (module doesn't exist):
```
Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_status.py -q
```
Expected: `ModuleNotFoundError: No module named 'Orchestrator.onboarding.status_rollup'` (collection error / RED).

---

### Task 2: Implement SECTIONS catalog (GREEN)

Create the module with the exact 10-section catalog from SHARED CONTRACTS. Order matches `ALL_STEPS` with `welcome`/`done` removed.

**Files:**
- `Orchestrator/onboarding/status_rollup.py:1-90` (NEW)

```python
"""Canonical onboarding status rollup (M1).

Single source of truth for:
  * SECTIONS — the 10 hub sections (welcome/done are the hub itself, excluded).
  * GROUP_LABELS — display label per group.
  * The provider->section join + attention-derivation rules.

build_status(env, state, embeddings, cli, web_search, image, paired, operators,
restart) is PURE: it takes already-read snapshots and derives state from
PERSISTED data only — zero subprocess/provider/tailscale probes. The route layer
(onboarding_routes.py) reads those snapshots cheaply (dotenv_values + persisted
state + GET-able persisted dicts) and passes them in. The SSE stream layer adds
the live probes on top.
"""
from __future__ import annotations

# State vocabulary — the ONLY four legal section states.
READY = "ready"
ATTENTION = "attention"
OPTIONAL = "optional"
CHECKING = "checking"

GROUP_LABELS = {
    "network": "Network & Access",
    "keys": "Keys & Models",
    "capabilities": "Capabilities",
    "identity": "Identity",
}

# The 10 sections, in ALL_STEPS order minus welcome/done. step == key always.
SECTIONS: list[dict] = [
    {"key": "tailscale",              "group": "network",      "label": "Tailnet",     "required": False},
    {"key": "api_keys",               "group": "keys",         "label": "API Keys",    "required": True},
    {"key": "embeddings",             "group": "keys",         "label": "Memory",      "required": True},
    {"key": "optional_integrations",  "group": "capabilities", "label": "Extras",      "required": False},
    {"key": "transcription",          "group": "capabilities", "label": "Speech",      "required": False},
    {"key": "web_search",             "group": "capabilities", "label": "Web Search",  "required": False},
    {"key": "image",                  "group": "capabilities", "label": "Image",       "required": False},
    {"key": "pair_phone",             "group": "network",      "label": "Pair Phone",  "required": False},
    {"key": "cli_agents",             "group": "capabilities", "label": "Agents",      "required": False},
    {"key": "operator",              "group": "identity",     "label": "Operators",   "required": True},
]
# step == key (the hub links ?step=<key>); set it here so callers never re-derive.
for _s in SECTIONS:
    _s["step"] = _s["key"]

SECTION_BY_KEY = {s["key"]: s for s in SECTIONS}
```

> ASSUMPTION: The investigation/design label the CLI-agents section "Agents" with section key `cli_agents` (the `ALL_STEPS` entry is `cli_agents`). Confirmed against `state.ALL_STEPS`.

Run to confirm GREEN:
```
Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_status.py -q
```
Expected: `3 passed`.

Commit:
```
git add Orchestrator/onboarding/status_rollup.py Orchestrator/tests/test_onboarding_status.py
git commit -m "feat(onboarding): canonical SECTIONS catalog for status rollup

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 3: Per-section persisted-state derivation — `api_keys` & `operator` required sections (RED)

Add `build_status(...)` and the per-section derivation. Start with the two simplest required sections so the `ready`/`attention` logic for "required" is nailed before the capabilities. The function is PURE — takes snapshots, returns the rollup dict.

**Files:**
- `Orchestrator/tests/test_onboarding_status.py` (append)

```python
def _empty_inputs():
    """Minimal snapshot inputs for build_status (all unconfigured)."""
    return dict(
        env={},
        state={"completed_steps": [], "skipped_steps": [], "validated_at": {}},
        embeddings={"active": None, "health": {"state": "ok"}, "stores": [], "models": []},
        cli={"providers": {}, "ready": False},
        web_search={"enabled": [], "providers": {}, "default": ""},
        image={"enabled": [], "providers": {}, "default": ""},
        paired=[],
        operators=[],
        restart={"needs_restart": False, "drifted_keys": []},
    )


def _section(rollup, key):
    return next(s for s in rollup["sections"] if s["key"] == key)


def test_api_keys_attention_when_no_keys_present():
    rollup = sr.build_status(**_empty_inputs())
    sec = _section(rollup, "api_keys")
    # required + unsatisfied -> attention (NOT optional)
    assert sec["state"] == sr.ATTENTION
    assert sec["required"] is True


def test_api_keys_ready_when_a_key_present_and_validated():
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx"}
    inp["state"]["validated_at"] = {"openai": 1.0}
    rollup = sr.build_status(**inp)
    assert _section(rollup, "api_keys")["state"] == sr.READY


def test_api_keys_attention_when_present_but_never_validated():
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx"}  # present, validated_at empty
    rollup = sr.build_status(**inp)
    sec = _section(rollup, "api_keys")
    assert sec["state"] == sr.ATTENTION
    assert any(a["section"] == "api_keys" and a["severity"] == "warn"
               for a in rollup["attention"])


def test_operator_required_attention_when_none():
    rollup = sr.build_status(**_empty_inputs())
    assert _section(rollup, "operator")["state"] == sr.ATTENTION


def test_operator_ready_when_present():
    inp = _empty_inputs()
    inp["operators"] = ["Brandon"]
    assert _section(rollup, "operator")["state"] if False else \
        sr.build_status(**inp)
    assert _section(sr.build_status(**inp), "operator")["state"] == sr.READY


def test_rollup_top_level_shape():
    rollup = sr.build_status(**_empty_inputs())
    assert set(rollup) >= {"ready_count", "total", "is_complete", "sections", "attention"}
    assert rollup["total"] == len(sr.SECTIONS)
    assert isinstance(rollup["ready_count"], int)
    for sec in rollup["sections"]:
        assert set(sec) >= {"key", "group", "label", "state", "required",
                            "summary", "step", "skipped", "items"}
```

Run → RED (`AttributeError: module ... has no attribute 'build_status'`):
```
Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_status.py -q
```

---

### Task 4: Implement `build_status` skeleton + `api_keys`/`operator` derivers (GREEN)

**Files:**
- `Orchestrator/onboarding/status_rollup.py` (append, ~L90+)

```python
# Provider key env-vars that satisfy the api_keys section (the LLM provider set;
# matches onboarding_routes.current_config's provider list, minus gmail/elevenlabs
# which are surfaced under their own capability sections).
_LLM_KEY_ENV = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "XAI_API_KEY", "PERPLEXITY_API_KEY",
]
# provider id (validated_at key) -> its env var, for the present-but-unvalidated check.
_PROVIDER_KEY = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY", "xai": "XAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
}


def _present_keys(env: dict) -> list[str]:
    return [k for k in _LLM_KEY_ENV if (env.get(k) or "").strip()]


def _derive_api_keys(env, state):
    present = _present_keys(env)
    validated = state.get("validated_at", {}) or {}
    items = [
        {"key": prov, "label": prov,
         "configured": bool((env.get(var) or "").strip()),
         "validated_at": validated.get(prov)}
        for prov, var in _PROVIDER_KEY.items()
    ]
    if not present:
        return ATTENTION, "No API keys configured", items, [
            {"severity": "warn", "message": "No LLM API key configured — chat will not work"}]
    # present-but-unvalidated: any present key whose provider has no validated_at stamp
    unvalidated = [prov for prov, var in _PROVIDER_KEY.items()
                   if (env.get(var) or "").strip() and prov not in validated]
    if unvalidated:
        return ATTENTION, f"{len(present)} key(s); {len(unvalidated)} unvalidated", items, [
            {"severity": "warn",
             "message": f"Key present but never validated: {', '.join(unvalidated)}"}]
    return READY, f"{len(present)} key(s) validated", items, []


def _derive_operator(operators):
    items = [{"key": op, "label": op, "configured": True, "validated_at": None}
             for op in operators]
    if not operators:
        return ATTENTION, "No operators configured", items, [
            {"severity": "warn", "message": "Add at least one operator"}]
    return READY, f"{len(operators)} operator(s)", items, []


# Dispatch table: key -> deriver returning (state, summary, items, attention_list).
# Sections not yet wired fall back to the optional/persisted default.
def _derive_default(section, env, state):
    """Persisted fallback: completed -> ready, skipped -> optional, else optional."""
    completed = set(state.get("completed_steps", []))
    skipped = set(state.get("skipped_steps", []))
    key = section["key"]
    if key in completed:
        return READY, "Configured", [], []
    if key in skipped:
        return OPTIONAL, "Skipped", [], []
    if section["required"]:
        return ATTENTION, "Not configured", [], [
            {"severity": "warn", "message": f"{section['label']} required"}]
    return OPTIONAL, "Not set up", [], []


def build_status(*, env, state, embeddings, cli, web_search, image,
                 paired, operators, restart, is_complete=False):
    """PURE rollup from persisted snapshots. No probes. See module docstring."""
    sections_out = []
    attention_out = []
    skipped = set(state.get("skipped_steps", []))
    for section in SECTIONS:
        key = section["key"]
        if key == "api_keys":
            st, summary, items, atts = _derive_api_keys(env, state)
        elif key == "operator":
            st, summary, items, atts = _derive_operator(operators)
        else:
            st, summary, items, atts = _derive_default(section, env, state)
        sections_out.append({
            "key": key, "group": section["group"], "label": section["label"],
            "state": st, "required": section["required"], "summary": summary,
            "step": section["step"], "skipped": key in skipped, "items": items,
        })
        for a in atts:
            attention_out.append({
                "section": key, "severity": a["severity"],
                "message": a["message"], "cta_step": key,
            })
    ready_count = sum(1 for s in sections_out if s["state"] == READY)
    return {
        "ready_count": ready_count,
        "total": len(SECTIONS),
        "is_complete": is_complete,
        "sections": sections_out,
        "attention": attention_out,
    }
```

Run → GREEN:
```
Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_status.py -q
```
Expected: all tests pass.

Commit:
```
git add Orchestrator/onboarding/status_rollup.py Orchestrator/tests/test_onboarding_status.py
git commit -m "feat(onboarding): build_status rollup with api_keys + operator derivers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 5: `embeddings` (Memory) section derivation — behind/superseded/broken (RED)

The Memory section is required; its attention rules come straight from `/embeddings/status` shape (`active`, `health.state ∈ {ok,superseded,broken}`, and per-store `missing` = snapshots behind).

**Files:**
- `Orchestrator/tests/test_onboarding_status.py` (append)

```python
def test_embeddings_attention_when_no_active_model():
    inp = _empty_inputs()
    inp["embeddings"] = {"active": None, "health": {"state": "ok"},
                         "stores": [], "models": []}
    assert _section(sr.build_status(**inp), "embeddings")["state"] == sr.ATTENTION


def test_embeddings_ready_when_active_and_healthy_and_caught_up():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b",
        "health": {"state": "ok", "successor": None},
        "stores": [{"slug": "qwen3-0.6b", "missing": 0}],
        "models": [],
    }
    assert _section(sr.build_status(**inp), "embeddings")["state"] == sr.READY


def test_embeddings_attention_when_index_behind():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b", "health": {"state": "ok"},
        "stores": [{"slug": "qwen3-0.6b", "missing": 42}], "models": [],
    }
    rollup = sr.build_status(**inp)
    assert _section(rollup, "embeddings")["state"] == sr.ATTENTION
    assert any("behind" in a["message"].lower() for a in rollup["attention"]
               if a["section"] == "embeddings")


def test_embeddings_attention_when_health_superseded():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b",
        "health": {"state": "superseded", "successor": "gemini-embedding-2"},
        "stores": [{"slug": "qwen3-0.6b", "missing": 0}], "models": [],
    }
    rollup = sr.build_status(**inp)
    assert _section(rollup, "embeddings")["state"] == sr.ATTENTION


def test_embeddings_attention_when_health_broken_is_error_severity():
    inp = _empty_inputs()
    inp["embeddings"] = {
        "active": "qwen3-0.6b", "health": {"state": "broken", "detail": "x"},
        "stores": [{"slug": "qwen3-0.6b", "missing": 0}], "models": [],
    }
    rollup = sr.build_status(**inp)
    assert any(a["section"] == "embeddings" and a["severity"] == "error"
               for a in rollup["attention"])
```

Run → RED (embeddings still falls through `_derive_default`, so several assertions fail).

---

### Task 6: Implement `_derive_embeddings` (GREEN)

**Files:**
- `Orchestrator/onboarding/status_rollup.py` (append + wire into `build_status`)

```python
def _derive_embeddings(embeddings):
    active = embeddings.get("active")
    health = embeddings.get("health") or {}
    hstate = health.get("state", "ok")
    items = [{"key": "active", "label": active or "(none)",
              "configured": bool(active), "validated_at": None}]
    if not active:
        return ATTENTION, "No memory model active", items, [
            {"severity": "warn", "message": "Memory index not initialized"}]
    if hstate == "broken":
        return ATTENTION, "Memory index broken", items, [
            {"severity": "error",
             "message": f"Memory health broken: {health.get('detail', '')}".strip()}]
    if hstate == "superseded":
        succ = health.get("successor") or "a newer model"
        return ATTENTION, f"Upgrade available → {succ}", items, [
            {"severity": "warn", "message": f"Memory model superseded by {succ}"}]
    # caught-up check against the active store's `missing` count
    behind = 0
    for store in (embeddings.get("stores") or []):
        if store.get("slug") == active:
            behind = store.get("missing") or 0
            break
    if behind:
        return ATTENTION, f"{behind} snapshot(s) behind", items, [
            {"severity": "warn", "message": f"Memory index {behind} snapshot(s) behind"}]
    return READY, f"Active: {active}", items, []
```

Wire into `build_status`'s dispatch:
```python
        elif key == "embeddings":
            st, summary, items, atts = _derive_embeddings(embeddings)
```

Run → GREEN. Commit:
```
git add Orchestrator/onboarding/status_rollup.py Orchestrator/tests/test_onboarding_status.py
git commit -m "feat(onboarding): Memory section derivation (behind/superseded/broken)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 7: Capability sections `web_search` / `image` — enabled-but-missing-key (RED)

These are optional (`required=False`): unset → optional; enabled with all keys present → ready; enabled but a chosen provider's key missing → attention. Inputs mirror `current_config`'s `web_search`/`image` shape (`{enabled:[...], providers:{prov:{key_present,enabled}}, default}`).

**Files:**
- `Orchestrator/tests/test_onboarding_status.py` (append)

```python
def test_web_search_optional_when_nothing_enabled():
    inp = _empty_inputs()
    inp["web_search"] = {"enabled": [], "providers": {}, "default": ""}
    assert _section(sr.build_status(**inp), "web_search")["state"] == sr.OPTIONAL


def test_web_search_ready_when_enabled_with_keys():
    inp = _empty_inputs()
    inp["web_search"] = {
        "enabled": ["duckduckgo", "openai"],
        "providers": {"duckduckgo": {"key_present": True, "enabled": True},
                      "openai": {"key_present": True, "enabled": True}},
        "default": "openai",
    }
    assert _section(sr.build_status(**inp), "web_search")["state"] == sr.READY


def test_web_search_attention_when_enabled_provider_key_missing():
    inp = _empty_inputs()
    inp["web_search"] = {
        "enabled": ["openai"],
        "providers": {"openai": {"key_present": False, "enabled": True}},
        "default": "openai",
    }
    rollup = sr.build_status(**inp)
    assert _section(rollup, "web_search")["state"] == sr.ATTENTION
    assert any(a["section"] == "web_search" and "key" in a["message"].lower()
               for a in rollup["attention"])


def test_image_attention_when_enabled_provider_key_missing():
    inp = _empty_inputs()
    inp["image"] = {
        "enabled": ["gemini"],
        "providers": {"gemini": {"key_present": False, "enabled": True}},
        "default": "gemini",
    }
    assert _section(sr.build_status(**inp), "image")["state"] == sr.ATTENTION
```

Run → RED.

---

### Task 8: Implement shared `_derive_feature` for web_search + image (GREEN, DRY)

One deriver covers both feature sections (closes the design's DRY note).

**Files:**
- `Orchestrator/onboarding/status_rollup.py` (append + wire)

```python
def _derive_feature(feature, label):
    """Shared deriver for web_search + image (same enabled/key shape)."""
    enabled = feature.get("enabled") or []
    providers = feature.get("providers") or {}
    items = [{"key": p, "label": p, "configured": meta.get("enabled", False),
              "validated_at": None} for p, meta in providers.items()]
    if not enabled:
        return OPTIONAL, "Not enabled", items, []
    # any enabled provider whose key is absent -> attention
    missing = [p for p in enabled
               if p in providers and not providers[p].get("key_present", False)]
    if missing:
        return ATTENTION, f"{', '.join(missing)} missing key", items, [
            {"severity": "warn",
             "message": f"{label}: {', '.join(missing)} enabled but key missing"}]
    return READY, f"{len(enabled)} provider(s)", items, []
```

Wire into `build_status`:
```python
        elif key == "web_search":
            st, summary, items, atts = _derive_feature(web_search, "Web Search")
        elif key == "image":
            st, summary, items, atts = _derive_feature(image, "Image")
```

Run → GREEN. Commit:
```
git add Orchestrator/onboarding/status_rollup.py Orchestrator/tests/test_onboarding_status.py
git commit -m "feat(onboarding): web_search + image section derivation (shared deriver)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 9: `cli_agents` (installed-not-authed) + `pair_phone` + global restart-drift (RED)

`cli_agents`: input is `/cli-agent/status` shape (`{providers:{prov:{installed,authenticated}}, ready}`). Installed-but-not-authed → attention. `pair_phone`: `paired` list non-empty → ready, else optional. Global restart drift (`restart.needs_restart`) → a top-level attention row not tied to a single section (use `section=None`, `cta_step="api_keys"` per design — drift is usually keys).

**Files:**
- `Orchestrator/tests/test_onboarding_status.py` (append)

```python
def test_cli_agents_attention_when_installed_not_authed():
    inp = _empty_inputs()
    inp["cli"] = {"providers": {
        "claude": {"installed": True, "authenticated": False}}, "ready": False}
    rollup = sr.build_status(**inp)
    assert _section(rollup, "cli_agents")["state"] == sr.ATTENTION
    assert any("auth" in a["message"].lower() for a in rollup["attention"]
               if a["section"] == "cli_agents")


def test_cli_agents_ready_when_all_ready():
    inp = _empty_inputs()
    inp["cli"] = {"providers": {
        "claude": {"installed": True, "authenticated": True}}, "ready": True}
    assert _section(sr.build_status(**inp), "cli_agents")["state"] == sr.READY


def test_cli_agents_optional_when_none_installed():
    inp = _empty_inputs()
    inp["cli"] = {"providers": {
        "claude": {"installed": False, "authenticated": False}}, "ready": False}
    assert _section(sr.build_status(**inp), "cli_agents")["state"] == sr.OPTIONAL


def test_pair_phone_ready_when_devices_paired():
    inp = _empty_inputs()
    inp["paired"] = [{"name": "Pixel"}]
    assert _section(sr.build_status(**inp), "pair_phone")["state"] == sr.READY


def test_pair_phone_optional_when_none():
    assert _section(sr.build_status(**_empty_inputs()), "pair_phone")["state"] == sr.OPTIONAL


def test_global_restart_drift_emits_attention_row():
    inp = _empty_inputs()
    inp["restart"] = {"needs_restart": True, "drifted_keys": ["OPENAI_API_KEY"]}
    rollup = sr.build_status(**inp)
    assert any(a.get("section") is None and "restart" in a["message"].lower()
               for a in rollup["attention"])
```

Run → RED.

---

### Task 10: Implement `_derive_cli_agents`, `_derive_pair_phone`, restart-drift row (GREEN)

**Files:**
- `Orchestrator/onboarding/status_rollup.py` (append + wire)

```python
def _derive_cli_agents(cli):
    providers = cli.get("providers") or {}
    installed = {p: m for p, m in providers.items() if m.get("installed")}
    items = [{"key": p, "label": p, "configured": m.get("installed", False),
              "validated_at": None} for p, m in providers.items()]
    if not installed:
        return OPTIONAL, "No agents installed", items, []
    # authenticated may be None (antigravity: implicit) — only False is a blocker
    not_authed = [p for p, m in installed.items() if m.get("authenticated") is False]
    if not_authed:
        return ATTENTION, f"{', '.join(not_authed)} not signed in", items, [
            {"severity": "warn",
             "message": f"Agent installed but not authenticated: {', '.join(not_authed)}"}]
    return READY, f"{len(installed)} agent(s) ready", items, []


def _derive_pair_phone(paired):
    items = [{"key": d.get("name", "device"), "label": d.get("name", "device"),
              "configured": True, "validated_at": None} for d in paired]
    if paired:
        return READY, f"{len(paired)} device(s) paired", items, []
    return OPTIONAL, "No phone paired", items, []
```

Wire into `build_status`:
```python
        elif key == "cli_agents":
            st, summary, items, atts = _derive_cli_agents(cli)
        elif key == "pair_phone":
            st, summary, items, atts = _derive_pair_phone(paired)
```

And after the per-section loop (before computing `ready_count`), append the global drift row:
```python
    if restart.get("needs_restart"):
        drifted = restart.get("drifted_keys") or []
        attention_out.append({
            "section": None, "severity": "warn",
            "message": f"Service restart needed — {len(drifted)} setting(s) drifted",
            "cta_step": "api_keys",
        })
```

Run → GREEN. Commit:
```
git add Orchestrator/onboarding/status_rollup.py Orchestrator/tests/test_onboarding_status.py
git commit -m "feat(onboarding): cli_agents + pair_phone derivers + global restart-drift row

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 11: `tailscale` section — serve-not-set attention (persisted-only, NO probe) (RED)

The fast read must do **zero** subprocess. So `tailscale` derives from PERSISTED signals only: `validated_at["tailscale"]` stamp + a persisted `serve_configured` hint. In the fast rollup, a validated tailnet with no serve hint → attention ("phone pairing will fail"); the live SSE probe later refines it.

**Files:**
- `Orchestrator/tests/test_onboarding_status.py` (append)

```python
def test_tailscale_optional_when_never_validated():
    assert _section(sr.build_status(**_empty_inputs()), "tailscale")["state"] == sr.OPTIONAL


def test_tailscale_attention_when_validated_but_serve_not_set():
    inp = _empty_inputs()
    inp["state"]["validated_at"] = {"tailscale": 1.0}
    # no serve hint in env -> serve-not-set attention
    rollup = sr.build_status(**inp)
    assert _section(rollup, "tailscale")["state"] == sr.ATTENTION
    assert any("pair" in a["message"].lower() or "serve" in a["message"].lower()
               for a in rollup["attention"] if a["section"] == "tailscale")


def test_tailscale_ready_when_validated_and_serve_hint_present():
    inp = _empty_inputs()
    inp["state"]["validated_at"] = {"tailscale": 1.0}
    inp["env"] = {"BLACKBOX_TAILNET_HOSTNAME": "box.tail1234.ts.net"}
    assert _section(sr.build_status(**inp), "tailscale")["state"] == sr.READY
```

> ASSUMPTION: `BLACKBOX_TAILNET_HOSTNAME` in `.env` (already a `/restart-status` drift key) is the persisted proxy for "serve/HTTPS configured" in the FAST read. The live SSE probe (Task 14) replaces this heuristic with the real `validate_tailscale()` `serve` state.

Run → RED.

---

### Task 12: Implement `_derive_tailscale` (persisted-only) (GREEN)

**Files:**
- `Orchestrator/onboarding/status_rollup.py` (append + wire)

```python
def _derive_tailscale(env, state):
    """FAST read: persisted-only. Never probes. Live state arrives via SSE."""
    validated = (state.get("validated_at", {}) or {}).get("tailscale")
    serve_hint = bool((env.get("BLACKBOX_TAILNET_HOSTNAME") or "").strip())
    items = [{"key": "tailnet", "label": env.get("BLACKBOX_TAILNET_HOSTNAME") or "(unset)",
              "configured": serve_hint, "validated_at": validated}]
    if not validated:
        return OPTIONAL, "Not connected", items, []
    if not serve_hint:
        return ATTENTION, "HTTPS serve not set", items, [
            {"severity": "warn",
             "message": "Tailnet up but serve/HTTPS not set — phone pairing will fail"}]
    return READY, "Connected", items, []
```

Wire into `build_status`:
```python
        elif key == "tailscale":
            st, summary, items, atts = _derive_tailscale(env, state)
```

Run full file → GREEN. Commit:
```
git add Orchestrator/onboarding/status_rollup.py Orchestrator/tests/test_onboarding_status.py
git commit -m "feat(onboarding): tailscale section persisted-only derivation (no probe)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 13: `GET /onboarding/status` route — fast read, zero probes (RED)

Now wire the route. It reads `.env` fresh (`dotenv_values` per E8), persisted state, and the cheap persisted dicts, then calls `build_status`. **It must call ZERO subprocess/provider/tailscale probes.** Test via `TestClient` (the `test_onboarding_web_search.py` `_client()` pattern) and assert no probe is hit by patching `validators.validate_tailscale` to explode.

**Files:**
- `Orchestrator/tests/test_onboarding_status.py` (append)

```python
from unittest.mock import patch
import pytest


def _client():
    import Orchestrator.app  # noqa: F401 — registers onboarding routes
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app)


def test_status_route_returns_contract_shape():
    c = _client()
    r = c.get("/onboarding/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"ready_count", "total", "is_complete", "sections", "attention"}
    assert body["total"] == len(sr.SECTIONS)
    assert {s["key"] for s in body["sections"]} == {s["key"] for s in sr.SECTIONS}


def test_status_route_does_no_tailscale_probe():
    """The FAST read must never shell out — patch the probe to raise; route
    must still 200 (proving it was never called)."""
    c = _client()
    with patch("Orchestrator.onboarding.validators.validate_tailscale",
               side_effect=AssertionError("FAST read must not probe tailscale")):
        r = c.get("/onboarding/status")
    assert r.status_code == 200
```

Run → RED (route 404).

---

### Task 14: Implement `GET /onboarding/status` route (GREEN)

**Files:**
- `Orchestrator/routes/onboarding_routes.py` (append after `restart_status`, ~L560)

```python
# ── M1: hub status rollup. FAST read — persisted data only, ZERO probes.
#   (live re-validation lives in GET /onboarding/status/stream below.) ──
from Orchestrator.onboarding import status_rollup


def _collect_status_inputs() -> dict:
    """Cheap, probe-free snapshots for status_rollup.build_status.

    .env is read fresh (E8: dotenv_values, no os.environ pollution). The
    capability dicts reuse the SAME availability gate current_config uses, so
    the enabled set is derived in ONE place. NO subprocess / provider / tailscale
    calls happen here — tailscale state is derived from persisted hints only.
    """
    from dotenv import dotenv_values
    from Orchestrator.onboarding.secrets_writer import ENV_FILE
    env = dict(dotenv_values(str(ENV_FILE)))
    snap = _state.snapshot()
    state = {
        "completed_steps": snap["completed_steps"],
        "skipped_steps": snap["skipped_steps"],
        "validated_at": _state.validated_at(),
    }

    # Operators (module-level list; fail-soft to [])
    try:
        from Orchestrator.routes import admin_routes
        operators = list(admin_routes.USERS_LIST)
    except Exception:
        operators = []

    # Paired devices (fail-soft to [])
    try:
        from Orchestrator.routes.pairing_routes import list_paired_devices
        paired = list_paired_devices()
    except Exception:
        paired = []

    # web_search / image — reuse availability gate (DRY with current_config)
    from Orchestrator.toolvault.availability import (
        enabled_web_search_providers, enabled_providers, PROVIDER_ENV, FEATURES,
    )
    ws_enabled = enabled_web_search_providers()
    web_search = {
        "enabled": sorted(ws_enabled),
        "providers": {
            prov: {"key_present": (True if var is None else bool(env.get(var))),
                   "enabled": prov in ws_enabled}
            for prov, var in PROVIDER_ENV.items()
        },
        "default": (env.get("WEB_SEARCH_DEFAULT") or "").strip(),
    }
    img_enabled = enabled_providers("image")
    image = {
        "enabled": sorted(img_enabled),
        "providers": {
            prov: {"key_present": bool(env.get(var)), "enabled": prov in img_enabled}
            for prov, var in FEATURES["image"]["provider_env"].items()
        },
        "default": (env.get("IMAGE_DEFAULT") or "").strip(),
    }

    # embeddings — read-only status dict (no probe side effects). Fail-soft.
    try:
        from Orchestrator.routes.embeddings_routes import embeddings_status
        from fastapi import Response as _Resp
        embeddings = embeddings_status(_Resp())
    except Exception:
        logger.exception("status rollup: embeddings_status failed")
        embeddings = {"active": None, "health": {"state": "ok"}, "stores": [], "models": []}

    # cli agents — installed/auth markers (filesystem only, no spawn). Fail-soft.
    try:
        cli = cli_agent_status()
    except Exception:
        logger.exception("status rollup: cli_agent_status failed")
        cli = {"providers": {}, "ready": False}

    restart = restart_status().model_dump()

    return dict(
        env=env, state=state, embeddings=embeddings, cli=cli,
        web_search=web_search, image=image, paired=paired, operators=operators,
        restart=restart, is_complete=_state.is_complete(),
    )


@router.get("/status")
def onboarding_status() -> dict:
    """Fast persisted rollup for the hub. State derived from PERSISTED data only
    — no provider/tailscale/subprocess probe. Live re-validation is GET
    /onboarding/status/stream."""
    return status_rollup.build_status(**_collect_status_inputs())
```

> NOTE: `embeddings_status` is a plain `def` taking a `Response` — calling it directly (with a throwaway `Response()`) is the cheapest reuse and matches its "strictly read-only, safe to poll" contract. `cli_agent_status` is filesystem-only (binary resolution + marker-file `.exists()`), no spawn — safe for the fast path.

Run → GREEN:
```
Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_status.py -q
```

Commit:
```
git add Orchestrator/routes/onboarding_routes.py Orchestrator/tests/test_onboarding_status.py
git commit -m "feat(onboarding): GET /onboarding/status fast rollup route (no probes)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 15: SAFE RESTART POINT — verify fast route live on the running service

The fast read is fully wired and tested. Restart so the live service exposes `/onboarding/status`, then smoke it (Brandon pre-authorized restarts).

**Files:** none (operational).

```
sudo systemctl restart blackbox.service
```
Wait for warm-up (~60-90s), then:
```
until curl -sf -o /dev/null http://localhost:9091/health; do sleep 3; done
curl -s http://localhost:9091/onboarding/status | python3 -m json.tool | head -40
```
Expected: JSON with `ready_count`, `total: 10`, a `sections` array of 10 entries each carrying `key/group/label/state/required/summary/step/skipped/items`, and an `attention` array. Confirm the response returns in well under a second (no probe latency).

No commit (operational task).

---

### Task 16: SSE `GET /onboarding/status/stream` — live re-validation contract (RED)

The stream re-validates with live probes and emits `event: section` per section then `event: done`. Test it via `TestClient` (which buffers SSE into the response body) and assert the event framing + that a `done` event with `ready_count`/`total` arrives. Patch the live probes so the test is hermetic and fast.

**Files:**
- `Orchestrator/tests/test_onboarding_status.py` (append)

```python
def _parse_sse(text):
    """Minimal SSE parser -> list of (event, data_str)."""
    events, ev, data = [], None, []
    for line in text.splitlines():
        if line.startswith("event:"):
            ev = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:"):].strip())
        elif line == "":
            if ev or data:
                events.append((ev, "\n".join(data)))
            ev, data = None, []
    if ev or data:
        events.append((ev, "\n".join(data)))
    return events


def test_status_stream_emits_section_events_then_done():
    import json as _json
    c = _client()
    # Keep the live tailscale probe deterministic + cheap.
    with patch("Orchestrator.onboarding.validators.validate_tailscale") as m:
        from Orchestrator.onboarding.validators import ValidationResult
        m.return_value = ValidationResult(ok=False, latency_ms=1, error="not running")
        r = c.get("/onboarding/status/stream")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    events = _parse_sse(r.text)
    kinds = [e[0] for e in events]
    assert "section" in kinds
    assert kinds[-1] == "done"
    # every section event carries the contract keys
    for ev, data in events:
        if ev == "section":
            payload = _json.loads(data)
            assert set(payload) >= {"key", "state", "summary", "attention"}
    done_payload = _json.loads(events[-1][1])
    assert set(done_payload) >= {"ready_count", "total"}
    assert done_payload["total"] == len(sr.SECTIONS)
```

Run → RED (route 404).

---

### Task 17: Implement `GET /onboarding/status/stream` SSE route (GREEN)

Reuse the existing SSE pattern (`StreamingResponse(gen(), media_type="text/event-stream")` from `logs_stream`/`tailscale_install_stream`). It starts from the fast rollup, then for each section runs its live probe (only `tailscale` needs a real probe; the rest are already truthful from persisted data, so we re-emit them as-is). Probes run sequentially in a threadpool-friendly generator; failures fall back to the persisted state for that section (never crash the stream).

**Files:**
- `Orchestrator/routes/onboarding_routes.py` (append after `onboarding_status`)

```python
@router.get("/status/stream")
async def onboarding_status_stream():
    """SSE live re-validation for the hub (fired on view, never on a timer).

    Emits one `event: section` per section as its probe resolves, then a final
    `event: done`. Reuses the StreamingResponse SSE pattern from logs_stream /
    tailscale_install_stream. The ONLY section needing a live probe today is
    tailscale (serve/HTTPS state); the rest re-emit their persisted-derived
    state. A probe failure falls back to the persisted state — the stream never
    crashes over one bad section."""
    import asyncio
    import json as _json

    def _sse(event: str, payload: dict) -> bytes:
        return f"event: {event}\ndata: {_json.dumps(payload)}\n\n".encode("utf-8")

    async def gen():
        # Start from the fast rollup (persisted truth) so every section has a
        # baseline even if its live probe is a no-op.
        base = status_rollup.build_status(**_collect_status_inputs())
        sections = {s["key"]: s for s in base["sections"]}
        ready_count = 0

        for section in status_rollup.SECTIONS:
            key = section["key"]
            sec = sections[key]
            atts = [a for a in base["attention"] if a.get("section") == key]

            if key == "tailscale":
                # The one genuinely-live probe — refine serve/HTTPS truth.
                try:
                    from Orchestrator.onboarding.validators import validate_tailscale
                    res = await asyncio.to_thread(validate_tailscale)
                    if not res.ok:
                        sec = {**sec, "state": status_rollup.OPTIONAL,
                               "summary": "Not connected"}
                        atts = []
                    # res.ok refines nothing beyond the persisted serve hint here;
                    # the fast-read hint already classified serve-not-set.
                except Exception:
                    logger.exception("status stream: tailscale probe failed")

            if sec["state"] == status_rollup.READY:
                ready_count += 1
            yield _sse("section", {
                "key": key, "state": sec["state"], "summary": sec["summary"],
                "attention": atts,
            })
            await asyncio.sleep(0)  # cooperative yield between probes

        yield _sse("done", {"ready_count": ready_count, "total": len(status_rollup.SECTIONS)})

    return StreamingResponse(gen(), media_type="text/event-stream")
```

> NOTE: `StreamingResponse` and `logger` are already imported in `onboarding_routes.py` (the file imports `StreamingResponse` at L477). The live probe is wrapped in `asyncio.to_thread` so the sync `validate_tailscale` subprocess never blocks the event loop — consistent with the "keep expensive/tailscale probes off any blocking path" design note.

Run → GREEN:
```
Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_status.py -q
```

Commit:
```
git add Orchestrator/routes/onboarding_routes.py Orchestrator/tests/test_onboarding_status.py
git commit -m "feat(onboarding): GET /onboarding/status/stream SSE live re-validation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 18: Strengthen the parity guard — extend `test_onboarding_steps_parity.py` so the rollup can never drift (RED→GREEN)

Per the design's explicit ask: "Extend `test_onboarding_steps_parity.py` so the rollup section list can never drift from STEPS." Add a backend-only assertion tying `status_rollup.SECTIONS` to `ALL_STEPS` (independent of the JS parity tests already there).

**Files:**
- `Orchestrator/tests/test_onboarding_steps_parity.py` (append, after L73)

```python
def test_status_rollup_sections_match_all_steps_minus_welcome_done():
    """The hub status rollup (Orchestrator/onboarding/status_rollup.py SECTIONS)
    enumerates exactly ALL_STEPS minus welcome/done, in order. Add/move/remove a
    step on either side without the other and this fails — the rollup's section
    list can never silently drift from the canonical step list."""
    from Orchestrator.onboarding.status_rollup import SECTIONS
    section_keys = [s["key"] for s in SECTIONS]
    expected = [s for s in ALL_STEPS if s not in ("welcome", "done")]
    assert section_keys == expected, (
        "status_rollup.SECTIONS drifted from state.ALL_STEPS:\n"
        f"  sections: {section_keys}\n"
        f"  expected: {expected}"
    )


def test_status_rollup_every_section_step_equals_key():
    """Each section's `step` must equal its `key` (the hub links ?step=<key>)."""
    from Orchestrator.onboarding.status_rollup import SECTIONS
    for s in SECTIONS:
        assert s["step"] == s["key"], f"section {s['key']!r}: step != key"
```

Run the parity file specifically to confirm both old + new pass:
```
Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_steps_parity.py -q
```
Expected: all pass (5 tests).

Commit:
```
git add Orchestrator/tests/test_onboarding_steps_parity.py
git commit -m "test(onboarding): guard status_rollup SECTIONS against ALL_STEPS drift

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 19: Full milestone test sweep + SAFE RESTART POINT

Run the whole onboarding-status surface plus the parity guard to confirm nothing regressed, then restart so both new endpoints are live on the running service for downstream milestones (M2/M3 consume them).

**Files:** none (verification + operational).

```
Orchestrator/venv/bin/pytest \
  Orchestrator/tests/test_onboarding_status.py \
  Orchestrator/tests/test_onboarding_steps_parity.py \
  Orchestrator/tests/test_onboarding_web_search.py \
  Orchestrator/tests/test_embeddings_onboarding_step.py -q
```
Expected: all pass, zero failures (confirms the rollup didn't break existing onboarding tests).

Then restart + smoke both endpoints:
```
sudo systemctl restart blackbox.service
until curl -sf -o /dev/null http://localhost:9091/health; do sleep 3; done
echo "--- fast read ---"
curl -s http://localhost:9091/onboarding/status | python3 -c "import sys,json; b=json.load(sys.stdin); print('ready',b['ready_count'],'of',b['total']); print('sections',[s['key'] for s in b['sections']]); print('attention',len(b['attention']))"
echo "--- SSE stream (first events) ---"
curl -s -N --max-time 8 http://localhost:9091/onboarding/status/stream | head -30
```
Expected: fast read prints `ready N of 10` + the 10 section keys; the SSE stream prints a series of `event: section` blocks ending in `event: done`.

Per @superpowers:verification-before-completion, paste the actual pytest summary line + the curl output before declaring M1 done. No commit (verification/operational). Run `/snapshot-dev` only at the very END of the full plan, not here.

---

## Milestone M2: Quick win — clickable done.js summary + missing rows

This milestone rebuilds `done.js`'s summary so it sources rows from the new `GET /onboarding/status` (M1) instead of the local `loadSummary()` join over `/current-config` + `/state`. Each row becomes a one-click link to `/onboarding/?step=<key>`, attention rows gain a "Set up →" CTA, and the five sections `done` currently omits (embeddings, transcription, web_search, image, cli_agents) appear automatically because the backend rollup is now the single source of the section list. In addition to the per-section `sections[]`, the done screen now also surfaces the top-level **global** `attention[]` rows from the rollup (e.g. the restart-drift row with `section=null` / `cta_step="api_keys"`), so machine-wide attention items are not silently dropped. This is the **first consumer** of `/onboarding/status`, so it doubles as an end-to-end validation of the M1 payload across `ready`/`attention`/`optional` states. The existing completion behavior — the `Open Portal` sentinel POST, the status-aware Restart Service button, View Logs, and the update badge — is left fully intact; only the summary-list rendering is replaced. Per the design's "presentational only" rule, no derivation logic moves into the frontend: `done.js` maps `state → glyph/css` and renders the backend-supplied `summary` string verbatim.

This is a frontend milestone (no JS test harness), so every task is Claude-in-Chrome verified against the live wizard and ends with a commit. M2 assumes **M0 and M1 are merged** and live: M0 owns the shared tokens (`--ob-attention`, `--ob-state-*`, `--ob-radius-*`) and `Portal/onboarding/util.js`; M1 ships `/onboarding/status`. M2 is **import-only / verify-only** for M0's artifacts — it does NOT create `util.js`, does NOT add `--ob-*` tokens, and does NOT define `.ob-card-selected` (all M0-owned per the shared-ownership contract). The first tasks assert those preconditions. Reference @superpowers:test-driven-development for the red→green discipline (here expressed as Chrome-observed FAIL→PASS) and @superpowers:verification-before-completion before any "done" claim.

---

### Task 1: Verify the M1 `/onboarding/status` payload is live (precondition gate)

This task is a guard, not a code change — M2 cannot render rows that don't exist in the payload. Confirm the endpoint returns the contracted shape before building the consumer.

**Files:** none (read-only verification).

1. Confirm the service is up and the endpoint responds with the contracted shape:
   ```bash
   curl -s http://localhost:9091/onboarding/status | python3 -m json.tool | head -60
   ```
   **Expected:** a JSON object with top-level keys `ready_count`, `total`, `is_complete`, `sections`, `attention`. `sections` is an array; each element has `key`, `group`, `label`, `state`, `required`, `summary`, `step`, `skipped`, `items`. The top-level `attention` is an array of global attention rows; each element has (at minimum) `state`, `summary`, and a deep-link target (`section` — which may be `null` for machine-wide rows — and/or `cta_step`, e.g. the restart-drift row with `section=null` / `cta_step="api_keys"`).
2. Confirm all 10 section keys are present and `state` only ever uses the contracted vocabulary:
   ```bash
   curl -s http://localhost:9091/onboarding/status \
     | python3 -c 'import sys,json; d=json.load(sys.stdin); print(sorted(s["key"] for s in d["sections"])); print(sorted({s["state"] for s in d["sections"]}))'
   ```
   **Expected first line (sorted):** `['api_keys', 'cli_agents', 'embeddings', 'image', 'operator', 'optional_integrations', 'pair_phone', 'tailscale', 'transcription', 'web_search']`
   **Expected second line:** a subset of `['attention', 'checking', 'optional', 'ready']`.
3. Inspect the shape of the top-level global `attention[]` so Task 6 can render it (this is the array surfaced by the new done-screen global rows):
   ```bash
   curl -s http://localhost:9091/onboarding/status \
     | python3 -c 'import sys,json; d=json.load(sys.stdin); a=d.get("attention",[]); print("count", len(a)); print(a[:2])'
   ```
   **Expected:** `attention` is a list (possibly empty); each element carries a `summary` string and a deep-link target (`section` and/or `cta_step`). Note the exact field names observed — Task 6 reads `section`/`cta_step` to build the row's `href`.
4. If either assertion in step 2 fails, STOP — M1 is not actually merged/live. Do not proceed. (No commit — this is a gate.)

---

### Task 2: VERIFY the shared `Portal/onboarding/util.js` exists (M0-owned — import only)

`Portal/onboarding/util.js` and its `cssEscape` helper are **created by M0** (sole creator per the shared-ownership contract). M2 must NOT create or re-inline them — it only confirms M0's file is present so later/this-milestone code can import from it. `done.js` does not yet need `cssEscape` for selectors, but this verify gate keeps M2 honest about its dependency on M0.

**Files:** none (read-only verification).

1. Confirm M0's util module exists and exports `cssEscape` (regex-replace fallback form):
   ```bash
   test -f Portal/onboarding/util.js && \
   grep -nE "export function cssEscape|CSS\.escape|replace\(/\[\^a-zA-Z0-9_-\]/" Portal/onboarding/util.js
   ```
   **Expected:** the file exists and `export function cssEscape` plus the `CSS.escape` native-path and the regex-replace fallback all appear.
2. Confirm it parses as a module (no syntax error):
   ```bash
   node --input-type=module -e 'import("./Portal/onboarding/util.js").then(m=>console.log(typeof m.cssEscape))'
   ```
   **Expected output:** `function`
3. If the file is missing, STOP — M0 is not merged/live. Do not create `util.js` here (that is M0's sole responsibility; creating a second copy would fork the shared helper). Flag the missing M0 precondition to the user. (No commit — this is a verify-only gate.)

---

### Task 3: VERIFY the `--ob-attention` / `--ob-state-*` tokens exist in onboarding-tokens.css (M0-owned — consume only)

The new summary rows are styled by `state` (`ready`/`attention`/`optional`/`checking`), which the contract maps onto `--ob-state-*` tokens. These tokens — along with `--ob-attention` (and the `--ob-warning`→amber repoint and `--ob-radius-*`) — are **defined by M0**, the sole creator. M2 must NOT add or redefine them; it only confirms they are present so the CSS in Task 4 resolves.

**Files:** none (read-only verification).

1. Confirm the attention + state tokens M2 consumes are present (M0 wrote them):
   ```bash
   grep -nE "ob-attention|ob-state-(ready|attention|optional|checking)" Portal/onboarding/onboarding-tokens.css
   ```
   **Expected:** `--ob-attention` and all four `--ob-state-*` declarations appear (M0-defined). For reference, M0's canonical values are:
   ```css
       /* Status-state vocabulary (shared by done summary, hub tiles). — defined in M0 */
       --ob-attention: #f5a623;
       --ob-state-ready: var(--ob-success);
       --ob-state-attention: var(--ob-attention);
       --ob-state-optional: var(--ob-text-tertiary);
       --ob-state-checking: var(--ob-text-tertiary);
   ```
2. Confirm all four `--ob-state-*` lines are present:
   ```bash
   grep -nE "ob-state-(ready|attention|optional|checking)" Portal/onboarding/onboarding-tokens.css
   ```
   **Expected:** all four `--ob-state-*` lines present.
3. If any are missing, STOP — M0 is not merged/live. Do NOT add the tokens here (M0 is their sole creator; adding a duplicate `:root` declaration forks the source of truth). Flag the missing M0 precondition to the user. (No commit — verify-only gate.)

---

### Task 4: Add CSS for clickable summary rows + "Set up →" CTA + state pips

The current `.ob-summary-row` is a `<li>` with hardcoded `ok`/`skip`/`warn` color rules (`onboarding.css:1746-1789`). Add new rules so a row can be an `<a>` link, carry a `data-state`, and host a `.ob-summary-cta`. Keep the old `.ob-summary-{ok,skip,warn}` rules in place (do not delete — completion-error path and any other caller may still use them; deleting is M5's CSS-sweep job, not M2's). This task **consumes** the M0-defined `--ob-state-*` tokens (verified in Task 3); it does not define them.

**Files:**
- `Portal/onboarding/onboarding.css` (insert after the `.ob-summary-error` block ending at line 1794)

1. Insert this block (uses the M0-defined `--ob-state-*` tokens verified in Task 3):
   ```css
   /* ── M2: clickable, state-driven summary rows (done.js + hub) ──────── */
   /* Row is an <a> linking to /onboarding/?step=<key>. data-state drives
      the glyph + left accent. summary text comes verbatim from the backend
      /onboarding/status rollup (presentational only — no derivation here). */
   a.ob-summary-row {
       text-decoration: none;
       color: inherit;
       border-left: 2px solid transparent;
       padding-left: var(--ob-space-3);
       transition: background 120ms ease, border-color 120ms ease;
   }
   a.ob-summary-row:hover,
   a.ob-summary-row:focus-visible {
       background: var(--ob-surface-elevated);
       outline: none;
   }
   a.ob-summary-row[data-state="ready"]     { border-left-color: var(--ob-state-ready); }
   a.ob-summary-row[data-state="attention"] { border-left-color: var(--ob-state-attention); }
   a.ob-summary-row[data-state="optional"]  { border-left-color: var(--ob-state-optional); }
   a.ob-summary-row[data-state="checking"]  { border-left-color: var(--ob-state-checking); }

   a.ob-summary-row[data-state="ready"]     .ob-summary-glyph { color: var(--ob-state-ready); }
   a.ob-summary-row[data-state="attention"] .ob-summary-glyph { color: var(--ob-state-attention); }
   a.ob-summary-row[data-state="optional"]  .ob-summary-glyph { color: var(--ob-state-optional); }
   a.ob-summary-row[data-state="checking"]  .ob-summary-glyph { color: var(--ob-state-checking); }

   /* "Set up →" CTA — only rendered on attention rows. Pushes to the row's
      trailing edge; the row grid's 1fr detail column absorbs the slack. */
   .ob-summary-cta {
       justify-self: end;
       align-self: center;
       font-family: var(--ob-font-body);
       font-size: var(--ob-text-xs);
       font-weight: 600;
       color: var(--ob-state-attention);
       white-space: nowrap;
   }
   a.ob-summary-row:hover .ob-summary-cta { text-decoration: underline; }
   ```
2. The row grid is `grid-template-columns: 2.5rem auto 1fr` (line 1748). The CTA occupies the `1fr` track alongside the detail. To give the CTA its own track on attention rows, add a modifier so the grid expands to a 4th column when a CTA is present:
   ```css
   a.ob-summary-row.ob-summary-has-cta {
       grid-template-columns: 2.5rem auto 1fr auto;
   }
   ```
3. Verify the rules parse (no unbalanced braces) — quick structural check:
   ```bash
   grep -c "ob-summary-cta\|ob-summary-row\[data-state" Portal/onboarding/onboarding.css
   ```
   **Expected:** `≥ 9`.
4. Commit:
   ```bash
   git add Portal/onboarding/onboarding.css && \
   git commit -m "feat(onboarding): CSS for clickable state-driven summary rows + Set up CTA

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
   ```

---

### Task 5: SAFE RESTART POINT — restart so token + CSS edits serve

Prod serves the Portal from the working tree, but `onboarding-tokens.css` / `onboarding.css` are static and will already serve fresh on hard-reload — a restart is not strictly required for CSS. However, to give the subsequent Chrome-verification tasks a clean, fully-current baseline (and to confirm nothing M0/M1-side regressed), do a named restart here.

**Files:** none.

1. Restart (pre-authorized per project memory — do not prompt):
   ```bash
   sudo systemctl restart blackbox.service
   ```
2. Wait for warm-up (60–90s; the snapshot index rebuilds), then confirm health:
   ```bash
   for i in $(seq 1 60); do curl -sf http://localhost:9091/health >/dev/null && echo UP && break; sleep 2; done
   ```
   **Expected:** `UP` within the loop.
3. Confirm the endpoint still answers post-restart:
   ```bash
   curl -s http://localhost:9091/onboarding/status | python3 -c 'import sys,json;d=json.load(sys.stdin);print("sections",len(d["sections"]))'
   ```
   **Expected:** `sections 10`.
   (No commit — restart only.)

---

### Task 6: Replace `loadSummary()` + `renderSummary()` to consume `/onboarding/status` (incl. global attention rows)

This is the core change. Rewrite the data source and renderer in `done.js` so the summary list is built from the backend rollup, with each row a link, attention rows getting a CTA, and all 10 sections appearing. **In addition**, render the top-level global `attention[]` rows (e.g. the restart-drift row with `section=null` / `cta_step="api_keys"`) above the per-section list so machine-wide attention items are not dropped. Preserve every other behavior (the `Open Portal` CTA still enables after the summary loads; restart/logs/updates wiring untouched).

**Files:**
- `Portal/onboarding/steps/done.js:62-64` (call site), `:160-265` (functions to replace), `:572-580` (keep `escapeHtml`)

1. Replace the `loadSummary()` call + render in `render()` (lines 62-64). Old:
   ```javascript
       // Fetch summary data + render
       const summary = await loadSummary();
       renderSummary(container, summary);
   ```
   New:
   ```javascript
       // Fetch summary data + render. Source is the backend /onboarding/status
       // rollup (M1) — the single source of the section list, so done shows
       // every section (incl. embeddings/STT/web_search/image/agents) and each
       // row deep-links to its step. Global attention[] rows (e.g. restart-drift,
       // section=null) render above the per-section list. Presentational only:
       // no derivation here.
       const status = await loadStatus();
       renderSummary(container, status);
   ```
2. Replace the entire `loadSummary()` function (lines 160-173) with `loadStatus()`:
   ```javascript
   async function loadStatus() {
       try {
           const r = await fetch("/onboarding/status");
           if (!r.ok) return { error: `/onboarding/status returned ${r.status}` };
           return await r.json();
       } catch (e) {
           return { error: e.message };
       }
   }
   ```
3. Replace the entire `renderSummary()` function (lines 175-254) with a version that maps the rollup's `sections` to rows AND renders the global `attention[]` rows above them:
   ```javascript
   function renderSummary(container, status) {
       const summaryEl = container.querySelector("#ob-done-summary");
       if (!status || status.error || !Array.isArray(status.sections)) {
           summaryEl.innerHTML = `
               <p class="ob-step-helper">
                   Couldn't load the summary (${escapeHtml((status && status.error) || "unknown error")}).
                   Setup is still complete &mdash; clicking Open Portal works.
               </p>
           `;
           return;
       }

       // Global (machine-wide) attention rows — e.g. the restart-drift row with
       // section=null / cta_step="api_keys". Rendered as their own list above the
       // per-section rollup so they aren't silently dropped. Empty array → no list.
       const globalAttention = Array.isArray(status.attention) ? status.attention : [];
       const globalRows = globalAttention.map(globalAttentionRow).join("");
       const globalBlock = globalRows
           ? `<ul class="ob-summary-list ob-summary-global">${globalRows}</ul>`
           : "";

       const rows = status.sections.map(summaryRow).join("");
       summaryEl.innerHTML = `
           ${globalBlock}
           <ul class="ob-summary-list">
               ${rows}
           </ul>
       `;
   }
   ```
4. Replace the entire `summaryRow()` function (lines 256-265). It now takes a section object from the rollup and emits a link `<li><a>`, and add a sibling `globalAttentionRow()` for the top-level `attention[]` entries:
   ```javascript
   // Maps one /onboarding/status section to a clickable summary row.
   // The <li> keeps .ob-summary-list grid semantics; the <a> is the row.
   // Glyph + accent derive ONLY from section.state (presentational mapping);
   // the one-line summary string is rendered verbatim from the backend.
   const STATE_GLYPH = {
       ready: "&check;",
       attention: "!",
       optional: "&#8856;",   // ⊘
       checking: "&hellip;",
   };

   function summaryRow(section) {
       const state = section.state || "optional";
       const glyph = STATE_GLYPH[state] || "&#8856;";
       const label = section.label || section.key || "";
       const detail = section.summary || "";
       const href = `/onboarding/?step=${encodeURIComponent(section.step || section.key)}`;
       const showCta = state === "attention";
       const ctaClass = showCta ? " ob-summary-has-cta" : "";
       const cta = showCta
           ? `<span class="ob-summary-cta" aria-hidden="true">Set up &rarr;</span>`
           : "";
       return `
           <li>
               <a class="ob-summary-row${ctaClass}" data-state="${escapeHtml(state)}"
                  href="${escapeHtml(href)}">
                   <span class="ob-summary-glyph" aria-hidden="true">${glyph}</span>
                   <span class="ob-summary-label">${escapeHtml(label)}</span>
                   <span class="ob-summary-detail">${escapeHtml(detail)}</span>
                   ${cta}
               </a>
           </li>
       `;
   }

   // Maps one top-level /onboarding/status attention[] entry (a machine-wide row,
   // possibly with section=null) to a clickable row. The deep-link target is
   // resolved from cta_step first, then section, falling back to api_keys so the
   // row is always actionable. These rows always render their state glyph + a
   // "Set up →" CTA (they exist precisely because something needs attention).
   function globalAttentionRow(att) {
       const state = att.state || "attention";
       const glyph = STATE_GLYPH[state] || "!";
       const detail = att.summary || "";
       const target = att.cta_step || att.section || "api_keys";
       const href = `/onboarding/?step=${encodeURIComponent(target)}`;
       const cta = `<span class="ob-summary-cta" aria-hidden="true">Set up &rarr;</span>`;
       return `
           <li>
               <a class="ob-summary-row ob-summary-has-cta" data-state="${escapeHtml(state)}"
                  href="${escapeHtml(href)}">
                   <span class="ob-summary-glyph" aria-hidden="true">${glyph}</span>
                   <span class="ob-summary-label">Needs attention</span>
                   <span class="ob-summary-detail">${escapeHtml(detail)}</span>
                   ${cta}
               </a>
           </li>
       `;
   }
   ```
   Note: `escapeHtml` (lines 572-580) stays; the `STATE_GLYPH` constant goes immediately above `summaryRow`, and `globalAttentionRow` directly below it. The old `loadSummary`'s dependence on `/current-config` + `/state` is removed — no other function in `done.js` calls them (the restart/logs/updates paths use their own endpoints), so this is safe.
5. Confirm the module still parses:
   ```bash
   node --input-type=module -e 'import("./Portal/onboarding/steps/done.js").then(()=>console.log("ok")).catch(e=>{console.error(e.message);process.exit(1)})'
   ```
   **Expected:** `ok` (the module imports `/ui/modules/updates-manager.js` lazily inside a handler, so top-level import succeeds).
6. Commit:
   ```bash
   git add Portal/onboarding/steps/done.js && \
   git commit -m "feat(onboarding): done summary sources rows from /onboarding/status

Clickable per-section rows linking to ?step=<key>, attention rows gain a
Set up CTA, and the previously-omitted sections (embeddings, transcription,
web_search, image, cli_agents) now appear automatically from the rollup.
Top-level global attention[] rows (e.g. restart-drift, section=null /
cta_step=api_keys) render above the per-section list. Completion behavior
(sentinel, restart, logs, updates) unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
   ```

---

### Task 7: Chrome-verify — all 10 rows render with correct states

Verify the live `done` screen in Chrome. The `done` step is the terminal step, reachable via the revisit deep-link `?step=done` (which renders the step without mutating state — see `onboarding.js:42,275-283`). This avoids needing to walk the whole funnel.

**Files:** none (verification).

1. Ensure the Claude-in-Chrome extension is connected (load tools with one ToolSearch call: `tabs_context_mcp,navigate,computer,read_page,find`).
2. Navigate to the done step directly:
   ```
   mcp__claude-in-chrome__navigate → http://localhost:9091/onboarding/?step=done
   ```
3. `read_page` (or `find`) and assert the DOM:
   - There is exactly one `ul.ob-summary-list` (the per-section list) — plus, if the payload has global attention items, a leading `ul.ob-summary-list.ob-summary-global`.
   - The per-section list contains **10** `a.ob-summary-row` elements (one per section).
   - The set of `data-state` values is a subset of `{ready, attention, optional, checking}` and matches the curl from Task 1 step 2.
   - Each per-section `a.ob-summary-row` has an `href` of the form `/onboarding/?step=<key>` with `<key>` ∈ the 10 section keys.
   - If the backend `attention[]` is non-empty (Task 1 step 3), assert the `ob-summary-global` list contains one `a.ob-summary-row.ob-summary-has-cta` per global attention entry, each with a "Set up →" CTA and an `href` matching its `cta_step`/`section` (e.g. the restart-drift row links to `?step=api_keys`).
4. Cross-check the per-section row count against the backend so the payload is validated end-to-end:
   ```bash
   curl -s http://localhost:9091/onboarding/status | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["sections"]))'
   ```
   **Expected:** `10` — equal to the rendered per-section row count.
5. Screenshot (`computer` screenshot) the summary list. **Assert visually:** ready rows show a green check + green left accent; optional rows show a dim ⊘; any attention row (per-section or global) shows an amber `!` + amber left accent + a "Set up →" label at the row's trailing edge.
6. If the count or states mismatch, debug per @superpowers:systematic-debugging (check console via `read_console_messages` for fetch errors) before claiming pass.
7. Commit the verification artifact only if a screenshot file is produced for the record; otherwise no commit (pure verification). If saving:
   ```bash
   git add docs/onboarding/verify/ 2>/dev/null && git commit -m "docs(onboarding): M2 done-summary Chrome verification screenshot

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C" || echo "no artifact to commit"
   ```

---

### Task 8: Chrome-verify — clicking a row navigates to the right step

Confirm the deep-link wiring actually round-trips. The `?step=<key>` revisit path is state-neutral (`onboarding.js:42`) and renders the target step.

**Files:** none (verification).

1. On the `?step=done` page from Task 7, use `find` to locate the `a.ob-summary-row` whose `href` ends with `step=api_keys` (a required section guaranteed to exist).
2. `computer` click that row.
3. `read_page` and assert the URL is now `http://localhost:9091/onboarding/?step=api_keys` and the rendered step is the API Keys step (assert a known api_keys DOM marker, e.g. the step title or a `data-provider` card — confirm by reading `steps/api_keys.js`'s root element during verification).
4. Navigate back to `?step=done` and repeat the click assertion for one capability row that the OLD `done.js` omitted — e.g. the row whose `href` ends with `step=embeddings`. Assert it lands on the embeddings step (look for a `.ob-cli-agent-card[data-slug=...]` element, which `embeddings.js:338` renders). This proves the newly-added rows are real, navigable sections.
5. If a global attention row was present in Task 7, navigate back to `?step=done`, click it, and assert it lands on its `cta_step` target (e.g. `?step=api_keys`). This proves the global rows are actionable, not decorative.
6. Check `read_console_messages` for errors during navigation. **Expected:** no uncaught errors; no failed `/onboarding/status` request.
7. No commit (pure verification).

---

### Task 9: Chrome-verify — completion behavior intact (Open Portal, Restart, Logs)

Regression guard: confirm M2 did not break the existing `done`-screen affordances.

**Files:** none (verification).

1. On `http://localhost:9091/onboarding/?step=done`, assert via `find`/`read_page`:
   - `#ob-done-open` ("Open Portal →") exists and is **enabled** (the `render()` flow still sets `openBtn.disabled = false` after `loadStatus()` resolves — confirm it's not stuck disabled, which would indicate `loadStatus()` threw).
   - `#ob-done-restart` (Restart Service) exists and is visible (the restart button is always visible per `renderRestartState`).
   - `#ob-view-logs-btn` ("View Logs") exists.
2. `computer` click `#ob-view-logs-btn`; assert the `#ob-logs-modal` appears and `#ob-logs-modal-status` transitions from "Connecting…" to "Connected" (SSE from `/onboarding/logs/stream` still works — unrelated to M2 but must not regress). Close it (Esc).
3. Do **NOT** click "Open Portal" during verification (it writes the `.onboarding_complete` sentinel and redirects to `/ui`, which would change machine state). Instead assert the button's wiring exists by confirming it is enabled and has the label "Open Portal".
4. `read_console_messages`: **Expected** no errors.
5. No commit (pure verification).

---

### Task 10: Chrome-verify the live SSE refinement is NOT in scope here (boundary check) + snapshot

M2 consumes only the fast `GET /onboarding/status` (persisted read); the live `GET /onboarding/status/stream` SSE refinement is an M3 hub concern. Confirm `done.js` does not open the stream (no `checking…` flicker on the done screen — rows render in their resolved state on first paint), then snapshot the milestone.

**Files:** none (verification + snapshot).

1. Reload `http://localhost:9091/onboarding/?step=done`; via `read_network_requests` assert that `/onboarding/status` was requested and `/onboarding/status/stream` was **NOT** requested (M2 stays on the fast read; the SSE live-fill is M3's job). This keeps the milestone boundary clean.
2. Confirm no row is stuck in the `checking` state after load (the fast read returns resolved states): assert no `a.ob-summary-row[data-state="checking"]` remains after the page settles. ASSUMPTION: per the M1 contract, `GET /onboarding/status` never returns `checking` (that state only arrives over the SSE stream); if the rollup does emit `checking`, treat a transient pip as acceptable but flag it to the user as a possible M1 contract deviation.
3. Run the relevant backend test suite to confirm M1's rollup tests still pass (M2 touched no backend, but this is the verification gate before snapshot):
   ```bash
   Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_steps_parity.py -q
   ```
   **Expected:** all pass (matches M1's established invocation — ASSUMPTION: confirm the exact test module name against `Orchestrator/tests/` and any `pytest.ini`/`conftest.py` discovered in M1; the standardized invocation is `Orchestrator/venv/bin/pytest` run from the repo root).
4. Snapshot the milestone via @snapshot-dev (operator resolved dynamically per `resolve-operator`):
   ```
   /snapshot-dev
   ```
   Record: "M2 done.js summary now sources clickable, state-driven rows from /onboarding/status; previously-omitted sections (embeddings/STT/web_search/image/agents) now appear; top-level global attention[] rows (e.g. restart-drift, section=null) render above the per-section list; attention rows get Set up → CTA; completion behavior intact; verify-only on M0-owned util.js + --ob-* tokens (no duplication); first end-to-end consumer validating the M1 payload."
5. This is the milestone's final commit boundary; the snapshot is the persistence point. No additional restart needed (Task 5 was the only required restart; CSS/JS serve from the tree on reload).

---

## Milestone M3: The Console Hub — `renderHub()` + `.ob-section-card` + readiness header + hub-first routing

This milestone assembles the console hub that the wizard always stubbed (`?mode=manage` → "Phase 2.10 — not yet implemented"). `onboarding.js` gains a `renderHub()` path (loaded as a hub module, `Portal/onboarding/hub.js`, via a **dynamic** `await import('./hub.js')` so a hub/status/util load error can never white-screen the linear first-run funnel) that paints the grouped 10-section structure + an "X of N ready" readiness header **immediately** from `GET /onboarding/status` (every tile `checking…`), then opens the SSE `/onboarding/status/stream` and live-fills each tile as its probe resolves. A new shared `.ob-section-card[data-state]` tile — distilled from the existing `.ob-cli-agent-card[data-state]` border-left-color pattern — renders label/group/summary + a `?step=<key>` revisit link. Routing flips so post-completion `GET /onboarding/` renders the hub (no `/ui` bounce), `?mode=manage` renders the hub, and `?step=` revisit returns to the hub on save instead of the old terminal panel. The presentational `status.js` (no derivation logic) is extracted; `cssEscape()` and `.ob-card-selected` are **owned by M0** — M3 only imports/verifies them.

This is **frontend-only** (Chrome-verified) and **depends on M1** having shipped `GET /onboarding/status` and `GET /onboarding/status/stream` with the shared contract payloads. It also consumes the `--ob-state-{ready,attention,optional,checking}` + `--ob-attention` tokens added in M0.

> **HUB DOM/INTERFACE CONTRACT (M3 authoritative — D4):** This milestone PINS the hub's public surface; M4 conforms to it (not the reverse).
> - `hub.js` exports `applySectionEvent(root, ev)` — the SSE per-section updater. M4 calls THAT (not `updateSectionCard`).
> - Hub DOM: container `.ob-hub`, groups `#ob-hub-groups`, attention `#ob-hub-attention`. M4 targets THESE.
> - The names `.ob-hub-shell` / `.ob-hub-header` are **BANNED** — do not introduce them. If M4 disagrees on any hub symbol/selector, **M3 wins** and M4 is rewritten to match.

> **SOLE-CREATOR boundaries (D3):** `Portal/onboarding/util.js` (with `cssEscape`, regex-replace form) and the `.ob-card-selected` rule (combined selector `.ob-cli-agent-card.ob-card-selected, .ob-section-card.ob-card-selected`) and ALL tokens (`--ob-attention`, `--ob-state-*`, `--ob-radius-*`, etc.) are **created by M0**. M3 is **IMPORT-ONLY / VERIFY-ONLY** for these — it MUST NOT re-add or redefine them.

> **ASSUMPTION (cross-milestone deps):** M1 is merged and live — `GET /onboarding/status` returns `{ready_count, total, is_complete, sections:[{key,group,label,state,required,summary,step,skipped,items}], attention:[{section,severity,message,cta_step}]}`, and `GET /onboarding/status/stream` emits `event: section` (`{key,state,summary,attention}`) per section then `event: done` (`{ready_count,total}`). M0 is merged — `onboarding-tokens.css` defines `--ob-attention`, `--ob-state-ready`, `--ob-state-attention`, `--ob-state-optional`, `--ob-state-checking`; `util.js` exports `cssEscape`; `.ob-card-selected` exists. If running M3 before those land, stub `/onboarding/status` with a static fixture first (Task 1's verification will reveal the gap).

> **CANONICAL SECTION ORDER (D2) — byte-identical everywhere a section list appears (= ALL_STEPS minus welcome/done):** `tailscale, api_keys, embeddings, optional_integrations, transcription, web_search, image, pair_phone, cli_agents, operator`. The 10 hub tiles render exactly these 10 sections.

> **Skills:** Use @superpowers:test-driven-development discipline conceptually (write the assertion first, see it fail in Chrome, then implement). Frontend has no JS harness — every task ends with an explicit Claude-in-Chrome assertion + a commit. Use @superpowers:verification-before-completion before any "works" claim.

---

### Task 1 — Add `.ob-section-card[data-state]` CSS (distilled from `.ob-cli-agent-card`)

Add the new shared console tile primitive to `onboarding.css`, distilled from the existing `.ob-cli-agent-card[data-state]` border-left-color pattern (lines 2398–2451) but keyed on the 4 canonical states using the M0 tokens. This is CSS-only; nothing renders it yet, so verification is "the rule parses and the selector resolves."

**Files:**
- `Portal/onboarding/onboarding.css` (append after the cli-agent block, after line ~2560)

**Steps:**
1. Append this block to the end of `Portal/onboarding/onboarding.css`:
```css
/* ════════════════════════════════════════════════════════════════
 * Console hub (M3) — .ob-section-card grouped tiles + readiness header
 * Distilled from .ob-cli-agent-card[data-state]: one attribute switches
 * the border-left accent. States are the 4 canonical ones (tokens from
 * onboarding-tokens.css, added in M0). No new color literals here.
 * NOTE: .ob-card-selected is OWNED BY M0 — do NOT define it here.
 * ════════════════════════════════════════════════════════════════ */
.ob-hub {
    flex: 1;
    max-width: var(--ob-content-wide);
    width: 100%;
    margin: 0 auto;
    padding: var(--ob-space-12) var(--ob-space-10) var(--ob-space-16);
    position: relative;
    z-index: 1;
    animation: ob-fade-up var(--ob-motion-moment) var(--ob-ease-out) backwards;
}
.ob-hub-head {
    display: flex;
    flex-direction: column;
    gap: var(--ob-space-2);
    margin-bottom: var(--ob-space-10);
}
.ob-hub-title {
    font-family: var(--ob-font-display);
    font-weight: var(--ob-display-weight-light);
    font-size: clamp(2.2rem, 5vw, var(--ob-text-3xl));
    letter-spacing: -0.03em;
    font-variation-settings: "opsz" 144, "SOFT" var(--ob-display-soft);
    color: var(--ob-text-primary);
}
.ob-hub-title em {
    font-style: italic;
    color: var(--ob-accent);
    font-weight: var(--ob-display-weight-regular);
}
.ob-hub-readiness {
    font-family: var(--ob-font-body);
    font-size: var(--ob-text-sm);
    color: var(--ob-text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.12em;
}
.ob-hub-readiness em {
    font-style: normal;
    color: var(--ob-state-ready);
    font-weight: 600;
}

/* Attention header — derived warn/error rows with a "Set up →" CTA */
.ob-hub-attention {
    display: flex;
    flex-direction: column;
    gap: var(--ob-space-2);
    margin-bottom: var(--ob-space-10);
}
.ob-attention-row {
    display: flex;
    align-items: center;
    gap: var(--ob-space-4);
    padding: var(--ob-space-3) var(--ob-space-5);
    background: var(--ob-accent-soft);
    border-left: 3px solid var(--ob-attention);
    font-family: var(--ob-font-body);
    font-size: var(--ob-text-sm);
    color: var(--ob-text-primary);
}
.ob-attention-row[data-severity="error"] {
    border-left-color: var(--ob-error);
}
.ob-attention-msg { flex: 1; }
.ob-attention-cta {
    color: var(--ob-attention);
    text-decoration: none;
    white-space: nowrap;
    border-bottom: 1px solid currentColor;
    font-weight: 500;
}
.ob-attention-cta:hover { opacity: 0.75; }

/* Grouped sections */
.ob-hub-group { margin-bottom: var(--ob-space-10); }
.ob-hub-group-label {
    font-family: var(--ob-font-body);
    font-size: var(--ob-text-xs);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    color: var(--ob-text-tertiary);
    margin-bottom: var(--ob-space-4);
    padding-bottom: var(--ob-space-2);
    border-bottom: 1px solid var(--ob-surface-border);
}
.ob-section-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: var(--ob-space-4);
}

/* The tile — one attribute (data-state) switches the border-left accent */
.ob-section-card {
    display: block;
    text-decoration: none;
    background: var(--ob-surface-elevated);
    border: 1px solid var(--ob-surface-border);
    border-left-width: 3px;
    border-left-color: var(--ob-state-optional);
    padding: var(--ob-space-4) var(--ob-space-5);
    transition: border-color var(--ob-motion-fast) var(--ob-ease-out),
                background var(--ob-motion-fast) var(--ob-ease-out),
                transform var(--ob-motion-fast) var(--ob-ease-out);
}
.ob-section-card:hover {
    border-color: var(--ob-surface-border-emphasis);
    background: var(--ob-surface-card-hover);
    transform: translateY(-2px);
}
.ob-section-card:focus-visible {
    outline: 2px solid var(--ob-text-primary);
    outline-offset: 4px;
}
.ob-section-card[data-state="ready"]     { border-left-color: var(--ob-state-ready); }
.ob-section-card[data-state="attention"] { border-left-color: var(--ob-state-attention); }
.ob-section-card[data-state="optional"]  { border-left-color: var(--ob-state-optional); }
.ob-section-card[data-state="checking"]  { border-left-color: var(--ob-state-checking); }

.ob-section-card-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: var(--ob-space-3);
    margin-bottom: var(--ob-space-2);
}
.ob-section-card-label {
    font-family: var(--ob-font-display);
    font-weight: var(--ob-display-weight-medium);
    font-size: var(--ob-text-lg);
    color: var(--ob-text-primary);
    font-variation-settings: "opsz" 36, "SOFT" var(--ob-display-soft);
}
.ob-section-card-pip {
    font-family: var(--ob-font-body);
    font-size: var(--ob-text-xs);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ob-text-tertiary);
    white-space: nowrap;
}
.ob-section-card[data-state="ready"]     .ob-section-card-pip { color: var(--ob-state-ready); }
.ob-section-card[data-state="attention"] .ob-section-card-pip { color: var(--ob-state-attention); }
.ob-section-card-summary {
    font-family: var(--ob-font-body);
    font-size: var(--ob-text-sm);
    line-height: 1.5;
    color: var(--ob-text-secondary);
    min-height: 1.4em;
}
/* checking… skeleton shimmer on the summary line */
.ob-section-card[data-state="checking"] .ob-section-card-summary {
    color: var(--ob-text-tertiary);
    opacity: 0.7;
}

@media (max-width: 720px) {
    .ob-hub { padding: var(--ob-space-8) var(--ob-space-6); }
    .ob-section-grid { grid-template-columns: 1fr; }
}
```

2. **Verify (Chrome):** Navigate `http://localhost:9091/onboarding/onboarding.css` and confirm it loads (200). Then in DevTools-equivalent via `mcp__claude-in-chrome__javascript_tool` on `http://localhost:9091/onboarding/?step=welcome`, run:
```js
const s = document.createElement('div'); s.className='ob-section-card'; s.setAttribute('data-state','ready'); document.body.appendChild(s);
const c = getComputedStyle(s).borderLeftColor; s.remove(); c;
```
Assert the returned color is the green (`rgb(39, 217, 128)` — `--ob-state-ready`/`--ob-success`), proving the token + selector resolve. If it returns a fallback/black, M0 tokens are missing — stop and confirm M0 landed.

3. **Commit:** `git add Portal/onboarding/onboarding.css && git commit -m "feat(onboarding): add .ob-section-card[data-state] hub tile CSS"` (ends with the Co-Authored-By + Claude-Session trailers).

App remains launchable (additive CSS only).

---

### Task 2 — VERIFY M0's `Portal/onboarding/util.js` (`cssEscape`) + adopt it in the dedup sites

`util.js` and its `cssEscape` are **owned and created by M0** (D3). M3 does NOT re-create them. This task (a) verifies M0's version exists and exports `cssEscape` in the regex-replace form, then (b) de-dupes the two identical local `cssEscape()` copies (`image.js:305`, `web_search.js:311`) by importing the shared module — which the hub also imports.

**Files:**
- `Portal/onboarding/util.js` (M0-owned — VERIFY ONLY, do not create/edit)
- `Portal/onboarding/steps/image.js:303-307`
- `Portal/onboarding/steps/web_search.js:309-313`

**Steps:**
1. **VERIFY (M0 sole-creator guard):** confirm M0's `util.js` exists and exports `cssEscape` in the regex-replace form. If this fails, M0 has not landed — STOP (do not create the file here; that would re-create an M0-owned artifact):
```bash
grep -n 'export function cssEscape' Portal/onboarding/util.js \
  && grep -n 'replace(/\[\^a-zA-Z0-9_-\]/g' Portal/onboarding/util.js \
  || echo "MISSING — M0 util.js not landed; STOP"
```
   Expected M0 content (for reference only — do **not** write it; it is already there):
```js
export function cssEscape(s) {
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}
```
2. In `Portal/onboarding/steps/image.js`, delete the local `cssEscape` (lines 303–307) and add at the top of the module (after the file's opening comment):
```js
import { cssEscape } from "../util.js";
```
3. Same edit in `Portal/onboarding/steps/web_search.js` (delete lines 309–313, add the import).
4. **Verify (Chrome):** Navigate `http://localhost:9091/onboarding/?step=image`, screenshot, assert the provider grid renders (cards visible, no console error). Then `read_console_messages` and assert there is **no** `cssEscape is not defined` / module-load error. Repeat for `?step=web_search`. The card-selection toggle still works (click a provider checkbox; the card highlights) — proves `cssEscape` resolves the attribute selector.
5. **Commit:** `git add Portal/onboarding/steps/image.js Portal/onboarding/steps/web_search.js && git commit -m "refactor(onboarding): adopt M0 shared util.js cssEscape in image+web_search"` (do NOT stage `util.js` — it is M0's, unchanged here).

App launchable (image + web_search still render).

---

### Task 3 — Create presentational `Portal/onboarding/status.js` (no derivation)

Add the presentational layer the hub renders from: pure functions that consume the M1 `/onboarding/status` payload and return HTML strings. **No derivation logic** — state/summary/attention all come pre-computed from the backend. This is the contract's `status.js`. It imports `cssEscape` from M0's `util.js`.

**Files:**
- `Portal/onboarding/status.js` (new)

**Steps:**
1. Create `Portal/onboarding/status.js`:
```js
// PRESENTATIONAL ONLY. Consumes GET /onboarding/status (M1 backend rollup).
// Contains NO derivation logic — state, summary, and attention rows are all
// pre-computed server-side. This file just turns the payload into HTML.

import { cssEscape } from "./util.js";

const GROUP_ORDER = ["network", "keys", "capabilities", "identity"];
const GROUP_LABELS = {
    network: "Network & Access",
    keys: "Keys & Models",
    capabilities: "Capabilities",
    identity: "Identity",
};

const PIP_TEXT = {
    ready: "✓ Ready",
    attention: "⚠ Attention",
    optional: "– Optional",
    checking: "Checking…",
};

export function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

// One tile. `state`/`summary` come straight from the payload.
export function sectionCardHtml(section) {
    const state = section.state || "checking";
    const summary = section.summary || (state === "checking" ? "Checking…" : "");
    const href = `/onboarding/?step=${encodeURIComponent(section.key)}`;
    return `
        <a class="ob-section-card" href="${href}"
           data-section="${escapeHtml(section.key)}" data-state="${escapeHtml(state)}">
            <div class="ob-section-card-head">
                <span class="ob-section-card-label">${escapeHtml(section.label)}</span>
                <span class="ob-section-card-pip">${escapeHtml(PIP_TEXT[state] || PIP_TEXT.checking)}</span>
            </div>
            <div class="ob-section-card-summary">${escapeHtml(summary)}</div>
        </a>
    `;
}

// Grouped section grids, in canonical group order. Within a group, sections
// preserve the canonical section order delivered by the M1 payload (D2:
// tailscale, api_keys, embeddings, optional_integrations, transcription,
// web_search, image, pair_phone, cli_agents, operator).
export function groupsHtml(sections) {
    return GROUP_ORDER.map((group) => {
        const inGroup = sections.filter((s) => s.group === group);
        if (inGroup.length === 0) return "";
        return `
            <div class="ob-hub-group" data-group="${group}">
                <div class="ob-hub-group-label">${escapeHtml(GROUP_LABELS[group] || group)}</div>
                <div class="ob-section-grid">
                    ${inGroup.map(sectionCardHtml).join("")}
                </div>
            </div>
        `;
    }).join("");
}

// Readiness meter line.
export function readinessHtml(readyCount, total) {
    return `<em>${escapeHtml(String(readyCount))}</em> of ${escapeHtml(String(total))} ready`;
}

// Attention header rows (warn/error), each with a "Set up →" CTA.
export function attentionHtml(attention) {
    if (!attention || attention.length === 0) return "";
    return attention.map((a) => {
        const sev = a.severity === "error" ? "error" : "warn";
        const cta = a.cta_step
            ? `<a class="ob-attention-cta" href="/onboarding/?step=${encodeURIComponent(a.cta_step)}">Set up &rarr;</a>`
            : "";
        return `
            <div class="ob-attention-row" data-severity="${sev}" data-section="${escapeHtml(a.section || "")}">
                <span class="ob-attention-msg">${escapeHtml(a.message)}</span>
                ${cta}
            </div>
        `;
    }).join("");
}

// Live SSE fill — update one tile in place from an `event: section` payload.
// This is the pinned hub updater (D4): applySectionEvent(root, ev).
export function applySectionEvent(root, ev) {
    if (!ev || !ev.key) return;
    const card = root.querySelector(`.ob-section-card[data-section="${cssEscape(ev.key)}"]`);
    if (!card) return;
    if (ev.state) card.setAttribute("data-state", ev.state);
    const pip = card.querySelector(".ob-section-card-pip");
    if (pip && ev.state) pip.textContent = PIP_TEXT[ev.state] || PIP_TEXT.checking;
    const sum = card.querySelector(".ob-section-card-summary");
    if (sum && typeof ev.summary === "string") sum.textContent = ev.summary;
}
```
2. **Verify (Chrome):** On `http://localhost:9091/onboarding/?step=welcome`, run via `javascript_tool`:
```js
const m = await import('/onboarding/status.js');
const html = m.groupsHtml([
  {key:'tailscale',group:'network',label:'Tailnet',state:'checking',summary:''},
  {key:'api_keys',group:'keys',label:'API Keys',state:'ready',summary:'2 configured'}
]);
[html.includes('data-group="network"'), html.includes('Tailnet'), html.includes('data-state="ready"')];
```
Assert `[true, true, true]`. Run `read_console_messages` and assert no import error.
3. **Commit:** `git add Portal/onboarding/status.js && git commit -m "feat(onboarding): presentational status.js for hub rendering"`.

App launchable (new module, not yet wired).

---

### Task 4 — Create `Portal/onboarding/hub.js` with `renderHub()` (skeleton paint from `/onboarding/status`)

The hub module: paints the grouped structure + readiness header **immediately** from `GET /onboarding/status` (tiles in whatever state the fast persisted read reports, summaries present). SSE fill is added in Task 5. Loaded **dynamically** by `onboarding.js` in Task 6 (`await import('./hub.js')`).

**PINNED PUBLIC SURFACE (D4 — M4 conforms to this):**
- `hub.js` exports `renderHub(container)`, `closeStream()`, and re-exports `applySectionEvent(root, ev)`.
- Hub DOM rendered into `container`: `<section class="ob-hub">` with `#ob-hub-readiness`, `#ob-hub-attention`, and `#ob-hub-groups`. (The names `.ob-hub-shell` / `.ob-hub-header` are banned.)

**Files:**
- `Portal/onboarding/hub.js` (new)

**Steps:**
1. Create `Portal/onboarding/hub.js`:
```js
// The console hub (M3). renderHub() paints grouped sections + a readiness
// header from the fast persisted GET /onboarding/status, then (Task 5) opens
// the SSE /status/stream to live-fill each tile. All HTML comes from the
// presentational status.js — this file is orchestration + the SSE lifecycle.
//
// PINNED INTERFACE (D4): exports renderHub/closeStream/applySectionEvent;
// container=.ob-hub, groups=#ob-hub-groups, attention=#ob-hub-attention.
// M4 conforms to these names — do not rename to ob-hub-shell/ob-hub-header.

import { groupsHtml, readinessHtml, attentionHtml, applySectionEvent } from "./status.js";
import { escapeHtml } from "./status.js";

let sse = null;  // active EventSource — closed on re-render / unmount

export async function renderHub(container) {
    closeStream();  // idempotent — never leak a prior stream
    container.innerHTML = `<div class="ob-loading">Loading console&hellip;</div>`;

    let data;
    try {
        const r = await fetch("/onboarding/status", { cache: "no-store" });
        if (!r.ok) throw new Error(`/onboarding/status returned ${r.status}`);
        data = await r.json();
    } catch (e) {
        container.innerHTML = `
            <div class="ob-step-error">
                <h2 class="ob-step-title">Console unavailable</h2>
                <p class="ob-step-lede">Couldn't load setup status. Check the service is running, then refresh.</p>
                <p class="ob-step-helper">Error: ${escapeHtml(e.message)}</p>
            </div>`;
        return;
    }

    const sections = data.sections || [];
    const readyCount = data.ready_count ?? 0;
    const total = data.total ?? sections.length;

    container.innerHTML = `
        <section class="ob-hub">
            <div class="ob-hub-head">
                <h1 class="ob-hub-title">Your <em>BlackBox</em> console.</h1>
                <div class="ob-hub-readiness" id="ob-hub-readiness">${readinessHtml(readyCount, total)}</div>
            </div>
            <div class="ob-hub-attention" id="ob-hub-attention">${attentionHtml(data.attention)}</div>
            <div class="ob-hub-groups" id="ob-hub-groups">${groupsHtml(sections)}</div>
        </section>
    `;

    openStream(container);
}

// SSE live-fill — added in Task 5.
function openStream(container) { /* Task 5 */ }

export function closeStream() {
    if (sse) {
        try { sse.close(); } catch (_) {}
        sse = null;
    }
}

// Re-export the pinned SSE updater (D4) for the SSE wiring + M4/tests.
export { applySectionEvent };
```
2. **Verify (Chrome):** Navigate `http://localhost:9091/onboarding/?step=welcome`, then via `javascript_tool`:
```js
const m = await import('/onboarding/hub.js');
const host = document.createElement('main'); document.body.appendChild(host);
await m.renderHub(host);
const r = {
  groups: host.querySelectorAll('.ob-hub-group').length,
  cards: host.querySelectorAll('.ob-section-card').length,
  hubGroups: !!host.querySelector('#ob-hub-groups'),
  hubAttention: !!host.querySelector('#ob-hub-attention'),
  readiness: host.querySelector('#ob-hub-readiness')?.textContent?.trim()
};
host.remove(); r;
```
Assert: `groups >= 1`, `cards === 10` (all 10 sections render — the canonical D2 set), `hubGroups === true`, `hubAttention === true` (pinned D4 DOM present), `readiness` matches `/\d+ of \d+ ready/`. Run `read_console_messages` — no errors. (Requires M1's `/onboarding/status` live; if it 404s, the error panel shows and `cards===0` — stop and confirm M1 landed.)
3. **Commit:** `git add Portal/onboarding/hub.js && git commit -m "feat(onboarding): renderHub() skeleton paint from /onboarding/status"`.

App launchable (module not yet referenced by the shell).

---

### Task 5 — Wire the SSE `/onboarding/status/stream` live-fill into `hub.js`

Implement `openStream()` so each tile live-fills as its server-side probe resolves, and the readiness meter updates on `event: done`. Mirror the `EventSource` lifecycle already used in `done.js`'s logs modal (open on view, close on re-render/unmount). The per-section update goes through the pinned `applySectionEvent(container, payload)` (D4).

**Files:**
- `Portal/onboarding/hub.js` (replace the `openStream()` stub)

**Steps:**
1. Replace the `function openStream(container) { /* Task 5 */ }` stub with:
```js
// Live re-validate: open the SSE stream and fill each tile as its probe
// resolves. Reuses the EventSource pattern from done.js's logs modal.
// Fired on view only (no timer). Closed by closeStream() on re-render.
function openStream(container) {
    let es;
    try {
        es = new EventSource("/onboarding/status/stream");
    } catch (_) {
        return;  // SSE unsupported — skeleton stays at persisted state, still usable
    }
    sse = es;

    es.addEventListener("section", (e) => {
        let payload;
        try { payload = JSON.parse(e.data); } catch (_) { return; }
        applySectionEvent(container, payload);  // pinned D4 updater
        // A section probe may carry its own attention rows; merge them in.
        if (Array.isArray(payload.attention)) {
            mergeAttention(container, payload.section || payload.key, payload.attention);
        }
    });

    es.addEventListener("done", (e) => {
        let payload;
        try { payload = JSON.parse(e.data); } catch (_) { payload = {}; }
        const meter = container.querySelector("#ob-hub-readiness");
        if (meter && payload.ready_count != null && payload.total != null) {
            meter.innerHTML = readinessHtml(payload.ready_count, payload.total);
        }
        closeStream();  // re-validation complete — no need to hold the connection
    });

    es.onerror = () => {
        // Stream dropped (or completed without a clean close). The persisted
        // skeleton remains valid; just stop. Don't surface an error — this is
        // a progressive enhancement over the fast read.
        closeStream();
    };
}

// Replace any prior attention rows for a section with the freshly-probed set,
// re-rendering the attention header. Keeps the header in sync with live state.
function mergeAttention(container, sectionKey, rows) {
    const host = container.querySelector("#ob-hub-attention");
    if (!host) return;
    // Drop existing rows for this section, then append the new ones.
    host.querySelectorAll(`.ob-attention-row[data-section="${cssEscape(sectionKey)}"]`)
        .forEach((el) => el.remove());
    host.insertAdjacentHTML("beforeend", attentionHtml(rows));
}
```
2. Update the imports at the top of `hub.js` to pull `cssEscape` from M0's shared `util.js` (used by `mergeAttention`). Do **not** define a local `cssEscapeLocal` — reuse the M0-owned helper (D3). Add to the import block:
```js
import { cssEscape } from "./util.js";
```
   `readinessHtml` and `attentionHtml` are already imported (from Task 4).
3. **Verify (Chrome):** Navigate `http://localhost:9091/onboarding/?mode=manage` (after Task 6 this is live; for now test on a temp host). Use a network-watch approach: via `javascript_tool` on `?step=welcome`:
```js
const m = await import('/onboarding/hub.js');
const host = document.createElement('main'); document.body.appendChild(host);
await m.renderHub(host);
const before = [...host.querySelectorAll('.ob-section-card')].map(c=>c.getAttribute('data-state'));
await new Promise(r=>setTimeout(r, 4000));  // let SSE fill
const after = [...host.querySelectorAll('.ob-section-card')].map(c=>c.getAttribute('data-state'));
const filled = after.filter(s=>s!=='checking').length;
host.remove(); ({before, after, filled});
```
Assert `filled >= 1` (at least one tile resolved off `checking` via the stream) **and** confirm the `EventSource` to `/onboarding/status/stream` appears in `read_network_requests`. (Requires M1's stream live.)
4. **Commit:** `git add Portal/onboarding/hub.js && git commit -m "feat(onboarding): SSE skeleton->fill for hub tiles + readiness meter"`.

App launchable.

---

### Task 6 — Route `?mode=manage` and post-completion `/onboarding/` to the hub (dynamic hub import)

Flip the routing in `onboarding.js`: render the hub for `?mode=manage` and for post-completion `GET /onboarding/` (no `/ui` bounce). First-run linear flow stays untouched. The `done` step still renders as the linear completion screen during first run.

**CRITICAL (D5):** the hub is loaded via a **dynamic** `await import('./hub.js')` inside the hub-render path — NOT a static top-level import. A hub.js/status.js/util.js load error MUST NOT white-screen the linear first-run funnel; on import failure, log and fall back gracefully (an error panel in the hub container; the linear funnel is never blocked because the import only runs on the hub path).

**Files:**
- `Portal/onboarding/onboarding.js:30-31` (MODE), `:150` (renderStep), `:272-295` (bootstrap IIFE)

**Steps:**
1. Do **NOT** add a static top-level `import { renderHub, closeStream } from "./hub.js";`. Instead, the hub module is imported lazily inside `renderHubView()` (step 3). This guarantees a hub/status/util load failure cannot break the first-run linear funnel (D5).
2. Replace the bootstrap IIFE (lines 272–295) with hub-aware routing:
```js
(async () => {
    try {
        await fetchState();
        if (REVISIT_STEP) {
            // Deep-linked revisit: jump straight to the requested step. On
            // save it now returns to the hub (see next()/skip()), not /ui.
            currentStepIdx = STEPS.indexOf(REVISIT_STEP);
            await renderStep();
            return;
        }
        if (MODE === "manage") {
            // Explicit console request — render the hub regardless of completion.
            await renderHubView();
            return;
        }
        if (state.is_complete) {
            // Post-completion landing: the hub IS the home. No /ui bounce.
            await renderHubView();
            return;
        }
        // First-run guided funnel — unchanged.
        await renderStep();
    } catch (e) {
        const container = document.getElementById("ob-step-container");
        container.innerHTML = `
            <div class="ob-step-error">
                <h2 class="ob-step-title">Setup unavailable</h2>
                <p class="ob-step-lede">Couldn't reach the BlackBox onboarding API. Check that the service is running, then refresh this page.</p>
                <p class="ob-step-helper">Error: ${escapeHtml(e.message)}</p>
            </div>
        `;
    }
})();
```
3. Add the `renderHubView()` helper (after `renderStep()`, before `updateProgress()`). It **dynamically** imports the hub (D5) and caches the module so repeated hub renders don't re-import:
```js
// Lazily-loaded hub module handle (D5). Kept module-scoped so a single
// dynamic import is reused across hub renders. A load failure here must NEVER
// white-screen the linear funnel — it only runs on the hub render path and
// degrades to an in-container error panel.
let _hubMod = null;
async function loadHub() {
    if (_hubMod) return _hubMod;
    _hubMod = await import("./hub.js");
    return _hubMod;
}

// Hub view — hide the linear step-progress chrome (it's meaningless in the
// console) and paint the grouped console. Dynamic-imported so hub/status/util
// load errors fall back gracefully instead of breaking first-run.
async function renderHubView() {
    const progress = document.querySelector(".ob-progress");
    if (progress) progress.style.visibility = "hidden";
    const container = document.getElementById("ob-step-container");
    try {
        const hub = await loadHub();
        await hub.renderHub(container);
    } catch (e) {
        console.error("[onboarding] hub module failed to load:", e);
        container.innerHTML = `
            <div class="ob-step-error">
                <h2 class="ob-step-title">Console unavailable</h2>
                <p class="ob-step-lede">The setup console couldn't load. Refresh to try again.</p>
                <p class="ob-step-helper">Error: ${escapeHtml(e && e.message ? e.message : String(e))}</p>
            </div>`;
    }
}

// Defensively close any hub SSE stream (used by revisit + unmount). Tolerates
// the hub module not yet being loaded.
async function closeHubStream() {
    if (_hubMod && typeof _hubMod.closeStream === "function") {
        try { _hubMod.closeStream(); } catch (_) {}
    }
}
```
4. **Verify (Chrome):** Navigate `http://localhost:9091/onboarding/?mode=manage`. Screenshot. Assert: the `.ob-hub` console renders with grouped section cards (not a step), the readiness meter shows "N of 10 ready", and the top-right linear "Step NN / NN" progress is hidden. `read_console_messages` — no errors.
   - **Post-completion check:** This box has `.onboarding_complete` set (it's a long-running dev box). Navigate plain `http://localhost:9091/onboarding/` (no query). Assert it renders the **hub** and does **not** redirect to `/ui` (URL stays `/onboarding/`).
   - **First-run regression guard (D5):** Since you cannot easily clear the sentinel without disrupting the box, assert the code path instead: via `javascript_tool` confirm `STEPS` and `renderStep` are untouched by inspecting `?step=welcome` still renders the linear welcome step with its sigil "01" (welcome's `ob-feature-num` `<em>01..04</em>` pillar cards are NOT step sigils). Additionally confirm there is **no** static top-level `import ... from "./hub.js"` in `onboarding.js` (`grep -n 'import .*hub.js' Portal/onboarding/onboarding.js` should match only the dynamic `import("./hub.js")` inside `loadHub`). (Full first-run is re-verified in M6.)
5. **Commit:** `git add Portal/onboarding/onboarding.js && git commit -m "feat(onboarding): hub-first routing — ?mode=manage + post-completion land on hub (dynamic hub import)"`.

App launchable. **SAFE RESTART POINT** — restart `blackbox.service` so the served files are confirmed fresh (prod serves from the working tree, but restart removes any doubt about cached module graphs): `sudo systemctl restart blackbox.service`, then wait for `/health` 200 (60–90s) before the next verify.

---

### Task 7 — `?step=` revisit returns to the hub on save (not the terminal panel)

Replace the old `renderRevisitDone()` terminal "you can close this page" panel with a return-to-hub: completing or skipping a deep-linked revisit re-renders the hub so the user sees their change reflected. The state-neutral revisit semantics (no backend mutation) are preserved. Uses the same dynamic-import hub path (D5).

**Files:**
- `Portal/onboarding/onboarding.js:187-210` (`renderRevisitDone`), `:212-213` (`next`), `:248-249` (`skip`)

**Steps:**
1. Replace `renderRevisitDone()` (lines 187–210) with a hub return:
```js
// Revisit save → return to the console hub so the change is reflected in the
// tiles. (Previously a terminal "close this page" panel.) Revisit never
// mutated backend onboarding state, so this is purely a view switch.
async function renderRevisitHub() {
    await closeHubStream();  // drop any stream the prior render may hold (defensive)
    const progress = document.querySelector(".ob-progress");
    if (progress) progress.style.visibility = "hidden";
    await renderHubView();   // dynamic-imports hub.js + paints (D5 graceful fallback)
    // Reflect hub state in the address bar so a refresh stays on the hub.
    try { history.replaceState(null, "", "/onboarding/?mode=manage"); } catch (_) {}
}
```
2. In `next()` (line 213), change `if (REVISIT_STEP) { renderRevisitDone(); return; }` →
```js
    if (REVISIT_STEP) { await renderRevisitHub(); return; }
```
3. In `skip()` (line 249), change `if (REVISIT_STEP) { renderRevisitDone(); return; }` →
```js
    if (REVISIT_STEP) { await renderRevisitHub(); return; }
```
4. **Verify (Chrome):** Navigate `http://localhost:9091/onboarding/?step=operator`. Screenshot — confirm the linear operator step renders. Click its primary "Continue"/"Save" CTA (or use `computer` to click the `.ob-cta`). Assert the view switches to the `.ob-hub` console (`.ob-section-card` tiles visible), the URL becomes `/onboarding/?mode=manage`, and there is **no** "All set — close this page" terminal panel. Round-trip: click a tile's `?step=` link from the hub → that step renders → save → back to hub. `read_console_messages` — clean.
5. **Commit:** `git add Portal/onboarding/onboarding.js && git commit -m "feat(onboarding): revisit save returns to console hub, not terminal panel"`.

App launchable.

---

### Task 8 — Add the single discreet "Setup / Console" link in `/ui` and point it at the hub (M3 owns this — D7)

**M3 is the SOLE owner of the `/ui` "Setup / Console" link (D7).** M4's duplicate is dropped/verify-only — this is the one implementation. The existing `btnManageSetup` ("⚙️ Manage Setup", Portal/index.html:634) already routes to `/onboarding/?mode=manage` (ui-setup.js:887–890), which now renders the hub — so that door is live. Add one **discreet, always-visible** top-bar link as the second door the contract asks for, and relabel the existing button to the console vocabulary.

**Files:**
- `Portal/index.html:37-68` (topbar), `:634` (existing button label)
- `Portal/modules/ui-setup.js` (existing handler — VERIFY only, near line 887)

**Steps:**
1. In `Portal/index.html`, add a discreet link in the `.topbar` actions area (near the menu button at line 52). Add after the `btnMenu` button:
```html
            <a id="lnkConsole" class="btn-icon" href="/onboarding/?mode=manage" title="Setup / Console" aria-label="Setup / Console">⚙️</a>
```
2. Relabel the existing menu button (line 634) to match the console vocabulary (keep the id so its existing handler stays wired):
```html
              <button id="btnManageSetup" class="btn" title="Open the setup console — keys, operators, integrations, status">⚙️ Setup / Console</button>
```
3. **VERIFY (no JS change):** the existing `btnManageSetup` handler (ui-setup.js:887–890) already does `location.href = "/onboarding/?mode=manage"` — confirm it is unchanged and still wired. The new `lnkConsole` is a plain `<a href>` and needs no wiring. (If `safeSetOnClick` is preferred for consistency, it's optional — the anchor works standalone.)
4. **Verify (Chrome):** Navigate `http://localhost:9091/ui`. Screenshot the top bar — assert the discreet ⚙️ "Setup / Console" link is present. Click it → assert navigation to `/onboarding/?mode=manage` and the `.ob-hub` console renders. Open the System Menu (`btnMenu` ☰), screenshot Advanced section — assert the button now reads "⚙️ Setup / Console"; click it → hub renders. `read_console_messages` on both → clean.
5. **Commit:** `git add Portal/index.html && git commit -m "feat(portal): single discreet Setup/Console link into /ui pointing at the hub"` (only stage explicit paths — never `git add -A`, per repo discipline; `ui-setup.js` is unchanged so it is not staged).

App launchable. **SAFE RESTART POINT** — `sudo systemctl restart blackbox.service`; wait `/health` 200 before the final cross-check.

---

### Task 9 — Adopt M0's `.ob-card-selected` in place of the duplicated inline `SELECTED_STYLE` (M0 owns the class — D3)

The `.ob-card-selected` rule is **created and owned by M0** (D3 — combined selector `.ob-cli-agent-card.ob-card-selected, .ob-section-card.ob-card-selected`). M3 does **NOT** define it. This task verifies M0's class exists, then swaps the duplicated inline `SELECTED_STYLE` literal in `embeddings.js` + `transcription.js` to use that class — keeping the hub's card-state CSS family the single source of card styling.

**Files:**
- `Portal/onboarding/onboarding.css` (M0 owns `.ob-card-selected` — VERIFY ONLY, do not append it)
- `Portal/onboarding/steps/embeddings.js:43-46` (`SELECTED_STYLE`), `:335` (`styleAttr`)
- `Portal/onboarding/steps/transcription.js:53` (`SELECTED_STYLE`), `:193` (`styleAttr`)

**Steps:**
1. **VERIFY (M0 sole-creator guard):** confirm M0's `.ob-card-selected` rule exists in the CSS with the combined selector. If missing, M0 has not landed — STOP (do not add it here):
```bash
grep -n 'ob-cli-agent-card\.ob-card-selected\|ob-section-card\.ob-card-selected' Portal/onboarding/onboarding.css \
  || echo "MISSING — M0 .ob-card-selected not landed; STOP"
```
   Do **NOT** append a `.ob-card-selected` definition in this milestone (that would re-create an M0-owned artifact).
2. In `embeddings.js`: delete the `SELECTED_STYLE` const (lines 43–46), and change line 335 from
   `const styleAttr = isSelected ? ` style="${SELECTED_STYLE}"` : "";`
   to inject the class instead. Locate where `styleAttr` is interpolated into the card's opening tag and replace the pattern with a class toggle:
   `const selCls = isSelected ? " ob-card-selected" : "";` — then append `${selCls}` to that card's `class="..."` attribute and remove the `${styleAttr}` interpolation.
3. Same swap in `transcription.js` (delete const at line 53, swap line 193 site to `ob-card-selected` class).
4. **Verify (Chrome):** Navigate `http://localhost:9091/onboarding/?step=embeddings`. Screenshot. Click a non-active model card (use `computer`). Assert the clicked card gains the accent border/glow (the `.ob-card-selected` treatment) — visually identical to before. `read_console_messages` — assert no `SELECTED_STYLE is not defined`. Repeat for `?step=transcription`.
5. **Commit:** `git add Portal/onboarding/steps/embeddings.js Portal/onboarding/steps/transcription.js && git commit -m "refactor(onboarding): adopt M0 .ob-card-selected, drop inline SELECTED_STYLE literal"` (do NOT stage `onboarding.css` — `.ob-card-selected` is M0's, unchanged here).

App launchable.

---

### Task 10 — Milestone cross-check + dev snapshot

Full M3 acceptance pass across Chrome, then a dev snapshot per @snapshot-dev so this session's work persists into searchable memory.

**Files:** none (verification + snapshot only).

**Steps:**
1. **SAFE RESTART POINT** — ensure the latest tree is served: `sudo systemctl restart blackbox.service`; poll `/health` until 200.
2. **Chrome acceptance matrix** (screenshot each, assert the stated condition):
   - `http://localhost:9091/onboarding/?mode=manage` → `.ob-hub` renders; 4 group headers ("Network & Access", "Keys & Models", "Capabilities", "Identity"); **10** `.ob-section-card` tiles (exactly the canonical D2 set: tailscale, api_keys, embeddings, optional_integrations, transcription, web_search, image, pair_phone, cli_agents, operator); readiness meter "N of 10 ready". The pinned D4 DOM is present (`#ob-hub-groups`, `#ob-hub-attention`).
   - **Skeleton→fill:** reload `?mode=manage`; within ~150ms tiles show their persisted state + summary; over ~1–4s at least one tile transitions via SSE (confirm `EventSource` to `/onboarding/status/stream` in `read_network_requests`).
   - **Revisit round-trip:** from the hub, click a tile → that `?step=` renders → save → returns to hub (URL `?mode=manage`).
   - **Post-completion landing:** plain `http://localhost:9091/onboarding/` → hub, **no** `/ui` redirect.
   - **/ui door (D7 — single link):** `http://localhost:9091/ui` → the one discreet ⚙️ "Setup / Console" link → hub.
   - **Attention header:** if `/onboarding/status` returns any `attention` rows, assert each renders amber with a "Set up →" CTA linking to `?step=<cta_step>`.
   - **First-run resilience (D5):** confirm `onboarding.js` has no static `import ... "./hub.js"` (only the dynamic `import("./hub.js")`) so a hub-module load error degrades to an in-console error panel and never white-screens the linear funnel.
   - `read_console_messages` on every page → **zero** errors.
3. **Verify embeddings/snapshot mechanics** are healthy (per CLAUDE.md): no action needed unless the snapshot in step 4 logs an embedding failure.
4. **Dev snapshot:** invoke @snapshot-dev (resolve operator dynamically via the `resolve-operator` procedure — single operator auto-selects; multiple → AskUserQuestion). Record: "M3 console hub shipped — renderHub() + .ob-section-card[data-state] + readiness/attention header + SSE skeleton→fill (via pinned applySectionEvent) + hub-first routing (dynamic await import('./hub.js'), ?mode=manage + post-completion land on hub, revisit→hub) + status.js presentational + adopted M0 util.js cssEscape + M0 .ob-card-selected + single /ui Setup/Console link. Pinned hub DOM contract for M4: .ob-hub / #ob-hub-groups / #ob-hub-attention. Frontend-only, consumes M1 /onboarding/status[+/stream] + M0 state tokens." VERIFY the journal logs `[EMBEDDING] Successfully generated embedding (N dimensions)` for the snapshot; flag any `[EMBEDDING] Warning/failed`.

No commit (verification + snapshot). App launchable throughout.

---

**Cross-milestone notes:** M3 is frontend-only and hard-depends on **M1** (`/onboarding/status` + `/status/stream` with the shared payload shapes) and **M0** (`--ob-state-*` + `--ob-attention` tokens; `--ob-warning` repointed to amber; the sole-created `util.js` `cssEscape` and `.ob-card-selected` class). **M3 pins the hub contract for M4 (D4):** `hub.js` exports `applySectionEvent(root, ev)` and the hub DOM is `.ob-hub` / `#ob-hub-groups` / `#ob-hub-attention` (NOT `.ob-hub-shell`/`.ob-hub-header`); M4 conforms. **M3 owns the single `/ui` "Setup / Console" link (D7).** The hub is loaded via a **dynamic** `await import('./hub.js')` so a hub/status/util load error can never white-screen the linear first-run funnel (D5). The `.ob-section-grid` deliberately re-uses the `auto-fit minmax(280px,1fr)` grid from `.ob-cli-agent-grid`. Real paths confirmed this run: Portal main app entry is `Portal/index.html` (there is **no** `Portal/app.js` despite the CLAUDE.md reference; JS lives in `Portal/modules/*.js`). The `/ui→hub` door already exists (`btnManageSetup` → `/onboarding/?mode=manage` in `Portal/modules/ui-setup.js:887-890`); M3 only had to make that route render the hub and add the one discreet top-bar link.

---

## Milestone M4: Left-rail navigator + visual elevation

This milestone adds a persistent grouped left-rail navigator (Direction C) and elevates the hub into a console (Direction G). The rail lists all 10 sections grouped by `network/keys/capabilities/identity`, each with a live status pip, and click-jumps to `/onboarding/?step=<key>` — present in the hub and on revisit, **hidden during the first-run guided funnel**. The visual elevation moves three already-built `done.js` affordances — the Portal HTTPS URL card, the live-logs SSE modal, and the status-aware Restart/drift control — into the hub head as console-grade controls, and confirms the readiness meter M3 already mounts in the hub head. Everything is frontend (Chrome-verified, no JS test harness); the one backend touch is extending the parity test so the rail's section/group list can't drift from `STEPS`. M4 **depends on M3** (which landed `renderHub(container)` in `Portal/onboarding/hub.js`, the presentational `Portal/onboarding/status.js` consuming `GET /onboarding/status`, the `.ob-section-card` tile, the `applySectionEvent(root, ev)` SSE updater, the `.ob-hub` / `#ob-hub-groups` / `#ob-hub-attention` hub DOM, the `.ob-hub-readiness` meter, hub-first routing, AND the discreet `/ui` "Setup / Console" door) and **M0** (the `--ob-attention` / `--ob-state-*` / `--ob-radius-*` tokens and `Portal/onboarding/util.js` `cssEscape()`). Where M4 references an exact symbol M3 produced, it conforms to M3's contract verbatim (D4: **if M3 and M4 disagree on any hub symbol/selector, M3 wins**).

> **Dependency contracts this milestone consumes (from M0/M1/M3 — the agreed interfaces, verified against the M3 draft this run):**
> - `GET /onboarding/status` returns `{ ready_count, total, is_complete, sections:[{key,group,label,state,required,summary,step,skipped,items}], attention:[...] }` (M1).
> - `GET /onboarding/status/stream` SSE emits `event: section data:{key,state,summary,attention}` per section then `event: done data:{ready_count,total}` (M1).
> - `renderHub(container)` lives in `Portal/onboarding/hub.js`, loaded by `onboarding.js` via `await import('./hub.js')` (D5). It mounts `<section class="ob-hub">` into the passed container, with `.ob-hub-head` (containing `.ob-hub-readiness#ob-hub-readiness`), `#ob-hub-attention`, and `#ob-hub-groups` as children (M3 Task 4).
> - The SSE lifecycle lives **inside** `hub.js`'s `openStream(container)`; its `section` listener calls `applySectionEvent(container, payload)` and its `done` listener updates `#ob-hub-readiness` (M3 Task 5). M4 hooks the rail fill **here** — not in a separate `onboarding.js` handler.
> - `Portal/onboarding/status.js` is presentational, exports `groupsHtml`, `readinessHtml`, `attentionHtml`, `applySectionEvent`, `escapeHtml`, `sectionCardHtml` (M3 Task 3). It declares `GROUP_ORDER` + `GROUP_LABELS` but derives groups by filtering the payload — it does **not** yet declare a greppable `const SECTIONS` literal. M4 Task 2 adds that literal (M4-owned; not a token/util/card re-creation, so D3 does not gate it).
> - Tokens `--ob-attention`, `--ob-state-{ready,attention,optional,checking}`, `--ob-radius-{sm,md,lg,full}` exist in `onboarding-tokens.css`; `--ob-warning` is repointed to `--ob-attention` (M0 is **sole creator** — D3; M4 imports/uses, never redefines).
> - `Portal/onboarding/util.js` exports `cssEscape()` (regex-replace form), created solely by M0 (D3). M4 imports it; M4 does **not** create or redefine it.
> - `.ob-card-selected` (combined selector) is M0-owned (D3) — M4 does not touch it.
> - **CSS sheet:** the CSS split is M5. M4 appends its CSS to the live single sheet `Portal/onboarding/onboarding.css` (today's reality); if M5 has already split, the identical rules go into `onboarding-shell.css` — selectors are unchanged either way.
> - **Radius tokens (D1):** any new M4 CSS that needs a radius uses `var(--ob-radius-md)` etc. directly (M0 defines them). M4 does **not** introduce `var(--radius-md, 8px)` inline fallbacks (those legacy refs are rewritten by M5, not added by M4).
> - **/ui "Setup / Console" door (D7):** owned and implemented by **M3 Task 8** (`lnkConsole` anchor + `btnManageSetup` button → `/onboarding/?mode=manage`). M4 does **not** re-add it; M4 Task 12 is VERIFY-ONLY.

Pytest invocation (D8 — standardized): `Orchestrator/venv/bin/pytest <path>` from the repo root. Frontend verification uses Claude-in-Chrome against `http://localhost:9091/onboarding/...` (prod serves the working tree; no build step). The hub is reachable post-completion or via `?mode=manage`, so Chrome steps use `?mode=manage` to force the hub without touching the `.onboarding_complete` sentinel.

Reference skills: @superpowers:test-driven-development (backend task), @superpowers:verification-before-completion (every Chrome assertion before commit).

---

### Task 1: Failing parity test — rail section model must match `STEPS`

The rail renders a fixed grouped section list (the 10 SECTIONS from the shared contract). Like the existing `STEPS↔ALL_STEPS` guard, this list must never silently drift from `STEPS`. Write the guard first.

**Files:**
- `Orchestrator/tests/test_onboarding_steps_parity.py` (append; existing file, ends at :74)
- M4's rail reuses a greppable `const SECTIONS = [...]` literal in `Portal/onboarding/status.js`. This test pins it (the literal is added in Task 2).

Append to `test_onboarding_steps_parity.py`:

```python

STATUS_JS = (
    Path(__file__).resolve().parents[2] / "Portal" / "onboarding" / "status.js"
)

# The 10 hub/rail sections (shared contract M3/M4). welcome+done are NOT
# sections (welcome→hub basis, done→summary model), so the section keys are
# exactly STEPS minus those two. Order is the canonical section order (D2):
#   tailscale, api_keys, embeddings, optional_integrations, transcription,
#   web_search, image, pair_phone, cli_agents, operator
_NON_SECTION_STEPS = {"welcome", "done"}


def _status_section_keys() -> list[str]:
    src = STATUS_JS.read_text(encoding="utf-8")
    m = re.search(r"const SECTIONS\s*=\s*\[(.*?)\];", src, re.DOTALL)
    assert m, "could not find `const SECTIONS = [...]` in Portal/onboarding/status.js"
    return re.findall(r'key:\s*"([a-z0-9_]+)"', m.group(1))


def test_status_sections_match_steps_minus_welcome_done():
    keys = _status_section_keys()
    expected = [s for s in _frontend_steps() if s not in _NON_SECTION_STEPS]
    assert keys == expected, (
        "Portal/onboarding/status.js SECTIONS keys must equal STEPS minus "
        "welcome/done, in order. The left-rail navigator (M4) and hub tiles "
        "iterate this list; drift = a section missing from the rail.\n"
        f"  status.js SECTIONS: {keys}\n"
        f"  expected (STEPS-2): {expected}"
    )
```

**Run (expect FAIL — the `const SECTIONS` literal does not exist in `status.js` yet; M3's status.js declares only `GROUP_ORDER`/`GROUP_LABELS` and derives groups by filtering the payload):**
```
Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_steps_parity.py::test_status_sections_match_steps_minus_welcome_done -q
```
Expected: `1 failed` (AssertionError: "could not find `const SECTIONS = [...]`", OR — once present — a key/order mismatch).

**Commit:** `test(onboarding): guard left-rail SECTIONS list against STEPS drift`

---

### Task 2: Make the parity test PASS — add the greppable `SECTIONS` literal in `status.js`

Add the grouped section model to `Portal/onboarding/status.js` as the greppable `const SECTIONS = [...]` literal the test (and the rail) depend on, with the exact 10 keys in **canonical section order (D2)**: `tailscale, api_keys, embeddings, optional_integrations, transcription, web_search, image, pair_phone, cli_agents, operator`. This is M4-owned new content; M3's `status.js` already exists (presentational-only), so this **adds** the literal next to the existing `GROUP_ORDER`/`GROUP_LABELS` without disturbing M3's exports.

**Files:**
- `Portal/onboarding/status.js` (from M3; add the `SECTIONS` literal near the top, presentational-only — no derivation; reuse M3's existing `GROUP_LABELS`/`GROUP_ORDER`, do not redefine them)

Add (next to M3's existing `GROUP_ORDER`/`GROUP_LABELS`) exactly:

```javascript
// Shared section model — the 10 hub/rail sections (welcome+done are NOT
// sections). In CANONICAL section order (D2) = onboarding.js STEPS minus
// welcome/done. Guarded by Orchestrator/tests/test_onboarding_steps_parity.py.
// Keep this a plain greppable literal (no computed construction) or update the
// regex there. The `group` field reuses M3's GROUP_LABELS keys.
// NOTE (D9): cli_agents' rail label "Agents" deliberately differs from the
// step chrome label "AGENTS" (STEP_LABELS) — intentional, not a typo.
const SECTIONS = [
    { key: "tailscale",              group: "network",      label: "Tailnet",     required: false },
    { key: "api_keys",               group: "keys",         label: "API Keys",    required: true  },
    { key: "embeddings",             group: "keys",         label: "Memory",      required: true  },
    { key: "optional_integrations",  group: "capabilities", label: "Extras",      required: false },
    { key: "transcription",          group: "capabilities", label: "Speech",      required: false },
    { key: "web_search",             group: "capabilities", label: "Web Search",  required: false },
    { key: "image",                  group: "capabilities", label: "Image",       required: false },
    { key: "pair_phone",             group: "network",      label: "Pair Phone",  required: false },
    { key: "cli_agents",             group: "capabilities", label: "Agents",      required: false },
    { key: "operator",               group: "identity",     label: "Operators",   required: true  },
];

export { SECTIONS };
```

> Note: `GROUP_LABELS` is already declared and used by M3's `status.js`. **Do not redefine it** — M4's rail (Task 4) imports M3's existing `GROUP_LABELS`. The rail renders groups in the fixed group order `network → keys → capabilities → identity` (same as M3's `GROUP_ORDER`), filtering `SECTIONS` by `group`; the per-section canonical order above governs ordering **within** each group and the parity test.

**Run (expect PASS):**
```
Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_steps_parity.py -q
```
Expected: all pre-existing parity tests + the new one pass (e.g. `4 passed`).

**Commit:** `feat(onboarding): export greppable SECTIONS literal for rail+hub`

---

### Task 3: Rail CSS — grouped navigator with status pips, sharp-edged brutalist treatment

Add the left-rail stylesheet. The rail reuses the `.ob-cli-agent-card[data-state]` border-left-color idiom but on the canonical M0 `--ob-state-*` tokens, not the off-token rgbas at `onboarding.css:2409-2411`. Pips switch color on `data-state`. The hub two-column layout is achieved by restyling M3's `.ob-hub` into a grid (rail column + content column) — M4 does **not** introduce a `.ob-hub-shell` wrapper (D4: the names `.ob-hub-shell` / `.ob-hub-header` are BANNED; conform to M3's `.ob-hub` DOM).

**Files:**
- `Portal/onboarding/onboarding.css` (append a new section block; same live sheet — see milestone note)

Append:

```css
/* ════════════════════════════════════════════════════════════════
 * M4 — Left-rail navigator (Direction C)
 * Persistent grouped section list with live status pips. Present in the
 * hub + on ?step= revisit; HIDDEN during first-run guided funnel (the
 * rail is only injected by renderHub()/revisit, never by the linear
 * renderStep path). Pips switch color via data-state on the canonical
 * --ob-state-* tokens (M0), NOT the off-token rgbas the cli-agent cards
 * still use.
 * ════════════════════════════════════════════════════════════════ */

.ob-rail {
    display: flex;
    flex-direction: column;
    gap: var(--ob-space-6);
    padding: var(--ob-space-8) var(--ob-space-6);
    border-right: 1px solid var(--ob-surface-border);
    min-width: 200px;
    animation: ob-fade-up var(--ob-motion-moment) var(--ob-ease-out) 60ms backwards;
}
.ob-rail-group {
    display: flex;
    flex-direction: column;
    gap: var(--ob-space-2);
}
.ob-rail-group-label {
    font-family: var(--ob-font-body);
    font-size: var(--ob-text-xs);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    color: var(--ob-text-tertiary);
    margin-bottom: var(--ob-space-2);
}
.ob-rail-item {
    display: flex;
    align-items: center;
    gap: var(--ob-space-3);
    padding: var(--ob-space-2) var(--ob-space-3);
    border-left: 2px solid transparent;
    color: var(--ob-text-secondary);
    text-decoration: none;
    font-family: var(--ob-font-body);
    font-size: var(--ob-text-sm);
    cursor: pointer;
    transition: color var(--ob-motion-fast) var(--ob-ease-out),
                border-color var(--ob-motion-fast) var(--ob-ease-out),
                background var(--ob-motion-fast) var(--ob-ease-out);
}
.ob-rail-item:hover {
    color: var(--ob-text-primary);
    background: linear-gradient(90deg, var(--ob-accent-soft) 0%, transparent 70%);
}
.ob-rail-item[aria-current="page"] {
    color: var(--ob-text-primary);
    border-left-color: var(--ob-accent);
}
.ob-rail-item:focus-visible {
    outline: 2px solid var(--ob-text-primary);
    outline-offset: 2px;
}
.ob-rail-pip {
    width: 8px;
    height: 8px;
    border-radius: var(--ob-radius-full);
    flex-shrink: 0;
    background: var(--ob-state-optional);
}
.ob-rail-item[data-state="ready"]     .ob-rail-pip { background: var(--ob-state-ready); }
.ob-rail-item[data-state="attention"] .ob-rail-pip { background: var(--ob-state-attention); }
.ob-rail-item[data-state="optional"]  .ob-rail-pip { background: var(--ob-state-optional); }
.ob-rail-item[data-state="checking"]  .ob-rail-pip {
    background: var(--ob-state-checking);
    animation: ob-rail-pip-pulse 1200ms var(--ob-ease-in-out) infinite;
}
@keyframes ob-rail-pip-pulse {
    0%, 100% { opacity: 0.35; }
    50%      { opacity: 1; }
}
.ob-rail-label { flex: 1; }
.ob-rail-required {
    font-size: var(--ob-text-xs);
    color: var(--ob-accent);
    line-height: 1;
}

/* Hub two-column layout: rail + content. The hub mounts inside the step
 * container as <section class="ob-hub"> (M3). M4 restyles that SAME .ob-hub
 * into a 2-col grid and prepends the rail as its first child; the existing
 * M3 children (.ob-hub-head, #ob-hub-attention, #ob-hub-groups) flow into
 * the content column. No .ob-hub-shell wrapper is introduced (D4). */
.ob-hub:has(.ob-rail) {
    display: grid;
    grid-template-columns: minmax(200px, 0.9fr) minmax(0, 5fr);
    column-gap: var(--ob-space-10);
    align-items: start;
}
/* Rail spans all content rows; content children stack in column 2. */
.ob-hub > .ob-rail {
    grid-column: 1;
    grid-row: 1 / -1;
}
.ob-hub > .ob-hub-head,
.ob-hub > #ob-hub-attention,
.ob-hub > #ob-hub-groups {
    grid-column: 2;
}
@media (max-width: 900px) {
    .ob-hub:has(.ob-rail) { grid-template-columns: 1fr; row-gap: var(--ob-space-8); }
    .ob-hub > .ob-rail,
    .ob-hub > .ob-hub-head,
    .ob-hub > #ob-hub-attention,
    .ob-hub > #ob-hub-groups { grid-column: 1; }
    .ob-hub > .ob-rail { grid-row: auto; }
    .ob-rail {
        border-right: none;
        border-bottom: 1px solid var(--ob-surface-border);
        flex-direction: row;
        flex-wrap: wrap;
        gap: var(--ob-space-8);
    }
}
```

**Run / Verify (Chrome — the rail isn't wired yet, so this just confirms no CSS syntax break):**
1. `mcp__claude-in-chrome__navigate` → `http://localhost:9091/onboarding/?mode=manage`
2. `mcp__claude-in-chrome__read_console_messages` → assert no CSS parse errors / no 404 for `onboarding.css`.
3. Screenshot — hub still renders single-column (rail markup comes in Task 4; the `:has(.ob-rail)` grid is inert until then).

**Commit:** `style(onboarding): left-rail navigator CSS on --ob-state-* tokens`

---

### Task 4: Render the rail in the hub and wire click-to-jump

Add a `renderRail()` to `status.js` that paints the grouped navigator from `SECTIONS`, marks each item `data-state="checking"` initially (live state comes from the fetched payload), and on click navigates to `/onboarding/?step=<key>`. Mount it inside M3's `.ob-hub` (prepended as the first child, so the Task 3 grid lays it out as the left column). Use `cssEscape()` from `util.js` (M0, D3) for any `[data-key=...]` selectors — do **not** reintroduce inline escape copies.

**Files:**
- `Portal/onboarding/status.js` (add `renderRail()` + `updateRailItem()`; presentational only — consumes the `sections` already fetched by the hub, does NO derivation)
- `Portal/onboarding/hub.js` (M3): in `renderHub()`, after `container.innerHTML = ...` builds `.ob-hub`, prepend the rail into `.ob-hub`

First, **VERIFY the M0/M3 dependencies exist** (do not re-create — D3):
```
grep -n "export function cssEscape\|export { cssEscape" Portal/onboarding/util.js
grep -n "const SECTIONS" Portal/onboarding/status.js
grep -n "applySectionEvent\|class=\"ob-hub\"\|id=\"ob-hub-groups\"" Portal/onboarding/hub.js
```
Expected: `util.js` exports `cssEscape` (M0); `status.js` has the Task-2 `SECTIONS` literal; `hub.js` builds `<section class="ob-hub">` and exports/uses `applySectionEvent`.

Add to `status.js`:

```javascript
import { cssEscape } from "./util.js";

// Presentational rail. `sections` is the array from GET /onboarding/status
// (already fetched by the hub); we DERIVE NOTHING here — state comes straight
// off each section object. Returns the rail element; caller mounts it.
// Groups render in the fixed group order; sections within a group keep their
// SECTIONS (canonical, D2) order.
function renderRail(sections) {
    const byKey = new Map((sections || []).map(s => [s.key, s]));
    const groups = ["network", "keys", "capabilities", "identity"];
    const nav = document.createElement("nav");
    nav.className = "ob-rail";
    nav.setAttribute("aria-label", "Section navigator");

    for (const group of groups) {
        const items = SECTIONS.filter(s => s.group === group);
        if (!items.length) continue;
        const groupEl = document.createElement("div");
        groupEl.className = "ob-rail-group";
        groupEl.insertAdjacentHTML("beforeend",
            `<div class="ob-rail-group-label">${escapeHtml(GROUP_LABELS[group] || group)}</div>`);
        for (const sec of items) {
            const live = byKey.get(sec.key);
            const state = (live && live.state) || "checking";
            const a = document.createElement("a");
            a.className = "ob-rail-item";
            a.href = `/onboarding/?step=${encodeURIComponent(sec.key)}`;
            a.dataset.key = sec.key;
            a.dataset.state = state;
            a.innerHTML = `
                <span class="ob-rail-pip" aria-hidden="true"></span>
                <span class="ob-rail-label">${escapeHtml(sec.label)}</span>
                ${sec.required ? `<span class="ob-rail-required" title="Required" aria-label="required">&lowast;</span>` : ""}
            `;
            groupEl.appendChild(a);
        }
        nav.appendChild(groupEl);
    }
    return nav;
}

// Live-update a single rail item's pip (called from hub.js's SSE section
// handler, in lockstep with applySectionEvent on the tiles).
function updateRailItem(railEl, key, state) {
    if (!railEl || !key) return;
    const item = railEl.querySelector(`.ob-rail-item[data-key="${cssEscape(key)}"]`);
    if (item && state) item.dataset.state = state;
}

export { renderRail, updateRailItem };
```

> `renderRail` references M3's existing `GROUP_LABELS` and `escapeHtml` (already declared in `status.js`) and the Task-2 `SECTIONS` — all in-module, no new imports beyond `cssEscape` from M0's `util.js`.

In `hub.js` `renderHub(container)` (M3 Task 4), import the rail and prepend it into `.ob-hub` right after the `container.innerHTML` paint, before `openStream(container)`:

```javascript
// M3 import line gains the rail symbols (same module):
import { groupsHtml, readinessHtml, attentionHtml, applySectionEvent,
         renderRail, updateRailItem } from "./status.js";

// ...inside renderHub(), AFTER container.innerHTML = `<section class="ob-hub">…`
// and BEFORE openStream(container):
const hubEl = container.querySelector(".ob-hub");
const rail = renderRail(sections);     // `sections` already fetched above
if (hubEl) hubEl.prepend(rail);        // Task-3 grid lays it out as column 1
// stash the rail element so openStream's section handler can fill its pips:
container.__obRail = rail;
```

**Run / Verify (Chrome):**
1. `mcp__claude-in-chrome__navigate` → `http://localhost:9091/onboarding/?mode=manage`
2. `mcp__claude-in-chrome__read_page` → assert the DOM contains `.ob-hub nav.ob-rail` with 4 `.ob-rail-group` blocks and exactly 10 `.ob-rail-item` anchors; group labels read `Network & Access / Keys & Models / Capabilities / Identity`.
3. `mcp__claude-in-chrome__find` the rail item with text "API Keys" → assert its `href` ends with `?step=api_keys` and it has a `.ob-rail-required` mark; assert "Tailnet" has no required mark.
4. `mcp__claude-in-chrome__computer` click the "Speech" rail item → assert URL becomes `…/onboarding/?step=transcription` and the transcription step renders.
5. `mcp__claude-in-chrome__read_console_messages` → no module/import errors (confirms M0 `util.js` `cssEscape` import resolved).
6. Screenshot the hub with the rail visible.

**Commit:** `feat(onboarding): grouped left-rail navigator with click-to-jump`

---

### Task 5: Live-fill rail pips from the SSE stream

The rail items start `checking`; as the existing hub SSE (`GET /onboarding/status/stream`, M1/M3) emits per-section events, flip each rail pip to its resolved state. The SSE consumer lives **inside `hub.js`'s `openStream(container)`** (M3 Task 5) — its `section` listener already calls `applySectionEvent(container, payload)` for the tiles. Add the `updateRailItem` call right beside it so the rail and tiles fill in lockstep. **Do not** introduce a separate handler in `onboarding.js` (D4 — conform to M3's hub SSE contract; the SSE lifecycle is owned by hub.js).

**Files:**
- `Portal/onboarding/hub.js` (the `openStream()` `section` listener from M3 Task 5)

In `hub.js`'s `openStream(container)`, inside the existing `es.addEventListener("section", ...)` handler, after the existing `applySectionEvent(container, payload)` call, add the rail update:

```javascript
es.addEventListener("section", (e) => {
    let payload;
    try { payload = JSON.parse(e.data); } catch (_) { return; }
    applySectionEvent(container, payload);              // M3 — fills the .ob-section-card tile
    updateRailItem(container.__obRail, payload.key, payload.state);  // M4 — fills the rail pip
    if (Array.isArray(payload.attention)) {
        mergeAttention(container, payload.section || payload.key, payload.attention);
    }
});
```

> `updateRailItem` is already in `hub.js`'s import list (added in Task 4). `container.__obRail` is the rail element stashed by `renderHub` in Task 4. If the rail isn't present (e.g. a future caller renders the hub without a rail), `updateRailItem` no-ops on the null guard.

**Run / Verify (Chrome — observe skeleton→fill):**
1. `mcp__claude-in-chrome__navigate` → `http://localhost:9091/onboarding/?mode=manage`
2. Immediately `mcp__claude-in-chrome__read_page` → at least one `.ob-rail-item` still has `data-state="checking"` (pulsing pip) on first paint.
3. Wait ~2-4s, `mcp__claude-in-chrome__read_page` again → no `.ob-rail-item` remains `data-state="checking"`; every pip is `ready`/`attention`/`optional`. Cross-check: each rail item's `data-state` equals the matching `.ob-section-card[data-section=<key>]`'s `data-state` (rail and tiles agree, driven by the same SSE event).
4. Screenshot.

**Commit:** `feat(onboarding): live-fill rail pips from /status/stream SSE`

---

### Task 6: Confirm the rail is HIDDEN during the first-run guided funnel

Non-negotiable: the rail must NOT appear in the linear flow. Because the rail is injected only by `hub.js`'s `renderHub()` (Tasks 4–5) and never by the linear `renderStep()` path in `onboarding.js`, the funnel stays rail-free. This task verifies that contract and adds a guard comment.

**Files:**
- `Portal/onboarding/onboarding.js` (the `renderStep()` function — add a one-line guard comment; no logic change expected)

Add above `renderStep()`:

```javascript
// M4 invariant: the left-rail navigator (.ob-rail) is injected ONLY by
// hub.js's renderHub()/revisit, NEVER here. The first-run guided funnel must
// stay rail-free (Direction C: "hidden during first-run guided funnel"). If
// you add rail markup to a step, you have broken that invariant.
```

**Run / Verify (the real guard is structural — the linear path never names the rail):**
1. **Do NOT delete the `.onboarding_complete` sentinel** (destructive). The rail cannot leak into the funnel because `renderStep` (`onboarding.js`) never references `ob-rail`/`renderRail`, and the rail is only injected by `hub.js`'s `renderHub`. Assert this structurally via `Bash`:
```
grep -n "ob-rail\|renderRail\|updateRailItem" Portal/onboarding/onboarding.js
grep -n "ob-rail\|renderRail\|updateRailItem" Portal/onboarding/hub.js
```
Expected: `onboarding.js` shows **only** the guard comment above `renderStep` (no rail injection / no `renderRail` call in the linear path); `hub.js` is where `renderRail`/`updateRailItem` are imported and called. This proves the rail markup is reachable exclusively through the hub.
2. Chrome spot-check (revisit path still shows the rail as designed): `mcp__claude-in-chrome__navigate` → `http://localhost:9091/onboarding/?mode=manage` → confirm `.ob-rail` present. Then `mcp__claude-in-chrome__navigate` → `http://localhost:9091/onboarding/?step=api_keys` (a revisit step) → confirm the **linear step** renders and (per M3 routing) `?step=` revisit is a step view; assert `document.querySelectorAll('.ob-rail').length === 0` on that linear step view via `mcp__claude-in-chrome__javascript_tool`.

**Commit:** `docs(onboarding): assert rail stays out of the first-run funnel`

---

### Task 7: Verify the hub readiness meter (M3-owned) shows total = 10

M3 Task 4/5 already mounts the readiness meter in the hub head — `.ob-hub-readiness#ob-hub-readiness`, painted from `GET /onboarding/status` via `readinessHtml(ready_count, total)`, and updated on the SSE `done` event. M4 does **not** add a second meter (that would duplicate M3 and violate D4/D10). This task is **VERIFY-ONLY**: confirm M3's meter exists, reads from the status payload (not a step cursor), and reports `total = 10` (matching the 10 SECTIONS).

**Files:** none (verification of M3's meter).

**Run / Verify (Chrome):**
1. `mcp__claude-in-chrome__navigate` → `http://localhost:9091/onboarding/?mode=manage`
2. `mcp__claude-in-chrome__find` `#ob-hub-readiness` → assert its text matches `/\d+ of \d+ ready/` and that the second number (`total`) is `10`, with `ready_count <= total`.
3. After SSE settles (~2-4s), re-read `#ob-hub-readiness` → the count may have changed (live re-validation via the `done` event); `total` stays `10`.
4. Screenshot the hub head showing the meter.

> If `total !== 10` here, the M1 `/onboarding/status` rollup or the SECTIONS list drifted — STOP and reconcile against the Task-1/2 parity guard before continuing.

**Commit:** none (no file change) — verification gate.

---

### Task 8: Surface the Portal HTTPS URL in the hub head

Tailscale Branch A persists `BLACKBOX_TAILNET_HOSTNAME` and computes `https://<hostname>`. Surface that URL as a copyable console affordance in the hub head, reusing the existing `.ob-tailnet-url-card` markup + `.ob-copy-btn` idiom already in tailscale.js. Read the hostname from `/onboarding/current-config` — **verified key shape** (handler at `Orchestrator/routes/onboarding_routes.py:117`): the response has `tailscale: { configured, validated_at, detail }` and `detail.hostname` is the tailnet DNSName (`validators.py:175`, `done.js:195` reads the same path). Fall open silently if absent (LAN-only boxes).

**Files:**
- `Portal/onboarding/status.js` (add `renderPortalUrlCard()` + a local minimal copy-button wiring; M5 may later extract a shared copy helper — for now inline a minimal copy using a local HTML escaper)
- `Portal/onboarding/hub.js` `renderHub()` mounts it into the hub head (`.ob-hub-head`)

**Verify the key shape first (DEPENDENCY guard):**
```
curl -s http://localhost:9091/onboarding/current-config | python3 -c "import sys,json; d=json.load(sys.stdin); print('tailscale.detail:', json.dumps(d.get('tailscale',{}).get('detail',{})))"
```
Expected: prints the `detail` object; on a tailnet box it contains `"hostname": "<name>.ts.net"`. (On a LAN-only box `detail` is `{}` — the card renders nothing, which is the fail-open path.) This confirms `cfg.tailscale.detail.hostname` is the correct path before coding against it.

Add to `status.js`:

```javascript
// Surface the Portal HTTPS URL (Android pairing / desktop access). Hostname
// comes from /onboarding/current-config (verified shape: tailscale.detail.hostname,
// same source as done.js:195). Fails open: LAN-only boxes (no tailnet hostname)
// render nothing.
async function renderPortalUrlCard() {
    let hostname = null;
    try {
        const r = await fetch("/onboarding/current-config");
        if (r.ok) {
            const cfg = await r.json();
            hostname = cfg?.tailscale?.detail?.hostname || null;
        }
    } catch (_) { /* fail open */ }
    if (!hostname) return null;

    const url = `https://${hostname}`;
    const el = document.createElement("div");
    el.className = "ob-tailnet-url-card ob-hub-url-card";
    el.innerHTML = `
        <div class="ob-tailnet-url-label">Your Portal URL</div>
        <div class="ob-tailnet-url-row">
            <code class="ob-tailnet-url-code">${escapeHtml(url)}</code>
            <button type="button" class="ob-copy-btn" data-copy="${escapeHtml(url)}">
                <span aria-hidden="true">&#9112;</span> Copy
            </button>
        </div>
    `;
    el.querySelector(".ob-copy-btn").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        try { await navigator.clipboard.writeText(url); btn.textContent = "Copied ✓"; }
        catch { btn.textContent = "Copy failed"; }
        setTimeout(() => { btn.innerHTML = `<span aria-hidden="true">&#9112;</span> Copy`; }, 1500);
    });
    return el;
}

export { renderPortalUrlCard };
```

> Reuses M3's existing `escapeHtml` (already declared in `status.js`) — no separate local escaper is added (avoids the duplicate-helper drift D3 warns against).

In `hub.js` `renderHub()`: import `renderPortalUrlCard` and mount it into the hub head after the readiness meter:
```javascript
import { renderPortalUrlCard } from "./status.js";  // (add to existing import)
// ...after container.innerHTML paints .ob-hub-head:
const head = container.querySelector(".ob-hub-head");
const urlCard = await renderPortalUrlCard();
if (head && urlCard) head.appendChild(urlCard);
```

**Run / Verify (Chrome):**
1. `mcp__claude-in-chrome__navigate` → `http://localhost:9091/onboarding/?mode=manage`
2. `mcp__claude-in-chrome__read_network_requests` → confirm a `GET /onboarding/current-config` 200.
3. If the box has a tailnet hostname: `mcp__claude-in-chrome__find` `.ob-hub-url-card code` → assert text starts with `https://` and matches the hostname from the curl-verify above; click Copy → button text becomes "Copied ✓". If LAN-only: assert `.ob-hub-url-card` is absent and the hub still renders (fail-open).
4. Screenshot.

**Commit:** `feat(onboarding): surface Portal HTTPS URL in hub header`

---

### Task 9: Relocate the live-logs SSE modal into the hub head

Move the View-Logs console modal out of `done.js` (where it's buried, :485-566) into a shared module so the hub head carries it as a console affordance. The modal already consumes `/onboarding/logs/stream` via `EventSource` (backend handler verified at `onboarding_routes.py:998-1053`) — extract it verbatim, no backend change.

**Files:**
- New: `Portal/onboarding/logs-modal.js` (export `openLogsModal` — moved verbatim from `done.js:485-566`, the SSE/EventSource/`autoScroll`/copy logic unchanged)
- `Portal/onboarding/steps/done.js` (replace the inline `openLogsModal` definition with `import { openLogsModal } from "../logs-modal.js";`)
- `Portal/onboarding/hub.js` (`renderHub()`: add a "View Logs" button to the hub head that calls `openLogsModal`)

In `logs-modal.js`, paste the exact `openLogsModal` function body from `done.js:485-566` (it's self-contained — builds `#ob-logs-modal`, opens `new EventSource("/onboarding/logs/stream?lines=200")`, handles `start`/`message`/`error`, Esc/backdrop/X close + EventSource close, Copy-all). Add `export { openLogsModal };`.

In `done.js`: remove the local `function openLogsModal() {...}` (lines 485-566) and add at top `import { openLogsModal } from "../logs-modal.js";`. The existing wiring at `done.js:81-82` (`logsBtn.addEventListener("click", openLogsModal)`) is unchanged.

In `hub.js` `renderHub()` (hub head builder), add:
```javascript
import { openLogsModal } from "./logs-modal.js";
// ...in renderHub, after the .ob-hub-head is in the DOM:
const head = container.querySelector(".ob-hub-head");
const logsBtn = document.createElement("button");
logsBtn.type = "button";
logsBtn.className = "ob-cta-secondary ob-hub-logs-btn";
logsBtn.textContent = "View Logs";
logsBtn.addEventListener("click", openLogsModal);
if (head) head.appendChild(logsBtn);
```

**Run / Verify (Chrome — both surfaces still work):**
1. Hub: `mcp__claude-in-chrome__navigate` → `?mode=manage`; click `.ob-hub-logs-btn`; assert `#ob-logs-modal` appears, footer status flips `Connecting…`→`Connected`, line count > 0 within a few seconds. Close via Esc; assert `#ob-logs-modal` is removed.
2. Done surface regression: `mcp__claude-in-chrome__navigate` → `http://localhost:9091/onboarding/?step=done`; click `#ob-view-logs-btn`; assert the same modal opens (proves the extraction didn't break done.js).
3. `mcp__claude-in-chrome__read_console_messages` → no import/module errors.

**Commit:** `refactor(onboarding): extract logs modal to shared module, add to hub header`

---

### Task 10: Relocate the status-aware Restart/drift control into the hub head

Move the Restart Service control (status-aware, drift-detecting) out of `done.js` (:299-443) into a shared module and mount it in the hub head. It consumes `/onboarding/restart-status` + `/onboarding/restart` + `/health` — all existing, no backend change. The drift/passive/restarting state machine (`renderRestartState`/`doRestart`/`pollHealth`/`pollRestartCleared`) moves verbatim.

**Files:**
- New: `Portal/onboarding/restart-control.js` (export `mountRestartControl(btnEl, statusEl)` wrapping `initRestartButton`+`renderRestartState`+`doRestart`+`pollHealth`+`pollRestartCleared` — moved verbatim from `done.js:299-474`; module-level `restartBusy` becomes module-scoped here, single instance, fine since one control exists at a time)
- `Portal/onboarding/steps/done.js` (replace inline restart logic with `import { mountRestartControl } from "../restart-control.js";` and call `mountRestartControl(restartBtn, restartStatusEl)` instead of `initRestartButton()` at :76)
- `Portal/onboarding/hub.js` (`renderHub()` hub head: add the Restart button + status span markup, call `mountRestartControl`)

In `restart-control.js`, export a single entry that takes the button + status elements (so both done and hub reuse it):
```javascript
// Moved verbatim from done.js: renderRestartState/doRestart/pollHealth/
// pollRestartCleared + restartBusy. Entry point binds to caller-provided
// elements so done.js and the hub head both reuse one implementation.
let restartBusy = false;
async function mountRestartControl(btn, statusEl) {
    if (!btn || !statusEl) return;
    try {
        const r = await fetch("/onboarding/restart-status");
        if (!r.ok) return;
        renderRestartState(btn, statusEl, await r.json());
    } catch (e) { console.warn("restart-status probe failed:", e); }
}
// ...renderRestartState, doRestart, pollHealth, pollRestartCleared, sleep
// pasted unchanged from done.js:332-474...
export { mountRestartControl };
```

In `done.js`: keep the existing `#ob-done-restart` button + `#ob-done-restart-status` markup; replace the `initRestartButton()` call (:76) with `mountRestartControl(document.getElementById("ob-done-restart"), document.getElementById("ob-done-restart-status"))` and delete the now-moved functions (:310-474). Add the import.

In `hub.js` `renderHub()` hub head:
```javascript
import { mountRestartControl } from "./restart-control.js";
// ...after .ob-hub-head is in the DOM:
const head = container.querySelector(".ob-hub-head");
const restartWrap = document.createElement("div");
restartWrap.className = "ob-cta-row ob-hub-restart";
restartWrap.innerHTML = `
    <button type="button" class="ob-cta ob-cta-restart" id="ob-hub-restart" hidden>
        <span class="ob-cta-restart-label">Restart Service</span>
        <span class="ob-cta-arrow" aria-hidden="true">&#x21bb;</span>
    </button>
    <span class="ob-restart-status" id="ob-hub-restart-status" hidden></span>
`;
if (head) head.appendChild(restartWrap);
mountRestartControl(restartWrap.querySelector("#ob-hub-restart"),
                    restartWrap.querySelector("#ob-hub-restart-status"));
```

**Run / Verify (Chrome — DO NOT actually restart):**
1. Hub: `mcp__claude-in-chrome__navigate` → `?mode=manage`; `mcp__claude-in-chrome__read_network_requests` → confirm `GET /onboarding/restart-status` 200; `mcp__claude-in-chrome__find` `#ob-hub-restart-status` → assert it shows either "Service up to date ✓" (passive) or a "N settings changed…" drift message. **Do not click Restart** (it would SIGTERM the service).
2. Done regression: `?step=done`; assert `#ob-done-restart-status` still renders the same passive/drift text (extraction intact).
3. `mcp__claude-in-chrome__read_console_messages` → no module/import errors.

**Commit:** `refactor(onboarding): extract restart control to shared module, add to hub header`

---

### Task 11: Hub head console-band layout CSS

Tie the elevated hub-head affordances (M3's readiness meter + M4's Portal URL, View Logs, Restart) into one cohesive console band. M4's affordances are appended into M3's existing `.ob-hub-head`, so the CSS extends `.ob-hub-head` rather than inventing a new `.ob-hub-header` wrapper (D4 — `.ob-hub-header` is BANNED). Reuse existing `.ob-cta-secondary` / `.ob-cta-restart` / `.ob-tailnet-url-card` styles already in the sheet.

**Files:**
- `Portal/onboarding/onboarding.css` (append; same live sheet)

```css
/* M4 — Hub console head band (Direction G). M3's .ob-hub-head holds the
 * title + readiness meter; M4 appends the Portal URL + View Logs + Restart
 * control into that SAME .ob-hub-head. These rules turn it into a wrapping
 * console band. No .ob-hub-header wrapper is introduced (D4). */
.ob-hub-head {
    flex-wrap: wrap;
    align-items: center;
    row-gap: var(--ob-space-4);
    column-gap: var(--ob-space-8);
}
.ob-hub-head .ob-hub-readiness { min-width: 200px; }
.ob-hub-url-card { margin: 0; flex: 1 1 320px; }
.ob-hub-logs-btn { margin-left: auto; }
.ob-hub-restart { margin: 0; }
@media (max-width: 720px) {
    .ob-hub-head { column-gap: var(--ob-space-5); }
    .ob-hub-logs-btn { margin-left: 0; }
}
```

> M3's `.ob-hub-head` is `display:flex; flex-direction:column`. M4 keeps `display:flex` but the affordances (title, readiness, URL card, View Logs, Restart) are siblings within it; the `flex-wrap` + `align-items` above turn the column into a wrapping band. If M3's `.ob-hub-head` direction must stay `column` for the title, the band can instead wrap the four non-title affordances in a `<div class="ob-hub-controls">` appended to `.ob-hub-head` — but the simplest conforming path (single `.ob-hub-head`, no new wrapper class) is the default; adjust only if the title/meter stacking regresses visually in the Chrome check below.

**Run / Verify (Chrome):**
1. `mcp__claude-in-chrome__navigate` → `?mode=manage`
2. `mcp__claude-in-chrome__read_page` → assert `.ob-hub-head` contains the readiness meter, (optional) URL card, View Logs button, and restart control as descendants, above the `#ob-hub-groups` grid and the rail.
3. Screenshot at desktop width; `mcp__claude-in-chrome__resize_window` to 700px wide, screenshot again — head band wraps, rail collapses to a horizontal row (Task 3 media query), no overflow.

**Commit:** `style(onboarding): cohesive hub console head band`

---

### Task 12: VERIFY the `/ui` "Setup / Console" door (owned by M3)

The discreet `/ui` → hub door is **implemented by M3 Task 8** (D7): a `lnkConsole` anchor in the Portal top bar and a `btnManageSetup` button in the System Menu, both pointing at `/onboarding/?mode=manage`. M4 does **not** add a second link. This task is **VERIFY-ONLY**: confirm M3's door exists and reaches the M4-elevated hub (rail + head band).

**Files:** none (verification of M3's `/ui` link).

**Run / Verify (Chrome):**
1. Confirm M3's link exists in the tree (do not add a duplicate):
```
grep -n "Setup / Console\|lnkConsole\|btnManageSetup" Portal/index.html
```
Expected: matches the M3-added anchor/button (e.g. `id="lnkConsole"` and/or `id="btnManageSetup"` → `/onboarding/?mode=manage`).
2. `mcp__claude-in-chrome__navigate` → `http://localhost:9091/ui`
3. `mcp__claude-in-chrome__find` the "Setup / Console" link/button → assert it targets `/onboarding/?mode=manage`.
4. Click it → assert navigation to `/onboarding/?mode=manage` renders the **M4-elevated** hub: `.ob-rail` visible (rail), `.ob-hub-head` band with readiness meter + View Logs + Restart, `#ob-hub-groups` grid present.
5. Screenshot.

> If `grep` finds NO `/ui` door, M3 Task 8 did not land — STOP and flag the missing dependency rather than adding the link here (M3 owns it; duplicating would create the drift D7 prevents).

**Commit:** none (no file change) — verification gate.

---

### Task 13: SAFE RESTART POINT — restart service and full cross-check

All M4 frontend lands in the working tree (prod serves it live; no restart needed for JS/CSS). The one backend-adjacent change is the parity test (Tasks 1-2), which a restart exercises via the test suite, not the running process. Restart here to confirm the box boots clean with the M4 tree and re-run the guard tests.

**Files:** none (operational).

**Run:**
1. `Orchestrator/venv/bin/pytest Orchestrator/tests/test_onboarding_steps_parity.py -q` → expect all parity tests pass (the 3 pre-existing + the new SECTIONS↔STEPS guard).
2. Restart (pre-authorized per project memory): `sudo systemctl restart blackbox.service` then wait for `/health` 200 (60-90s warm-up).
   ```
   sudo systemctl restart blackbox.service
   ```
   Then poll: `Monitor`/until-loop on `curl -s -o /dev/null -w '%{http_code}' http://localhost:9091/health` until `200`.
3. Final Chrome smoke: `mcp__claude-in-chrome__navigate` → `http://localhost:9091/onboarding/?mode=manage` → assert in one `read_page`: `.ob-hub nav.ob-rail` with 10 items, `.ob-hub-head` with readiness meter (`total=10`), View Logs + Restart present, `#ob-hub-groups` section grid present, no `data-state="checking"` left on rail or tiles after SSE settles, no console errors.
4. Click one rail item ("Operators") → lands on `?step=operator`; on save (M3 routing) returns to the hub.
5. Screenshot the finished console (before/after pair with the M3 baseline screenshot for the M6 visual diff).

**Commit:** none (no file change) — this is a verification gate.

---

### Task 14: Snapshot the milestone

Per CLAUDE.md, mint a dev snapshot at the end of substantial work.

**Files:** none.

**Run:** Invoke `/snapshot-dev` (operator resolved dynamically via `resolve-operator`). Record: "M4 onboarding uplift — grouped left-rail navigator (canonical-order SECTIONS literal, live SSE pips via hub.js applySectionEvent path, click-to-jump, hidden in first-run funnel) mounted into M3's `.ob-hub` 2-col grid + hub head console elevation (verified M3 readiness meter total=10, Portal HTTPS URL card from current-config tailscale.detail.hostname, extracted logs-modal.js + restart-control.js into the hub head) + verified M3-owned /ui Setup/Console door + SECTIONS↔STEPS parity guard. Conforms to M3 hub contract: .ob-hub / #ob-hub-groups / #ob-hub-attention / applySectionEvent (no .ob-hub-shell/.ob-hub-header)." Verify the embedding generated (tail journalctl for `[EMBEDDING] Successfully generated embedding`).

**Commit:** snapshot is persisted via `/chat/save` auto-mint (no git commit).

---

**Relevant absolute paths for this milestone:**
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/status.js` (SECTIONS literal, renderRail/updateRailItem, renderPortalUrlCard)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/hub.js` (M3 — renderHub rail prepend into `.ob-hub`, head band mounts, SSE `section` handler rail-fill)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/logs-modal.js` (new, extracted from done.js:485-566)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/restart-control.js` (new, extracted from done.js:299-474)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/steps/done.js` (imports the two extracted modules)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/onboarding.js` (renderStep rail-free invariant comment)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/onboarding.css` (rail + hub-head band CSS)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/onboarding/util.js` (M0-owned — imported only, cssEscape)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Portal/index.html` (M3-owned /ui door — VERIFY-ONLY in M4)
- `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/tests/test_onboarding_steps_parity.py` (SECTIONS↔STEPS guard)

---

## Milestone M5: CSS split & restyle finish

This milestone shrinks the 81KB `onboarding.css` monolith and finishes the brutalist normalize/restyle. It splits the file into a shared `onboarding-shell.css` (every chrome + primitive that more than one step uses — header/progress/footer, `.ob-step`, `.ob-cta`, status-badge primitives, the cross-step `.ob-cli-agent-*` family that 5 steps share, plus the new `.ob-section-card`/rail/hub primitives from M3/M4) and per-step `steps/<name>.css` files colocated with each step module. `onboarding.css` becomes a thin `@import` manifest (one `<link>`, zero build step, offline-safe). It then ports every off-token rule below the L1865 seam onto tokens (kill the `--accent, #4a9eff` blue bleed, the `rgba(255,180,50,…)` amber literals → `--ob-attention`, the inline `var(--radius-md, 8px)` fallbacks → the canonical `--ob-radius-*` scale that M0 defines), restyles the late-era components (banners, restart CTA, agent cards, copy buttons, logs modal) to sharp edges + hard-shadow CTAs + red-accent/amber-for-attention, deletes the doubled `@keyframes ob-spin` and the dead no-op selectors, and de-dupes the inline `SELECTED_STYLE` into `.ob-card-selected` (M0-owned class) + imports `cssEscape()` from M0's `util.js`.

**Ownership note (D3):** M0 is the **sole creator** of `Portal/onboarding/util.js` (`cssEscape`), the `.ob-card-selected` rule (combined selector `.ob-cli-agent-card.ob-card-selected, .ob-section-card.ob-card-selected`), and ALL tokens (`--ob-attention`, `--ob-attention-soft`, `--ob-state-*`, the `--ob-warning`→amber repoint, and `--ob-radius-{sm,md,lg,full}` = 6/8/12/9999px). **M5 is IMPORT-ONLY / VERIFY-ONLY for those — it MUST NOT re-create or redefine util.js, `.ob-card-selected`, or any token.** Every "create" task below for those artifacts is a one-line grep-verify of M0's version + a wire-up.

**This milestone is HIGH regression surface.** Every extraction step is byte-preserving (move rules, do not rewrite them) so the rendered page is pixel-identical; restyle steps come *after* the split and each gets a per-step before/after Chrome screenshot. **The strict per-step visual before/after diff gate is the real safety net of this milestone** — the `grep`/`wc`/"manifest only" gates are weak assertions (they confirm structure moved, not that it renders identically), so a passing grep is NEVER sufficient: every extraction and restyle task MUST be confirmed by a pixel diff against the Task-1 baseline before its commit. Each task's commit leaves the app launchable because the working tree is what prod serves. **SAFE RESTART POINTS are named explicitly** — between them, no `blackbox.service` restart is needed (CSS/JS are static assets served from disk; a hard browser refresh picks them up).

**Preconditions (from M0–M4, assumed landed on main before M5 runs):**
- `onboarding-tokens.css` defines `--ob-attention` (~`#f5a623`), `--ob-attention-soft`, `--ob-state-{ready,attention,optional,checking}`, and the radius scale `--ob-radius-{sm,md,lg,full}` (6/8/12/9999px); `--ob-warning` repointed → `var(--ob-attention)`. *(M0 token work — DO NOT redefine here.)*
- `Portal/onboarding/util.js` exists with `cssEscape()` (M0-owned). *(M5 imports it — DO NOT recreate.)*
- `.ob-card-selected` (combined selector) exists (M0-owned). *(M5 toggles the class + relocates the rule into the shell — DO NOT redefine.)*
- `.ob-section-card[data-state]`, rail, and hub-grid rules exist somewhere in the CSS (added inline by M3/M4). **ASSUMPTION:** M3/M4 appended these to the bottom of `onboarding.css`; M5 relocates them into the shell.

Uses @superpowers:test-driven-development discipline (frontend = explicit Chrome verification, every task ends in a commit) and @superpowers:verification-before-completion (run the grep/render check and read the output before claiming pass).

---

### Task 1: Capture the per-step visual baseline (before-screenshots)

Before touching any CSS, record a golden before-state for every render branch so later extraction commits can be diffed pixel-for-pixel. **This baseline set is the load-bearing safety net for Tasks 4–15 — the grep gates alone do not prove pixel-identity.**

**Files:** (read-only) `Portal/onboarding/onboarding.css`, `Portal/onboarding/index.html`

1. Confirm the service is up and the wizard renders:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9091/onboarding/
   ```
   Expected: `200`.
2. Connect Claude-in-Chrome (Skill `claude-in-chrome`), then for EACH of these URLs navigate + full-page screenshot, saving to the scratchpad with a `before-` prefix. URLs follow the canonical section order (tailscale, api_keys, embeddings, optional_integrations, transcription, web_search, image, pair_phone, cli_agents, operator), bracketed by welcome/done:
   - `http://localhost:9091/onboarding/?step=welcome`
   - `?step=tailscale` · `?step=api_keys` · `?step=embeddings` · `?step=optional_integrations`
   - `?step=transcription` · `?step=web_search` · `?step=image` · `?step=pair_phone`
   - `?step=cli_agents` · `?step=operator` · `?step=done`
   - `?mode=manage` (the hub — exists after M3)
   Save into: `/tmp/claude-1000/-home-ai-black-box-fc-Desktop-blackbox-poc--blackbox-poc/27d53ee6-c9e0-47ea-8a63-e5183b722bf6/scratchpad/m5-before/`
3. For `done` and `cli_agents`, also screenshot the View-Logs modal open and a `data-state="ready"`/`"needs-auth"` card so the restyle steps have those branches captured.
4. **No commit** (screenshots are scratch). This is the reference set for Tasks 4–14.

**Verification:** `ls m5-before/*.png | wc -l` ≥ 13.

---

### Task 2: Wire up M0's shared `cssEscape()` (util.js) and remove the two inline copies

**M0 owns `util.js` — this task is VERIFY-ONLY for the file, then rewires the two step modules to import it.** (D3: do NOT create `util.js` here.)

**Files:**
- `Portal/onboarding/util.js` (M0-created — VERIFY ONLY, do not write)
- `Portal/onboarding/steps/image.js:305-307` (remove local `cssEscape`)
- `Portal/onboarding/steps/web_search.js:311-313` (remove local `cssEscape`)

1. **Verify M0's `util.js` exists with the regex-replace form of `cssEscape`** (D3 — M0's version uses the conservative `replace(/[^a-zA-Z0-9_-]/g, "\\$&")` escaper, chosen because the wizard ships offline to possibly-old WebKitGTK):
   ```bash
   grep -n "export function cssEscape" Portal/onboarding/util.js
   ```
   Expected: one match. **If `util.js` is missing, STOP — M0 did not land; do NOT create it here (that would create the duplicate-creation drift D3 exists to kill).**
2. In `steps/image.js`, delete the local definition (lines 305-307) and add to the existing top-of-file imports:
   ```js
   import { cssEscape } from "../util.js";
   ```
   ASSUMPTION: if `image.js` has no import block yet, add the line directly under the leading comment block.
3. Same in `steps/web_search.js` (delete 311-313, add the import).
4. Verify no remaining local copies:
   ```bash
   grep -rn "function cssEscape" Portal/onboarding/steps/
   ```
   Expected: **no output**.
5. **Chrome verify:** navigate `http://localhost:9091/onboarding/?step=image`, open DevTools console (`read_console_messages`), assert **zero** `SyntaxError`/`import` errors, then toggle a provider checkbox and confirm the card border updates (proves `cssEscape` selector still resolves). Repeat for `?step=web_search`.
6. Commit:
   ```bash
   git add Portal/onboarding/steps/image.js Portal/onboarding/steps/web_search.js
   git commit -m "$(cat <<'EOF'
   refactor(onboarding): import shared cssEscape from util.js in image + web_search

   image.js + web_search.js had byte-identical local cssEscape(); remove the
   inline copies and import M0's Portal/onboarding/util.js. No behavior change.

   Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
   Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C
   EOF
   )"
   ```

---

### Task 3: Replace inline `SELECTED_STYLE` with M0's `.ob-card-selected` class

**M0 owns the `.ob-card-selected` rule — this task is VERIFY-ONLY for the CSS rule, then rewires the two step modules to toggle the class instead of inlining `SELECTED_STYLE`.** (D3: do NOT append/redefine `.ob-card-selected` here.)

**Files:**
- `Portal/onboarding/onboarding.css` (M0's `.ob-card-selected` lives inline here until Task 7 relocates it to the shell — VERIFY ONLY, do not author the rule)
- `Portal/onboarding/steps/embeddings.js:43-47, :335`
- `Portal/onboarding/steps/transcription.js:53-57, :193`

1. **Verify M0's `.ob-card-selected` rule exists** (combined selector `.ob-cli-agent-card.ob-card-selected, .ob-section-card.ob-card-selected`):
   ```bash
   grep -rn "ob-card-selected" Portal/onboarding/onboarding.css Portal/onboarding/onboarding-tokens.css
   ```
   Expected: at least one match defining the rule. **If absent, STOP — M0 did not land; do NOT author the rule here (D3).**
2. In `embeddings.js`, delete the `SELECTED_STYLE` const (43-47). At line ~335 replace:
   ```js
   const styleAttr = isSelected ? ` style="${SELECTED_STYLE}"` : "";
   ```
   with a class toggle in the card's class list — find the card template literal and change its `class="ob-cli-agent-card …"` to:
   ```js
   class="ob-cli-agent-card${isSelected ? " ob-card-selected" : ""} …"
   ```
   and delete the now-unused `${styleAttr}` from the element's attributes.
3. Same surgery in `transcription.js` (delete 53-57; replace the `styleAttr` usage at ~193 with the `ob-card-selected` class toggle).
4. Verify removal:
   ```bash
   grep -rn "SELECTED_STYLE" Portal/onboarding/steps/
   ```
   Expected: **no output**.
5. **Chrome verify (load-bearing — color must be IDENTICAL):** navigate `?step=embeddings`, screenshot the selected model card, compare against `m5-before/before-embeddings.png` — the red border + glow + faint-red fill must match. Repeat `?step=transcription`. **If the selected-card fill reads visibly different from the baseline**, the discrepancy is in M0's `.ob-card-selected` definition (not this task's to fix) — flag it back to whoever owns M0 rather than overriding the M0-owned rule here, then re-screenshot once corrected.
6. Commit both step files:
   ```
   refactor(onboarding): toggle M0's .ob-card-selected class instead of inline SELECTED_STYLE
   ```
   (full trailer as in Task 2.)

---

### Task 4: SAFE RESTART POINT — restart + smoke the JS de-dupe

The previous three tasks changed JS module shape (`util.js` import, removed consts). Static assets don't need a service restart, but this is a clean checkpoint before the CSS surgery begins.

**Files:** none (operational).

1. `sudo systemctl restart blackbox.service` (pre-authorized — see memory `feedback_blackbox_restart_authorized`). Warm-up 60-90s.
2. Wait for ready:
   ```bash
   until curl -s -o /dev/null -w "%{http_code}" http://localhost:9091/onboarding/ | grep -q 200; do sleep 3; done; echo UP
   ```
3. **Chrome verify:** navigate `?step=image`, `?step=embeddings`, `?step=transcription`, `?step=web_search` — console clean, cards render, selection toggles. This confirms the module graph (`util.js`) loads from the served tree.
4. No commit (operational checkpoint).

---

### Task 5: Create the `@import` manifest scaffold (empty shell + manifest, monolith still authoritative)

Establish the new file structure WITHOUT moving any rules yet, so the import wiring is proven before content moves.

**Files:**
- NEW `Portal/onboarding/onboarding-shell.css` (empty placeholder w/ header comment)
- NEW empty `Portal/onboarding/steps/{tailscale,api_keys,embeddings,optional_integrations,transcription,web_search,image,pair_phone,cli_agents,operator,done,welcome}.css`
- `Portal/onboarding/onboarding.css` (PREPEND the `@import` manifest above the existing rules)

1. Create `onboarding-shell.css`:
   ```css
   /* onboarding-shell.css — SHARED design system for the onboarding wizard.
    * Everything used by MORE THAN ONE step: page chrome (header/progress/
    * footer), .ob-step layout, .ob-cta + button family, status primitives
    * (.ob-status-badge / .ob-status-pill), the cross-step .ob-cli-agent-*
    * card family, .ob-section-card / .ob-card-selected, the hub grid + rail,
    * loading/error placeholders, shared @keyframes, and responsive chrome.
    *
    * Tokens come from onboarding-tokens.css (loaded first via index.html).
    * Per-step-only styles live in steps/<name>.css, imported by onboarding.css.
    */
   ```
2. Create the 12 empty `steps/*.css` files each with a one-line header, e.g. `Portal/onboarding/steps/done.css`:
   ```css
   /* done.css — styles unique to the Done / handoff step. Imported by onboarding.css. */
   ```
   (welcome.css can stay a one-line stub — welcome currently uses only shell `.ob-feature*` rules; keep the file for manifest symmetry.)
3. PREPEND to `onboarding.css` (above line 1, keeping the entire existing body untouched below the manifest for now). The `@import` order follows the canonical section order (D2: tailscale, api_keys, embeddings, optional_integrations, transcription, web_search, image, pair_phone, cli_agents, operator), with welcome/done at the end:
   ```css
   /* onboarding.css — THIN IMPORT MANIFEST.
    * The wizard's CSS is split into a shared shell + per-step files.
    * One <link> in index.html → this file → @imports below. No build step.
    * (Rules still inline below the manifest are migrated in M5 Tasks 6-9;
    *  once empty, the inline block is deleted and only @imports remain.)
    */
   @import "onboarding-shell.css";
   @import "steps/tailscale.css";
   @import "steps/api_keys.css";
   @import "steps/embeddings.css";
   @import "steps/optional_integrations.css";
   @import "steps/transcription.css";
   @import "steps/web_search.css";
   @import "steps/image.css";
   @import "steps/pair_phone.css";
   @import "steps/cli_agents.css";
   @import "steps/operator.css";
   @import "steps/done.css";
   @import "steps/welcome.css";
   ```
   CSS rule: `@import` statements must precede all other rules — they're at the very top, so this is valid even with the existing rules below.
4. **Chrome verify:** navigate `?step=welcome`, screenshot, compare to `m5-before/before-welcome.png` — must be **pixel-identical** (imports are empty; the monolith below still does all the work). Check `read_network_requests`: confirm `onboarding-shell.css` + each `steps/*.css` return `200` (not 404) — a 404 import is silently ignored but pollutes the console.
5. Commit all new files + the manifest prepend:
   ```
   refactor(onboarding-css): add @import manifest scaffold + empty shell/step css

   onboarding.css now begins with @imports for onboarding-shell.css and
   steps/<name>.css (all empty); the existing monolith rules stay inline
   below and remain authoritative. Pixel-identical. Rules migrate next.
   ```

---

### Task 6: Migrate shared shell rules (chrome + primitives, L1–L961) into `onboarding-shell.css`

Move the entire top-of-file shared block — reset, body, header, progress, `.ob-step*`, features, CTA/secondary/back/skip, focus-visible, loading/error, footer, `@keyframes ob-fade-up`/`ob-bar-grow`/`ob-underline-grow`, responsive — PLUS the status-badge/action-card/code-block/disclosure/step-nav primitives (L638–961) that the design names as shared shell. These are byte-moved, not rewritten.

**Files:**
- `Portal/onboarding/onboarding-shell.css` (append the moved block)
- `Portal/onboarding/onboarding.css` (delete L12–961 of the original body — i.e. everything from the reset through the tailscale responsive collapse at ~L961, EXCEPT keep the `@import` manifest at the very top)

1. Cut original `onboarding.css` lines covering `*, *::before` (orig L12) through the `@media (max-width: 720px){ .ob-code-block … }` block (orig L958-961) and append verbatim to `onboarding-shell.css` (after its header comment). This includes: body + `::before`/`::after`, header, `.ob-progress*`, `.ob-step` + sigil + body + title + lede + helper, `.ob-features`/`.ob-feature*`, `.ob-cta*`/`.ob-secondary`/`.ob-back`/`.ob-skip`, focus-visible, `.ob-loading`/`.ob-step-missing`/`-error`, footer, `@keyframes ob-fade-up`, the two `@media` chrome blocks, AND the status-badge/action-card/code-block/disclosure/step-nav primitives (orig L638-961).
   - Rationale for including L638-961 in the shell: `.ob-status-badge`, `.ob-action-card`, `.ob-code-block`, `.ob-copy-btn`, `.ob-disclosure`, `.ob-step-nav` are explicitly called out as reusable primitives in the file's own comment (L634-636) and are consumed by api_keys/pair_phone/done, not just tailscale.
   - LEAVE in `onboarding.css` (do NOT move yet): the tailscale-only Branch-specific blocks from orig L1865+ (off-token, restyled later) and all step-specific blocks L963+ — those migrate in Tasks 7-9.
   - Keep the duplicate `html { scroll-behavior }` only once.
2. **Chrome verify (full sweep — this is the biggest move):** for welcome / tailscale / api_keys / done, navigate + screenshot, diff against `m5-before/`. Header bar, progress meter, sigil numerals, CTA hard-shadow, footer dots, the tailscale status-badge + code-block + disclosure must all be pixel-identical. If ANY element loses styling, the rule was missed in the cut — re-check.
3. Confirm shell now contains the chrome:
   ```bash
   grep -c "ob-cta\|ob-header\|ob-step-sigil\|ob-status-badge\|ob-disclosure" Portal/onboarding/onboarding-shell.css
   ```
   Expected: a non-zero count covering all five families. **(Weak assertion — the pixel diff in step 2 is the real gate.)**
4. Commit:
   ```
   refactor(onboarding-css): move shared chrome + primitives into onboarding-shell.css
   ```

---

### Task 7: Migrate cross-step `.ob-cli-agent-*` family + `.ob-card-selected` + dedupe `@keyframes ob-spin` into the shell

The `.ob-cli-agent-*` card family is used by 5 steps (cli_agents, embeddings, image, transcription, web_search) — it is **shared**, so it belongs in the shell, NOT in any one step file. Also relocate the M0-owned `.ob-card-selected` rule (verified in Task 3), and resolve the duplicate `@keyframes ob-spin` (orig L1978 AND L2558 — two definitions).

**Files:**
- `Portal/onboarding/onboarding-shell.css` (append the agent-card family + the relocated `.ob-card-selected` rule + one `ob-spin`)
- `Portal/onboarding/onboarding.css` (delete orig L2386-2561 `.ob-cli-agent-*` block, the M0-authored `.ob-card-selected` block, and the SECOND `@keyframes ob-spin` at orig L2558-2561; delete the FIRST `@keyframes ob-spin` at orig L1978 too — it'll be re-added once in shell)

1. Move the entire `.ob-cli-agent-*` block (orig L2386-2556) to `onboarding-shell.css`. **Relocate** (byte-move, do NOT rewrite — this is M0's rule) the `.ob-card-selected` rule there too (group it right after the `[data-state]` rules).
2. There are **two** identical-purpose `@keyframes ob-spin`: orig L1978 (`to{transform:rotate(360deg)}`) and orig L2558-2561 (`from…to…`). Delete both from `onboarding.css`; add ONE canonical copy to the shell:
   ```css
   /* Single spin keyframe — was duplicated (ob-banner-spinner + cli-agent re-check). */
   @keyframes ob-spin { to { transform: rotate(360deg); } }
   ```
3. Verify only one `ob-spin` definition project-wide, and that `.ob-card-selected` was relocated (not duplicated):
   ```bash
   grep -rn "@keyframes ob-spin" Portal/onboarding/
   grep -rc "ob-card-selected" Portal/onboarding/onboarding.css Portal/onboarding/onboarding-shell.css
   ```
   Expected: exactly **one** `@keyframes ob-spin` line, in `onboarding-shell.css`; and the `.ob-card-selected` definition now in `onboarding-shell.css`, gone from `onboarding.css`.
4. **Chrome verify:** navigate `?step=cli_agents` and `?step=embeddings`; screenshot; diff vs `m5-before/` — the 3-card agent grid + selected-model highlight must be unchanged. Hover the Re-check button on cli_agents and confirm the ↻ still spins (proves `ob-spin` resolves).
5. Commit:
   ```
   refactor(onboarding-css): move shared .ob-cli-agent-* card family + .ob-card-selected to shell; dedupe @keyframes ob-spin
   ```

---

### Task 8: Migrate per-step blocks (api_keys, optional_integrations, pair_phone, operator, done) into their `steps/*.css`

Move each step-EXCLUSIVE block out of the monolith into its colocated file. Each move is byte-preserving; commit after EACH step file so a regression bisects to one step.

**Files (per move):**
- api_keys: orig L963-1182 → `steps/api_keys.css`
- optional_integrations: orig L1184-1296 (integration/walkthrough/gmail-fields) + L1298-1436 (creds card) + L2330-2384 (gmail-uri list) → `steps/optional_integrations.css`
- pair_phone: orig L1438-1616 (qr/pair states) + L2283-2328 (prereq + mark-done) → `steps/pair_phone.css`
- operator: orig L1618-1728 → `steps/operator.css`
- done: orig L1730-1801 (summary + cta-large) + L2078-2281 (restart CTA + restart-status + logs modal) + L2563-2596 (update-banner) → `steps/done.css`

1. For each step in order, cut its blocks from `onboarding.css` and append to its `steps/<name>.css` (verbatim — restyle happens later). Do **5 separate commits** (one per step file).
2. After EACH step move, **Chrome verify** that step + diff against `m5-before/`:
   - api_keys: provider cards, input rows, validate pill states.
   - optional_integrations: Gmail card, walkthrough disclosure, creds dropzone, redirect-URI rows.
   - pair_phone: QR frame + corner brackets, prereq banner, mark-done button.
   - operator: name rows, add-row dashed button, existing-operator rows.
   - done: summary list glyphs, large CTA, restart CTA, View-Logs modal (open it), update banner.
3. After all 5: confirm no orphaned step selectors remain inline:
   ```bash
   grep -cn "ob-provider-card\|ob-creds\|ob-qr-frame\|ob-operator-row\|ob-summary-row\|ob-logs-modal" Portal/onboarding/onboarding.css
   ```
   Expected: `0` (all migrated). **(Weak assertion — the per-step pixel diffs in step 2 are the real gate.)**
4. Commit messages (one each), e.g.:
   ```
   refactor(onboarding-css): colocate api_keys styles into steps/api_keys.css
   ```

---

### Task 9: Migrate remaining tailscale-only + embeddings-only blocks; reduce `onboarding.css` to pure manifest

Move the tailscale Branch-specific blocks (the off-token L1865+ region that is tailscale-exclusive: auth-prompt/link/banner/install-stream/auth-url-card, plus the on-token tailnet-url card) and the embeddings-only `.ob-emb-*` block into their step files, leaving `onboarding.css` containing ONLY the `@import` manifest.

**Files:**
- tailscale: orig L1803-1863 (tailnet-url card) + L1865-2028 (auth/banner/install-stream/auth-url) → `steps/tailscale.css`
- embeddings: orig L2598-2765 (`.ob-emb-*`) → `steps/embeddings.css`
- `Portal/onboarding/onboarding.css` → manifest only

1. Cut the tailscale blocks → `steps/tailscale.css`; cut the `.ob-emb-*` block → `steps/embeddings.css`. (Both still off-token / on-token as-is — restyle is Tasks 10-13.)
2. Confirm `onboarding.css` is now JUST the manifest:
   ```bash
   grep -vE '^\s*(/\*|\*|@import|$)' Portal/onboarding/onboarding.css | grep -v '\*/' | head
   ```
   Expected: **no output** (no rule bodies remain — only comments + `@import`).
   ```bash
   wc -l Portal/onboarding/onboarding.css
   ```
   Expected: ~20 lines. **(Both are weak structural assertions — the visual sweep in step 3 is the real correctness gate.)**
3. **Chrome verify (every step + hub — THE final correctness gate of the split):** sweep all 13 URLs from Task 1, diff each against `m5-before/`. Every step must be pixel-identical to before M5 began. A passing grep in step 2 does NOT substitute for this sweep.
4. Confirm total size moved (no rules lost):
   ```bash
   cat Portal/onboarding/onboarding-shell.css Portal/onboarding/steps/*.css | wc -l
   ```
   Sanity: combined line count ≈ original monolith body (minus the deleted duplicate `ob-spin` + the inline `SELECTED_STYLE` already removed).
5. Commit:
   ```
   refactor(onboarding-css): finish split — onboarding.css is now a pure @import manifest
   ```

---

### Task 10: SAFE RESTART POINT — restart + full split smoke

The CSS architecture is fully split. Restart so the served tree is exercised end-to-end (catches any `@import` path that resolves in your shell but not under the service's working dir).

**Files:** none (operational).

1. `sudo systemctl restart blackbox.service`; wait-loop as in Task 4.
2. **Chrome verify:** hard-refresh (`Ctrl+Shift+R`) `?step=welcome`, `?step=done`, `?mode=manage`. Open `read_network_requests`: every `steps/*.css` + `onboarding-shell.css` returns `200`. Console clean. Visual matches `m5-before/`.
3. No commit.

---

### Task 11: Restyle tailscale off-token blocks onto tokens (kill the blue bleed + amber literals)

Now the split is done, finish the normalize. `steps/tailscale.css` is the worst offender: `var(--accent, #4a9eff)` (undefined → blue), `rgba(255,180,50,…)` amber literals, the inline `var(--radius-md, 8px)` fallbacks, `rgba(255,255,255,…)` greys, hard-coded `#ff7878`/`#78ff9b`.

**Files:** `Portal/onboarding/steps/tailscale.css`

1. Global replacements within `steps/tailscale.css` (preserve structure, swap colors/radii to tokens):
   - `var(--accent, #4a9eff)` → `var(--ob-accent)` (this kills the blue bleed on `.ob-auth-link`, `.ob-banner-link`, `.ob-banner-link-primary`, `.ob-copy-btn-primary`, `.ob-auth-hint a`).
   - The inline radius fallbacks `var(--radius-md, 8px)` / `var(--radius-sm, 6px)` / `var(--radius-lg, 12px)` → `var(--ob-radius-md)` / `var(--ob-radius-sm)` / `var(--ob-radius-lg)` (D1: migrate the inline refs onto M0's canonical `--ob-radius-*` scale — drop the literal fallback; M0's tokens carry the 8/6/12px values, so this is visually identical).
   - amber literals `rgba(255,180,50,X)` → `var(--ob-attention)` for solid color, `var(--ob-attention-soft)` for the soft backgrounds (`.ob-auth-hint`, `.ob-banner-warn`) — `--ob-attention-soft` is M0-defined; do NOT inline a `color-mix`.
   - `rgba(255,255,255,0.85|0.8|0.75|0.7|0.6)` body greys → `var(--ob-text-secondary)` / `var(--ob-text-tertiary)`.
   - `color: #ff7878` (`.ob-auth-err`) → `var(--ob-accent)`; `#78ff9b` (`.ob-auth-ok`) → `var(--ob-success)`.
   - info-banner cyan `rgba(120,200,255,…)` → keep as `var(--ob-info)` based tints (`--ob-info` = `#4dd0e1` already exists).
   - install-stream green-badge `rgba(80,200,100,…)` → `var(--ob-success)` / `var(--ob-success-soft)`.
   - Restyle `.ob-banner-link-primary` / `.ob-copy-btn-primary` to the brutalist hard-shadow CTA idiom: `border-radius: 0; box-shadow: 3px 3px 0 var(--ob-text-primary);` on the accent buttons.
2. Verify no off-token refs remain in this file — and specifically that the inline `--radius-*` refs were MIGRATED to `--ob-radius-*` (not deleted):
   ```bash
   grep -nE '\-\-accent[,)]|#4a9eff|255, ?180, ?50|var\(--radius-(sm|md|lg)' Portal/onboarding/steps/tailscale.css
   ```
   Expected: **no output** (the stale `var(--radius-*)` inline refs are gone). Then confirm the migration landed:
   ```bash
   grep -cn "var(--ob-radius-" Portal/onboarding/steps/tailscale.css
   ```
   Expected: **non-zero** (refs now point at M0's `--ob-radius-*` scale). Do NOT assert `--ob-radius-*` is absent — those are the real tokens and MUST remain.
3. **Chrome before/after:** the tailscale step has 3 branches. Drive to (or, if state can't be forced, screenshot whichever branch renders) and compare to `m5-before/before-tailscale.png`. Assert: NO blue anywhere (the auth-link is now RED), warning banners are AMBER not orange-literal, sharp corners. Capture `m5-after/after-tailscale.png`.
4. Commit:
   ```
   style(onboarding): normalize tailscale step off-token CSS → tokens (kill blue bleed, migrate radius)
   ```

---

### Task 12: Restyle done-step late-era components (restart CTA, logs modal, update banner) onto tokens

**Files:** `Portal/onboarding/steps/done.css`

1. Within `steps/done.css`:
   - `.ob-cta-restart`: replace `rgba(255,180,50,0.95)` bg → `var(--ob-attention)`; keep the hard-shadow `4px 4px 0 var(--ob-text-primary)` (already brutalist); restyle the glow to `var(--ob-attention)`-based. The `.ob-cta-restart-done` green state → `var(--ob-success)`. Drop the `rgba(255,255,255,…)` passive-state greys → `var(--ob-surface-elevated)` / `var(--ob-text-tertiary)`.
   - `.ob-restart-status-warn` `rgba(255,180,50,0.9)` → `var(--ob-attention)`; `-done` green → `var(--ob-success)`; `-passive` → `var(--ob-text-tertiary)`.
   - `.ob-cta-secondary` (View Logs): the inline `var(--radius-md,8px)` → `var(--ob-radius-md)`; greys → tokens; make border sharp/brutalist.
   - `.ob-logs-modal-*`: inline `var(--radius-lg,12px)` / `var(--radius-sm,6px)` → `var(--ob-radius-lg)` / `var(--ob-radius-sm)`; `#0a0a0a` → `var(--ob-surface-elevated)`; status pips `rgba(255,200,80…)` (connecting) → `var(--ob-attention)`, connected `rgba(120,255,155…)` → `var(--ob-success)`, disconnected `rgba(255,120,120…)` → `var(--ob-accent)`.
   - `.ob-update-banner` cyan → `var(--ob-info)` tint; `.ob-update-banner-major` amber → `var(--ob-attention)`.
2. Verify off-token refs gone and inline radius migrated to `--ob-radius-*`:
   ```bash
   grep -nE '#4a9eff|--accent[,)]|255, ?180, ?50|var\(--radius-(sm|md|lg)' Portal/onboarding/steps/done.css
   ```
   Expected: **no output**. (Do not assert against `--ob-radius-*` — those are the migrated, correct refs.)
3. **Chrome before/after:** navigate `?step=done`, screenshot full page + open the View-Logs modal + (if restart status renders) the restart CTA. Diff vs `m5-before/before-done.png`: restart CTA is now AMBER-token (not orange literal), logs modal corners sharp, status pips on-token. Save `m5-after/after-done*.png`.
4. Commit:
   ```
   style(onboarding): restyle done-step restart CTA + logs modal + banners onto tokens
   ```

---

### Task 13: Restyle agent-card family + embeddings + remaining off-token blocks; repoint to state tokens

The `.ob-cli-agent-card[data-state]` family (now in the shell) and `.ob-emb-*` (embeddings.css) + optional_integrations off-token bits still use literal rgba state colors. Repoint to the canonical `--ob-state-*` tokens so all surfaces share one status vocabulary.

**Files:**
- `Portal/onboarding/onboarding-shell.css` (`.ob-cli-agent-card[data-state]`, badges, action buttons, hints)
- `Portal/onboarding/steps/embeddings.css`
- `Portal/onboarding/steps/optional_integrations.css`
- `Portal/onboarding/steps/pair_phone.css`

1. In `onboarding-shell.css`, repoint the agent-card state borders:
   ```css
   .ob-cli-agent-card[data-state="missing"]    { border-left-color: var(--ob-accent); }
   .ob-cli-agent-card[data-state="needs-auth"] { border-left-color: var(--ob-attention); }
   .ob-cli-agent-card[data-state="ready"]      { border-left-color: var(--ob-success); background: var(--ob-success-soft); }
   ```
   Badges: `-ok` → `var(--ob-success)`/`-soft`; `-needs-auth` → `var(--ob-attention)` + `var(--ob-attention-soft)`; `-missing` → `var(--ob-accent)`/`-soft`; `-info` → `var(--ob-info)` tint. Action buttons `-install` red literal → `var(--ob-accent)`; `-auth` amber literal → `var(--ob-attention)`; keep the brutalist hard-shadows but on tokens. The inline `var(--radius-*)` fallbacks → `var(--ob-radius-*)`.
2. In `steps/embeddings.css`: `.ob-emb-progress-fill` `var(--ob-accent,#cc0000)` → `var(--ob-accent)` (drop fallback); `.ob-emb-banner-broken` red literal → `var(--ob-accent)`/`-soft`; `.ob-emb-blockers` amber `rgba(255,220,150…)` → `var(--ob-attention)`; warm-toggle green `rgba(120,200,140…)` → `var(--ob-success)`; inline `var(--radius-*)` → `var(--ob-radius-*)`; `rgba(255,255,255,…)` greys → text tokens.
3. In `steps/optional_integrations.css`: `.ob-gmail-uri-copy` `var(--accent,#4a9eff)` → `var(--ob-accent)` + sharp corners; greys → tokens; inline `var(--radius-*)` → `var(--ob-radius-*)`. (The on-token `.ob-creds-*` block needs no change.)
4. In `steps/pair_phone.css`: `.ob-pair-prereq` cyan `var(--accent,#4a9eff)` link → `var(--ob-accent)`; `.ob-pair-mark-done` green literals → `var(--ob-success)`/`-soft`; inline `var(--radius-*)` → `var(--ob-radius-*)`.
5. Verify NO stale off-token / inline-radius refs remain anywhere in the wizard CSS (note: this asserts the migrated `--ob-radius-*` refs are NOT flagged — only the stale `var(--radius-*)` form is):
   ```bash
   grep -rnE '#4a9eff|var\(--accent[,)]|255, ?180, ?50|var\(--radius-(sm|md|lg|full)|#ff7878|#78ff9b' Portal/onboarding/onboarding-shell.css Portal/onboarding/steps/*.css
   ```
   Expected: **no output**.
6. **Chrome before/after:** `?step=cli_agents` (all card states if forceable), `?step=embeddings` (progress + banners), `?step=optional_integrations`, `?step=pair_phone`. Diff each vs `m5-before/`: state colors now read green=ready / amber=attention / red=error consistently. Save `m5-after/`.
7. Commit:
   ```
   style(onboarding): repoint agent-card + embeddings + integrations state colors to --ob-state-* tokens
   ```

---

### Task 14: Delete dead rules + no-op selectors

Remove the rules the file's own comments flag as unused/no-op, now safely in their final homes.

**Files:** `Portal/onboarding/onboarding-shell.css`

1. Delete these dead/no-op rules (orig L793-808 region, now in shell after Task 6):
   - `.ob-cta:hover .ob-cta-arrow:not([data-glyph="arrow"])` — empty `/* No-op safety */` body.
   - `.ob-cta-row .ob-cta:has(.ob-cta-arrow:only-child) .ob-cta-arrow` — explicitly `/* unused */`.
   - The `#ob-recheck-btn:hover .ob-cta-arrow { transform: rotate(60deg); }` is LIVE (recheck button) — **keep it**, but verify the id still exists:
     ```bash
     grep -rn "ob-recheck-btn" Portal/onboarding/steps/
     ```
     If no output, the id is dead and the rule can go too; if present, keep.
2. Remove any now-empty comment headers left behind by the migrations (e.g. the `/* Re-check button uses the ↻ … */` preamble that only described the deleted no-ops).
3. **Chrome verify:** `?step=tailscale` (recheck button hover still rotates ↻ if id present), `?step=welcome` (CTA arrow still translates on hover). No visual regression vs `m5-after/`.
4. Confirm shrinkage:
   ```bash
   echo "manifest+shell+steps total bytes:"
   cat Portal/onboarding/onboarding.css Portal/onboarding/onboarding-shell.css Portal/onboarding/steps/*.css | wc -c
   echo "original was 81110"
   ```
   Expected: total < 81110 (dead-rule + dup-keyframe + inline-style deletions net a reduction; the manifest overhead is tiny).
5. Commit:
   ```
   style(onboarding): delete dead/no-op CSS rules flagged in-comment
   ```

---

### Task 15: SAFE RESTART POINT — final restart + full before/after visual pass + dev snapshot

**Files:** none (operational) + snapshot.

1. `sudo systemctl restart blackbox.service`; wait-loop as in Task 4.
2. **Chrome final sweep (the milestone's authoritative correctness gate — grep checks below are secondary):** all 13 URLs from Task 1 → screenshot into `m5-after/`. For each, assert against the corresponding `m5-before/`:
   - Layout/spacing pixel-identical (split is non-visual).
   - Color deltas ONLY where intended: zero blue anywhere; warnings are amber-token; state colors consistent green/amber/red; sharp corners on late-era CTAs.
   - Console clean, all CSS `200`.
3. Confirm the architecture end-state (structural sanity — the `--ob-radius-*` tokens are EXPECTED to be present; only the stale `var(--radius-*)` form and off-token colors must be absent):
   ```bash
   wc -l Portal/onboarding/onboarding.css                 # ~20 (manifest only)
   ls Portal/onboarding/onboarding-shell.css Portal/onboarding/util.js
   ls Portal/onboarding/steps/*.css | wc -l               # 12
   grep -rcE '#4a9eff|var\(--accent[,)]|255, ?180, ?50|var\(--radius-(sm|md|lg|full)' Portal/onboarding/onboarding-shell.css Portal/onboarding/steps/*.css | grep -v ':0' || echo "ALL CLEAN"
   ```
   Expected: manifest ~20 lines, 12 step css files, `ALL CLEAN`. (Note `util.js` is M0-owned and only listed here to confirm the import target still exists — M5 did not author it.)
4. If everything passes, mint the milestone snapshot via `/snapshot-dev` (operator resolved dynamically per `resolve-operator`). Record: M5 complete — CSS split into shell + 12 colocated step files, manifest-only `onboarding.css`, `cssEscape` rewired to M0's `util.js`, `.ob-card-selected` (M0-owned) replacing inline `SELECTED_STYLE`, full off-token→token normalize (blue bleed killed, amber-for-attention adopted, inline `var(--radius-*)` migrated onto M0's `--ob-radius-*` scale), dead rules + duplicate `@keyframes ob-spin` deleted, before/after screenshots in scratchpad. List the commit range.

**This is the milestone-final snapshot — do NOT also call `/mint`.**

---

## Milestone M6: Verification & Snapshot

This is the final, cross-cutting milestone: it proves the whole uplift (M0–M5) actually works rather than asserting it does. It runs the complete backend pytest suite green, then drives an explicit Claude-in-Chrome smoke **matrix** over the live wizard (`http://localhost:9091`) covering first-run linearity, post-completion hub landing, every section tile's resolved state, SSE live-fill, revisit round-trips, the left-rail jump, `pair_phone` already-paired truth, correct sigil numbering, and a token-bleed audit (no stray blue/amber). It produces an after-screenshot set diffed against the pre-uplift baseline, and ends by minting a dev snapshot via the `/chat/save` auto-mint procedure with embedding verification. Per @superpowers:verification-before-completion, **every assertion in this milestone is backed by a command/screenshot whose output is read — no success is claimed without evidence.** No production code is written here except trivial fixes for any regression the matrix surfaces (each such fix is its own TDD micro-loop and commit).

**Preconditions:** M0–M5 are merged on `main`; the Claude-in-Chrome extension is connected (verify with `mcp__claude-in-chrome__list_connected_browsers` before any navigate); the pre-uplift baseline screenshots captured at the start of M0 exist under `docs/onboarding/screenshots/before/` (ASSUMPTION: M0's first task captured them there — if absent, see Task 2's fallback).

---

### Task 1 — Full backend pytest suite green (all onboarding + wizard tests)

Run the complete onboarding/wizard backend suite and confirm zero failures before any frontend work. This is the regression gate for M1's `/onboarding/status` + `/status/stream` and the M0 parity guard. @superpowers:verification-before-completion — read the actual summary line, do not assume.

**Files:** (read-only verification; no edits)
- `Orchestrator/tests/test_onboarding_steps_parity.py`
- `Orchestrator/tests/test_onboarding_validators.py`
- `Orchestrator/tests/test_onboarding_image.py`
- `Orchestrator/tests/test_onboarding_web_search.py`
- `Orchestrator/tests/test_embeddings_onboarding_step.py`
- `Orchestrator/tests/test_stt_onboarding.py`
- `Orchestrator/tests/test_stt_onboarding_step.py`
- `Orchestrator/tests/test_wizard_routes.py`
- `pytest.ini` (root; `pythonpath = .`, `testpaths = Orchestrator/tests`)

**Command (run from repo root):**
```bash
Orchestrator/venv/bin/pytest \
  Orchestrator/tests/test_onboarding_steps_parity.py \
  Orchestrator/tests/test_onboarding_validators.py \
  Orchestrator/tests/test_onboarding_image.py \
  Orchestrator/tests/test_onboarding_web_search.py \
  Orchestrator/tests/test_embeddings_onboarding_step.py \
  Orchestrator/tests/test_stt_onboarding.py \
  Orchestrator/tests/test_stt_onboarding_step.py \
  Orchestrator/tests/test_wizard_routes.py \
  -p no:cacheprovider -q
```

**Expected output (must read the actual summary line):**
```
........................................
NN passed in M.MMs
```
— **zero** `failed`/`errored`. If any test fails, STOP the milestone, treat it as a regression introduced by M0–M5, fix it via @superpowers:systematic-debugging + @superpowers:test-driven-development in its own commit, and re-run until green.

**Then also run the full collection to confirm no import-time breakage anywhere** (the wizard routes import the whole app):
```bash
Orchestrator/venv/bin/pytest Orchestrator/tests/ -q -p no:cacheprovider --co 2>&1 | tail -5
```
Expected: a collected-count line, no `ERROR` / `ImportError`.

**Contract gate — `set_current()` stays UNEXPOSED (no random-access route):** `set_current()` is an internal `_state` mutator (it is called *inside* `/step/complete` to advance the cursor) and MUST NOT be reachable as its own `@router` route — exposing it would let a caller arbitrarily jump the persisted cursor, breaking the linear-funnel invariant. Assert this with a source-scoped grep gate (run from repo root):
```bash
# 1) set_current must NOT appear in any route handler signature or be wired as a path op.
#    Legal: it appears ONLY as an internal call (e.g. `_state.set_current(...)`) inside another handler's body.
#    Illegal: a @router.<verb>(...) decorator whose handler IS named set_current, or a route path literally
#    named /set-current / /set_current, or add_api_route(... set_current ...).
ROUTES_FILE=Orchestrator/routes/onboarding_routes.py

# (a) no route path advertises set_current
if grep -nE '@router\.(get|post|put|delete|patch)\([^)]*set[_-]current' "$ROUTES_FILE"; then
  echo "FAIL: a @router decorator references set_current as a path"; exit 1
fi
# (b) no handler function is itself named set_current directly under a @router decorator
if grep -nE '@router\.' -A1 "$ROUTES_FILE" | grep -nE 'def\s+set_current\b'; then
  echo "FAIL: set_current is defined as a route handler"; exit 1
fi
# (c) no manual route registration exposes it
if grep -nE 'add_api_route\([^)]*set_current' "$ROUTES_FILE"; then
  echo "FAIL: set_current wired via add_api_route"; exit 1
fi
# (d) sanity: it DOES still exist as an internal call (the legal usage), proving the grep is meaningful
grep -nE '\.set_current\(' "$ROUTES_FILE" | grep -qv '@router' && echo "OK: set_current is internal-call-only" || { echo "FAIL: set_current internal call missing"; exit 1; }
echo "CONTRACT-OK: set_current unexposed"
```
**Assert** the final line reads `CONTRACT-OK: set_current unexposed` (and `OK: set_current is internal-call-only`); any `FAIL:` line is a contract breach → STOP and treat as a regression. (Today's tree: `set_current` appears only at `onboarding_routes.py:65` as `_state.set_current(ALL_STEPS[idx + 1])` inside the `/step/complete` handler — internal, not a route. This gate locks that.) If a matching parity/route test (`test_wizard_routes.py`) does not already encode this, add a `test_set_current_not_exposed` asserting no route path contains `set` + `current` and re-run Task 1's suite.

**Commit** (only a doc/log artifact if you record the run; otherwise nothing to commit — proceed). If you fixed a regression (including adding the `set_current` route-exposure test):
```bash
git add <changed files> && git commit -m "fix(onboarding): <regression> surfaced by M6 backend gate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 2 — SAFE RESTART POINT + baseline screenshot reconciliation

Restart the service so the live wizard reflects all merged M0–M5 work (prod serves from the working tree, but a restart guarantees the SSE routes, new tokens, and CSS manifest are all live), then confirm the before-baseline exists for the visual diff in Task 12.

**Files:**
- `docs/onboarding/screenshots/before/` (read-only check)
- create `docs/onboarding/screenshots/after/` (mkdir)

**Restart (pre-authorized — see memory `feedback_blackbox_restart_authorized`):**
```bash
sudo systemctl restart blackbox.service
```
Wait for warm-up (60–90s; snapshot index rebuild) using a Monitor-style until-loop, NOT foreground sleep:
```bash
until curl -s -o /dev/null -w "%{http_code}" http://localhost:9091/onboarding/ | grep -q 200; do :; done; echo "up"
```
Expected: `up`.

**Verify the M1 endpoint is now live (it 404'd pre-M1):**
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:9091/onboarding/status
```
Expected: `200`.

**Reconcile baseline:**
```bash
mkdir -p docs/onboarding/screenshots/after
ls docs/onboarding/screenshots/before/*.png 2>/dev/null | wc -l
```
- If the count is **> 0**, the M0 baseline exists — proceed.
- If **0** (ASSUMPTION above failed): the pre-uplift UI is gone, so a true before/after is impossible. Record this explicitly in the snapshot (Task 13) and instead diff the after-set against the *design intent* (the screenshots embedded/referenced in the investigation doc). Do NOT fabricate a before-set.

**Commit:**
```bash
git add docs/onboarding/screenshots/.gitkeep 2>/dev/null; mkdir -p docs/onboarding/screenshots/after && touch docs/onboarding/screenshots/after/.gitkeep
git add docs/onboarding/screenshots/after/.gitkeep
git commit -m "chore(onboarding): create after-screenshot dir for M6 visual diff

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 3 — Chrome harness up + first-run linear flow intact (matrix row 1)

Confirm the Claude-in-Chrome extension is connected, then assert the sacred invariant: a true first-run (no `.onboarding_complete` sentinel) still renders the **linear** guided flow with **no left-rail**. @claude-in-chrome.

**Tooling:** Load the Chrome tools in ONE ToolSearch call:
```
ToolSearch select:mcp__claude-in-chrome__list_connected_browsers,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__computer,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__javascript_tool
```

**Matrix row 1 — first-run linear.** This row has TWO sub-checks: **1a (revisit-rendered, PARTIAL)** drives the linear renderer via `?step=welcome` *without* mutating sentinel state, and **1b (true first-run, FULL)** exercises the real bootstrap branch behind a *disposable/monkeypatched* sentinel so the row earns a real PASS rather than only a revisit-path proxy.

1. `list_connected_browsers` → assert ≥1 browser. If none, STOP: extension must be connected before verify milestones (design §6).
2. Inspect sentinel state (do NOT delete it yet):
   ```bash
   ls -la /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/.onboarding_complete 2>&1 | head -1
   ```
   On this dev box the sentinel exists (box is configured), so a bare `/onboarding/` lands on the hub — it does NOT exercise the first-run bootstrap.

**Sub-check 1a — revisit-rendered linear (PARTIAL, non-destructive):**
3. `navigate` → `http://localhost:9091/onboarding/?step=welcome`
4. `read_page` → **assert** the welcome step's "4 pillars" framing renders AND there is **no** `.ob-left-rail` element in the linear funnel (left-rail is hidden first-run per locked decision #8). Use `javascript_tool`:
   ```js
   ({ hasWelcome: !!document.querySelector('.ob-step'),
      railHidden: !document.querySelector('.ob-left-rail') || getComputedStyle(document.querySelector('.ob-left-rail')).display === 'none' })
   ```
   **Assert** `hasWelcome === true && railHidden === true`.
5. `computer` screenshot → save reference; this is matrix evidence row 1a.

> **HONESTY NOTE (do not skip):** Sub-check 1a navigates `?step=welcome`, which is a **REVISIT render of the welcome step**, NOT the true first-run bootstrap branch (the codepath taken when `/onboarding/` is hit with **no** sentinel). The revisit path can render the welcome chrome even on a completed box. Therefore 1a alone proves only that the linear welcome *view* still renders — it is recorded as **PARTIAL (revisit-rendered)**. The row is NOT marked full-PASS on 1a alone. Sub-check 1b below exercises the real branch.

**Sub-check 1b — true first-run bootstrap (FULL, behind a disposable sentinel):** temporarily make the box look unconfigured so `/onboarding/` takes the genuine first-run branch, then restore. The sentinel is a small file (`.onboarding_complete`); move it aside rather than deleting it so state is provably recoverable.
6. Snapshot + move the sentinel aside (recoverable):
   ```bash
   SENT=/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/.onboarding_complete
   if [ -f "$SENT" ]; then cp -a "$SENT" /tmp/m6_onboarding_complete.bak && mv "$SENT" "$SENT.m6bak" && echo "sentinel moved aside"; else echo "no sentinel (already first-run)"; fi
   ```
7. `navigate` → `http://localhost:9091/onboarding/` (bare, no `?step=`) — with the sentinel absent this MUST take the **true first-run** branch, rendering the linear funnel (welcome), NOT the hub. `javascript_tool`:
   ```js
   ({ path: location.pathname,
      hasWelcome: !!document.querySelector('.ob-step'),
      hubAbsent: !document.querySelector('.ob-hub') && !document.querySelector('.ob-section-card'),
      railHidden: !document.querySelector('.ob-left-rail') || getComputedStyle(document.querySelector('.ob-left-rail')).display === 'none' })
   ```
   **Assert** `hasWelcome === true && hubAbsent === true && railHidden === true` on the BARE `/onboarding/` URL — this is the genuine first-run linearity proof (the hub must NOT appear when unconfigured).
8. `computer` screenshot → save as row 1b evidence (true first-run).
9. **ALWAYS restore the sentinel** (run even if step 7 failed — use this as a cleanup that cannot be skipped):
   ```bash
   SENT=/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/.onboarding_complete
   if [ -f "$SENT.m6bak" ]; then mv "$SENT.m6bak" "$SENT" && echo "sentinel restored"; elif [ -f /tmp/m6_onboarding_complete.bak ] && [ ! -f "$SENT" ]; then cp -a /tmp/m6_onboarding_complete.bak "$SENT" && echo "sentinel restored from /tmp backup"; else echo "WARN: verify sentinel state manually"; ls -la "$SENT" 2>&1 | head -1; fi
   ```
   Then re-confirm the box is configured again (hub returns) before proceeding to Task 4:
   ```bash
   curl -s http://localhost:9091/onboarding/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('sections', len(d.get('sections',[])))" 2>&1 | head -1
   ```

**Row 1 verdict:** mark **PASS** only if BOTH 1a (revisit-rendered welcome, PARTIAL proxy) AND 1b (true first-run bootstrap on bare `/onboarding/`, FULL) assert true. If you cannot safely run 1b (e.g. sentinel move-aside fails or cannot be restored), record row 1 as **PARTIAL (revisit-rendered only)** in the matrix verdict — do NOT claim full first-run PASS via the revisit path alone.

**Commit:** none (read-only verification; the sentinel is moved-and-restored, not committed). Record pass/fail in your running matrix notes (kept in-message, NOT a .md file).

---

### Task 4 — Post-completion hub lands, no bounce to /ui (matrix row 2)

Assert the headline routing change: a completed box visiting `/onboarding/` renders the hub and does **not** redirect to `/ui`.

**Tooling:** already loaded (Task 3).

**Matrix row 2 — hub landing:**
1. `navigate` → `http://localhost:9091/onboarding/`
2. `read_page` → confirm we are still on `/onboarding/` (NOT bounced). `javascript_tool`:
   ```js
   ({ path: location.pathname, hasHub: !!document.querySelector('[data-hub]') || !!document.querySelector('.ob-section-card') })
   ```
   **Assert** `path` starts with `/onboarding` AND `hasHub === true`. A bounce would show `path === '/ui'` (or the main app) → FAIL.
3. `navigate` → `http://localhost:9091/onboarding/?mode=manage` → same assertion (explicit manage entry must also render the hub).
4. `computer` screenshot → `docs/onboarding/screenshots/after/02-hub-landing.png` (save via the screenshot path).

**Commit:** none.

---

### Task 5 — Every section tile resolves to the correct state (matrix rows 3–12)

Drive the hub and assert each of the 10 sections renders an `.ob-section-card[data-state]` whose resolved state is one of the legal values and matches the persisted truth from `/onboarding/status`. This cross-checks frontend rendering against the backend rollup (the SoT per design §2).

**Tooling:** already loaded.

**First capture backend truth (the oracle):**
```bash
curl -s http://localhost:9091/onboarding/status | python3 -m json.tool > /tmp/m6_status.json
python3 - <<'PY'
import json
d=json.load(open('/tmp/m6_status.json'))
keys={s['key']:s['state'] for s in d['sections']}
expected=['tailscale','api_keys','embeddings','optional_integrations','transcription','web_search','image','pair_phone','cli_agents','operator']
assert sorted(keys)==sorted(expected), f"section keys drift: {sorted(keys)} != {sorted(expected)}"
assert all(v in {'ready','attention','optional','checking'} for v in keys.values()), keys
print("ORACLE", json.dumps(keys))
print("ready_count", d['ready_count'], "total", d['total'])
PY
```
**Assert** the script prints `ORACLE {...}` with all 10 keys and legal states; it raises (non-zero exit) on drift. (The `expected` list is the canonical section order D2: `tailscale, api_keys, embeddings, optional_integrations, transcription, web_search, image, pair_phone, cli_agents, operator` — `sorted()` makes membership order-independent here, but keep the literal in canonical order for consistency with every other surface.)

**Matrix rows 3–12 — tile states match oracle:** in Chrome on `http://localhost:9091/onboarding/`, after the SSE settle (Task 6 covers the transient; here assert the resolved end-state). `javascript_tool`:
```js
Object.fromEntries(
  [...document.querySelectorAll('.ob-section-card[data-state]')]
    .map(c => [c.querySelector('a[href*="step="]')?.getAttribute('href')?.match(/step=([a-z0-9_]+)/)?.[1], c.dataset.state])
)
```
**Assert** the returned map has all 10 section keys and **every** value is in `{ready,attention,optional,checking}`, AND once SSE has fired (give the page a moment, re-query) no tile is left `checking`. Compare each tile's state to `/tmp/m6_status.json`'s persisted `state` for that key — they must match for `ready`/`optional` (the live SSE probe may *upgrade* persisted→attention, which is legal and expected for the attention rows).

**Required + optional sanity:** assert `api_keys`, `embeddings`, `operator` are `required:true` in the payload (per the shared contract) and that the hub groups render `network`, `keys`, `capabilities`, `identity`.

**Commit:** none (read-only). If a tile renders a state the backend never emits (e.g. legacy `warn`), that's a rendering bug → fix in `Portal/onboarding/status.js` (presentational mapping) via a micro-commit.

---

### Task 6 — SSE skeleton→live-fill (matrix row 13)

Assert the live re-validation actually streams: tiles start `checking` and fill via `/onboarding/status/stream`, fired on view only.

**Tooling:** already loaded; add network reader:
```
ToolSearch select:mcp__claude-in-chrome__read_network_requests
```

**Matrix row 13 — SSE live-fill:**
1. **Backend stream smoke (oracle):**
   ```bash
   curl -sN --max-time 8 http://localhost:9091/onboarding/status/stream | head -40
   ```
   **Assert** the output contains `event: section` lines with `data:{...,"state":...}` and a terminating `event: done` with `data:{"ready_count":...,"total":...}`. Zero `event: section` lines → SSE not wired → FAIL.
2. **Frontend wiring:** `navigate` fresh → `http://localhost:9091/onboarding/`, then `read_network_requests` → **assert** a request to `/onboarding/status/stream` exists with an event-stream response. Confirms the hub opens the SSE on view (not on a timer).
3. **Visual skeleton:** immediately after navigate, `computer` screenshot → expect at least one tile showing the `checking` skeleton state; a second screenshot ~moment later shows resolved states. Save the resolved one as `docs/onboarding/screenshots/after/03-hub-resolved.png`.

**Commit:** none.

---

### Task 7 — Revisit round-trip returns to hub (matrix row 14)

Assert `?step=<name>` revisit is state-neutral and, on save, **returns to the hub** (not the old terminal "close page" panel).

**Tooling:** already loaded.

**Matrix row 14 — revisit round-trip:** pick a no-side-effect section to round-trip. Use `operator` (Identity) or `web_search` (a capability) — choose one whose save does not mutate external state destructively. Steps:
1. `navigate` → `http://localhost:9091/onboarding/?step=web_search`
2. `read_page` → **assert** the step renders standalone (no left-rail forced-hidden requirement here; revisit MAY show the rail per decision #8) and there is a Save/continue affordance.
3. Trigger the step's save (click the primary CTA via `computer`, or if the step has no-op save, use `javascript_tool` to invoke the documented return path). After save, `javascript_tool`:
   ```js
   ({ path: location.search, backToHub: !!document.querySelector('.ob-section-card') || /mode=manage/.test(location.search) })
   ```
   **Assert** `backToHub === true` — the post-save view is the hub, NOT a terminal panel. The old behavior (terminal "you may close this page") → FAIL.

**Commit:** none. If revisit lands on the legacy terminal panel, that's the M3 "revisit-returns-to-hub" contract broken → fix in `Portal/onboarding/onboarding.js` revisit save handler, micro-commit.

---

### Task 8 — Left-rail jump (matrix row 15)

Assert the persistent grouped left-rail navigator is present in the hub and a jump click reaches the target section.

**Tooling:** already loaded.

**Matrix row 15 — left-rail jump:**
1. `navigate` → `http://localhost:9091/onboarding/`
2. `javascript_tool` → **assert** the rail exists and lists grouped sections with live pips:
   ```js
   ({ railPresent: !!document.querySelector('.ob-left-rail'),
      items: [...document.querySelectorAll('.ob-left-rail a[href*="step="]')].map(a=>a.getAttribute('href')) })
   ```
   **Assert** `railPresent === true` and `items.length === 10` (one per section).
3. Click the rail entry for `embeddings` via `computer` (or `navigate` to its href). `read_page`/`javascript_tool` → **assert** the embeddings step (or hub-scrolled-to-section) is now the focus. Confirms mid-flow jump works without state bleed.

**Commit:** none.

---

### Task 9 — pair_phone already-paired truth (matrix row 16)

Assert the biggest pre-uplift state-truth bug (H sweep) is fixed: revisiting `pair_phone` when already paired shows the **already-paired** view, not a fresh QR as if unpaired.

**Tooling:** already loaded.

**Matrix row 16 — pair_phone truth:**
1. Determine pairing truth from the backend:
   ```bash
   curl -s http://localhost:9091/onboarding/status | python3 -c "import sys,json; d=json.load(sys.stdin); s=[x for x in d['sections'] if x['key']=='pair_phone'][0]; print('state',s['state'],'items',s['items'])"
   ```
2. `navigate` → `http://localhost:9091/onboarding/?step=pair_phone`
3. `javascript_tool`:
   ```js
   ({ qr: !!document.querySelector('[data-qr], canvas, img[alt*="QR" i]'),
      alreadyPaired: /already paired|paired device|this device is paired/i.test(document.body.innerText) })
   ```
   **Assert:** IF the backend reports `pair_phone` as paired/`ready`, then `alreadyPaired === true` (and a fresh QR must NOT be the primary affordance). If unpaired, a QR is legitimate. This wires the dead `renderAlreadyPaired()` (design §5) — a stale QR on an already-paired box is FAIL.

**Commit:** none. If a fresh QR shows on a paired box, `renderAlreadyPaired()` is still unwired → fix in `Portal/onboarding/steps/pair_phone.js`, micro-commit.

---

### Task 10 — Sigil numbers correct (matrix row 17)

Assert the off-by-N sigil bug is dead: step numbers derive from `STEPS` order, and no two steps show the same number (the old "two steps both show 05" / 8-vs-12 bug).

**Tooling:** already loaded.

**Matrix row 17 — sigil numbering:** walk the linear order and collect the rendered sigil per step.
1. From the backend step list (oracle):
   ```bash
   curl -s http://localhost:9091/onboarding/status | python3 -c "import sys,json; d=json.load(sys.stdin); print([s['step'] for s in d['sections']])"
   ```
   (Note: hub sections are 10; the linear `STEPS` includes `welcome`+`done` — the sigil is over the *linear* list. Cross-check against `Portal/onboarding/onboarding.js` `STEPS`.)
2. For each linear step, `navigate` → `?step=<name>` and `javascript_tool` to read the rendered sigil:
   ```js
   document.querySelector('.ob-sigil, [data-sigil]')?.textContent?.trim()
   ```
3. Collect all sigils into a list. **Assert** they are a strictly increasing, zero-padded sequence with **no duplicates** and length === `STEPS.length`. A duplicate (`05` twice) or a max beyond the count → FAIL. (Scope the read to the step-sigil element only — the `ob-step-sigil-num` `<em>\d{2}` numerals — so welcome.js's `ob-feature-num` `<em>01..04</em>` pillar cards do NOT contaminate the sigil collection.)

**Commit:** none. If sigils duplicate, the M0 "derive sigil from STEPS order" fix regressed → fix in `Portal/onboarding/onboarding.js`, micro-commit.

---

### Task 11 — No blue/amber off-token bleed (matrix row 18)

Assert the design-token discipline holds: the only blue is intentional, amber appears **only** on attention, and there is no undefined-`--accent`→browser-default-blue bleed (the M0 normalization).

**Tooling:** already loaded.

**Matrix row 18 — token bleed audit:**
1. `navigate` → `http://localhost:9091/onboarding/` (hub) and separately `?step=embeddings` and `?step=transcription` (the two steps that carried the inline `SELECTED_STYLE` literal → now `.ob-card-selected`).
2. `javascript_tool` — scan computed colors for off-token blue/amber:
   ```js
   (() => {
     const root = getComputedStyle(document.documentElement);
     const attention = root.getPropertyValue('--ob-attention').trim();
     const bad = [];
     for (const el of document.querySelectorAll('*')) {
       const cs = getComputedStyle(el);
       for (const prop of ['color','background-color','border-left-color','border-color']) {
         const v = cs.getPropertyValue(prop);
         // browser-default link blue / undefined --accent fallbacks
         if (/rgb\(0,\s*0,\s*238\)|rgb\(0,\s*122,\s*255\)|#?0066ff/i.test(v)) bad.push({sel: el.className, prop, v, kind:'blue'});
       }
     }
     return { attentionToken: attention, offTokenBlue: bad.slice(0,10) };
   })()
   ```
   **Assert** `attentionToken` is a non-empty amber (≈`#f5a623`) AND `offTokenBlue` is empty (`[]`). Any populated `offTokenBlue` = the undefined-`--accent` bleed survived → FAIL.
3. **Amber-only-for-attention check:** confirm amber appears only on `[data-state="attention"]` tiles / attention rows. `javascript_tool`:
   ```js
   [...document.querySelectorAll('*')].filter(el => {
     const c = getComputedStyle(el).borderLeftColor || getComputedStyle(el).color;
     return /245,\s*166,\s*35/.test(c); // ~#f5a623
   }).map(el => el.closest('[data-state]')?.dataset.state || el.className)
   ```
   **Assert** every hit is either `attention` or an attention-row class — no `ready`/`optional` tile wears amber.
4. `computer` screenshot of the hub → `docs/onboarding/screenshots/after/04-token-audit.png`.

**Commit:** none. Any bleed → fix the offending rule in the relevant `Portal/onboarding/steps/<name>.css` or `onboarding-shell.css`, micro-commit.

---

### Task 12 — Visual before/after diff + after-set capture

Capture the full after-screenshot set and produce the before/after comparison the design milestone calls for.

**Tooling:** already loaded (`computer`, `navigate`).

**Capture the after-set** (one screenshot per surface; `navigate` then `computer` to the listed path):
- `docs/onboarding/screenshots/after/01-first-run-welcome.png` — `?step=welcome`
- `docs/onboarding/screenshots/after/02-hub-landing.png` — `/onboarding/` (already from Task 4)
- `docs/onboarding/screenshots/after/03-hub-resolved.png` — hub post-SSE (from Task 6)
- `docs/onboarding/screenshots/after/05-revisit-step.png` — `?step=web_search`
- `docs/onboarding/screenshots/after/06-left-rail.png` — hub with rail visible
- `docs/onboarding/screenshots/after/07-pair-phone.png` — `?step=pair_phone`

**Side-by-side diff:** for each before/after pair that exists, lay them out and visually inspect (read both images with the Read tool, which renders them). Produce a concise written before→after delta IN YOUR FINAL MESSAGE (NOT a .md file), e.g.:
- before: linear-only, blue link bleed on CTAs, no hub → after: hub-first, amber attention state, tokenized CTAs.

**Send the key comparison to the user:**
```
SendUserFile([
  "docs/onboarding/screenshots/before/02-hub-landing.png" (if exists),
  "docs/onboarding/screenshots/after/02-hub-landing.png"
], caption: "Onboarding uplift before vs after — hub landing", status: "proactive")
```
(If no before-set exists per Task 2's fallback, send the after-set alone and note the baseline gap.)

**Commit the after-set:**
```bash
git add docs/onboarding/screenshots/after/*.png
git commit -m "docs(onboarding): M6 after-screenshot set for production-uplift visual diff

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C"
```

---

### Task 13 — Matrix sign-off + final full-suite green re-run

Before snapshotting, re-run the full backend suite once more (any micro-fixes from Tasks 5/7/9/10/11 must not have regressed anything) and assemble the final matrix verdict. @superpowers:verification-before-completion.

**Command:**
```bash
Orchestrator/venv/bin/pytest Orchestrator/tests/ -q -p no:cacheprovider 2>&1 | tail -8
```
**Assert** the summary line reads `NN passed` (plus any pre-existing skips), zero `failed`/`error`.

**Re-assert the `set_current` contract gate** (re-run Task 1's grep block) so any micro-commit did not accidentally expose it:
```bash
ROUTES_FILE=Orchestrator/routes/onboarding_routes.py
grep -nE '@router\.(get|post|put|delete|patch)\([^)]*set[_-]current' "$ROUTES_FILE" && { echo "FAIL: set_current exposed"; exit 1; } || echo "CONTRACT-OK: set_current unexposed"
```
**Assert** `CONTRACT-OK: set_current unexposed`.

**Assemble the matrix verdict** (kept in-message): one line per row, each `PASS` (or honestly `PARTIAL`) with its evidence pointer (screenshot filename or assertion). The 18 rows:

| # | Row | Evidence |
|---|---|---|
| 1 | first-run linear, no rail | 1a `01-first-run-welcome.png` + `hasWelcome&&railHidden` (PARTIAL/revisit-rendered) **AND** 1b bare `/onboarding/` true-first-run `hasWelcome&&hubAbsent&&railHidden` (FULL) — row PASS requires both; PARTIAL if 1b unsafe |
| 2 | hub lands, no /ui bounce | `02-hub-landing.png` + `path startsWith /onboarding` |
| 3–12 | 10 tile states legal & match oracle | `/tmp/m6_status.json` vs tile `data-state` map |
| 13 | SSE skeleton→fill | `event: section`+`done` + network `/status/stream` |
| 14 | revisit returns to hub | `backToHub===true` |
| 15 | left-rail jump | `railPresent && items.length===10` |
| 16 | pair_phone already-paired | `alreadyPaired` matches backend |
| 17 | sigil numbers unique/ordered | no-dup sigil list (scoped to `ob-step-sigil-num`) |
| 18 | no blue/amber bleed | `offTokenBlue===[]` + amber-only-on-attention |

Plus the **contract gate**: `set_current()` unexposed (Task 1 + re-asserted here).

Any row not `PASS` (or row 1 left `PARTIAL` because 1b could not run) is recorded honestly and blocks the snapshot until either fixed or explicitly accepted as PARTIAL with the reason stated.

**Commit:** none (verification only).

---

### Task 14 — Dev snapshot via /chat/save auto-mint (operator resolved dynamically)

Mint the closing dev snapshot recording the entire M0–M6 uplift, per `.claude/commands/snapshot-dev.md`. @snapshot-dev procedure: POST to `/chat/save` (NOT `/chat`, NOT `/mint`), operator resolved dynamically, verify the embedding in journalctl.

**Files:** `.claude/commands/snapshot-dev.md` (procedure), `.claude/commands/resolve-operator.md` (operator resolution).

**Step 1 — resolve operator dynamically (NEVER hard-code):**
```
ToolSearch select:mcp__blackbox__get_current_operator
```
Call `get_current_operator`. If it returns a single operator → use it automatically. If it reports `needs_selection` (multiple) → present an AskUserQuestion dropdown (pre-select `default`). This work is product code (not pure system infra), so it records under the resolved human operator, not `system`.

**Step 2 — write the payload** (use the Write tool to avoid shell-quoting hell):

`/tmp/snap_payload.json`:
```json
{
  "operator": "<resolved — from get_current_operator, NOT a literal>",
  "user_message": "Onboarding wizard production-quality uplift (M0–M6) shipped + verified",
  "assistant_response": "<full structured summary — see body below>",
  "model": "claude-opus-4-8",
  "tokens": {"prompt": 0, "completion": 0}
}
```

The `assistant_response` body must capture (per snapshot-dev "What To Capture"):
- **Problem solved:** linear-only onboarding wizard → hub-first, status-driven console; no functionality removed.
- **Backend:** new `GET /onboarding/status` (fast persisted rollup; provider→step map canonicalized server-side) + `GET /onboarding/status/stream` (SSE live re-validate, fired on view) in `Orchestrator/routes/onboarding_routes.py`; `set_current()` stays **unexposed** (internal `_state` mutator only — verified by the M6 contract grep gate; no random-access route). Sections contract: 10 sections in canonical order (tailscale, api_keys, embeddings, optional_integrations, transcription, web_search, image, pair_phone, cli_agents, operator), 4 groups (network/keys/capabilities/identity), states `ready|attention|optional|checking`.
- **Frontend:** `renderHub()` + `.ob-section-card[data-state]` + readiness header + left-rail navigator + `status.js` (presentational only); revisit returns to hub; first-run linear UNCHANGED (verified via true-first-run bare-`/onboarding/` branch behind a disposable sentinel, not just the revisit path); `.ob-card-selected` replaced inline `SELECTED_STYLE`; shared `util.js cssEscape()`; CSS split into `onboarding-shell.css` + colocated `steps/<name>.css`.
- **Tokens:** added `--ob-attention` (#f5a623) + `--ob-state-{ready,attention,optional,checking}` + `--ob-radius-{sm,md,lg,full}`; repointed `--ob-warning`→amber.
- **Correctness sweep (H):** wired `renderAlreadyPaired()`, sigil numbers derived from `STEPS` order (scoped to `ob-step-sigil-num`), per-render singleton scoping, tailscale container-reconstruction fix.
- **Test totals:** `<NN>` backend tests passing (full `Orchestrator/tests/` suite green via `Orchestrator/venv/bin/pytest`); parity guard locks rollup↔`STEPS`; `set_current`-unexposed contract gate green.
- **Verification evidence:** 18-row Chrome smoke matrix all PASS (cite the rows; row 1 split into 1a revisit-rendered PARTIAL proxy + 1b true-first-run FULL); `/onboarding/status` returns 200 with 10 legal-state sections; SSE emits `event: section`×N + `event: done`; no off-token blue/amber bleed; before/after screenshots under `docs/onboarding/screenshots/{before,after}/`.
- **Search hints:** "onboarding hub renderHub ob-section-card", "/onboarding/status SSE status stream", "amber attention --ob-state token", "wizard production uplift M6 verification".
- **Resolution status:** RESOLVED — this ships the uplift designed in `docs/plans/2026-06-23-onboarding-wizard-production-uplift-design.md` and investigated in `docs/onboarding/2026-06-22-wizard-production-uplift-investigation.md`.

**Step 3 — POST:**
```bash
curl -s -X POST http://localhost:9091/chat/save \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/snap_payload.json | python3 -m json.tool
```
**Expected:** `{"success": true, "operator": "<resolved>", "minted": true, "snap_id": "SNAP-YYYYMMDD-NNNN", ...}`. Capture the `snap_id`.

**Step 4 — verify the embedding generated** (proof point — must read the dims):
```bash
sudo journalctl -u blackbox.service --since "60 seconds ago" --no-pager 2>&1 | \
  grep -E "MINT|EMBEDDING|SNAP-" | head -10
```
**Assert** a line `[EMBEDDING] Successfully generated embedding (N dimensions)` where **N matches the active model's dims** (cross-check `curl -s http://localhost:9091/embeddings/status`). If you instead see `[EMBEDDING] Warning: Failed to generate embedding` or `[EMBEDDING] <slug>: embedding generation failed`, **flag it to the user** — the snapshot minted but is not searchable.

**Commit:** none (snapshot is the artifact). 

**Report back** (final message table per snapshot-dev): `snap_id`, `operator`, `embedding N dimensions ✓`, search-hint phrases, and the 18-row matrix verdict (with row 1's 1a/1b split honestly noted).

---

**Files referenced as ground truth this milestone:** `pytest.ini` (root), `Orchestrator/venv/bin/pytest`, `Orchestrator/tests/test_wizard_routes.py` (TestClient style + `sync_embeddings` mock fixture), `Orchestrator/tests/test_onboarding_steps_parity.py` (parity guard), `Orchestrator/routes/onboarding_routes.py` (`set_current` internal-only at the `/step/complete` handler), `.onboarding_complete` (first-run sentinel, moved-and-restored in Task 3 sub-check 1b), `.claude/commands/snapshot-dev.md` (snapshot procedure), `.claude/commands/resolve-operator.md` (operator resolution). Live endpoints confirmed this run: `/onboarding/` → 200, `/onboarding/status` → 404 today (becomes 200 after M1+restart, asserted in Task 2).

---

---

## Execution

Plan complete. Per @superpowers:executing-plans, implement task-by-task, milestone-by-milestone, committing after each task and restarting `blackbox.service` only at the named SAFE RESTART POINTs. Run the M6 verification matrix before declaring done, then mint the dev snapshot.