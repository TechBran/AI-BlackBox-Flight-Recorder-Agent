# Cron / Scheduler Subsystem — Production Audit

**Date:** 2026-06-23
**Method:** Read-only multi-agent audit (6 parallel finders → per-finding adversarial verification → synthesis). 46 agents, 39 raw findings → **36 confirmed, 3 refuted**.
**Scope reviewed:** `Orchestrator/scheduler/{manager,executor}.py`, `Orchestrator/routes/cron_routes.py`, `Portal/modules/cron-manager.js`, `Portal/index.html` (cron form), `Portal/styles/**/_cron.css`, `Orchestrator/startup.py`, `Orchestrator/routes/admin_routes.py` (model catalog), `blackbox.service`.

---

## Executive Summary

The cron scheduler is **functionally live** — jobs fire, definitions persist in SQLite (`cron_jobs.db`), the UI lists/searches/runs them. It falls short of the five stated goals along four fault lines:

1. **Model/provider parity (the screenshotted complaint).** The UI offers only a 5-option provider-family `<select>` (`#cronModel`, `index.html:1394-1401`); every job collapses to the provider's `*_MODEL_DEFAULT`. **The backend already carries arbitrary model ids end-to-end without loss** — free-text DB column, no API whitelist, executor passes unknown ids through — so this gap is **overwhelmingly frontend**, no DB migration required. One real backend bug rides along: computer-use jobs hardcode `CU_MODEL_DEFAULT` (`executor.py:161`).

2. **Restart / reboot / power-off survival.** Definitions survive all three. But **any run scheduled inside a downtime window is silently lost** — APScheduler uses the volatile in-memory jobstore, `start()` rebuilds triggers forward from `now()` with no coalesce/catch-up, and one-shot jobs missed during downtime never fire.

3. **RTC / clock baseline.** This is the literal cause of the **frozen-at-March `next_run`** in the screenshot. `next_run_at` is a denormalized cache advanced *only* on run/create/resume/schedule-edit; `pause_job` never touches it and startup never recomputes it, so paused jobs freeze forever. Compounding it: the scheduler is configured `timezone='UTC'` while every `CronTrigger.from_crontab()` silently inherits OS-local time (firing *happens* to match local-wall-clock intent, but config contradicts behavior), and persisted ISO offsets go DST-stale.

4. **UI restyle.** Status is triple-encoded as a solid card gradient **fill** + left accent + badge pill. Dropping the fills in favor of border/accent-only is the requested restyle. (Padding is already consistent with sibling cards — see refuted UI-2.)

Plus correctness defects from the full feature pass: **notification delivery is a silent no-op** (advertised in UI + AI tool, delivers nothing), **no per-job execution lock** (run-now can double-fire SMS/voice), **failed one-shots are deleted anyway** (permanent data loss), no server-side validation, and a hardcoded `'Brandon'` operator fallback that violates the fresh-box gate.

**Good news for the build:** backend is fully pytest-testable; the canonical model catalog (`GET /models/{provider}`) + the chat composer's `fetchAvailableModels`/`buildHydratedModels`/`updateModelDropdown` are reusable as-is; Android already consumes the same catalog; **no DB migration** is needed to store specific model ids.

---

## Theme A — Model & Provider Selector Parity *(the screenshotted complaint)*

- **HIGH (feature-gap)** Cron model `<select>` offers only 5 provider-coarse options, no specific model ids — the literal screenshotted defect. `index.html:1394-1401`; read/written as a static provider string at `cron-manager.js:270/302/529`.
- **HIGH (model-parity)** `_resolve_model_name()` flattens every provider alias to that provider's `*_MODEL_DEFAULT` (`executor.py:370-393`). The function **already passes specific ids through unchanged** (L393), so the defect is upstream (the form), not here.
- **HIGH (real bug, latent)** Computer-use cron jobs hardcode `CU_MODEL_DEFAULT`, ignoring any selected model (`executor.py:161`, dispatched without the model at `:92-93`). Only becomes user-visible once the form can pick a CU model.
- **MEDIUM (ui)** Cron form has no provider control distinct from model — conflated into the single coarse `#cronModel`; chat uses a two-control provider+model pattern (`app-init.js:130-149`).
- **INFO (no-defect)** Backend accepts any model id with zero schema/API change: DB `model TEXT` free-text (`manager.py:114`), no API whitelist (`cron_routes.py:21/33`), executor passthrough (`executor.py:393`). **Fix is frontend-only.**
- **INFO (source-of-truth)** Canonical catalog is ONE endpoint `GET /models/{provider}` (`admin_routes.py:787`), locked JSON `{provider, models[{id,name[,backend]}], source, default_id, fetched_iso, cached}`, providers google/anthropic/openai/xai/computer-use, live vendor SDK lists w/ 10-min cache. Reuse `fetchAvailableModels`/`buildHydratedModels`/`updateModelDropdown` (`state-management.js:473/561/592`).
- **LOW (ui)** Job-card + history render the raw stored model string (`cron-manager.js:186/641`) — forward-compatible but shows `gpt-5-mini-2025-08-07` instead of a friendly name.
- **LOW (model-parity)** Default cron model resolves to a `-preview` id (`GEMINI_MODEL_DEFAULT=gemini-3.1-pro-preview`, `config.py:699`); flags the GA-over-preview convention.

## Theme B — Restart / Reboot / Power-off Survival

- **HIGH (restart-survival)** Runs missed during downtime are silently skipped — no catch-up/coalesce. `start()` (`manager.py:157-177`) rebuilds a fresh `CronTrigger.from_crontab()` (`:562-585`) with no `coalesce`/`next_run_time` carry-over; next fire is computed forward from now. One-shots missed during an outage are lost **permanently**. `misfire_grace_time=30` is inert after a cold start (memory jobstore wiped).
- **MEDIUM (restart-survival)** Default in-memory jobstore (`manager.py:95`). A malformed schedule on reload is logged-and-skipped (`:166-170`), leaving the job `status='active'` in DB+UI with **no live trigger** — an invisible DB↔scheduler divergence.
- **MEDIUM (restart-survival)** `next_run_at` is never recomputed/persisted on startup — the display value goes stale across restarts even for active jobs.
- **MEDIUM (correctness)** A one-shot job is deleted even when its run **FAILED** (`manager.py:742-745`, unconditional after the caught-error path `:645-648`; cascade-deletes the history row `:428`). A transient failure (chat API down) evaporates the task with no trace.
- **LOW (restart-survival)** Boot ordering correct (rides `blackbox.service`, `Restart=always`, lifespan-wired) but **no `After=time-sync.target`/chrony gate** — a no-RTC cold boot can compute next-fire against a pre-NTP wrong clock (relevant given the project's Jetson clock-skew history).
- **LOW (restart-survival)** `cron_jobs.db` runs rollback-journal `synchronous=FULL` (power-off-safe, no corruption) but not WAL; concurrent execution + API writes share the DB unguarded (5s busy_timeout). Hardening, not blocking.

## Theme C — Real-Time Clock Baseline & `next_run` Staleness *(the March-frozen job)*

- **HIGH (time-rtc)** Paused (and never-run) jobs freeze `next_run_at` forever — **the literal cause of the March-stale display.** `pause_job` (`manager.py:439-472`) writes only status+updated_at. Live DB proof: paused `Daily Log Health Check` `next_run_at='2026-03-02T15:00:00-05:00'`, ~3.5 months stale. UI renders it verbatim (`cron-manager.js:191`).
- **HIGH (time-rtc)** Two conflicting clock baselines: `AsyncIOScheduler(timezone='UTC')` (`manager.py:95`) but every `from_crontab()` omits tz and inherits OS-local (verified `→ America/New_York`). Firing IS local (matches intent) but is accidental/undocumented; persisted `next_run_at` carries a local offset while all other timestamps are UTC → mixed-offset storage already in the DB.
- **HIGH (time-rtc/portability)** Local-time UI vs UTC-configured scheduler can fire at the wrong wall-clock hour on a fresh non-UTC box: `simpleToCron` (`cron-manager.js:402-424`) emits raw `<input type="time">` values with no offset conversion; no per-job timezone column.
- **MEDIUM (time-rtc)** Persisted DST offset goes stale across spring/fall transition — `.isoformat()` bakes the offset at compute time (`manager.py:225/366/489/680`); an active daily job shows a 1-hour-wrong "Next" for up to a day after each DST change.
- **LOW (ui)** `frequency_hint` is a frozen client-authored string never reconciled with the cron expression (`cron-manager.js:181/412/442-460`); can show "Daily at 3:00 PM" with no tz next to a stale "Next".

## Theme D — UI Restyle (status fills → border/accent + padding)

- **LOW (ui)** Status is triple-encoded — solid card gradient **FILLS** (`_cron.css:127` active green `rgba(76,175,80,0.04)`, `:139` paused amber `rgba(255,167,38,0.03)`) **plus** `border-left: 3px solid` accent (`:108/126/138`) **plus** 15% badge pills (`:170-178`). Render applies statusClass to card (`cron-manager.js:178`) and badge (`:180`). Requested restyle: **drop the card gradient fills, keep the left accent + badge as the status color carrier, add card padding.** Purely cosmetic, zero accessibility impact (badge text already conveys status).

## Theme E — Cron Correctness, Validation & Portability *(full feature pass)*

- **HIGH (feature-gap)** **Notification delivery is a silent no-op** — `_build_prompt` (`executor.py:240-269`) only special-cases voice_call/sms; `delivery=='notification'` falls through to the snapshot path (byte-identical). Advertised in the UI (`index.html:1417`) AND the AI tool schema. No app-layer push primitive exists today (platform `PushNotification` is the path to wire in).
- **HIGH (correctness)** **No per-job execution lock** — `run_job_now` (`manager.py:520-533`) calls `_execute_job` directly, bypassing APScheduler's `max_instances=1`. Run-now overlapping a scheduled fire (or a double-click) double-executes: two /chat tasks, two history rows, `run_count++` twice, and for sms/voice the recipient is contacted **twice**. Only CU has a *global* (not per-job) lock.
- **MEDIUM (portability)** Hardcoded `'Brandon'` operator fallback (`cron-manager.js:88` and `:580`) violates the fresh-box rule — on a box with operators `['Alice']`, jobs are created/queried under a phantom 'Brandon'. Dropdown is already server-driven; fallback should be `_operatorList[0]` or block-with-error.
- **MEDIUM (correctness)** No server-side validation of delivery/model/operator — only the cron expression is validated. Empty operator, unknown delivery, typo'd models persist and mis-route at execution time.
- **LOW (portability)** Executor hardcodes `BASE_URL='http://localhost:9091'` (`executor.py:24`) — loopback HTTP; port not config-derived (low blast radius, 9091 is hardcoded app-wide).
- **LOW (correctness)** `/run` blocks the HTTP request up to 180s (600s CU) (`cron_routes.py:140`); UI has no client timeout → a proxy idle-out shows a misleading "Failed to run job" on success. Should be fire-and-forget 202 + the existing 5s poll.
- **LOW (correctness)** Cron fire mutates the process-global `CURRENT_OPERATOR` (`executor.py:95-100`) — can cross-contaminate a concurrent interactive request. Pre-existing smell amplified by autonomous firing; out of cron scope to fully fix.
- **INFO (dead-code)** Executor stub-import `except ImportError` fallback (`manager.py:629-643`, `delivery_status='stub'`) is unreachable — remove.

---

## Refuted / Downgraded by Verification (kept for honesty)

- **MP-3 "CU hardcode is a parity break" → DOWNGRADED to latent/low.** The hardcode is real (`executor.py:161`) but the cron form has no CU-model picker and the schema stores only `"computer-use"`, so nothing is silently overridden *today*. Becomes a real bug only once M3 adds CU model selection.
- **UI-2 "padding too tight" → REFUTED.** `_cron.css` padding `12px/14px` + gap `8px` is *consistent* with `.contact-card` (`14px 16px`), device items (`12px 14px`), task items (`12px`). Bumping it would make cron cards *more* spacious than siblings. The "cramped" perception comes from the block fills + clipped text, not the padding.
- **PT-1 "Android lacks a /models consumer" → REFUTED.** Android already ships the full picker (`ChatViewModel.kt:2699`, `ChatRepository.kt:138`); the grep missed the `(2)` folder name. The catalog contract is already followed on Android.

---

## Proposed Milestone Plan *(for a TDD subagent-driven build)*

> Backend = pytest TDD; frontend = Chrome-verified. Every committed step leaves the app launchable (prod serves from the working tree).

**M1 — Time/RTC baseline unification + `next_run` truthfulness** *(backend, TDD)*
Make the PC local wall clock the single authoritative baseline; stop persisting `next_run_at` as a frozen cache. Derive `LOCAL_TZ` once, pass it to both the scheduler and every `from_crontab(..., timezone=LOCAL_TZ)`. Recompute `next_run_at` in `pause_job` (or null it for paused) and on every `start()`/re-register read-back from the live trigger → self-heals the March-frozen value. Normalize stored instants. Regenerate `frequency_hint` server-side from the validated cron. *Optional:* per-job timezone column. **Risk: medium** (codifying current local behavior; tz column is an additive migration).

**M2 — Restart / reboot / power-off run recovery** *(backend + systemd, TDD)*
Explicit startup catch-up (preferred over a 2nd jobstore to avoid DB↔jobstore drift): on `start()`, compare `last_run_at` + cron vs now and enqueue one coalesced catch-up run (or write a "missed" history row) per policy; fire past-due one-shots. Harden reload (flag failed-to-register jobs as `error`, not silent-active). Add `GET /api/cron/health` (every active job has a live trigger). Add `After=time-sync.target` to `blackbox.service`. *Optional:* WAL + busy_timeout. **Risk: medium-high** (catch-up policy must avoid a thundering herd after a long outage).

**M3 — Full model+provider selector parity** *(frontend Chrome-verified + CU bug backend TDD)*
Replace the coarse `#cronModel` with the chat composer's two-control pattern (`cronProvider` + `cronModelId` hydrated from `GET /models/{provider}` via the existing helpers), `(Auto - <default>)` first. Store the concrete id (or `''` for Auto); `openEditModal` derives provider from `job.model` via the same substring logic as `_model_to_provider`. Render friendly names on cards/history. **Backend:** parameterize `_execute_cu_job` to honor the selected CU model (fixes the latent MP-3). No DB migration. **Risk: low-medium** (legacy jobs storing bare aliases must still display).

**M4 — Execution-safety + validation hardening** *(backend, TDD)*
Per-job `asyncio.Lock` in `_execute_job` (prevents run-now × scheduled double-fire + double SMS/voice). Delete one-shots **only on success** (failed ones survive). Server-side validation (operator non-empty/known, delivery enum, model resolvable, require `delivery_target` for sms/voice → 400). Make `/run` fire-and-forget (202 + existing poll). Remove the dead stub branch. **Risk: low.**

**M5 — Notification delivery + UI restyle + portability polish** *(frontend Chrome-verified + backend TDD + Android native)*
**Notification (RESOLVED mechanism — extend `control_phone`, no FCM/WebSocket):**
- **Android** — add a `POST /notify {title, body, operator}` case to `RemoteControlServer.routeRequest` (`RemoteControlServer.kt:82`), gated by the existing `authorize()` operator-match (`:126/130-133`), calling the existing `BlackBoxNotificationManager.showTaskNotification(...)` (`:182`). Hosted by the already-running `LocalModelService` — no new service. Fires a real system notification even when the WebView is closed.
- **Backend** — new deliverer (~30 lines, copy-shaped from `control_phone/executor.py:60-95/105-116`): `node = mesh.resolve_origin(operator)` (`local_provider/mesh.py:149`) → `POST http://{node.dns_name or node.ip}:{control_port}/notify`. Degrade gracefully to snapshot + log when `resolve_origin` returns `None` (Tailscale down / device offline / unattested) — the fresh-box path.
- **Cron** — deterministic post-step in `executor.py` after `result_text = await _poll_task_until_done(...)` (`:133`): if `delivery=='notification'`, call the deliverer with `operator`, `title=job_name`, `body=result_text`. (Not prompt-injection — there is no `send_notification` LLM tool, so the post-step is the reliable mirror of the sms/voice pattern at `_build_prompt` `:240-269`.)

UI restyle: remove the solid card gradient fills (`_cron.css:127/139`), keep border-left accent + badge as the status carrier. Replace `'Brandon'` literals with `_operatorList[0]`/block-with-error (`cron-manager.js:88/580`). Derive executor `BASE_URL` from config (or call in-process). **Risk: low-medium** (notification touches Android native + tailnet resolution, but reuses a proven primitive — strictly additive).

---

## Decisions Locked (2026-06-23)

1. **Clock contract → BOX-LOCAL FOR ALL.** Every job uses the PC's local wall clock (`LOCAL_TZ` everywhere). **No per-job timezone column.** Each downloaded box uses its own RTC. *(M1)*
2. **Catch-up policy → ALWAYS CATCH UP ONCE.** On boot, run a single **coalesced** catch-up for any job whose fire was missed during downtime, then resume. **No per-job toggle.** Coalesce must yield exactly one run per job, never N. *(M2)*
3. **M2 mechanism → SQLite-as-source-of-truth + explicit/coalesced startup catch-up** (recommended default; avoids the APScheduler-jobstore drift risk). *(M2)*
4. **Notification delivery → WIRE REAL PUSH NOW.** Implement real push to the operator's paired device(s). Larger scope — push subsystem under investigation (see follow-up). *(M5)*
5. **GA-vs-preview default → GA** for cron "Auto" on scheduled customer-facing jobs (recommended default). *(M3)*
6. **Run-now UX → FIRE-AND-FORGET** (202 + existing 5s poll; recommended default). *(M4)*
7. **CU per-model scheduling → ALLOW PICKING A CU MODEL.** M3 parameterizes the `_execute_cu_job` pin (`executor.py:161`), adds CU models to the selector, validates against `CU_MODEL_FILTERS`. *(M3)*
