# CU Multi-Desktop Sessions + Task-Manager Revival + In-View Narration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> subagent-driven-development) to implement task-by-task. TEST-FIRST per task.

**Goal:** Chat CU prompts append a new desktop when the current agent is busy;
sessions persist and are re-selectable (watch + chat re-target); the task
manager shows every agent's live chat flow with session-exact Live/Stop; the
live view gets a status chip, STOP, and a narration bubble.

**Architecture:** All decisions locked in
`docs/plans/2026-07-23-cu-multi-session-task-manager-design.md` (D1 busy→new /
idle→continue; D2 rail tap re-targets chat; D3 gemini grid mitigations
deferred). The linchpin ordering: task rows carry `session_id` FIRST (M2);
both frontends and the activity endpoint hang off it.

**Tech stack:** FastAPI (Orchestrator), vanilla-JS Portal modules, served
cu-view page (web + Android WebView), Kotlin Compose (Android), pytest.

---

## M1 — Append-new-desktop semantics (backend)

### Task 1.1: `force_new` + MRU pointer + no cross-operator reclaim
**Files:** `Orchestrator/browser/session_manager.py`;
test `Orchestrator/tests/test_cu_multi_session.py` (new)
1. RED tests: (a) `get_or_create_session(op, force_new=True)` returns a NEW
   session while the old one survives in `_sessions` and keeps status;
   (b) `_operator_sessions[op]` points at the newest (MRU);
   (c) creating a session for op B does NOT destroy op A's idle session
   (delete the reclaim loop at ~:287-291);
   (d) `_cleanup_session` of the MRU repairs the pointer to the operator's
   most-recent surviving session (or drops the key).
2. Implement: `force_new: bool = False` param skipping both lookups; MRU
   repair in `_cleanup_session` (scan `_sessions` for same-operator, newest
   `last_activity`); remove the reclaim loop.
3. `cleanup_inactive_sessions` unchanged (already per-session).
4. Run `test_cu_multi_session.py` + full `-k "cu or browser"` → green; commit.

### Task 1.2: busy→new in the three chat CU streams
**Files:** `Orchestrator/routes/chat_routes.py` (stream_computer_use ~:4074,
stream_openai_computer_use ~:4658, stream_gemini_computer_use ~:4366);
test additions in `test_cu_multi_session.py`
1. RED test (anthropic path, monkeypatched session store): busy session +
   new prompt → a SECOND session is created, `cu_session` SSE carries the
   NEW id, the busy session's queue is untouched.
2. Implement: in each stream's busy branch, replace enqueue with
   `get_or_create_session(operator, force_new=True)` → proceed as a fresh
   launch; keep enqueue ONLY when the incoming session_id explicitly matches
   the busy session (a deliberate follow-up to a running agent from its own
   thread). Gemini: same via its session manager's `force_new` twin.
3. Cap surfacing: `ensure_browser` returns/raises the allocator's message;
   streams yield `error` with "All 3 virtual desktops are in use — close one
   from the live-view rail or stop an agent." RED test: allocator raising cap
   RuntimeError → that message reaches the SSE error event.
4. Commit.

### Task 1.3: session-explicit status/stop
**Files:** `chat_routes.py` (/chat/cu-status ~:7646, /chat/cu-stop),
`test_cu_sessions_endpoint.py` additions
1. RED: `/chat/cu-status?operator=X&session_id=S` reports S (not the MRU);
   stop with session_id stops exactly S.
2. Implement via `get_session(operator, session_id)` (exists). Commit.

## M2 — session_id on the task row (linchpin)

### Task 2.1: persist + project
**Files:** `Orchestrator/browser/headless.py` (run_cu_task ~:559+582,
_run_gemini_cu_task ~:473), `Orchestrator/tasks.py` (update_task result_data),
`Orchestrator/routes/task_routes.py` (/tasks/list + /tasks/status projection
~:78-99); tests `test_cu_task_session_link.py` (new)
1. RED: after a (mocked) run_cu_task, the task row's result_data carries
   `session_id` + `view_url`, and /tasks/list projects `session_id` top-level.
2. Implement: `session.task_id = task_id` at claim; `update_task(task_id,
   result_data={**rd, "session_id": sid, "view_url": f"/cu/view/{sid}"})`
   right after session resolution (both anthropic/openai and gemini paths);
   clear `session.task_id` in `reset_task_state`. Project in both routes.
3. Commit.

## M3 — Task-manager revival

### Task 3.1: Portal in-chat agent pill lives again
**Files:** `Orchestrator/routes/chat_routes.py` (emit `browser_task` from ALL
provider use_computer branches — grep the 6 dispatch sites; add `cli_task`
sibling for claude_code_task/gemini_cli_task/codex_cli_task);
`Portal/modules/chat-send.js` (SSE switch case ~:1204 beside image_task →
`taskManager.addTask(task_id, 'use_computer', prompt)`; `cli_task` case);
`Portal/modules/task-manager.js` (Live+Stop buttons on showAgentPlaceholder,
reuse task-ui.js `canShowLiveView` + ui-setup.js createTaskItem pattern
~:617-653, Live passes `opts.sessionId` from the task row)
1. Backend RED test: each provider branch emits browser_task (parametrized
   over the dispatch table; source-level assertion acceptable + one
   behavioral SSE test on the anthropic stream).
2. Frontend: manual verification checklist (Portal has no JS test rig):
   launch CU from chat → in-chat pill appears with reasoning window, Live
   opens the task's OWN session (exact-match via cu-viewer-route.js:49-58),
   Stop cancels. Bump `?v=genui` in index.html.
3. CLI stdout tail → `append_task_reasoning` in `cli_agent/headless.py`'s
   ~2s flush (full CLI chat flow everywhere). RED unit on the flush helper.
4. Commit.

### Task 3.2: session-exact Live everywhere + screenshot fallback
**Files:** `Portal/modules/ui-setup.js` (openLiveView ~:816 passes
`sessionId`), `Orchestrator/routes/browser_routes.py`
(/browser/screenshot/live gains `session_id` param →
`capture_screenshot_display(h.display_num, native=False)`);
test `test_cu_view_routes.py` additions
1. RED: /browser/screenshot/live?session_id=S captures S's display (fake
   allocator handle; assert capture called with display_num + native=False).
2. Commit.

### Task 3.3: Android pill → the agent's own desktop
**Files (Android):** `data/model/ChatMessage.kt` (TaskStatus.sessionId +
effectiveSessionId()), `ChatViewModel.kt` (:741-768 discovery-loop builder —
the SOLE TaskPanel feed), `NativeMainActivity.kt` (:878-883 onLiveView →
`Routes.CU_LIVE_VIEW/{sid}` when sessionId present and device is blackbox;
keep `?liveDevice=` for remote devices / native opt-in),
`ui/computeruse/CuScreen.kt` (:713-716 — let a virtual-session pill arrival
fall through to the auto-route instead of unconditional early-return)
1. Gate: `./gradlew :app:testDebugUnitTest --offline` (~35s).
2. Fold-validate: pill Live lands on the running agent's Splashtop view.
3. Commit + build APK.

## M4 — Live-view status + STOP + narration bubble

### Task 4.1: producers
**Files:** `Orchestrator/browser/driver_anthropic.py`,
`Orchestrator/gemini_cu/agent_loop.py`, `Orchestrator/openai_cu/agent_loop.py`,
`Orchestrator/browser/session_manager.py` + `gemini_cu/session_manager.py`
(bounded `reasoning_tail` field, REASONING cap discipline);
tests `test_cu_activity_feed.py` (new)
1. RED: after a scripted driver run (reuse _ScriptedHTTPX), the session's
   `reasoning_tail` contains the thinking text + "→ action(...)" lines,
   bounded to N chars; gemini/openai loops append `cu_log` entries.
2. Implement: fold narration onto the session at the driver emit sites
   (thinking/content/cu_action), mirroring _drain_and_fold's format.

### Task 4.2: activity + stop endpoints
**Files:** `Orchestrator/routes/browser_routes.py`; session-manager
`get_session_by_id` helpers (both managers); tests in
`test_cu_activity_feed.py`
1. RED: GET `/cu/session/{sid}/activity` → `{status, step, total,
   latest_action, reasoning_tail, task_id, operator}` for browser AND gemini
   sessions; 404 unknown. POST `/cu/session/{sid}/stop` → cancels the task
   when task-launched (tasks.cancel_task), else request_stop(); assert both
   dispatch paths.
2. Commit.

### Task 4.3: cu-view page chrome
**Files:** `Portal/cu-view/cu-view.js`, `index.html`, `cu-view.css`
1. Status dot + STOP button in `#cuvTopbar` (STOP hidden for "main");
   collapsible narration bubble bottom-left (collapsed: latest line;
   expanded: scrolling transcript, max-height 40vh, `overflow-y`), fed by a
   2.5s poll of `/cu/session/{sid}/activity` piggybacked on the existing
   switcher poll loop; honors the ?bi= inset. Bump `?v=fit4`.
2. Verify on desktop browser + Fold (served page = both).

### Task 4.4: rail tap re-targets chat (D2)
**Files:** `Portal/cu-view/cu-view.js` (rail tap handler → postMessage/
localStorage `cu_session_id` write-back), `Portal/modules/chat-send.js`
(read current session pointer before CU sends — it already stores one at
cu_session events; unify), Android `CuLiveViewScreen`/`ChatViewModel`
(WebView message or on-return sync so the next prompt targets the selected
session)
1. Manual checklist both frontends; document the pointer contract in the
   design doc.
2. Commit.

## Verification (end-to-end)
1. Chat: launch CU task A (long-running); send a second CU prompt → NEW
   desktop B appears in the rail; A keeps working (watch its narration).
2. Rail: tap A → watching A AND next chat prompt drives A.
3. Task Monitor + in-chat pill: both show live reasoning for A and B; Live
   opens the RIGHT desktop; Stop kills only its agent.
4. Live view: status chip flips working→idle on completion; STOP works;
   narration bubble streams the model's thinking.
5. Cap: launch a 4th agent → friendly "3 desktops in use" chat error.
6. `Scripts/cu-verify.sh` unit gate green; full pytest green;
   `./gradlew :app:testDebugUnitTest --offline` green.

## Non-goals
- Gemini grid/chess mitigations (D3 deferred).
- Headless anthropic/openai per-task session isolation (phase 2).
- MAX_VIRTUAL_SESSIONS > 3.
