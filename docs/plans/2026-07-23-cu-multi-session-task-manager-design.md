# CU Next Level — Multi-Desktop Sessions, Task-Manager Revival, In-View Narration

Investigation: 7-agent recon 2026-07-23 (wf_fef458a9). Precedes the implementation
plan; decisions marked ⚑ need Brandon's call.

## Context — what the recon established

**Chess forensics (MS02, session 033dbc34, 18:11–18:15).** The pipeline is
exonerated: every de-normalization was an exact 0-999→1440×900 map, and the
model successfully clicked taskbar → lichess → game setup → played 1...c6.
The "trouble clicking" is the documented Gemini-CU-preview flipped-board
weakness with a now-precise mechanism: playing Black, the model wrote an
ARITHMETIC file/rank map assuming a square board in normalized space
(62 units/file), but 0-999 space is anisotropic on 16:10 — the real board is
~34.5 units/file — so its "d7" landed 64px right of the board's edge, in dead
space, five times in a row. The parallel claude-opus-4-8 game (46 moves,
18:02–18:31) clicked flawlessly and lost on chess merit only.
The run then DIED at step 22 on a Gemini 400 (131,072-token input cap):
fossil injection was 311,810 chars + 21 unbudgeted screenshots.
**→ Token blowup FIXED and shipped (da1de59a): fossil cap 20K chars + in-loop
keep-last-3 screenshot budget.** Chess mitigations remain (M5 below).

**Task manager "taken out" — the regression trail.** The Task Monitor still
exists (floating self-hiding badge, `Portal/modules/ui-setup.js`) with
progress line, expandable reasoning transcript, Stop, and Live. What actually
regressed / never worked:
- The IN-CHAT agent pill (`task-manager.js showAgentPlaceholder`, full chat
  flow of the model) is DEAD CODE: `browser_task` SSE is emitted from only 1
  of 6 provider branches (`chat_routes.py:3576`) and no Portal consumer
  exists; CLI-agent dispatches emit no event at all.
- The Live button passes `deviceId` only → streams the FIRST streamable
  session from `/cu/sessions` — possibly the wrong agent's desktop (no
  session_id on the task row anywhere).
- Android pill Live (a3233c8b) navigates to the NATIVE CuScreen
  (`computer_use?liveDevice=`) which screenshot-polls the REAL desktop —
  wrong surface for the virtual-by-default world; `CuScreen.kt:716`
  explicitly skips the auto-route for pill arrivals.
- The `/browser/screenshot/live` fallback captures ACTIVE_DISPLAY (real
  desktop), never a session Xvfb.

**Session model today.** Hard 1:1 operator→session (`_operator_sessions`);
new prompt on a busy session gets QUEUED into it; fresh creates DESTROY other
operators' idle sessions. The display layer below is already multi-session
(allocator keyed by session_id, 3 slots, `/cu/sessions` lists all, cu-view
switcher rail renders them).

**No narration reaches the live view.** Three feeds exist (chat SSE → chat
bubble only; task rows' progress_text/reasoning_text → /tasks poll; session
status → /chat/cu-status) — none joinable from the cu-view page today
(task rows lack session_id; chat-launched runs accumulate no server-side
transcript).

## Design

### M1 — Append-new-desktop session semantics
- `get_or_create_session(force_new=True)`; `_operator_sessions` becomes an
  MRU "current" pointer (repaired on cleanup, not load-bearing).
- DELETE the cross-operator idle reclaim (multi-desktop world).
- Chat CU launch (all 3 providers): current session BUSY + new prompt →
  create NEW session/desktop + emit `cu_session` with the new id (Portal
  already repoints on that event) — replaces silent queueing. Idle session →
  reuse (preserves "go to X / now click Y" continuation).
- Cap surfacing: `ensure_browser` propagates the allocator's message so chat
  says "All 3 virtual desktops are in use — close one from the rail" instead
  of "Failed to start browser session".
- E-stop/status: `/chat/cu-status` + stop gain explicit session_id params.
- ✅ D1 (Brandon 2026-07-23): busy→new desktop, idle→continue the current
  session (preserves "go to X / now click Y").
- ✅ D2 (Brandon 2026-07-23): rail tap = watch AND re-target the chat
  composer to that session — selecting truly "goes back" to the agent.

### M2 — session_id on the task row (the linchpin)
- Stamp `session.task_id` at launch and persist `session_id` (+ view_url)
  onto the task row; project in `/tasks/list` + `/tasks/status`.
  Unlocks BOTH frontends' Live buttons targeting the task's OWN desktop
  (`cu-viewer-route.js` exact-match branch already exists; Android navigates
  `cu_live_view/{sid}`).

### M3 — Task-manager revival (both frontends)
- Portal: `browser_task` case in chat-send.js SSE switch (revives the
  in-chat agent pill + its reasoning window); emit `browser_task` from ALL
  provider branches; sibling `cli_task` event for CLI-agent dispatches; CLI
  stdout tail → `append_task_reasoning` (full CLI chat flow in every panel);
  Live+Stop on the in-chat pill.
- Android: `TaskStatus.sessionId`; pill Live → `cu_live_view/{sid}` (keep
  `?liveDevice=` only for remote devices / native opt-in).
- `/browser/screenshot/live` gains session awareness (fallback shows the
  agent's display, not the real desktop).

### M4 — Live-view status chip + STOP + narration bubble
- New `GET /cu/session/{sid}/activity` → `{status, step, total,
  latest_action, reasoning_tail, task_id, operator}` (resolves across both
  session managers). Producers: task_id stamp (M2); chat-launched runs mirror
  thinking/content/action lines into a bounded `session.reasoning_tail`;
  gemini/openai loops start writing `cu_log`.
- New `POST /cu/session/{sid}/stop` → task cancel when task-launched, else
  `request_stop()` — one button, correct for both launch paths.
- cu-view page (served → ships to web + Android WebView at once): status dot
  + STOP in the topbar; collapsible narration bubble (collapsed = one line of
  latest action/thought; expanded = scrolling transcript overlay) — polls
  activity at 2-3s alongside the existing 4s switcher poll. No WebSocket.

### M5 — Gemini chess/grid mitigations (small)
- Repeated-coordinate loop breaker: same click_at within a few px 3× with
  unchanged screens → inject corrective observation ("re-derive coordinates
  visually; do not reuse computed maps").
- System-prompt rule: normalized units are anisotropic (per-axis scale);
  never build arithmetic grid maps; re-ground every click visually.
- ✅ D3 (Brandon 2026-07-23): DEFERRED — ship the session/task-manager/
  narration work first; revisit after a stronger Google CU model ships.

## Non-goals
- Headless task path isolation for anthropic/openai (gemini-style per-task
  sessions) — phase 2.
- Raising MAX_VIRTUAL_SESSIONS above 3 — revisit after cap-hit telemetry.
