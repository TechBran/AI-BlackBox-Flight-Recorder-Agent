# Android CLI Terminal Production Uplift — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development in-session) to implement this plan task-by-task.

**Goal:** Fresh-spawn-per-tap terminal sessions with a grok agent, per-agent YOLO launch buttons, and production-grade layout/rotation/input/scrollback behavior on the Android MVP.

**Architecture:** Server (FastAPI `cli_agent_routes.py` + `zellij_client.py`) gains additive `yolo` launch support, a grok provider, and a per-operator session cap; the Android client (Compose + Termux TerminalView over zellij-web WebSockets) flips to fork-always launches, adds YOLO/grok UI, and fixes inset double-padding, resize negotiation, mouse-sequence leaks, and the Esc key. All server changes are additive — the Portal web path keeps working unchanged.

**Tech Stack:** Python/FastAPI + pytest (server), Kotlin/Jetpack Compose + JUnit/Robolectric (Android), zellij 0.44.3 two-socket web protocol.

**Design doc:** `docs/plans/2026-07-03-android-cli-terminal-production-uplift-design.md`

**Worktree:** `.worktrees/android-cli-terminal-uplift`, branch `feat/android-cli-terminal-uplift`.
Baselines verified 2026-07-03: server `Orchestrator/tests/test_cli_agent/` 113 passed (needs `config.ini` + `.env` symlinked from the main tree — already done); Android `./gradlew testDebugUnitTest --tests "com.aiblackbox.portal.ui.cli_agent.*" --tests "com.aiblackbox.portal.data.api.ZellijWebSocketClientTest"` green.

**Path shorthand:**
- `SERVER` = `Orchestrator`
- `APP` = `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal`
- `APPTEST` = same but `src/test/java/com/aiblackbox/portal`
- Android test command (run from `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/`): `./gradlew testDebugUnitTest --tests "<pattern>" -q`
- Server test command (run from worktree root): `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_cli_agent/ -q`

**Verified CLI flags (checked via `--help` on this box 2026-07-03):**
| provider | binary | YOLO flag |
|---|---|---|
| claude | claude | `--dangerously-skip-permissions` |
| gemini | gemini | `--yolo` |
| codex | codex | `--dangerously-bypass-approvals-and-sandbox` |
| grok | grok (`~/.local/bin/grok`) | `--always-approve` |
| antigravity | agy | `--dangerously-skip-permissions` |
| terminal | (shell) | none — reject `yolo=true` with 400 |

---

## Task 1: Server — grok provider

**Files:**
- Modify: `SERVER/routes/cli_agent_routes.py` — `_PROVIDER_BINARY_NAMES` (~line 38), `SUPPORTED_PROVIDERS` (line ~84), `_ZELLIJ_PROVIDER_BINARIES` (~line 381)
- Test: `SERVER/tests/test_cli_agent/test_provider_bin_resolution.py`, `SERVER/tests/test_cli_agent/test_zellij_endpoints.py`

**Steps:**
1. Write failing tests: `provider_bin("grok")` returns a path when a `grok` binary is on the extended PATH (mirror the existing per-provider resolution tests); `POST /cli-agent/zellij/launch` with `provider="grok"` is accepted (mock `zellij_client` like the existing launch tests — do NOT spawn real sessions).
2. Run: server test command with `-k grok`. Expected: FAIL (unknown provider).
3. Implement: add `"grok": "grok"` to `_PROVIDER_BINARY_NAMES`, `"grok"` to `SUPPORTED_PROVIDERS`, `"grok": "grok"` to `_ZELLIJ_PROVIDER_BINARIES`. No fallback path needed (`~/.local/bin` is already in `path_extension.extended_path_dirs()` — verify, it resolves claude from there today).
4. Run full `test_cli_agent` suite. Expected: 113+new passed.
5. Commit: `feat(cli-agent): grok CLI provider (server)`

## Task 2: Server — YOLO launch plumbing

**Files:**
- Modify: `SERVER/routes/cli_agent_routes.py` (`zellij_launch`, ~line 515-669; new `_YOLO_FLAGS` const next to `_ZELLIJ_PROVIDER_BINARIES`)
- Modify: `SERVER/cli_agent/zellij_state.py` (`add_session` line 126, `list_for_operator`)
- Test: `SERVER/tests/test_cli_agent/test_zellij_endpoints.py`, `test_zellij_state.py`

**Behavior spec:**
- New const:
  ```python
  _YOLO_FLAGS: dict[str, str] = {
      "claude": "--dangerously-skip-permissions",
      "gemini": "--yolo",
      "codex": "--dangerously-bypass-approvals-and-sandbox",
      "grok": "--always-approve",
      "agy": "--dangerously-skip-permissions",
      "antigravity": "--dangerously-skip-permissions",
  }
  ```
- `zellij_launch` parses `yolo = bool(body.get("yolo", False))`. `yolo=true` with `provider="terminal"` → 400.
- Args passed to `zellij_client.launch_session(name, binary, args)`: `args = list(PROVIDER_ARGS.get(provider, []))` plus the YOLO flag when `yolo` — **note this also carries codex's `--no-alt-screen` onto the zellij path** (today it's tmux-only; it's the documented fix for codex scrollback — keep it).
- YOLO session names get a `_yolo` suffix AFTER the timestamp: `{op}__{provider}__{app}__{ms}_yolo` (only forks can be YOLO in practice since Android always forks, but apply the suffix in both name helpers for consistency; deterministic YOLO name = `{op}__{provider}__{app}__yolo`).
- `zellij_state.add_session(..., yolo: bool = False)` — persist `"yolo"` in the row; `zellij_list_sessions` response gains `"yolo": row.get("yolo", False)`. Keyword default keeps every existing caller/test working (additive).
- `launch_session` and `_build_layout_kdl` in `zellij_client.py` **already accept `args`** (verified) — no changes there.

**Steps:** TDD as in Task 1 — failing endpoint tests first (yolo claude launch passes the flag to `launch_session` args (assert via mock), terminal+yolo → 400, sessions list returns yolo field, state row round-trips yolo), then implement, then full suite, then commit `feat(cli-agent): YOLO (skip-permissions) launch support (server)`.

## Task 3: Server — per-operator session cap

**Files:**
- Modify: `SERVER/routes/cli_agent_routes.py` (`zellij_launch`)
- Test: `SERVER/tests/test_cli_agent/test_zellij_endpoints.py`

**Behavior spec:** `_MAX_ZELLIJ_SESSIONS_PER_OPERATOR = 12` module const. In `zellij_launch`, on the **create** path only (never blocks attach/resume), count this operator's live sessions (same state∩live intersection as `zellij_list_sessions` — extract a small shared helper) and raise `HTTPException(409, "Session limit reached (12). Close a session (X) first.")` when at cap.

**Steps:** failing test (12 live mocked sessions → 409 with that message; resume of an existing session at cap still succeeds), implement, full suite, commit `feat(cli-agent): 12-session per-operator soft cap`.

## Task 4: Android — fresh-by-default launches + grok + YOLO UI

**Files:**
- Modify: `APP/data/model/CliAgentModels.kt` (`ZELLIJ_PROVIDER_SLUGS` line ~100, `CliAgentProvider` enum)
- Modify: `APP/ui/cli_agent/SessionSwitcherTopBar.kt` (`PROVIDER_SHORTCUTS` line ~511, session rows, "+ new" menu)
- Modify: `APP/ui/cli_agent/CliAgentEmptyState.kt` (shortcut buttons ~line 160-244)
- Modify: `APP/ui/cli_agent/CliAgentScreen.kt` (launch call sites lines ~205-216, ~281-288)
- Modify: `APP/ui/cli_agent/CliAgentScreenState.kt` (`launch`, toast copy ~line 222-227), `APP/ui/cli_agent/CliAgentSessionRepository.kt` (launch body, session row model)
- Test: `APPTEST/ui/cli_agent/{CliAgentScreenStateTest,SessionSwitcherTopBarTest,CliAgentSessionRepositoryTest}.kt`

**Behavior spec:**
- Every launch path sends `fork=true` — tap and long-press become identical; delete the fork-vs-tap distinction from UI copy/comments. The "Resumed session" toast branch goes away (server still returns `resumed` for Portal compat; Android ignores it).
- Add `grok` to `PROVIDER_SHORTCUTS`, `ZELLIJ_PROVIDER_SLUGS`, and the provider enum with a display label ("Grok").
- Each agent row (empty state + switcher "+ new" menu) gains a compact amber ⚡ button → `launch(provider, fork=true, yolo=true)`. `terminal` gets no ⚡.
- Repository `launch()` adds `yolo: Boolean = false` → request body. Session row model parses `yolo` from the response (default false).
- Switcher session rows show a persistent ⚡ badge when `yolo == true` (fallback: name ends in `_yolo`).
- HTTP 409 from launch → toast the server's message (session cap).

**Steps:** failing unit tests first (screen-state launch always forks; yolo flag serialized; grok in shortcut list; badge logic; 409 surfaces message), implement, run Android cli_agent test command, commit `feat(cli-terminal): fresh-spawn taps, grok shortcut, YOLO buttons (Android)`.

## Task 5: Android — insets: terminal fills the screen

**Files:**
- Modify: `APP/ui/cli_agent/ZellijTerminalScreen.kt` (insets chain ~line 272-284)
- Modify: `APP/ui/cli_agent/CliAgentScreen.kt` (Terminal branch `innerPadding` ~line 360)
- Test: `APPTEST/ui/cli_agent/ZellijTerminalScreenTest.kt`

**Behavior spec:** Exactly ONE layer owns insets. The Terminal branch stops applying Scaffold `innerPadding` to the terminal surface (the top bar hides during active terminal use anyway via `LocalShowAppChrome`); `ZellijTerminalScreen` keeps `statusBarsPadding/navigationBarsPadding/imePadding` as the single owner. When app chrome IS visible (switcher bar shown), the terminal must not underlap it — handle via the top inset only, not blanket padding. `ReconnectBanner` becomes an overlay (Box + zIndex) so it never shrinks the grid.

**Steps:** capture the current padding composition in a characterization test if feasible; implement; verify no double-padding (unit-level: modifier chain assertions where the test suite already does this); commit `fix(cli-terminal): single-owner insets — terminal grid fills the screen`.

## Task 6: Android — rotation/resize negotiation

**Files:**
- Modify: `APP/ui/cli_agent/ZellijTerminalScreen.kt` (resize `LaunchedEffect` ~line 250-269, `onSizeChanged` ~line 398-410)
- Modify: `APP/data/api/ZellijWebSocketClient.kt` (`sendResize`/`requestRepaint` ~line 189-227, replay ~line 452-453)
- Test: `APPTEST/data/api/ZellijWebSocketClientTest.kt`, `APPTEST/ui/cli_agent/ZellijTerminalScreenTest.kt`

**Behavior spec:**
- **Debounce** resize sends (~150ms trailing) so rotation/IME animations emit one final size, not a spam of intermediates (each one makes zellij reflow the whole session).
- After the debounced resize, **always** `requestRepaint()` — today repaint only happens on reattach; a rotation that races the redraw leaves the old grid painted ("hanging off screen").
- On `QueryTerminalSize` and on reconnect, reply with the CURRENT measured size (not just cached last-sent) — reconcile unconditionally.
- Guard: never send cols/rows ≤ 0 or unchanged values (debounce dedups).

**Steps:** failing tests (debounce collapses burst to last value; repaint follows resize; QueryTerminalSize answered with current size), implement, run tests, commit `fix(cli-terminal): debounced resize + unconditional repaint — rotation never strands the grid`.

## Task 7: Android — tap leak, scrollback, Esc key

**Files:**
- Modify: `APP/ui/cli_agent/ZellijTerminalScreen.kt` (pointer interception ~line 286-397, tap handling ~line 451-456)
- Modify: `APP/ui/cli_agent/ExtraKeysBar.kt` (Esc emission ~line 101-119; handler wiring ~line 602-625 in ZellijTerminalScreen)
- Test: `APPTEST/ui/cli_agent/ZellijTerminalScreenTest.kt`, new `ExtraKeysBarTest.kt` if absent

**Behavior spec:**
- **Phantom chars:** while mouse tracking is active, NO raw touch reaches `TerminalView`: the Compose `pointerInput` layer consumes everything — taps → focus + keyboard only (no bytes), vertical drags → SGR wheel sequences (existing path), all else swallowed. Removes the touch-slop escape hatch that leaks `<65;44;17M`-style sequences.
- **Scrollback:** TUI state (mouse-tracking / alt-buffer / normal) is read from the live emulator **at gesture time** — never cached across frames or sessions. Must behave identically for a manually-launched agent inside a plain terminal (Brandon's repro) — nothing may key off the session's launch provider. Keep the three-branch delivery (wheel / PgUp / topRow).
- **Esc:** currently dead — root-cause first using @superpowers:systematic-debugging (candidates: stale `onKeyBytes` closure not re-keyed per session — same bug class as the fixed scroll closure at `CliAgentScreen.kt:331-345`; byte swallowed by the IME path; wrong byte emitted). Fix + regression test asserting a tap on Esc sends exactly `0x1b` to the active session's client.

**Steps:** TDD where the harness allows; systematic-debugging for Esc (instrument, find root cause, THEN fix); run Android suite; commit `fix(cli-terminal): airtight touch interception, gesture-time scroll state, Esc key`.

## Task 8: Verification + finish

**Steps:**
1. Full server suite: `pytest Orchestrator/tests/` (not just test_cli_agent) — expected: no regressions vs baseline.
2. Full Android unit suite: `./gradlew testDebugUnitTest -q` — expected green.
3. `./gradlew assembleDebug` — APK builds.
4. @superpowers:requesting-code-review — adversarial review of the whole branch diff against this plan (every prior project caught ≥1 real issue this way).
5. Fix findings, re-run suites.
6. Fold device validation checklist (Brandon): spawn 3+ mixed-agent sessions via taps (each is FRESH), switch between them, X-kill one (gone), YOLO ⚡ claude session (badge persists after app restart), grok launches, rotate once → fits (both orientations, folded + unfolded), no phantom chars tapping during claude TUI, scrollback in plain-terminal→manual-claude repro, Esc works in claude + shell, session cap toast at 12.
7. @superpowers:finishing-a-development-branch — merge to main; deploy note: **prod serves the MAIN working tree** — server changes go live only after merge + `sudo systemctl restart blackbox.service` (pre-authorized).
8. `/snapshot-dev` to mint the dev snapshot.
