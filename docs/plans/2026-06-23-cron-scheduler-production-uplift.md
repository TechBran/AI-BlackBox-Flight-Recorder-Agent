# Cron / Scheduler Production-Quality Uplift — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (same-session, fresh implementer per task + spec-review then code-review) OR superpowers:executing-plans (parallel session). Backend tasks are pytest-TDD; frontend tasks are Chrome-verified; Android tasks compile via `./gradlew compileDebugKotlin`. **Every committed step must leave the app launchable** (prod serves from the working tree).

**Goal:** Bring the cron/scheduler subsystem to production quality: truthful local-clock scheduling, restart/power-off survival, full model+provider selection across all four surfaces, a hardened AI-facing tool, real push-notification delivery, and a cleaner card UI.

**Architecture:** Backend = FastAPI + APScheduler over SQLite (`cron_jobs.db`); the `CronJobManager` is the single sink all four surfaces funnel through. Surfaces: Portal web (`cron-manager.js`), HTTP API (`cron_routes.py`), ToolVault AI tools (`create_cron_job`/`edit_cron_job`/`search_cron_jobs`), and Android (`CronManagerScreen.kt`/`CronViewModel.kt`). The live model catalog is `GET /models/{provider}` (reused as-is — no new catalog). Notification delivery extends the proven `control_phone` Tailscale primitive.

**Tech stack:** Python 3 / FastAPI / APScheduler / SQLite / pytest (`Orchestrator/venv/bin/pytest`); vanilla ES-module Portal (Chrome-verified); Android Jetpack Compose / Kotlin (kotlinx.serialization, OkHttp).

**Inputs:** Audit `docs/plans/2026-06-23-cron-scheduler-production-audit.md`; two verified multi-agent investigations (73 confirmed findings total).

**Locked decisions (operator, 2026-06-23):** ① clock = **box-local for all**, *no per-job timezone column*; ② missed-run policy = **always catch up once** (coalesced); ③ M-catch-up mechanism = **SQLite-as-source-of-truth + coalesce** (no second jobstore); ④ notifications = a **wholesale, deterministic, per-operator subsystem** (the new **MN** milestone) — **FGS-over-Tailscale + durable snapshot inbox** (FCM deferred), fresh-box **opt-in** (subscribe to nothing by default), cross-operator safety via **USERS_LIST validation + audit + metadata-only payloads**; ⑤ "Auto" default = **GA/configured `*_MODEL_DEFAULT`**; ⑥ run-now = **fire-and-forget**; ⑦ **allow picking a CU model**; ⑧ AI `delete_cron_job` = **not built — delete stays UI-only**.

**Suggested execution order:** M1 → M2 → M3 → M4 → **MN** → M5. MN (Notification Subsystem) must land before M5 (M5.1 calls the bus). M6 is explicitly deferred (documented, not built). Pure-backend, high-value, low-risk milestones land first; the multi-surface milestones (M4 selector, MN notifications) land later.

**Resolved (operator, 2026-06-23):** **delete stays UI-only.** No AI-facing `delete_cron_job` tool — the deliberate guardrail at `cron_routes.py:108` is kept. The AI uses pause/disable (`edit_cron_job pause=true`) for "cancel that job"; deletion remains a human-only Portal/Android action.

---

## Reference: file map

| Concern | File |
|---|---|
| Job store / scheduler | `Orchestrator/scheduler/manager.py` (757) |
| Job execution | `Orchestrator/scheduler/executor.py` (393) |
| HTTP API | `Orchestrator/routes/cron_routes.py` (151) |
| Startup/shutdown wiring | `Orchestrator/startup.py:529-538 / 755-763` |
| Model catalog endpoint | `Orchestrator/routes/admin_routes.py:787` (`GET /models/{provider}`) |
| Tool-arg coercion chokepoint | `Orchestrator/tools/blackbox_tools.py:47-85` (`_coerce_stringified_json_args`) |
| ToolVault cron tools | `ToolVault/tools/{create_cron_job,edit_cron_job,search_cron_jobs}/` |
| Portal UI | `Portal/modules/cron-manager.js` (884) + `Portal/index.html:1394-1417` |
| Portal CSS | `Portal/styles/**/_cron.css` |
| Portal model-catalog helpers | `Portal/modules/state-management.js:473/561/592`, `app-init.js:130-149` |
| Android cron surface | `…/ui/cron/CronManagerScreen.kt`, `…/ui/cron/CronViewModel.kt`, `CronJob.kt` |
| Android live-models helper | `…/ChatViewModel.kt:~2699` (`fetchLiveModels`, CU backend partitioning) |
| Notification deliverer target | `…/data/remote/RemoteControlServer.kt:82/126`, `BlackBoxNotificationManager.kt:182` |
| Operator→device resolve | `Orchestrator/local_provider/mesh.py:149` (`resolve_origin`), `:115` (`reachable_devices`) |

---

# M1 — Clock / RTC truthfulness (box-local)

**Why:** Fixes the screenshotted March-frozen `next_run`, makes the PC local wall clock the single authoritative baseline, and stops persisting `next_run_at` as a frozen cache. Pure backend → fully pytest-testable. **No per-job timezone column** (operator decision ①).

### Task M1.1 — Derive one box-local timezone and use it everywhere

**Files:** Modify `Orchestrator/scheduler/manager.py` (L95 scheduler ctor; the 5 `CronTrigger.from_crontab(...)` sites ~L216/360/487/566/678). Test: `Orchestrator/tests/test_cron_local_tz.py` (create).

- **Step 1 (failing test):** Assert the scheduler's timezone equals the host local zone and that a trigger built for `"0 15 * * *"` has the same `.timezone` as the scheduler (no silent OS-local inheritance vs UTC-config mismatch).
  ```python
  def test_scheduler_and_triggers_share_local_tz():
      from Orchestrator.scheduler.manager import CronJobManager, LOCAL_TZ
      m = CronJobManager(db_path=tmp_db)
      assert str(m.scheduler.timezone) == str(LOCAL_TZ)
      trig = m._build_trigger("0 15 * * *")  # new tiny helper wrapping from_crontab(..., timezone=LOCAL_TZ)
      assert str(trig.timezone) == str(LOCAL_TZ)
  ```
- **Step 2:** Run → fails (`LOCAL_TZ`/`_build_trigger` undefined).
- **Step 3 (implement):** Add `LOCAL_TZ = get_localzone()` (use `tzlocal`; if unavailable, fall back to `datetime.now().astimezone().tzinfo`). Pass `timezone=LOCAL_TZ` to `AsyncIOScheduler(...)`. Add `def _build_trigger(self, schedule): return CronTrigger.from_crontab(schedule, timezone=LOCAL_TZ)` and route ALL 5 sites through it. Verify `tzlocal` is in `Orchestrator/requirements.txt`; add if missing.
- **Step 4:** Run → passes. Run the full cron test module.
- **Step 5 (commit):** `feat(cron): bind scheduler + all cron triggers to box-local timezone`

### Task M1.2 — Recompute `next_run_at` on pause and on every startup re-register

**Files:** Modify `manager.py` (`pause_job` ~L439-472; `_register_job_with_scheduler` ~L562-585; `start()` ~L157-177). Test: `test_cron_next_run_truthful.py`.

- **Step 1 (failing tests):** (a) After `pause_job`, the row's `next_run_at` is `None`/`""` (paused jobs show `—`, never a stale fire time). (b) After `start()` re-registers an active job, its persisted `next_run_at` equals the live `scheduler.get_job(id).next_run_time` (self-heals a stale DB value). Seed a job with a deliberately stale `next_run_at='2026-03-02T15:00:00'` and assert `start()` overwrites it.
- **Step 3 (implement):** In `pause_job`, set `next_run_at=None` in the UPDATE. In `_register_job_with_scheduler` (runs on create AND every boot), after `add_job(...)` read back `self.scheduler.get_job(job_id).next_run_time` and persist it to `cron_jobs.next_run_at` (a single UPDATE). This is the self-heal: every restart refreshes the displayed Next against the live trigger.
- **Step 5 (commit):** `fix(cron): recompute next_run_at on pause + every startup so the displayed Next is never stale (fixes March-frozen job)`

### Task M1.3 — Regenerate `frequency_hint` from the cron expression (server-side, can't lie)

**Files:** Modify `manager.py` (`create_job` ~L214; `update_job` schedule branch ~L339-366). Test: `test_cron_frequency_hint.py`.

- **Step 1 (failing test):** Creating a job with `schedule="30 6 * * 1-5"` yields a `frequency_hint` derived from the cron (e.g. "Weekdays at 6:30 AM"), regardless of any client-supplied hint string; editing the schedule regenerates it.
- **Step 3 (implement):** Add `def _hint_from_cron(schedule) -> str` (small humanizer; `cron-descriptor` if already vendored, else a compact hand-rolled formatter covering minute/hour/dow common cases with an "(local)" suffix). Call it in `create_job` and the `update_job` schedule branch; treat the cron as the single source of truth (ignore client `frequency_hint`).
- **Step 5 (commit):** `fix(cron): derive frequency_hint from the cron expression so the label can't contradict the schedule`

---

# M2 — ToolVault + validation hardening & execution safety

**Why:** The AI-facing creation path and the manager sink are unhardened. Centralize validation in `CronJobManager.create_job/update_job` so all four surfaces inherit one set of guards. Includes the most dangerous bug (CR-1 silent SMS non-delivery). Pure backend + ToolVault schemas → pytest-testable.

### Task M2.1 — Central validation sink: enum allow-lists + required E.164 delivery_target

**Files:** Modify `manager.py` (`create_job` ~L189-262, `update_job` ~L326-404); `cron_routes.py` (wrap PUT ~L95-103 in try/except→400). Test: `test_cron_validation.py`.

- **Step 1 (failing tests):** `create_job`/`update_job` raise `ValueError` when: `delivery` ∉ `{snapshot,sms,voice_call,notification}`; `status` ∉ `{active,paused}`; `operator` is empty/whitespace; `delivery∈{sms,voice_call}` with missing/blank `delivery_target` OR a `delivery_target` not matching `^\+[1-9]\d{6,14}$`. Assert a valid job still creates. Assert PUT returns 400 (not 500) on a bad value.
- **Step 3 (implement):** Add a private `_validate_job_fields(fields, *, partial)` called at the top of both methods. Regex E.164. `model` is *not* hard-enum'd (free-text by design) but if non-empty is checked resolvable (see M2.2). Wrap the PUT route body in `try/except ValueError → HTTPException(400, str(e))`. (POST already maps ValueError per `create_cron_job/executor.py:27`.)
- **Step 5 (commit):** `fix(cron): central field validation at the manager sink (enum allow-lists + required E.164 delivery_target) — closes loud-success/silent-non-delivery`

### Task M2.2 — Validate `model` resolvability at create/edit time (loud, not at fire time)

> **MOVED to M4 (as M4.x) during execution (2026-06-23):** this check couples to the live `/models/{provider}` catalog, which M4 builds out (provider column + catalog-backed selector). Validating a model against the catalog belongs with the milestone that wires the catalog, so it ships there to avoid a premature `manager → admin_routes` coupling. Kept here for traceability.

**Files:** Modify `manager.py` `_validate_job_fields`; reuse `executor._model_to_provider`. Test: extend `test_cron_validation.py`.

- **Step 1 (failing test):** A job with `model="totally-made-up"` is rejected at create time with a clear message; `model=""` (Auto) and any real id/alias pass.
- **Step 3 (implement):** When `model` non-empty, resolve provider via `_model_to_provider`; if the provider is unknown OR (optionally) the id isn't in the cached `/models/{provider}` list, raise `ValueError`. Keep this lenient enough to not block a just-released id when the live catalog is briefly unreachable (allow when catalog fetch fails — defense-in-depth, not a hard gate).
- **Step 5 (commit):** `feat(cron): reject unresolvable model ids at create/edit time instead of failing silently at fire time`

### Task M2.3 — Extend the tool-arg coercion chokepoint to booleans

**Files:** Modify `Orchestrator/tools/blackbox_tools.py:47-85` (`_coerce_stringified_json_args`). Test: `Orchestrator/tests/test_tool_arg_coercion_bool.py`.

- **Step 1 (failing test):** A tool whose schema marks `one_shot: boolean` receiving `"false"` (string) coerces to `False`; `"true"/"1"/"yes"`→`True`; `"false"/"0"/"no"/""`→`False`. Real booleans pass through.
- **Step 3 (implement):** Extend the `ptype in ("array","object")` gate to also handle `"boolean"`: normalize common string forms. This fixes `one_shot='false'`→auto-delete and `pause='false'`→wrong toggle for ALL tools, at one chokepoint (per the project's stringified-tool-args lesson).
- **Step 5 (commit):** `fix(tools): coerce stringified booleans at the tool-arg chokepoint (one_shot/pause '"false"' bugs)`

### Task M2.4 — Make `edit_cron_job` pause/resume non-terminal (combine with field edits)

**Files:** Modify `ToolVault/tools/edit_cron_job/executor.py:15-35` and `schema.json` (add `one_shot`; optional `operator`). Test: `Orchestrator/tests/test_edit_cron_tool.py`.

- **Step 1 (failing test):** A single `edit_cron_job` call with `pause=false` AND `schedule="0 8 * * *"` both resumes the job and applies the new schedule (today the early-return drops the schedule).
- **Step 3 (implement):** Remove the early-return; translate `pause:true/false` → `updates["status"]="paused"/"active"` and fall through to the single `update_job` path (which already whitelists `status` and re-registers with APScheduler). Add `one_shot` (+ optional `operator`) to `edit_cron_job/schema.json`. `POST /toolvault/reload` after schema edit.
- **Step 5 (commit):** `fix(cron-tool): edit_cron_job applies field updates alongside pause/resume (no more silent dropped edits)`

### Task M2.5 — Operator ownership checks on mutating tools

**Files:** Modify `ToolVault/tools/edit_cron_job/executor.py` (+ the `cron_job_history` tool in M2.7). Test: `test_cron_tool_ownership.py`.

- **Step 1 (failing test):** Operator `A` editing operator `B`'s job gets a generic "Job not found" (no existence leak); `system` operator may edit any; owner may edit own.
- **Step 3 (implement):** Pre-mutation `manager.get_job(job_id)`; compare `job['operator']==ctx.operator or ctx.operator=='system'`, else return `ToolResult(False, "Job not found")`.
- **Step 5 (commit):** `feat(cron-tool): operator-ownership scoping on cron mutations (intra-perimeter blast-radius hygiene)`

### Task M2.6 — Per-job execution lock + delete-one-shot-only-on-success + remove dead stub

**Files:** Modify `manager.py` (`_execute_job` ~L629-745; `run_job_now` ~L520-533). Test: `test_cron_execution_safety.py`.

- **Step 1 (failing tests):** (a) Two concurrent `_execute_job(job_id)` calls → only one runs the body, the other no-ops with a history note (per-`job_id` `asyncio.Lock`). (b) A one-shot whose run raised is NOT deleted (survives as `status` active/`failed`), while a successful one-shot IS deleted. (c) The unreachable `except ImportError` stub branch is gone.
- **Step 3 (implement):** Add `self._job_locks: dict[str, asyncio.Lock]`; acquire (non-blocking `locked()` check → skip) at the top of `_execute_job`. Guard the one-shot delete (`manager.py:742`) with `and error_text is None`. Delete the dead stub branch (`manager.py:629-643`).
- **Step 5 (commit):** `fix(cron): per-job execution lock + keep failed one-shots + drop dead stub branch`

### Task M2.7 — New ToolVault tool: `cron_job_history`; enrich `search_cron_jobs`

**Files:** Create `ToolVault/tools/cron_job_history/{schema.json,executor.py}`. Modify `ToolVault/tools/search_cron_jobs/executor.py:28-39`. Test: `test_cron_new_tools.py`. Validate with `python -m Orchestrator.toolvault.validate` then `POST /toolvault/reload`.

- **Decision:** No `delete_cron_job` tool — **delete stays UI-only** (operator decision; the `cron_routes.py:108` guardrail is kept). The AI uses `edit_cron_job pause=true` to disable a job.
- **Step 1 (failing tests):** `cron_job_history` returns past runs via existing `manager.get_job_history` (`manager.py:539`), operator-ownership-scoped (M2.5); `search_cron_jobs` output now includes `last_run_result`/`error_count`/`last_run_duration_ms` (already in the dicts — render-only).
- **Step 5 (commit):** `feat(cron-tool): add cron_job_history + surface run outcomes in search`

### Task M2.8 — Retry-on-failure with bounded backoff

**Files:** Modify `manager.py` `_execute_job` (record per-attempt history rows). Test: `test_cron_retry.py`.

- **Step 1 (failing test):** A job whose run fails is retried up to `MAX_RETRIES` (default 2) with backoff; each attempt writes a history row; after exhausting retries `error_count` increments once and the job stays scheduled. A succeeding retry stops further attempts.
- **Step 3 (implement):** Wrap the execution body in a bounded retry loop (asyncio.sleep backoff e.g. 5s,30s). Keep retries within the single fire (do not re-enqueue). Respect the M2.6 one-shot-on-success rule (a one-shot that ultimately fails survives).
- **Step 5 (commit):** `feat(cron): bounded retry-on-failure with per-attempt history`

### Task M2.9 — Fire-and-forget `/run`

**Files:** Modify `cron_routes.py:140` (`/run`). Test: `test_cron_run_endpoint.py`.

- **Step 1 (failing test):** `POST /api/cron/jobs/{id}/run` returns `202` immediately (does not block up to 180/600s); the run proceeds in the background; completion is observable via the existing history/poll.
- **Step 3 (implement):** Schedule `run_job_now` as a background task (`asyncio.create_task` / FastAPI `BackgroundTasks`) and return `202 {status:"started"}`. (UI already polls every 5s.) Ensure the per-job lock (M2.6) prevents a background run from colliding with a scheduled fire.
- **Step 5 (commit):** `feat(cron): fire-and-forget run-now (202 + existing poll) instead of a blocking request`

---

# M3 — Restart / reboot / power-off catch-up

**Why:** Definitions survive; *runs* missed during downtime are silently lost. Operator decision: **always catch up once** (coalesced), **SQLite-as-source-of-truth** (no second jobstore). Backend + one systemd unit edit.

### Task M3.1 — Coalesce + single-instance job defaults

**Files:** Modify `manager.py` (scheduler `job_defaults` at ctor ~L95; `_register_job_with_scheduler` add_job kwargs ~L576-584). Test: `test_cron_coalesce.py`.

- **Step 1 (failing test):** A job registered with several missed fire times in the past produces **exactly one** catch-up execution when the scheduler processes them (not N). Assert `coalesce=True` and `max_instances=1` are in effect.
- **Step 3 (implement):** Pass `job_defaults={'coalesce': True, 'max_instances': 1, 'misfire_grace_time': <window>}` to `AsyncIOScheduler`, and/or set per-`add_job`. Choose `misfire_grace_time` to cover a realistic downtime window (config knob, default e.g. 6h) so a fire that elapsed during a short outage still counts as one coalesced catch-up.
- **Step 5 (commit):** `feat(cron): coalesce missed fires into a single catch-up run (max_instances=1)`

### Task M3.2 — Startup catch-up for past-due one-shots + reload hardening

**Files:** Modify `manager.py` (`start()` ~L157-177). Test: `test_cron_startup_catchup.py`.

- **Step 1 (failing tests):** (a) A one-shot whose absolute target time is in the past at boot fires once on startup (then is deleted only on success per M2.6). (b) A job whose `from_crontab` fails to register is marked `status='error'` (or a `load_error` column) — NOT left silently `active` with no trigger.
- **Step 3 (implement):** In `start()`, after re-registering, detect past-due one-shots (store/compare an absolute target) and enqueue one run. Wrap each `_register_job_with_scheduler` so a failure flags the job as errored and logs a WARN, instead of `continue`-ing silently.
- **Step 5 (commit):** `feat(cron): startup catch-up for past-due one-shots + surface failed-to-register jobs as errored`

### Task M3.3 — `GET /api/cron/health` + systemd time-sync gate + WAL

**Files:** Modify `cron_routes.py` (new health route); `manager._init_db` (PRAGMA); `blackbox.service` (drop-in). Test: `test_cron_health.py`.

- **Step 1 (failing test):** `GET /api/cron/health` returns, for every `active` DB job, whether a live scheduler trigger exists + its next fire; flags any divergence.
- **Step 3 (implement):** Implement the health route (iterate `list_jobs('active')` vs `scheduler.get_job`). Add `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000` in `_init_db`. Add `After=time-sync.target` + `Wants=systemd-timesyncd` to `blackbox.service` (drop-in) so a no-RTC cold boot computes fires against a synced clock. `sudo systemctl daemon-reload && restart blackbox.service` (pre-authorized).
- **Step 5 (commit):** `feat(cron): /api/cron/health + WAL + time-sync boot gate`

---

# M4 — Full model + provider selector across all 4 surfaces + CU

**Why:** Operator requirement — "select any model AND provider, same as chat." Thin-surface (backend already routes specific ids). Must update **all four** surfaces in lockstep or parity regresses. Frontend Chrome-verified + Android native + backend TDD + ToolVault schema.

### Task M4.1 — Backend: `provider` column + Auto/empty resolution + CU model threading

**Files:** Modify `manager.py` (schema `_init_db` ~L102-151 add `provider TEXT`, migration-safe `ALTER`; `create_job`/`update_job` persist it); `executor.py` (`_resolve_model_name` empty→provider-default ~L370-393; `_execute_cu_job` ~L148-163 honor chosen CU model; `_model_to_provider` recognize CU sub-model ids). Test: `test_cron_provider_column.py`, `test_cron_cu_model.py`.

- **Step 1 (failing tests):** (a) A job stored with `provider='anthropic', model=''` resolves at fire time to `ANTHROPIC_MODEL_DEFAULT` (Auto tracks default bumps) — provider identity preserved even with empty model. (b) Existing rows with no `provider` value still load (migration-safe default derived via `_model_to_provider(model)`). (c) `_execute_cu_job` sends the job's chosen CU model id (validated against `CU_MODEL_FILTERS`), falling back to `CU_MODEL_DEFAULT` only on empty/Auto.
- **Step 3 (implement):** Additive `ALTER TABLE … ADD COLUMN provider TEXT` guarded by a column-exists check. Persist provider; on read, backfill `provider = provider or _model_to_provider(model)`. Thread the resolved model into `_execute_cu_job(job_name, content, operator, model)`; let `resolve_backend(model)` pick the driver. Teach `_model_to_provider` to map CU sub-model ids → `computer-use`.
- **Step 5 (commit):** `feat(cron): persist provider + honor Auto/empty default + thread chosen CU model through the executor`

### Task M4.2 — ToolVault schemas: loosen `model` enum → free-text + document exclusions

**Files:** Modify `ToolVault/tools/create_cron_job/schema.json` (~L36-42) and `edit_cron_job/schema.json` (~L40-46); add an optional `provider` param. Validate + reload. Test: `test_cron_tool_model_freetext.py`.

- **Step 1 (failing test):** The AI can create a job with a specific id (`model="claude-opus-4-8"`) and it persists/resolves; the schema description documents "a provider word OR a specific model id from `/models/{provider}`" and notes `agents/gemini-agents/realtime/local` are out of cron scope (not server-routable). Off-catalog ids are rejected at create time (M2.2), loudly.
- **Step 3 (implement):** Change `model` from the 5-value enum to `type:string` with the documented description; add optional `provider`. `python -m Orchestrator.toolvault.validate` → `POST /toolvault/reload`.
- **Step 5 (commit):** `feat(cron-tool): allow any provider/model id in create/edit schemas (validated against the live catalog)`

### Task M4.3 — Portal: two-stage provider→model picker hydrated from `/models/{provider}`

**Files:** Modify `Portal/index.html:1394-1401` (replace `#cronModel` with `#cronProvider` + `#cronModelId`); `Portal/modules/cron-manager.js` (form read ~L529 `_doSaveJob`, edit populate ~L270/302, card/history render ~L186/641). Reuse `state-management.js` `fetchAvailableModels`/`buildHydratedModels`/`updateModelDropdown`. Cache-bust `index.html` (`?v=genuiNN`). **Chrome-verified.**

- **Steps:** Add the two selects; on `#cronProvider` change → `fetchAvailableModels(mapProvider(p))` → populate `#cronModelId` with `(Auto - <default>)` (value `''`) + every id. `_doSaveJob` reads `cronModelId.value` (+ provider). `openEditModal` derives provider from `job.provider || _model_to_provider(job.model)` and selects both. Render friendly catalog names on cards/history. For provider `computer-use`, hydrate from `/models/computer-use` with `backend` optgroups (mirror the composer). Persist last-used model per provider in `localStorage` (`bb_cron_model_<provider>`).
- **Chrome verification:** dropdown hydrates per provider; Auto round-trips; edit re-selects both controls; a legacy job storing a bare alias still displays. Screenshot before/after.
- **Commit:** `feat(cron-ui): two-stage provider+model picker reusing the chat composer's live catalog`

### Task M4.4 — Android: `CronViewModel`/`ModelSelector` live-model uplift (peer surface)

**Files:** Modify `…/ui/cron/CronManagerScreen.kt:~995` (`ModelSelector` hardcoded list) + `…/ui/cron/CronViewModel.kt`; reuse `ChatViewModel.kt:~2699` (`fetchLiveModels`, CU backend partitioning). `CronJob.kt` already carries `model` as free string. Build: `./gradlew compileDebugKotlin`.

- **Steps:** Factor the `/models/{provider}` fetch into `CronViewModel` (reuse the existing chat fetcher), feed `ModelSelector` a live provider→model list with an Auto option; persist provider+model on save; on edit, re-select from the job's provider/model. CU uses the backend-partitioned list.
- **Verification:** compiles; device/emulator smoke if available (create + edit a job, confirm model round-trips). Per "Frontend = 3 surfaces", this keeps Android at parity.
- **Commit:** `feat(android-cron): live provider+model selector matching Portal/chat`

---

# MN — Notification Subsystem (wholesale, deterministic, per-operator)

**Why:** Operator requirement — ANY background BlackBox event can fire a real system notification to a phone, even backgrounded or closed, with **zero model/LLM/Gemma/ADB in the path**, routed **per-operator** by phone subscription. This **replaces M5's old cron-only notification deliverer** (the standalone `notification_delivery.py` + the LocalModelService-hosted `/notify` are superseded); cron becomes just one caller. Backend = pytest-TDD; receiver/UI = Android-native (`./gradlew compileDebugKotlin`) + Portal Chrome-verified. **Must land before M5.1** (which calls the bus).

**Decisions applied:** delivery = **FGS-over-Tailscale + durable snapshot inbox** (FCM **deferred to M6** — force-stop/Tailscale-off are covered by record-and-replay, not cloud infra); fresh-box default = **opt-in (a never-configured phone is subscribed to NOTHING)**; cross-operator safety = **USERS_LIST validation + audit/revoke + metadata-only payloads** (no new app-layer auth — perimeter doctrine).

**Delivery coverage (honest):** foreground / background / **swiped-closed** / post-reboot / cellular-with-Tailscale-up = live ✅. **Force-stopped + Tailscale-off** = recorded to the snapshot inbox, replayed on reconnect (force-stop is OS-imposed, unsolvable in-perimeter).

**Identity model:** subscription key = `stableDeviceId()` (ANDROID_ID); routing match = `tailnet_name` (the identity `control_phone` already trusts; `mesh._name_matches` tolerates IP/hostname/FQDN — no new resolution code).

### Task MN.1 — Subscription store (backend, TDD)

**Files:** Create `Orchestrator/notifications/subscriptions.py` (`SubscriptionStore` over `Manifest/device_notification_subs.json`). Test: `test_notify_subscriptions.py`.

- **Step 1 (failing test):** `subscribers_for("Brandon")` returns devices whose sub has `"Brandon"` or `"all"`; an absent store file yields zero subscribers (no raise); a corrupt file reads as empty.
- **Step 3 (implement):** Atomic write + corrupt-tolerant read (copy `pairing_routes.py:77-110/52-75`: tmp→`os.replace`, empty on missing/corrupt). Keyed on `device_id`. Schema `{device_id:{all:bool, operators:[...], tailnet_name, device_kind, display_name, updated_at}}`. Pure predicates `subscribers_for(operator)` / `is_subscribed(device_id, operator)` = `'all' in operators or operator in operators`. **Fresh-box: empty store → opt-in (subscribed to nothing).**
- **Step 5 (commit):** `feat(notify): per-device operator-subscription store (atomic, fresh-box opt-in)`

### Task MN.2 — The `notify()` bus (backend, TDD)

**Files:** Create `Orchestrator/notifications/bus.py` (`async def notify(operator, title, body, category="general", *, dedup_key=None) -> NotifyResult`). Test: `test_notify_bus.py`.

- **Step 1 (failing tests):** With a stubbed `mesh` status seam + a seeded sub, `notify()` posts to exactly the subscribed∩reachable devices; with zero reachable it returns `recorded=True, delivered=[]` and **does not raise**; a slow device times out at 5s without stalling others; the event is recorded as a snapshot in every case.
- **Step 3 (implement):** `subs = subscribers_for(operator)` ∩ `mesh.reachable_devices(operator)` (`mesh.py:115`; `[]` when Tailscale down) → `asyncio.gather(*posts, return_exceptions=True)` with a **5s per-device timeout** → POST `…/notify` via `control_phone`'s `_phone_base_url(node)` idiom. **ALWAYS record** via in-process `mint_with_content` (`checkpoint.py:306`, bypasses the 3s debounce): `[NOTIFY:{category}] {title}` / body / operator. **Cross-operator payloads carry metadata only** (title/category/notif_id — recipient re-fetches content operator-scoped). `notify()` **never raises, never blocks past the timeout**; emits one `[NOTIFY] op=… cat=… delivered=N/M` line.
- **Step 5 (commit):** `feat(notify): generic notify() bus (subscriber∩reachable, fire-and-forget, always-record)`

### Task MN.3 — Subscription REST API (backend, TDD)

**Files:** Create `Orchestrator/routes/notification_routes.py`; register in `app.py` like `device_router`. Test: `test_notify_routes.py`.

- **Step 1 (failing tests):** `POST /notifications/subscribe` with an unknown operator → 422; with a known operator → upserts (no prior device-registry row needed); admin `GET /notifications/subscriptions` lists device→operators; `DELETE` revokes.
- **Step 3 (implement):** `POST /notifications/subscribe {device_id, tailnet_name, device_kind, display_name?, operators}` → upsert, **validate each operator against `USERS_LIST` (`config.py:140`)**. `GET …?device_id=` (reconcile), `GET …` (admin audit), `DELETE …/{device_id}` (revoke), `POST /notifications/send {operator,title,body,category}` (drives `notify()` for non-Python callers/tests).
- **Step 5 (commit):** `feat(notify): device subscription REST API (USERS_LIST-gated, auditable)`

### Task MN.4 — Android: model-free listener FGS + `/notify` route (Android-native)

**Files:** Create `…/portal/NotificationListenerFgs.kt`, `…/portal/util/DeviceId.kt` (hoist the duplicated `stableDeviceId()` from `LocalModelViewModel.kt:400` + `ProviderPickerViewModel.kt:152`). Modify `…/data/remote/RemoteControlServer.kt`, `AndroidManifest.xml`. Build: compile.

- **`NotificationListenerFgs`:** `foregroundServiceType=connectedDevice` (exempt from the Android 14+ dataSync 6h cap), `START_STICKY`, low-importance ongoing channel `blackbox_remote_notify`, owns a `BlackBoxNotificationManager(applicationContext)` + a minimal NanoHTTPD. **Independent of the Gemma engine — starts with no model installed** (does NOT inherit the `remoteTaskHandlerFactory` gate that currently disables the listener on model-less devices).
- **`RemoteControlServer`:** add a 4th `routeRequest` (`:82-113`) branch `POST /notify` → decode `{operator,title,body,category,notif_id,ts}` → injected `(title,body,category)->Unit` notifier → `200`. Extend `authorize()` (`:126`) with a `/notify` rule: existing tailnet-source check (`isTailnetSource :139`) **+ device-local subscription allow-list re-check** (defense in depth). Constructable with a no-op task handler so `/healthz`+`/notify` work model-free. Map `notif_id`→`(tag,id)` so retries collapse (`notify(tag,id,…)`) — replaces the non-idempotent time-based id (`:224`).
- **Manifest:** add `RECEIVE_BOOT_COMPLETED`; `<service .NotificationListenerFgs connectedDevice exported=false>`. (`POST_NOTIFICATIONS` already declared + granted.)
- **Commit:** `feat(android): model-free notification listener FGS + /notify route (decoupled from Gemma)`

### Task MN.5 — Android: boot persistence (Android-native)

**Files:** Create `…/portal/BootReceiver.kt`; modify `AndroidManifest.xml`. Build: compile.

- `BootReceiver` for `ACTION_BOOT_COMPLETED` → enqueue a WorkManager `OneTimeWorkRequest` to (re)start `NotificationListenerFgs` (**not** a direct `startForegroundService()` — that throws `ForegroundServiceStartNotAllowedException` from a background boot context on Android 12+). Manifest `<receiver … BOOT_COMPLETED>`.
- **Commit:** `feat(android): boot-persist the notification listener via WorkManager`

### Task MN.6 — Subscription settings UI (Android-native + Portal Chrome)

**Files:** Modify Android `SettingsSheet.kt` (+ a `NotificationSubscriptionViewModel` modeled on the persona panel `SettingsViewModel.kt:197-261`); Portal `device-manager.js`/menu modal + `notifications.js`. Build: compile + Chrome-verify.

- **Android:** a "Notifications" `CollapsibleSection` at the bottom of the sheet — `ToggleRow("All operators")` + per-operator `Checkbox` rows bound to the **existing live `ChatViewModel.operators` flow** (health-driven 60s poll — *verified to already auto-update as operators are added; no refresh-on-open needed*). PUT body via `Json.encodeToString(SubscriptionPutBody(all, operators))` (never string-interpolated — array-stringification class). Persist **both** DataStore (local pre-filter) **and** `PUT /notifications/subscribe` (write-through; GET reconciles on open, backend authoritative). PUT carries `device_id=stableDeviceId()` + `display_name=Build.MODEL`. **Default UI = nothing selected (opt-in).**
- **Portal (3rd surface):** admin read view (`GET /notifications/subscriptions` → device→operators table) + upgrade `notifications.js` scalar `currentOperator` filter (`:53`) to the subscription-set model so a browser session can opt into multiple operators. *(If time-boxed: ship Android + Portal-admin-read first, browser opt-in as a fast-follow — contracts stay additive.)*
- **Commit:** `feat(notify): per-operator subscription UI (Android + Portal)`

### Task MN.7 — Wire the wholesale producers (backend, TDD)

**Files:** Modify `Orchestrator/tasks.py:update_task` (~L205), `Orchestrator/embeddings/watcher.py` (health transitions), `Orchestrator/routes/agent_routes.py` (completion ~L775/1110), and the cron `_execute_job` caller. Test: `test_notify_producers.py`.

- **`update_task`:** on terminal status (COMPLETED/FAILED) call `notify(operator=task.operator, category='media'|'task', …)` — **one edit covers every media/checkpoint/chat worker** (the proof the bus is wholesale). Handle `operator='system'` (broadcast tier).
- **Watcher:** `notify(operator='system', category='index', …)` on ok→broken / model-superseded (+ surface the today-silent per-mint embedding failures). **Agent:** `notify(category='agent')` on completion (delivers even after the live socket dropped). **Cron `_execute_job`:** `notify(category='cron', …)` on done — realizes the dead `delivery='notification'` enum.
- **system/broadcast tier:** `operator='system'` events route to devices subscribed to `'all'` (distinct from per-operator routing).
- **Commit:** `feat(notify): wholesale producers (tasks/index/agent/cron) call the bus`

---

# M5 — Cron remainder (failure alert + next-run preview + restyle + portability)

**Why:** What's left of the original M5 after the Notification Subsystem (MN) absorbed delivery. Cron-scoped only. **Depends on MN** (M5.1 calls the bus). Backend TDD + Portal Chrome-verified.

### Task M5.1 — Cron failure alert as a bus caller

**Files:** Modify `manager._execute_job` terminal-failure path (after retries, M2.8). Test: `test_cron_failure_alert.py`.

- **Step 1 (failing test):** A job that fails all retries mints a **flagged cron-failure snapshot** (always) AND calls `notify(operator, category='alert', …)` exactly once (idempotent per terminal failure); a retry-storm collapses to one alert via the `dedup_key`.
- **Step 3 (implement):** On terminal failure: flagged failure snapshot (job name, error, timestamp) + `notify(operator, category='alert', title=…, body=error, dedup_key=f"cronfail:{job_id}:{fire_ts}")` — no bespoke deliverer.
- **Step 5 (commit):** `feat(cron): operator alert on job failure via the notification bus`

### Task M5.2 — Next-run preview endpoint + debounced UI render

**Files:** Create `POST /api/cron/preview` in `cron_routes.py`; wire a debounced call in `cron-manager.js` (schedule/time inputs) + Android edit screen. Test: `test_cron_preview.py`.

- **Step 1 (failing test):** `POST /api/cron/preview {schedule}` returns the next N fire times in box-local time using `_build_trigger(...).get_next_fire_time(...)`; rejects an invalid cron with 400.
- **Step 3 (implement):** Implement the endpoint (reuse M1 `_build_trigger`). In the editors, on schedule/time change (debounced ~300ms) show "Next: …" so the user catches tz/off-by-one before saving.
- **Step 5 (commit):** `feat(cron): live next-run preview while editing (box-local)`

### Task M5.3 — UI restyle: border/accent status (drop block fills) + portability fixes

**Files:** Modify `Portal/styles/**/_cron.css` (remove gradient fills L127/139; keep `border-left` accent L108/126/138 + badge L170-178; adjust padding); `cron-manager.js:88/580` (replace `'Brandon'` literals with `_operatorList[0]`/block-with-error); `executor.py:24` (`BASE_URL` from config). Cache-bust. **Chrome-verified.**

- **Steps:** Drop the solid card gradient fills so the card is a clean dark surface with a colored left accent + status badge (badge text already conveys status — no accessibility regression). Keep padding consistent with sibling cards (the audit refuted "too tight"; the cramped look was the fills + clipped text — verify text no longer clips after removing fills). Replace the hardcoded operator fallback. Derive executor `BASE_URL` from config (loopback default).
- **Chrome verification:** before/after screenshot; active/paused/error states show as accent + badge, not block fills; text not clipped; fresh-box operator (non-Brandon) works.
- **Commit:** `feat(cron-ui): border/accent status styling (drop block fills) + fresh-box operator fallback + config-derived BASE_URL`

---

# M6 — Cron operations & portability (DEFERRED — documented, not built now)

Explicitly **not** in this build (low value / manual workaround exists / risk-gated). Captured so they aren't silently dropped:

- **Dry-run / test-run** that doesn't count as a real run or place a real call/SMS (medium; genuinely useful — promote if the operator wants it).
- **`run_cron_job` AI tool** — needs a return-then-poll design (run_job_now blocks up to ~660s); risky as a naive sync tool.
- **History retention / pruning** — `cron_job_history` grows unbounded (~105k rows/yr for `*/5` jobs); low urgency at current volume.
- **Fleet observability `/api/cron/health` strip** in the UI (the endpoint itself ships in M3.3; the visual strip is deferred).
- **NL→cron parsing** — cheap partial already covered by M1.3 (derive hint from cron); a full natural-language parser is deferred.
- **FCM push transport** (notifications) — the ONLY path that reaches a **force-stopped** or **Tailscale-off** phone. A clean later add gated behind a new per-device `fcm_token` field that the MN bus also fans out to (adds a Firebase project + `google-services.json` + a `FirebaseMessagingService`). Deferred per decision ④; MN's durable snapshot inbox covers these cases via record-and-replay until/unless a real off-tailnet need appears.

**Hard-deferred (do NOT build):** job templates/duplicate, import/export JSON, bulk enable/disable — convenience/QoL with existing manual workarounds; would dilute the core coverage+hardening build.

---

## Cross-cutting verification (run after each milestone)

- Backend: `Orchestrator/venv/bin/pytest Orchestrator/tests/test_cron_*.py -q` (plus the touched non-cron tests). The pre-existing unrelated `test_embeddings_providers.py::test_ollama_keep_alive_passthrough` failure is NOT ours.
- ToolVault: `python -m Orchestrator.toolvault.validate` (CI gate) then `POST /toolvault/reload` after any schema/executor edit.
- Portal: Chrome-verify the real `.onclick`/handler path on a throwaway `test` operator; clean up markers from the prefs/jobs store after.
- Android: `./gradlew compileDebugKotlin`; device smoke where a notification/model round-trip is involved.
- Restart is pre-authorized: `sudo systemctl restart blackbox.service` (60-90s warm-up).
- After the final milestone: dispatch a final code-reviewer over the whole branch, then `/snapshot-dev` (operator resolved dynamically).
