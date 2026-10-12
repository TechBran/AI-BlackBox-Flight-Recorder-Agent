# Onboarding Wizard — Production-Quality Uplift (Design)

> **Status:** Design validated 2026-06-23. Next: `superpowers:writing-plans` → task-by-task implementation plan.
> **Investigation it builds on:** `docs/onboarding/2026-06-22-wizard-production-uplift-investigation.md`

**Goal:** Turn the linear-only onboarding wizard into a hub-first, status-driven "console" — random-access navigation to any section, system state surfaced at a glance, production-grade look — **without removing any existing functionality**.

**Core principle:** Elevate, don't rewrite. The bones (modular step loader, disciplined `/onboarding/*` REST layer, distinctive dark "recorder" aesthetic, per-capability status reads) are good. We assemble four already-shipped ingredients — working `?step=` revisit, per-capability status reads, persisted state, a status-pip visual vocabulary — into the console that was always stubbed (`?mode=manage`, "Phase 2.10 — not yet implemented").

---

## Locked Decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Landing model | **Hub-first home** — linear guided flow stays for true first-run; the `?mode=manage` console hub becomes the home after completion |
| 2 | "Needs attention" color | **Adopt amber officially** as a distinct status state (alongside green=ready, red=accent/error) |
| 3 | Visual change scope | **Normalize + restyle** — port off-token CSS back onto tokens AND restyle the late-era components to the brutalist front-half |
| 4 | Build scope | **Full sweep** — all of directions A–H |
| 5 | Hub layout | **Grouped sections + attention header** (not a flat grid or pure triage list) |
| 6 | Status liveness | **Live re-validate on every view** — fired on view (never on a timer), rendered skeleton→fill via SSE |
| 7 | Backend random-access | `set_current()` stays **unexposed**; hub uses state-neutral `?step=` revisit |
| 8 | Left-rail on first-run | **Hidden during first-run guided funnel**; appears post-completion and on revisit |
| 9 | Frontend testing | **Agent-driven Chrome verification** (Claude-in-Chrome) + before/after screenshots — no Playwright dependency |

---

## 1. Architecture & Routing

Two surfaces, one shell. `onboarding.js` gains a `renderHub()` path alongside the existing `renderStep()`. Linear mechanics are untouched.

| Entry | Today | New |
|---|---|---|
| `/onboarding/` — first run (no `.onboarding_complete` sentinel) | Linear flow | **Unchanged** — guided `welcome → … → done` |
| `/onboarding/` — after completion | Redirects to `/ui` | **Lands on the console hub** (`mode=manage`). No bounce. |
| `?step=<name>` revisit | Works; ends on a "close page" terminal | Still works, state-neutral — but on save **returns to the hub** |
| `?mode=manage` | Stubbed (falls through to linear) | **Renders the hub** |

- A discreet **"Setup / Console"** entry point is added in `/ui` as a second door to the hub.
- `welcome` stays as linear step 1; its "4 pillars" framing is the conceptual basis for the hub's grouped sections.
- `done.js`'s `renderSummary()` becomes the **shared data model** the hub renders from; during first-run, `done` remains the completion screen but its rows become clickable and gain the sections it currently omits (embeddings, STT, web_search, image, cli_agents).

The hub is **additive**: linear first-run is sacred; the hub reuses revisit plumbing + the `done` summary model; no backend random-access cursor.

## 2. Status Model & Backend

**Sections (10), grouped** — `welcome`/`done` are the hub itself:

| Group | Sections |
|---|---|
| Network & Access | Tailnet · Pair Phone |
| Keys & Models | API Keys · Memory (embeddings) |
| Capabilities | Speech · Web Search · Image · Agents · Extras |
| Identity | Operators |

**State vocabulary — 3 resolved + 1 transient:**
- `checking…` — transient, live probe in flight (skeleton)
- ✓ **ready** (`--ob-state-ready` = success green) — configured AND last live probe healthy
- ⚠ **attention** (`--ob-state-attention` = new amber) — configured-but-wrong, OR a required section unsatisfied
- – **optional** (`--ob-state-optional` = text-tertiary dim) — unset/skipped; box works without it

**Attention derivation** (normalized list the header surfaces; all computable from existing signals):
- Tailnet up but `serve`/HTTPS not set → "phone pairing will fail"
- API key present but validation failed/never validated; or a feature enabled whose provider key is missing
- Memory index N snapshots behind, or health `superseded`/`broken`
- Web Search / Image enabled but default provider's key missing
- Agent installed but not authenticated; Extras half-connected (client-id, no secret)
- Global: `restart-status` reports drifted keys / needs-restart

**New backend endpoints (`Orchestrator/routes/onboarding_routes.py`):**
1. `GET /onboarding/status` — fast persisted rollup: `{ sections: [{key, group, label, state_from_persisted, items, validated_at, skipped}], attention: [...], ready_count, total }`. **Canonicalizes the provider→step map in the backend** (closes the "join lives only in frontend" gap). Pytest-testable.
2. `GET /onboarding/status/stream` (SSE) — live re-validation: server-side parallel probes, one event per section as it resolves. Drives skeleton→fill; fires on view only; keeps expensive/`tailscale` probes off any blocking path. Reuses the SSE pattern from tailscale-install + `/logs/stream`.
3. `set_current()` stays **unexposed**.

## 3. Frontend Components

- **`renderHub()`** — paints grouped structure + readiness header immediately from `GET /onboarding/status` (all tiles `checking…`), then opens the SSE stream and live-fills each tile. Each tile one-click → `?step=<name>` → edit → back to hub.
- **`.ob-section-card`** — new shared console primitive distilled from `.ob-cli-agent-card[data-state=…]` (border-left color already switches on one attribute). Props `{label, group, state, summary, href}`.
- **Readiness header** — reuses `.ob-progress` chrome as an "X of N ready" meter + attention rows (`.ob-summary-row` + amber `-attention` + "Set up →" CTA).
- **Left-rail navigator (C)** — persistent grouped section list with live status pips, replacing the lone sigil-gutter; lets you jump mid-flow; derives "Back to …" labels from `STEPS` order. Hidden on first-run; present in hub + revisit.
- **`status.js`** — deliberately presentational only; all derivation lives in the backend rollup so untestable vanilla-JS logic is minimized.
- **Enabling fix:** leaky module-level singletons scoped per-render so steps mount non-linearly from the hub without state bleed (prerequisite, not just cleanup).

## 4. Design System & CSS

- **Tokens:** add `--ob-attention` (amber) + canonical `--ob-state-{ready,attention,optional,checking}`; repoint `--ob-warning` (today aliased→accent) to amber; collapse the 4–5 competing status vocabularies onto this set.
- **Normalize + restyle:** port every post-line-1865 off-token reference (undefined `--accent`→blue bleed, rgba amber literals, missing `--radius-*`) onto tokens; add a minimal radius scale; restyle late-era components (banners, restart CTA, agent cards, copy buttons) to the brutalist front-half — sharp edges, hard-shadow CTAs, red accent, **amber only for attention**.
- **CSS split (E):** `onboarding.css` becomes a thin manifest that `@import`s `onboarding-shell.css` + colocated `steps/<name>.css`. Zero build step, single `<link>`, offline-safe. Shrinkage from deleting dead rules, the doubled `@keyframes ob-spin`, the inline `SELECTED_STYLE` (→ `.ob-card-selected`), and unifying `cssEscape()`.

## 5. Correctness Sweep (H)

- Wire dead `renderAlreadyPaired()` into `pair_phone.render()` (biggest state-truth bug — revisit currently shows a fresh QR as if unpaired)
- Derive sigil numbers from `STEPS` order (kills the 8-vs-12 off-by-N bug; two steps both show "05")
- Scope leaky singletons per-render (`busy`, `recheckBusy`, `_authInFlight`, `pollRef`, `activeToken`)
- Fix tailscale's brittle `scope.closest('.ob-step')?.parentElement || document.body` container reconstruction (breaks inside hub/modal mounts)
- Tighten tailscale Branch-C catch-all (genuine errors mislabeled "installed, needs auth")

## 6. Testing Strategy

- **Backend = where tests concentrate** (TDD, pytest, established style): `/onboarding/status` rollup, attention derivation, provider→step map, SSE stream. Extend `test_onboarding_steps_parity.py` so the rollup's section list can never drift from `STEPS`.
- **Frontend = agent-driven Chrome verification:** Claude navigates the live wizard (`http://localhost:9091/onboarding/`), exercises every component (hub tiles, SSE skeleton→fill, revisit round-trips, left-rail, each status state), and captures before/after screenshots per milestone. No Playwright/Jest dependency added to the offline vanilla-JS product. *(Requires the Claude-in-Chrome extension connected before verify milestones run.)*

## 7. Phasing — 6 Shippable Milestones

Each milestone keeps all functionality and is independently shippable.

- **M0 — Foundation & correctness:** correctness sweep (H) + amber/status-state tokens + normalize off-token CSS (kill blue bleed). Low-risk, high-truth.
- **M1 — Backend status:** `/onboarding/status` rollup + provider→step map + attention derivation + `/status/stream` SSE (pytest TDD).
- **M2 — Quick win:** clickable `done.js` summary + missing rows (A) — validates the status payload end-to-end.
- **M3 — The hub:** `renderHub()` + `.ob-section-card` + readiness header + grouped layout + skeleton→SSE-fill + hub-first routing + revisit-returns-to-hub.
- **M4 — Navigator & elevation:** left-rail navigator (C) + visual elevation (G — readiness meter, surface Portal HTTPS URL, logs/restart affordances into hub header).
- **M5 — CSS split & restyle finish:** colocate `steps/<name>.css`, finish late-era restyle, delete dead CSS, final visual-consistency pass.
- **M6 — Verification:** full pytest run + agent-driven cross-surface Chrome smoke + visual before/after diff + dev snapshot.

## Non-Goals (YAGNI)

- No rewrite of the linear flow or any step's core logic.
- No new frontend framework, bundler, or JS test harness.
- No backend random-access cursor (`set_current()` route).
- No change to the BYOK secrets-writing or validation mechanisms.
- No Android/Portal-surface changes beyond the single `/ui` "Console" entry link (the wizard is device-local first-run; the 3-surface rule does not extend the hub to Android in this effort).

---

## Key Files

- `Portal/onboarding/onboarding.js` — shell, `?step=` revisit, stubbed `?mode=manage`, `STEPS`/`STEP_LABELS`
- `Portal/onboarding/onboarding.css` (81KB monolith, seam ~L1865) · `onboarding-tokens.css` (token contract)
- `Portal/onboarding/steps/{done,cli_agents,embeddings,api_keys,pair_phone}.js` — status-model exemplars + dead-code bug
- `Orchestrator/routes/onboarding_routes.py` — `/state`, `/current-config`, `/restart-status`, `/step/*` (+ new `/status`, `/status/stream`)
- `Orchestrator/routes/embeddings_routes.py` — `/embeddings/status` · `Orchestrator/onboarding/state.py` — state model + sentinel + unexposed `set_current()`
- `Orchestrator/tests/test_onboarding_steps_parity.py` — extend for rollup↔STEPS parity
