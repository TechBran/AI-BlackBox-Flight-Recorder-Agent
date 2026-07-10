# Multi-Backend CU Routing + CLI-Agents-as-Tools + Task-Manager Live Streaming — Plan

> **For Claude:** REQUIRED SUB-SKILL: superpowers:executing-plans / subagent-driven-development. Companion to the OpenAI Responses migration plan (2026-07-09-openai-responses-migration.md); sequencing note at the end.

**Goal:** (1) `use_computer` routes to ANY of the 3 CU backends (Anthropic/Google/OpenAI) from ANY model incl. all voice agents, selected by **stable model-class name**; (2) the official CLI agents (claude/gemini/codex) become callable ToolVault tools that run headless as **concurrent** background tasks, using their OWN OAuth sign-ins (subscription routing, zero token handling by the BlackBox); (3) the task-manager UI streams a live text preview per task + a CU live-view button. Default behaviors unchanged; frontend SSE/task contracts extended additively.

**Architecture note:** all three capabilities converge on the existing background-task system (`TaskType`, `process_*` workers, `/tasks/*` polls, `/chat/save` auto-snapshot on completion) and the task-manager pills — which is why they're one plan. Nothing needs a new push channel in v1; everything rides the existing 2s/3s polls.

---

## Verification status (2026-07-09)

Adversarially verified against the live code: **50 claims confirmed**, 27 alleged defects raised,
13 killed by 3-lens skeptics (correctness / materiality / redundancy), **12 survived**, +2 found by a
completeness critic. Every surviving defect below carries ≥2 independent confirming votes.
Findings are folded into the milestones. Raw run: workflow `wf_b3a557cc-332`.

**Headline:** M1.1 was mis-scoped as "invert one fail-fast." It is not. See M1.1 below.

---

## DECISIONS — ANSWERED BY BRANDON 2026-07-09

**D1 — `mcp` group on the YOLO CLI-agent tools = remote RCE over Funnel. → DECIDED: OMIT `mcp`.**
`get_mcp_tools()` (tool_registry.py:304) feeds `MCP/blackbox_mcp_server.py`, exposed over Tailscale
**Funnel = public HTTPS ingress** (not the private tailnet). Putting `claude_code_task` /
`gemini_cli_task` / `codex_cli_task` (spawned with `--dangerously-skip-permissions` /
`--approval-mode yolo` / `--sandbox danger-full-access`) in the `mcp` group would grant arbitrary command
execution under the operator's shell to any authenticated remote MCP client — a leaked or over-scoped
bearer token becomes RCE. The tailnet-perimeter posture does not cover the Funnel surface.
**DECISION: groups = `chat` + `realtime` + `gemini_live` + `grok_live`. NO `mcp`.**
This is a standing invariant for these three tools — do not add `mcp` later without re-deciding.

**D2 — CLI-agent tasks have no kill switch. → DECIDED: SHIP CANCEL IN GROUP 2.**
There is no `/tasks/{id}/cancel` endpoint anywhere (only `embeddings_routes.py:423 /migrate/cancel` and
`cli_agent_routes.py:153 kill_session`, a different substrate). A YOLO CLI agent started by any model —
including a voice agent — cannot be stopped until its own timeout.
**DECISION: `/tasks/{id}/cancel` + worker cancellation (process-group kill) + a STOP button on CLI-agent
pills ship in group 2, in the SAME milestone as the tools.** Not deferred to group 3. The tools do not
land without the kill switch.

---

## AMENDMENT — model selection by stable CLASS name (Brandon, 2026-07-09)

**Principle: the schema carries bare model CLASS names; the version resolves to the NEWEST at
execution time** — exactly how `claude --model opus` behaves in the terminal. Model ids are provider
*facts* (live discovery); classes are our *taxonomy* (stable across releases). Never put an id in a
schema. [[feedback_provider_api_sot]] [[feedback_brandon_ga_vs_preview]]

**Evidence that version-pinning rots — live, today:**
```python
# Orchestrator/config.py:201-205
CU_MODEL_FILTERS = {
    "anthropic": r"claude-(opus|sonnet|fable|mythos)-([4-9]|\d{2,})",   # class + open version tail → Opus 4.8 matches, zero edits
    "google":    r"gemini-.*computer-use",
    "openai":    r"(computer-use-preview|gpt-5\.5($|-\d))",             # VERSION-PINNED → gpt-5.6 does NOT match
}
```
The OpenAI filter froze at 5.5, so the model Brandon runs daily cannot be selected for CU. The
in-repo comment states the mechanism: *"gpt-5.5 (+ dated snapshots) carries the built-in `computer`
tool"* — the capability lives in the **tool**, not the version. The Anthropic line is the pattern.

**Filters become CAPABILITY GATES, not version pins.** The filter answers *"can this vendor's class do
CU at all?"*; the live catalog answers *"which concrete id is newest in that class?"*
Confirmed exclusions to preserve: **haiku has no CU support** (config.py:195 comment); gemini `-pro` is
excluded (undocumented for CU); google CU is its own `*-computer-use` line, not flash/pro.

**A. `use_computer.model` — CU-capable class names.**
`opus` (DEFAULT) | `sonnet` | `fable` | `gemini` | `gpt` | `<concrete id>` | omitted.
Backend derives from the resolved class/id. Google has one CU model today, so `gemini` collapses to it.
`gpt` → newest GA `gpt-*`; CU-ness comes from the built-in computer tool (the OpenAI driver is already
on `/v1/responses` with it — verified).

**B. `<cli>_task.model` — vendor class names, passed straight to the CLI's own `--model` flag.**
`claude_code_task.model`: fable | opus | sonnet | haiku · `gemini_cli_task.model`: pro | flash ·
`codex_cli_task.model`: gpt | gpt-sol. **The CLI is the resolver** — we pass the class, the CLI picks
the newest version, and we never name a version (which is also what keeps subscription routing intact).
No catalog lookup on this path. Confirm exact flag spelling per CLI via `--help` during M2.1.

**C. Shared resolver (CU path only, since we call provider APIs directly).**
`resolve_model_class(class_or_id, live_catalog) -> concrete_id`: exact id wins if it passes the vendor
capability gate; alias → newest production-track id containing the class token (a literal `-preview-`
disqualifies); empty → configured default; **unresolvable → raise a model-readable error that NAMES the
classes currently available.** Never silently default.
*Constraint:* the MCP server runs in a LEAN VENV — an `x-source` dynamic enum could only ever serve
stale static-fallback ids. That is why the class set is hardcoded in the schema and the id is resolved
in the executor, where the live catalog is reachable.

**D. Retry contract.** Tool failures return structured, not prose:
`{success: false, retryable: bool, reason: str, available: [classes...]}` so the caller can re-issue
against a different class/backend. (Precedent: the custom-provider "resend" error the model acts on.)

**E. Concurrency.** CLI-agent tasks MUST run concurrently, each independently addressable by `task_id`
and pollable via `get_task_status`. CU does **not** inherit this posture (see M1.1 display lock).

---

## Milestone group 1 — Multi-backend CU routing (do first)

### M1.1 — `use_computer` accepts a model class, routes to the derived backend.

**Schema.** Add optional `model` string param documenting the closed class set from Amendment A;
rewrite the description provider-agnostic (drop "powered by Claude Opus 4.6", `schema.json:3`).
Plain string, NOT an `x-source` enum (lean-venv constraint above). Validate + reload.

**Executor.** Thread `model` into `result_data` (`ToolVault/tools/use_computer/executor.py:17` currently
builds `{"device_id": device_id}` and drops everything else). Resolve via `resolve_cu_model` (hoisted to
`browser/dispatch.py` in M1-T1) — **confirmed safe**: `scheduler/executor.py:178-205` was a pure function
of the model string + `CU_MODEL_FILTERS`/`CU_MODEL_DEFAULT`, consults no job config, so the hoist preserves
scheduler parity (re-provision the module `logger` at line 201).

**Legacy inline handlers.** `chat_routes.py:1037` and `:3544` hardcode `result_data={"url": url} if url else {}`,
bypassing the ToolVault executor — they drop **both `device_id` AND `model`**. Thread both, or route them
through `executor.execute` like the other five call sites (`:1586, :2706, :5447, :6008, :6587`).

**Runner — THE REAL WORK (this is not a one-line guard inversion).**

1. **Move the API-key gate below backend resolution.** `headless.py:63-64` returns
   `"ANTHROPIC_API_KEY not set"` *before* `headless.py:66 resolve_backend(model)` ever runs. Resolve the
   backend FIRST, then require `ANTHROPIC_API_KEY` only for `anthropic`, `GOOGLE_API_KEY` for `google`,
   `OPENAI_API_KEY` for `openai`. Without this, multi-backend CU is dead on any non-Anthropic-keyed box
   (fails the fresh-box portability gate).
2. **Three session shapes, not one.** `run_cu_task` builds a `ComputerUseSession` via
   `browser/session_manager.get_or_create_session` (`headless.py:79`), which acquires the **single local
   X display lock**.
   - **OpenAI: compatible.** `openai_cu/agent_loop.py:300-301` documents that it takes the browser
     `ComputerUseSession`. Reuse as-is.
   - **Gemini: NOT compatible.** `run_gemini_cu_loop` requires a `GeminiCUSession`
     (`gemini_cu/agent_loop.py:398`) from a different factory with a different signature
     (`gemini_cu/session_manager.py:102 get_or_create_session(operator, device_id, environment, session_id=None)`)
     and reads Gemini-only state (`session.environment`, genai-typed history). Build it explicitly, port
     the **device→environment resolution** from `chat_routes.py:4342-4370`, and **skip the Anthropic
     single-display lock** for this backend.
> **Execution note (2026-07-09):** steps 3 and 4 below were originally scoped as a separate follow-up task.
> They were merged back into this one during execution. A dispatch that drains the non-Anthropic loops but
> reads the wrong token keys and mishandles step-exhaustion is a *broken* drain: it would land a state where
> every Gemini/OpenAI CU task silently records `tokens {0,0}` and reports FAILED-with-empty-result on the
> iteration cap. On a box that serves production from the working tree, that is not a shippable intermediate.
> "Fold into the pinned contract" is not separable from "make the fold correct."

3. **Normalize the usage/token keys.** The fold reads `data.get("prompt_tokens")` / `("completion_tokens")`
   (`headless.py:245-246`, the Anthropic driver's names at `driver_anthropic.py:179-182`), but Gemini
   (`gemini_cu/agent_loop.py:641`) and OpenAI (`openai_cu/agent_loop.py:572`) both yield
   `{"type":"usage","data":{"input":X,"output":Y}}`. Unfixed, every non-Anthropic CU task silently records
   `tokens {input:0, output:0}` while still passing the Anthropic-only golden test. Map both key sets in
   the fold. **Add a contract test for the Gemini and OpenAI token fold.**
4. **Synthesize `done` on step exhaustion.** `run_anthropic_cu_loop` always emits a final `done`
   (`driver_anthropic.py:478-479, 491`). Gemini emits `done` only when a turn has no function calls
   (`gemini_cu/agent_loop.py:559-562`); on `MAX_ITERATIONS` exhaustion it falls through to `yield usage`
   (line 641) with **no done**. OpenAI's for-else exhaustion path (`openai_cu/agent_loop.py:566-572`) is the
   same. `run_cu_task` requires `done_seen` for success (`headless.py:285-287`), so an iteration-capped
   Gemini/OpenAI task is reported **failed with an empty result** despite real work. Synthesize a final
   result from accumulated content (as `_gemini_cu_agent_loop` does at `chat_routes.py:4514-4520`). Test it.
5. **Preserve the pinned result contract** `{success, result_text, screenshots, final_screenshot, steps,
   tokens{input,output}}` (`headless.py:285-292`, pinned by `test_cu_golden_browser_run.py:240-248`).
   **Invert** `test_cu_headless_runner.py:138-146 test_non_anthropic_backend_rejected`. TDD throughout.

### M1.2 — Voice agents can drive + poll CU. *(Confirmed a genuine one-liner + restart.)*

- Add `realtime`/`gemini_live`/`grok_live` to `ToolVault/tools/get_task_status/schema.json` groups.
  `use_computer` already has them (`schema.json:5-12`) and already executes in all three voice loops
  (`realtime_routes.py:1031`, `gemini_live_routes.py:1248`, `grok_live_routes.py:963`).
- **No new executor branch needed:** each voice loop has a catch-all routing any tool through
  `BlackBoxToolExecutor.execute` (`realtime_routes.py:1043-1050` + twins). Both tools work without a chat
  SSE session (`use_computer` only creates a task; `get_task_status` is a plain HTTP poll).
- Both schemas serialize safely to the flat `openai_realtime` and `_strip_for_gemini` shapes (plain string
  params only, no enum/x-source/nested constructs). `schema_spec.py:12 KNOWN_GROUPS` already accepts all
  three group names, so `validate` passes. No test pins voice-group membership.
- **SERVICE RESTART REQUIRED** (not just `POST /toolvault/reload`): `REALTIME_TOOLS` /`GEMINI_LIVE_TOOLS` /
  `GROK_LIVE_TOOLS` are import-time module constants (`realtime_routes.py:90`, `gemini_live_routes.py:97`,
  `grok_live_routes.py:87`); `tool_registry.py:67-79 reset_cache` says so in its own docstring.
- Add a COMPUTER CONTROL paragraph to the voice system prompts (start async → announce → poll
  `get_task_status`; "CALL the tool, don't narrate intent" — the SNAP-3675 stall directive). These are inline
  f-strings per route (`realtime_routes.py:411`, `gemini_live_routes.py:332`, `grok_live_routes.py:355`) —
  **not** `behavioral_core` (editing that would leak CU guidance into its 6 non-voice injection sites).
  **Each route builds `system_instructions` in TWO branches (custom_role + default) — edit both.**
- Live-validate a voice CU task end-to-end.

## Milestone group 2 — CLI agents as ToolVault tools

*(D1 + D2 answered: no `mcp` group; cancel ships here.)*

### M2.0 — REAL task cancellation (D2). **Ships before/with the tools, not after.**

> **Correction to the verification pass:** an earlier finding claimed "no cancel machinery exists
> anywhere." **Wrong — `POST /tasks/cancel-all` exists** (`task_routes.py:104-113`) and is wired to the
> Portal "Cancel Tasks" button (`ui-setup.js:967`, `index.html:692`) and Android
> (`SettingsSheet.kt:938` → `SettingsViewModel.kt:301` → `TaskRepository.kt:64`). The finding's
> *conclusion* stands and is in fact worse than stated:

**`/tasks/cancel-all` is COSMETIC.** It only flips `PENDING`/`PROCESSING` DB rows to `FAILED`
("Manually cancelled by operator"). It never signals the worker and never touches a process.
For a stuck image-gen poll that is harmless — which is what it was written for. For a
`claude --dangerously-skip-permissions` agent it is **actively dangerous**: the pill vanishes from the
UI while the process keeps running with full filesystem access and nothing on screen to indicate it.
**This button must gain real teeth before any CLI-agent tool exists.**

**Brandon's design (2026-07-09): per-pill cancel is primary; cancel-all is built on the same path.**

1. **`TaskStatus.CANCELLED = "cancelled"`** (`models.py:194-198` — currently only
   pending/processing/completed/failed). DB-safe: the column is `status TEXT NOT NULL`
   (`models.py:256`) with **no CHECK constraint**. *Audit every status reader* so an unknown status
   doesn't render as "processing forever": `ui-setup.js renderTaskItem`, `task-manager.js`
   `handleTaskComplete` (`:246-268`, which has **no final else**), Android `TaskPanel.kt` +
   `ChatViewModel.kt`. Distinguishing `cancelled` from `failed` is what stops a cancelled agent from
   looking like a crash.
2. **Cancel registry: `task_id -> cancel handle`**, three handle kinds:
   - **CLI agent:** spawn with `start_new_session=True` (own process group); cancel =
     `os.killpg(os.getpgid(pid), SIGTERM)`, then `SIGKILL` after a grace period. Process-group kill is
     what makes the YOLO agent's *children* die too, not just the CLI.
   - **Computer use:** delegate to the existing **`POST /chat/cu-stop`** (`chat_routes.py:7491`).
     It resolves through per-operator session lookup — and `test_cu_catalog.py:269` **pins** that
     cu-status/cu-stop must go through session lookup and must NOT string-sniff model ids. Do not
     reinvent this; route into it.
   - **Everything else** (image/video/tts/music): cooperative cancel flag only, checked by the
     provider poll loop. No process to kill.
3. **`POST /tasks/{task_id}/cancel`** — new. Looks up the handle, cancels it, marks `CANCELLED`.
   Idempotent; a task with no live handle (a genuinely stuck row) is simply marked `CANCELLED`.
4. **Rework `POST /tasks/cancel-all`** to loop the per-task path rather than flipping rows.
   Keeps its stuck-task-reaper behavior for handle-less rows. **Behavior change:** it now marks
   `CANCELLED`, not `FAILED` — audit readers per (1). This is the button Brandon asked to repurpose;
   after this it means what its label says.
5. **Mint hygiene: a cancelled task must NOT auto-snapshot.** `process_browser_use`'s completion path
   mints to `/chat/save`; the cancel path must skip it, so a killed YOLO agent's partial stdout never
   enters the immutable ledger. (Precedent: the Android error-mint bug.)
6. **Per-pill STOP button — all 3 surfaces** ([[feedback_frontend_three_surfaces]]): the top-bar Task
   Monitor (`ui-setup.js renderTaskItem`), the in-chat placeholder pills (`task-manager.js`), and
   Android `TaskPanel.kt`. Shown on `PENDING`/`PROCESSING` pills, next to the streaming
   `progress_text` line from M3.1. **CU pills additionally get the "Live" button (M3.2).**

- **Gate:** no CLI-agent tool is registered on any surface until per-task cancel is proven end-to-end
  (spawn a sleep-loop agent, cancel it, assert the process group is gone and status is `cancelled`).

### M2.1 — Headless runner + task type.

- `TaskType.CLI_AGENT` (`models.py`); new `Orchestrator/cli_agent/headless.py::run_cli_agent(provider,
  prompt, model_class, cwd, permission_mode, task_id)` with per-provider argv builders.
- **Proven precedent:** `agent_routes.py:108-117` already runs claude headless one-shot as
  `[claude, "-p", "--output-format", "stream-json", "--verbose", ...]` (+ `--dangerously-skip-permissions`
  at :117), non-blocking stdout pipe at :156-157. **`--verbose` is REQUIRED for stream-json to emit.**
  Confirm every flag (incl. `--model`) per CLI via `--help` before writing argv.
- **Env strip is per-provider and does NOT exist yet.** The plan previously mis-cited this:
  `_augmented_spawn_path` (`cli_agent/session_manager.py:68`) only builds PATH and strips nothing;
  `_ENV_DENYLIST_FOR_PANES` lives in `cli_agent/zellij_client.py:72` and equals
  `frozenset({"ANTHROPIC_API_KEY"})` — **claude-only**. `GOOGLE_API_KEY`/`GEMINI_API_KEY` are live in the
  process env and unstripped, so `gemini_cli_task` would silently **bill the API key instead of routing
  through OAuth** — the opposite of this plan's goal. Build a per-provider denylist: claude →
  `ANTHROPIC_API_KEY`; gemini → `GOOGLE_API_KEY`, `GEMINI_API_KEY` (decide on `GOOGLE_APPLICATION_CREDENTIALS`,
  which the zellij denylist intentionally keeps for Vertex); codex → per `~/.codex/auth.json` mode.
- Read stdout JSONL; append tail to `result_data` every ~2s (poll-visible); completion via process exit +
  final event; timeout + **process-group kill** (D2).
- `process_cli_agent` worker mirroring `process_browser_use`, incl. `/chat/save` auto-snapshot parity.
  **Register it in `process_task`'s if/elif dispatch (`tasks.py:341-369`) — a single switch, easily missed.**
- **Concurrency (Brandon, hard requirement).** `tasks.py:314` is `ThreadPoolExecutor(max_workers=MAX_CONCURRENT=4)`
  (`:300`) with **no per-type cap**. Add a per-type sub-cap so long CLI runs cannot starve image/TTS/video,
  with the CLI-agent slice **> 1** ("concurrently at the same exact time").

### M2.2 — Per-agent tools (provider-explicit naming).

- `claude_code_task`, `gemini_cli_task`, `codex_cli_task` — thin schema+executor each,
  `create_task(TaskType.CLI_AGENT, ..., result_data={provider, model_class, cwd, permission_mode})`.
  `model` param per Amendment B, passed to the CLI's own `--model` flag.
- **Groups: `chat` + `realtime` + `gemini_live` + `grok_live`. NO `mcp` (D1, decided).**
- **Auth gating is NOT x-availability.** `toolvault/availability.py` gates only on env-var presence against a
  **closed `FEATURES` registry containing only `web_search` and `image`** (`:17-48`); `enabled_providers` does
  `spec = FEATURES[feature]` (`:97`), so an unknown `feature="cli_agent"` raises **KeyError → `filter_available`
  crashes → MCP `list_tools` returns ZERO tools and the live injector breaks for every group containing a CLI
  tool.** The CI validator would not catch it. CLI OAuth is marker-file based (`_AUTH_MARKERS` in
  `onboarding_routes.py`, not lean-venv importable), which `requires_env` cannot express.
  **Instead:** leave the tools ungated in the schema and have each **executor fail-fast** with a clear
  "run `codex login` / authenticate the claude CLI" error (checking `GET /onboarding/cli-agent/status`).
  Also add a `validate.py` check that any `x-availability.feature` is a known `FEATURES` key.
- **Permission posture (Brandon's decision): YOLO fully open** — `claude --dangerously-skip-permissions`,
  `gemini --approval-mode yolo`, `codex --sandbox danger-full-access --skip-git-repo-check`. Any model that can
  call these (incl. voice) runs arbitrary commands under the operator's shell. Accepted for the tailnet surface;
  **see D1 for why that acceptance does not extend to the Funnel-exposed `mcp` group.**
- **Codex auth:** `~/.codex/auth.json` is in `auth_mode=apikey` on this box → codex delegation bills the API key
  until Brandon runs `codex login` (ChatGPT mode). **Marker presence ≠ subscription** (the marker exists in
  apikey mode), so the executor must log/report which mode is active. Claude is already OAuth-subscription.
- **Mint hygiene:** `process_browser_use` mints a *bounded, model-summarized* string
  (`result_text[:1000]`, `prompt[:300]`). A CLI agent's raw stdout must be bounded the same way before it
  reaches the immutable ledger. (Precedent: the Android error-mint bug.)

## Milestone group 3 — Task-manager live streaming + CU live view

### M3.1 — `progress_text` on the task record (additive).

- `models.py` Task gains `progress_text: Optional[str]` + ALTER-TABLE guard + column maps;
  `append_task_progress(task_id, line)` helper. Producers: the CU runner stops discarding its per-step events;
  video poll count; CLI-agent output tail. Expose additively on `/tasks/list` + `/tasks/status`.
- **ALSO expose `device_id`** on both payloads, read out of `result_data['device_id']` (default `"blackbox"`,
  as `tasks.py:1216` does). **Do NOT add it as a top-level Task column** — it has never been one
  (`models.py:216-230`). Required by M3.2 (see below).

### M3.2 — Portal pills stream it + CU live-view button.

- `ui-setup.js renderTaskItem`: a `.task-live-line` bound to `progress_text` (existing 2s poll refreshes it) +
  CU/browser_use/gemini_cu type icons.
- **"Live" button:** `/tasks/list` currently exposes only
  `task_id/task_type/status/progress/created_at/updated_at/result_url/operator/prompt`
  (`task_routes.py:69-79`) — **no `device_id`**. `setCUDeviceId(task.device_id)` would pass `undefined` and
  `cu-interact` would poll the `"blackbox"` fallback, showing the wrong device (or nothing) for a remote-device
  CU task. Consume the field M3.1 adds, and gate the button on it.
- **STOP button per pill** already shipped in M2.0 (uniform `/tasks/{id}/cancel` for every task type,
  including CU — which routes internally to `/chat/cu-stop`). M3.2 only places it beside the new
  `progress_text` live line; it does not re-implement cancellation.
- **Second Portal surface, previously omitted:** `Portal/modules/task-manager.js` (the in-chat placeholder pills
  — where a chat-initiated `use_computer`/CLI-agent task actually renders) polls `/tasks/status/{id}` (`:221`)
  and `updateTaskProgress` (`:286-290`) sets **only** `${status.progress}%`. Its `handleTaskComplete` switch
  (`:246-268`) has branches for image/video/audio/tts/browser_use/use_computer, **no CLI_AGENT branch and no
  final else** → a completed `claude_code_task` renders nothing. Read `progress_text` there and add the
  CLI_AGENT branch. (The repo's known "dispatch duplicated in several places" hazard.)
- CLI-agent pills stream the same `progress_text` + the M2.0 STOP button + an "open terminal" escape hatch.
  `?v` bump. NOT the SSE drawer (single-consumer queue — poll-based `cu-interact` is the right tool).
- The existing **"Cancel Tasks"** button (`index.html:692` / Android `SettingsSheet.kt:938`) keeps its
  place as the panic button, now backed by the real cancel-all from M2.0.

### M3.3 — Android parity.

- `progressText` on the TaskStatus model **is not free.** `TaskPanel` is fed by `chatViewModel.activeTasks`
  (`NativeMainActivity.kt:685-686`), whose StateFlow comes from a **manual field-by-field `TaskStatus(...)`
  construction at `ChatViewModel.kt:721-728`** reading only `task_id/task_type/status/progress/operator/result_url`.
  The kotlinx auto-parse path (`TaskRepository.kt:43`) *would* pick a new `@SerialName` field up automatically —
  but it does not feed TaskPanel. **Add `progress_text` to the manual construction at `ChatViewModel.kt:721-728`**
  (and the `TaskRepository.kt:26-33` fallback for parity), then add the Text row in `TaskPanel.kt`.
- CU live view via the WebView wrapper of `cu-interact` (cheap); native Compose live-view screen is a later nicety.

---

## Sequencing

1. **CU routing group 1 FIRST** — high daily value (voice computer control), no dependency on the migration.
   Verified low-collision: the OpenAI CU driver is **already** on `/v1/responses` with the built-in computer tool.
2. **Responses migration M1-M6** — the big one, already adversarially verified, awaiting go.
3. **CLI-agent tools (group 2)** — after D1/D2 are answered.
4. **Task-manager streaming (group 3)** — last; the observability layer that makes 1-3 shine.

Each group is independently shippable + Fold-validatable.
