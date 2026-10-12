# AI BlackBox Onboarding Wizard — Deep Investigation Report

*Pre-planning synthesis for senior engineering + product owner review. Scope: elevate look + navigability to production quality, surface system state, enable random-access navigation — **without** rewriting. The bones are good.*

---

## 1. Executive Summary

The onboarding wizard is a genuinely distinctive, dark "console/recorder" linear flow of **12 step modules** (`welcome → tailscale → api_keys → embeddings → optional_integrations → transcription → web_search → image → pair_phone → cli_agents → operator → done`), dynamically imported one at a time into a shared shell (`onboarding.js`), backed by a disciplined `/onboarding/*` REST layer that reads `.env` fresh on every call. Its **biggest strength is that the hard part is already done**: every meaningful step already probes and renders real "already configured / needs attention" state (`api_keys`, `cli_agents`, `embeddings`, and the `done` summary are gold-standard), the backend already persists `{completed_steps, skipped_steps, current_step}` and exposes rich per-capability status via `/onboarding/current-config` + `/embeddings/status` + `/onboarding/cli-agent/status` + `/onboarding/restart-status`, and a **working one-click `?step=<name>` deep-link revisit** already exists. The single clearest path to the user's vision is to **build the already-stubbed `?mode=manage` hub** as a status-grid landing page that renders the `done.js` summary model as clickable tiles (reusing `.ob-cli-agent-card[data-state]` + `.ob-summary-row` primitives over the existing state endpoints) — turning four already-built ingredients (state persistence, per-capability status reads, deep-link revisit, a status-pip vocabulary) into the console the user is asking for, with no rewrite.

---

## 2. Architecture Map

```
Portal/onboarding/
├── onboarding.js ............ SHELL: STEPS[] order, progress chrome, dynamic step loader,
│                              ctx{next/skip/back}, ?step= revisit, ?mode=manage (STUBBED),
│                              fetchState(), renderStepNav/wireStepNav/wireCopyBtn helpers
├── onboarding-tokens.css .... DESIGN TOKENS (166 lines, disciplined) — extends Portal/_variables.css
├── onboarding.css ........... 81KB / 2765 lines MONOLITH (two eras; seam at ~line 1865)
├── fonts/ ................... self-hosted Fraunces (display) + JetBrains Mono (body)
└── steps/
    ├── welcome.js ........... 01  intro + 4 pillars (purely presentational — hub candidate)
    ├── tailscale.js ......... 02  tailnet join / HTTPS serve (4-branch live probe)
    ├── api_keys.js .......... 03  BYOK LLM keys (configured-aware, GOLD STANDARD)
    ├── embeddings.js ........ 04  memory/search model (RICHEST status step)
    ├── optional_integrations.js  EXTRAS  Google Workspace / ElevenLabs / creds
    ├── transcription.js ..... SPEECH  STT provider pick
    ├── web_search.js ........ WEB     multi-select + default
    ├── image.js ............. IMAGE   multi-select + default (clone of web_search)
    ├── pair_phone.js ........ PAIR    QR pairing (already-paired path = DEAD CODE)
    ├── cli_agents.js ........ AGENTS  4 CLI agents (BEST live status model)
    ├── operator.js .......... 07  operator CRUD (live-synced roster)
    └── done.js .............. DONE    aggregated summary (closest thing to a console TODAY)

BACKEND  Orchestrator/
├── routes/onboarding_routes.py ..... /onboarding/state, /current-config, /validate, /save,
│                                     /step/{complete,skip}, /complete, /reset, /restart-status,
│                                     /restart, /tailscale/*, /cli-agent/*, /logs/stream
├── routes/credentials_routes.py .... /onboarding/credentials{,/upload,/{filename}}
├── routes/embeddings_routes.py ..... /embeddings/status (NOT under /onboarding)
├── onboarding/state.py ............. OnboardingState singleton -> {ROOT}/.onboarding_state.json
│                                     + .onboarding_complete SENTINEL (authoritative is_complete)
└── onboarding/secrets_writer.py .... update_env()/remove_env_keys() -> .env (atomic, 0600, backup×5)

LIVE STATE FILE  .onboarding_state.json
{
  started_at: float,
  completed_steps: [StepName],   # mutually exclusive with skipped
  skipped_steps:   [StepName],   # (live file: empty, 11/12 completed)
  current_step:    StepName,     # single LINEAR cursor, default "welcome"
  validated_at:    {provider -> unix_ts},  # KEYED BY PROVIDER, not step
  completed_at:    int
}
# is_complete is NOT in JSON — it's the existence of the .onboarding_complete sentinel file.
```

**Key architectural facts that shape every recommendation below:**
- **E8 fresh-read:** `/current-config` + `/config/{key}` read `.env` via `dotenv_values()` every call — saves are visible without restart. Running chat handlers keep stale `Orchestrator.config.*` until restart; `/restart-status` reports that drift.
- **`validated_at` is keyed by PROVIDER, not step** — so there is no canonical backend step→validated mapping; the provider→step join (`api_keys` = 5 providers) lives only in frontend logic.
- **`current_step` is a single linear cursor.** `/step/complete` and `/step/skip` *always* auto-advance to `index+1`. `set_current()` exists but is **not exposed via any route** — there is no backend random-access.

---

## 3. Per-Step Inventory Table

| # | Step (label) | Configures | Key backend endpoints | Already shows "already configured"? |
|---|---|---|---|---|
| 01 | **welcome** | Nothing (intro + 4 pillars) | `step/complete`, `step/skip` | **NO — always blank/static.** Best hub candidate (already lists the 4 pillars). |
| 02 | **tailscale** (Tailnet) | `BLACKBOX_TAILNET_HOSTNAME`, `accept-dns`, `serve` HTTPS:443 | `validate{tailscale}`, `save`, `tailscale/{up,poll,serve,install/stream,accept-dns,cancel}` | **STRONG but EPHEMERAL** — live probe-on-mount → 4-branch badge; persists nothing client-side, re-probes every render. |
| 03 | **api_keys** (Keys) | `OPENAI/ANTHROPIC/GOOGLE/XAI/PERPLEXITY_API_KEY` | `current-config`, `validate`, `save` | **STRONG + PERSISTED — GOLD STANDARD.** Per-provider `{present, last4, validated_at}` → "Already configured ••••XXXX" + Replace. |
| 04 | **embeddings** (Memory & Search) | active embedding model, re-embed migration, keep_alive | `/embeddings/status`, `/embeddings/{validate,migrate,migrate/cancel,ollama/pull,keep_alive}` | **RICHEST — never blank.** ACTIVE badge, freshness ("N behind"), blockers, health banner (superseded/broken), in-flight job adoption. |
| — | **optional_integrations** (Extras) | `GOOGLE_OAUTH_CLIENT_ID/SECRET`, `ELEVENLABS_API_KEY`, service-account JSON | `current-config`, `credentials{,/upload,/{f}}`, `validate{gmail,elevenlabs}`, `save` | **STRONG rehydration** — configured cards w/ redacted preview + Reconnect/Replace; fail-open to blank. |
| — | **transcription** (Speech) | `STT_PROVIDER` (pref; ''=auto) | `/stt/catalog`, `current-config`, `save` | **PARTIAL** — pre-selects saved provider, per-card Ready/Needs-setup; no whole-step summary, always re-renders full picker. |
| — | **web_search** (Web Search) | `WEB_SEARCH_ENABLED`, `WEB_SEARCH_DEFAULT` | `current-config`, `save`, `step/complete` | **PARTIAL/GOOD per-provider, read-once** — pre-checks enabled set + default; only key_present providers shown; no whole-step marker. |
| 07* | **image** (Image) | `IMAGE_ENABLED`, `IMAGE_DEFAULT` | `current-config`, `save`, `step/complete` | **PARTIAL/GOOD, read-once** — clone of web_search, no keyless floor; no whole-step marker. |
| 05* | **pair_phone** (Pair) | paired-device registry entry | `pair/{start,qr,status}`, `step/complete` | **PARTIAL / BROKEN** — `renderAlreadyPaired()` is fully coded but **NEVER invoked**; revisit shows a fresh QR as if unpaired. DEAD CODE. |
| 06* | **cli_agents** (Agents) | spawns terminals (writes no config itself) | `/cli-agent/status`, `/cli-agent/spawn-terminal` | **STRONG + LIVE — BEST status model.** Per-provider `data-state=ready/needs-auth/implicit-auth/missing` badges from `{installed,authenticated,auth_method}`. |
| 07 | **operator** (Operator) | operators via `/operator/add` + `DELETE`; `DEFAULT_OPERATOR` (fresh only) | `current-config`, `/operator/add`, `/operator/{name}`, `save` | **STRONG + LIVE-SYNCED** — rehydrates roster as deletable rows; CRUD against current state. Names only (no metadata). |
| 08* | **done** (Done) | `.onboarding_complete` sentinel; optional `/update/start`, `/restart` | `current-config`, `state`, `/update/status`, `/restart-status`, `/logs/stream`, `complete` | **STRONG AGGREGATION — closest thing to a console today.** 6 status-pip rows (ok/skip/warn) but **READ-ONLY, not clickable**; omits embeddings/STT/web/image/agents. |

\* **Sigil numbers are WRONG.** The `STEPS` array has 12 entries but sigils/labels (`done.js` hardcodes "08", "Back to operator setup"; `STEP_LABELS` reference "/ 08"; two steps both show "05") were written for an **8-step flow and never updated**. High-visibility production-polish bug.

**Blank-vs-state-aware verdict:** Only `welcome` renders truly blank/static. Every other step reads live or persisted state, but they fall into three tiers: **persisted-aware** (api_keys, embeddings, operator, cli_agents, optional_integrations), **ephemeral live-probe** (tailscale), and **read-once pre-select with no whole-step summary** (transcription, web_search, image, + pair_phone whose already-paired path is dead).

---

## 4. Navigation & State Model

**What EXISTS:**
- **Linear flow** — the spine. `STEPS[]` in `onboarding.js` drives a single cursor; `/step/complete` and `/step/skip` both auto-advance `current_step` to `index+1`. Three-state per step: completed / skipped / pending (mutually exclusive completed↔skipped).
- **`?step=<name>` deep-link revisit (WORKING):** `REVISIT_STEP` in `onboarding.js` jumps to any step, renders it, and on completion shows `renderRevisitDone()` — **deliberately never mutates backend state**. This is the one-click random-access plumbing G1 needs, already shipped.
- **Backend state persistence:** `.onboarding_state.json` + `.onboarding_complete` sentinel. `/state` returns `{is_complete, completed_steps, skipped_steps, current_step, all_steps}`. `/current-config` embeds the same as `onboarding_state`.

**What is STUBBED:**
- **`?mode=manage` hub — NEVER BUILT.** `onboarding.js` (header line ~2-3) computes `MODE='manage'` from the query string and passes it to `render()`, but there is **no manage/step-grid landing renderer** — header comment: *"Phase 2.10 — not yet implemented."* Manage mode currently behaves identically to linear setup.
- **No backend random-access:** `set_current()` exists in `state.py` but is exposed by **no route**. There is no "enter manage mode" or "set current_step to arbitrary X without rewinding." Random access today is purely a frontend revisit that touches no backend state.

**Net:** G1's hard plumbing (one-click jump to any step without corrupting linear progress) **already works via `?step=`**. What's missing is purely the **landing grid** that links to it — the `?mode=manage` renderer.

---

## 5. Design-System Audit

**Visual identity (genuinely distinctive — the bones the user praises are real):** A dark editorial *"New Yorker meets terminal"* aesthetic. Pure-black `#000` canvas + two fixed atmospheric layers (`body::before` low-opacity radial **red** glow; `body::after` SVG fractalNoise film-grain at 0.6 opacity / overlay blend — a CRT/recorder texture). Accent is a deep zero-G/B red **`#cc0000`** (deliberately deeper than Portal's `#ff4a4a` per a documented 2026-05-11 audit). Identity moves: Fraunces italic-accent display titles with animated hand-drawn underline; asymmetric left-gutter "sigil" (huge Fraunces number + vertical-rl label); brutalist HR "feature" rows; signature CTA = uppercase JetBrains Mono with a **hard 4px white offset drop-shadow** (risograph motif). **Single-attention philosophy:** red = accent + warning + error; green `#27d980` reserved strictly for validation success.

**Token system:** `onboarding-tokens.css` (166 lines) is disciplined and well-documented — Fraunces + JetBrains Mono (self-hosted, offline-install-safe), 1.333 modular type scale, 4px linear spacing, named motion durations, pure-black surface ramp, and **status-color tokens already exist** (`--ob-accent`, `--ob-success`, `--ob-warning` aliased→accent, `--ob-error`=accent, `--ob-info` `#4dd0e1`).

**Reusable component classes (already shipped):**
- `.ob-status-badge` (`-success`/`-error` + `-pip`/`-label`/`-version`) — the closest real **status BADGE** primitive.
- `.ob-status-pill` (`-validating`/`-ok`/`-error`/`-glyph`) — inline per-card status pill.
- `.ob-cli-agent-card[data-state=ready|needs-auth|implicit-auth|missing]` + `.ob-cli-agent-grid` (`repeat(auto-fit,minmax(280px,1fr))`) — **literally a status-grid card pattern; border-left color switches via ONE attribute.** `.ob-emb-grid` is the same.
- `.ob-summary-list`/`.ob-summary-row` + `.ob-summary-ok`/`-skip`/`-warn` glyph-state modifiers — **the most console-like component in the app.**
- `.ob-provider-configured` (green pill + masked last4 + Replace), `.ob-cta` family, `.ob-disclosure`, `.ob-progress`, `.ob-feature` HR-rows.

**The 81KB / 2765-line monolith concern — size is the symptom, not the disease.** The real problem is a **two-eras split with a hard seam at ~line 1865**:
- **Lines 1–1863:** token-driven, mock-derived, beautifully commented — the genuinely production-grade shell + first-wave steps.
- **Lines 1865–2765:** an accretion of T7/T8/E1–E20/Task13 patches that **abandon the token system** and hardcode a *different* palette: raw rgba literals + references to **UNDEFINED tokens** (`--accent` falls back to **blue `#4a9eff`**, `--radius-sm/md/lg/full`). So off-brand **blue bleeds** into a red-identity wizard (`.ob-auth-link`, `.ob-banner-link`, `.ob-copy-btn-primary`, etc.), and **amber** (`rgba(255,180,50)`) reappears pervasively (restart CTA, banners, cli-agent needs-auth) **despite the design system deliberately collapsing warning into red.**

**Other concrete debt:** duplicate `@keyframes ob-spin` (defined twice); **4–5 parallel status-color vocabularies** for the same ok/attention/error (token badges vs `.ob-status-pill` vs `.ob-cli-agent-badge` rgbas vs `.ob-logs-status` rgbas vs `.ob-summary` glyphs); inline `SELECTED_STYLE` string literal duplicated verbatim across `embeddings.js` + `transcription.js`; `cssEscape()` reimplemented 3× across steps; ID-selector coupling (`#ob-recheck-btn`); sharp-corner (early) vs rounded-corner (late) split mid-flow; dead no-op hook rules.

**MISSING primitives for a console look:**
1. **One canonical status-state token set** — `--ob-state-ok / -attention / -skipped / -pending` (mapping to existing `--ob-success` / `--ob-accent` / `--ob-text-tertiary`) to collapse the 5 competing vocabularies. **Requires resolving the amber-vs-red identity question explicitly.**
2. **One shared `.ob-status-dot/-pip`** primitive (currently re-implemented ≥5 ways).
3. **A `.ob-section-card` / step-tile** component (distill from `.ob-cli-agent-card`) that takes a status state + configured/skipped/needs-attention + a one-click revisit link.
4. **The `--radius-*` tokens** the late era assumes (or an explicit decision to stay 0-radius).
5. **A hub/grid layout container** (the never-built `?mode=manage` landing).

---

## 6. Gap Analysis vs. the User's 3 Goals

### G1 — One-click random access (jump to any section)
- **Already supports it:** The `?step=<name>` deep-link revisit fully works and *never corrupts linear state*. `done.js` summary rows already know each row's step name. The plumbing is done.
- **Missing:** A **landing grid** that surfaces every section as a one-click link to `/onboarding/?step=<name>` — i.e. the `?mode=manage` hub renderer. `done.js` rows are **read-only, not clickable** (directly contradicts G1). Backend has **no** random-access cursor model (`set_current()` unexposed) — but G1 doesn't strictly need it, since revisit is intentionally state-neutral.

### G2 — State kept & surfaced (configured / skipped / validated / needs-attention)
- **Already supports it:** **This is largely a BACKEND-SOLVED problem.** `/state` persists `{completed_steps, skipped_steps, current_step}`; `/current-config` exposes per-provider `{present, last4, validated_at}`, operators, paired_devices, live tailscale, stt/web_search/image blocks; `/embeddings/status`, `/cli-agent/status`, `/restart-status` cover the rest. `api_keys`/`embeddings`/`cli_agents`/`operator` already *render* configured-vs-not per item.
- **Missing:** (a) **No per-STEP rollup** — nothing JOINS the per-capability signals into a per-step verdict ("api_keys: 3/5 present, 2 validated, 1 needs attention"). `validated_at` is keyed by **provider not step**; the provider→step map lives only in frontend. (b) **No staleness semantics** — a key validated last week but since revoked still shows "Already configured" (api_keys, optional_integrations don't re-validate on mount). (c) `done.js` summary **omits embeddings, transcription, web_search, image, cli_agents entirely** — the one aggregate view is incomplete.

### G3 — Console feeling (at-a-glance, scannable, status-driven, "how to set it up")
- **Already supports it:** The status vocabulary exists (`.ob-summary-ok/-skip/-warn`, `.ob-cli-agent-card[data-state]`, `.ob-status-badge`). `cli_agents` and `embeddings` *feel* like a console panel. `done.js` is "a console data model that just isn't navigable."
- **Missing:** (a) **No landing hub** — the console feeling exists per-step but there's no at-a-glance whole-system view. (b) **No "how to set it up" CTA on attention rows** — `done.js` details are plain text, no "Set up →" link. (c) **No aggregate readiness header** ("X of N configured", outstanding-vs-optional count). (d) The **off-token blue/amber back-half** actively undercuts the polished console look in exactly the late-era components (banners, restart, agents) a console would surface.

---

## 7. Backend Readiness for a Status Console

**Aggregate signals that EXIST today** (a console can render from these now):
- `/onboarding/state` — `{is_complete, completed_steps, skipped_steps, current_step, all_steps}`.
- `/onboarding/current-config` — per-provider key presence/last4/validated_at, operators, paired_devices, **live** tailscale probe, stt/web_search/image blocks, embedded `onboarding_state`.
- `/onboarding/cli-agent/status` — per-agent `{installed, authenticated, bin_path}` + aggregate `ready` bool (already a clean status payload).
- `/embeddings/status` — active model, health, freshness, blockers, live migration job.
- `/onboarding/restart-status` — `{needs_restart, drifted_keys, reason}` — **the only existing "X needs attention" signal**, across 9 onboarding-writable keys.
- `/onboarding/credentials` — service-account file state.

**What a `/onboarding/status` hub endpoint would need to ADD (the gaps):**
1. **A single aggregate read.** Today a hub must fan out to **4–5+ calls** (`state` + `current-config` + `restart-status` + `cli-agent/status` + `embeddings/status` + maybe `credentials`/pairing). No one read returns the whole hub view.
2. **A per-step rollup / provider→step join.** Backend has no canonical map of which signals belong to which step; needs a `step → {state, configured, validated, attention}` aggregator. (api_keys→5 providers; optional_integrations→gmail/elevenlabs/creds.)
3. **A poll-safe variant.** `/current-config` runs a **live `tailscale` subprocess probe every call** — explicitly documented *"do NOT call from auto-polling UI."* A refreshable console needs a cached/last-known status read.
4. **Embeddings inside the namespace.** `/embeddings/status` lives in a *separate router, not under `/onboarding`* and isn't in `current-config` — a unified read must reach outside the namespace.
5. **Normalized attention reasons.** Signals like "key present but never validated," "enabled provider missing its key," "tailscale up but HTTPS/serve not set (pairing will fail)," "cli agent installed but not authenticated" are all **derivable** but not surfaced as a normalized `{capability, state, severity, cta}` list — every consumer reinvents the derivation.
6. **Staleness semantics.** `validated_at` has no `last_checked` vs `last_ok` distinction and no expiry.
7. **An overall-readiness score.** `is_complete` is a binary sentinel; no "percent complete" / "blocking-items count" for a console header (G3).
8. *(Optional)* **A persisted random-access cursor route** — if the product wants manage-mode to set `current_step` arbitrarily, expose `set_current()` (it exists, unexposed). Not required if the hub relies on state-neutral `?step=` revisit.

---

## 8. Enhancement Directions — A Menu of Approaches

*Differentiated options, roughly low-effort/high-impact → ambitious. All are ELEVATE-not-rewrite; existing functionality stays sacred.*

### A. Make the `done.js` summary clickable (the "free G1" quick win)
- **What:** Make each `.ob-summary-row` a one-click link to `/onboarding/?step=<name>` (rows already know their step), add a "Set up →" CTA on `warn` rows, and add the **missing rows** (embeddings, STT, web_search, image, cli_agents) the summary currently omits.
- **Goals:** G1 (mostly), G2 (completeness), G3.
- **Effort:** **Low** (frontend only; deep-link + status data already exist).
- **Risk/tradeoff:** Highest impact-per-hour. Doesn't yet give a *landing* hub — done is still terminal-only. Low risk.

### B. Build the `?mode=manage` console hub (THE headline direction)
- **What:** Implement the stubbed manage-mode renderer: a status-grid landing page that lists all 12 sections as tiles (reusing `.ob-cli-agent-card[data-state]` + `.ob-section-card`), each showing configured/skipped/needs-attention and deep-linking to `?step=`. Reshape `welcome.js`'s 4-pillar grid as the conceptual basis. Reuse `done.js`'s `renderSummary()` as the data model.
- **Goals:** **G1 + G2 + G3 directly.**
- **Effort:** **Medium** (new renderer + tile component; can ship over the existing fan-out reads, no new backend strictly required for v1).
- **Risk/tradeoff:** The central deliverable. Key product decision: hub-first vs linear-first default. Manageable risk because all ingredients exist.

### C. Persistent left-rail / section navigator inside the linear flow
- **What:** Add a persistent rail (reusing the sigil-gutter real estate) listing all sections with live status pips, letting the user jump mid-flow — not just next/back. Replaces "Back to operator setup" hardcoded labels with derived ones.
- **Goals:** G1 (in-flow), G3.
- **Effort:** **Medium** (shell change in `onboarding.js`; fixes the hardcoded back-labels + brittle DOM-walk re-check in tailscale as a side effect).
- **Risk/tradeoff:** Changes the core flow chrome — must not break linear progress semantics or the module-level singletons several steps rely on. Complements B (rail + hub) or substitutes for it if hub is deferred.

### D. Per-step status badges + "configured/skipped/needs-attention" states
- **What:** Add a normalized per-step status verdict shown on the progress chrome and any tile/rail: green configured / gray skipped / amber-or-red needs-attention / neutral pending. Backed by a frontend provider→step join initially (or endpoint F).
- **Goals:** G2 + G3.
- **Effort:** **Low–Medium** (mostly a frontend aggregator + the missing shared status-pip primitive).
- **Risk/tradeoff:** Forces the **amber-vs-red identity decision** (the design system collapsed warning into red, but the late era reintroduced amber). Prerequisite primitive for B and C.

### E. CSS architecture refactor / componentization (pay down the two-eras debt)
- **What:** Split along the existing ~line-1865 seam: extract `onboarding-shell.css` (lines 1–~618, the production-grade shared system), move each step block into `steps/<name>.css` colocated with its `.js` (matching the dynamic-import architecture), **and during the move port every rgba/`--accent`/`--radius` reference back onto tokens, collapse the 4–5 status vocabularies into one, de-dupe the doubled `@keyframes ob-spin`, delete no-op hooks.** Add the missing `--radius-*` + status-state tokens. The delete-on-migrate is where 81KB shrinks.
- **Goals:** Indirect (META look quality) — unblocks a consistent console aesthetic for B/D.
- **Effort:** **Medium–High** (mechanical but broad; risk of visual regressions).
- **Risk/tradeoff:** High value for production quality and the off-brand-blue/amber fix, but touches the whole surface — needs visual diffing. Can be staged: **normalize tokens first (fixes blue bleed cheaply), split later.**

### F. Unified `/onboarding/status` backend endpoint
- **What:** One read that fans in `state` + `current-config` (with a **cached/poll-safe** tailscale snapshot) + `cli-agent/status` + `embeddings/status` + `restart-status` + `credentials`, returns a **per-step rollup** (`step → {state, configured, validated, attention}`) + normalized `{capability, state, severity, cta}` attention list + an overall readiness score. Houses the provider→step map canonically.
- **Goals:** G2 + G3 (and makes B/C/D cheap + pollable).
- **Effort:** **Medium** (pure aggregation over existing reads; the derivations already exist scattered in frontends).
- **Risk/tradeoff:** Eliminates per-consumer derivation drift and the "do-not-poll" tailscale trap. Not strictly required for a v1 hub but the right foundation if the console is meant to live/refresh.

### G. Visual elevation of the console aesthetic
- **What:** Lean into the existing "recorder" identity for the hub: aggregate readiness meter (reuse `.ob-progress` chrome as "X of N configured"), status-pip grid with the hard-shadow CTA motif, surface the **Portal HTTPS URL** (tailscale Branch A already computes it) and the **live-logs SSE modal** + **status-aware Restart/drift** control (today buried on `done.js`) into the hub header as console-grade affordances.
- **Goals:** G3 (and operational utility).
- **Effort:** **Low–Medium** (mostly relocating already-built widgets; depends on E for consistency).
- **Risk/tradeoff:** Pure polish; best done after B + E so it builds on consistent primitives.

### H. Correctness sweep (state-integrity fixes that any console will expose)
- **What:** Fix the bugs a hub would surface: (1) **wire `renderAlreadyPaired()`** into `pair_phone.render()` (dead code; revisit shows a fresh QR as if unpaired — the single biggest state bug). (2) Fix the **sigil/denominator off-by-N** (8-step numbers on a 12-step flow). (3) Add staleness re-validation affordances to api_keys/optional_integrations configured cards. (4) Audit module-level singletons (`busy`, `recheckBusy`, `_authInFlight`, `pollRef`, `activeToken`) that **leak across renders** and will break under non-linear hub/modal mounts.
- **Goals:** G2 (truthfulness of surfaced state).
- **Effort:** **Low–Medium** (targeted fixes).
- **Risk/tradeoff:** Non-negotiable hygiene — a console that surfaces *wrong* state is worse than none. #4 specifically de-risks B/C (mounting steps non-linearly).

**Suggested sequencing:** **H + E-normalize-only** (truth + kill blue/amber) → **A** (free G1 win) → **D + F** (status primitives + unified read) → **B** (the hub) → **C/G** (rail + visual elevation) → **E-split** (colocate CSS). This front-loads cheap correctness/identity fixes, then builds the hub on a consistent, pollable foundation.

---

## 9. Open Questions for the Planning Session

1. **Hub-first vs linear-first default?** After onboarding completes, should the BlackBox land on the **`?mode=manage` console hub** (random-access, G1-native) or keep the linear flow as default with the hub as an opt-in? Does the hub *replace* or *sit beside* the `done.js` terminal screen?
2. **Amber-vs-red — resolve the identity conflict.** The design system deliberately collapsed warning into red (single-attention philosophy); the late-era CSS reintroduced amber (`rgba(255,180,50)`) for needs-attention. A console leans hard on a distinct "needs attention" color. **Keep red-only (purist) or formally adopt amber as the attention state?** This decision gates the status-state token set (D/E).
3. **How aggressive is the visual change?** "Elevate don't rewrite" — but the back-half off-token blue bleed is a real defect. Is the mandate (a) *normalize only* (port to tokens, kill blue, keep all current layouts), or (b) *normalize + restyle* the late-era components to match the brutalist front half (sharp corners, hard-shadow CTAs)?
4. **Split the CSS now or later?** Do we commit to the `onboarding-shell.css` + `steps/<name>.css` colocation (matching the dynamic-import architecture) as part of this effort, or normalize-in-place and defer the file split to reduce regression surface?
5. **Build the unified `/onboarding/status` endpoint, or fan out client-side for v1?** Determines whether the hub is a quick frontend assembly over 5 existing reads (with the documented tailscale poll hazard) or waits on backend aggregation + a poll-safe cached read. Also: **does manage-mode need persisted random access** (expose `set_current()`), or is state-neutral `?step=` revisit sufficient?
6. **Per-step rollup — where does the provider→step map live?** Canonicalize it in the backend (endpoint F) or keep it in frontend? Affects whether other surfaces (Android, Portal) can ever reuse the status model.
7. **Staleness policy.** Should the hub re-validate keys on view (live API round-trips, cost) or trust `validated_at` and show "validated N days ago"? Defines whether G2's "what's validated" means *was-valid* or *is-valid*.
8. **Scope of correctness sweep (H).** Are the dead `renderAlreadyPaired`, the sigil off-by-N, and the leaky module-level singletons in-scope for this effort, or tracked separately? (They will become user-visible the moment a hub surfaces them.)

---

*Relevant paths: `Portal/onboarding/onboarding.js` (shell, `?step=` revisit, stubbed `?mode=manage`), `Portal/onboarding/onboarding.css` (81KB monolith, seam ~L1865), `Portal/onboarding/onboarding-tokens.css` (token contract), `Portal/onboarding/steps/{done,cli_agents,embeddings,api_keys,pair_phone}.js` (status-model exemplars + dead-code bug), `Orchestrator/routes/onboarding_routes.py` (`/state`, `/current-config`, `/restart-status`, `/step/*`), `Orchestrator/routes/embeddings_routes.py` (`/embeddings/status`), `Orchestrator/onboarding/state.py` (state model + sentinel + unexposed `set_current()`), `Orchestrator/onboarding/secrets_writer.py` (`.env` writes), `.onboarding_state.json` (live state).*