# Android CLI Terminal — Production Uplift (Design)

**Date:** 2026-07-03
**Status:** Validated with Brandon (brainstorm 2026-07-03)
**Scope:** Android MVP CLI-agent terminal (zellij stack) + Orchestrator launch endpoint. No external/multi-window support (explicitly deferred). Server changes stay additive so the Portal web path keeps working.

## Problems (as reported on the Fold)

1. Shortcut tap **resumes** the old session instead of spawning a new terminal; user could not open a second terminal.
2. No **grok** CLI shortcut (binary is installed at `~/.local/bin/grok` but never wired in).
3. No way to launch any agent with **skip-permissions / YOLO** mode.
4. Terminal doesn't fill the phone screen — visible wasted border space.
5. Rotation leaves the terminal **hanging off-screen**; takes two rotations to recover.
6. Tapping the screen sprays **phantom characters** that then disappear.
7. **Scrollback** broke in the flow: plain terminal → manually ran `claude` inside it.
8. **Esc key** on the ExtraKeysBar has stopped working.

## Root causes (from code mapping 2026-07-03)

- Resume-by-design: plain tap → `launch(provider, fork=false)` → server attach-if-exists on deterministic name `{op}__{provider}__{app|root}` (`cli_agent_routes.py:417-438`, `515-669`). Fresh session requires long-press fork (`CliAgentScreen.kt:212-216`). One-session-per-agent is baked into the name.
- Zellij launch path passes **zero CLI args** (`zellij_client.py:667-728` `_build_layout`); no flag plumbing exists.
- Double insets: terminal applies `statusBarsPadding().navigationBarsPadding().imePadding()` (`ZellijTerminalScreen.kt:272-279`) **and** the Scaffold's `innerPadding` (`CliAgentScreen.kt:360`). ReconnectBanner pushes content down.
- Rotation: activity survives config change (`configChanges` in manifest); correctness depends on re-measure → `TerminalResize` reaching zellij. A stale/lost resize = content drawn for the old grid.
- Phantom chars: with mouse tracking on (Claude TUI), a slightly-draggy tap passes through `TerminalView` and is encoded as an SGR mouse sequence (`<65;44;17M`), rendered until next redraw (`ZellijTerminalScreen.kt:286-397`).
- Scrollback branches on live TUI state (mouse-tracking / alt-buffer / normal, `ZellijTerminalScreen.kt:338-392`); the alt-buffer-no-mouse branch sends PgUp which Claude ignores; manual-launched agents get no provider hooks.

## Design

### 1. Session model — fresh by default

- **Every** shortcut tap (claude, gemini, codex, grok, antigravity, plain terminal, YOLO variants) spawns a **new timestamped session** (`{op}__{provider}__{app|root}__{unix_ms}` — the existing fork-name path). The deterministic-resume tap behavior is removed from the Android client; long-press stops being a distinct gesture.
- The **session switcher is the only way back**: lists all live/attachable sessions with agent name + creation time (derived from the name's timestamp), tap to attach.
- **X remains the only permanent kill** (client teardown + backend DELETE, unchanged).
- **Soft cap: 12 live sessions per operator.** At the cap, launch returns a friendly error → toast "Session limit reached — close one first (X)". Server-enforced (count operator-prefixed sessions before launch).
- Server stays backward-compatible: `fork` param remains; Android simply always sends `fork=true`. Portal unaffected.

### 2. Grok agent + YOLO launches

- **grok** added as a provider: server `_ZELLIJ_PROVIDER_BINARIES["grok"] = "grok"`, `SUPPORTED_PROVIDERS`, allowed slugs; Android `PROVIDER_SHORTCUTS`, `ZELLIJ_PROVIDER_SLUGS`, `CliAgentProvider`.
- **YOLO**: launch endpoint gains additive `yolo: bool`. Server-owned flag map (verified against `--help` on this box):
  - claude → `--dangerously-skip-permissions`
  - gemini → `--yolo`
  - codex → `--dangerously-bypass-approvals-and-sandbox`
  - grok → `--always-approve`
  - terminal → no YOLO variant; antigravity → only if `agy` exposes an equivalent flag (check at implementation; omit button otherwise).
- `_build_layout` learns to append args to the pane command (KDL `args`).
- **UI**: amber ⚡ button beside each agent shortcut (empty state + switcher "+ new" menu). YOLO marker is **encoded in the session name** (e.g. `…__{ms}_yolo`) so the switcher shows a persistent ⚡ badge — durable across app restarts because the list is rebuilt from `zellij list-sessions`.

### 3. Layout / resize / rotation

- **Single-owner insets**: exactly one layer applies system-bar/IME padding; Scaffold `innerPadding` double-application removed. Terminal grid fills every usable pixel. ReconnectBanner overlays instead of pushing content.
- **Rotation**: on config change force re-measure → push fresh cols×rows → request full repaint; reconcile on `QueryTerminalSize` and on reconnect (already partially present — make it unconditional). **Debounce** resize sends so mid-animation intermediate sizes don't spam zellij reflows.
- Acceptance: rotate once → fits; rotate back → fits; fold/unfold → fits. No two-rotation ritual.

### 4. Input + scrollback reliability

- **Airtight tap handling**: when mouse tracking is active, no raw touch reaches `TerminalView` — taps only focus/raise keyboard, deliberate drags become clean SGR wheel sequences, everything else consumed. Closes the phantom-character leak by construction, not touch-slop luck.
- **Scrollback**: re-evaluate TUI state per gesture (never cached); prefer wheel sequences whenever accepted; behavior depends only on live emulator state, never on the provider the session was created as. Acceptance repro: plain terminal → manually run `claude` → scrollback works.
- **Esc key fix**: ExtraKeysBar Esc is currently non-functional — root-cause during implementation (candidate: stale closure / byte routing / TUI consuming ESC) using systematic-debugging; fix + regression test.

### 5. Verification

- Unit tests extend existing suites: `TerminalSessionManagerTest`, `CliAgentScreenStateTest`, `ZellijWebSocketClientTest`, `SessionSwitcherTopBarTest`, server route tests.
- Fold device pass: spawn 3+ concurrent sessions (mixed agents), switch, kill, rotate, fold/unfold, YOLO badge persists after app restart, grok launches, both scrollback paths, Esc works in claude + plain shell.
- Fresh-box gate: no hardcoded operator/host; grok button hidden/failing gracefully if binary absent (backend-status driven where possible).

## Key files

Android: `ui/cli_agent/{ZellijTerminalScreen,TerminalSessionManager,CliAgentScreenState,CliAgentScreen,CliAgentEmptyState,SessionSwitcherTopBar,ExtraKeysBar,CliAgentSessionRepository}.kt`, `data/api/ZellijWebSocketClient.kt`, `data/model/CliAgentModels.kt`.
Server: `Orchestrator/routes/cli_agent_routes.py`, `Orchestrator/cli_agent/zellij_client.py`.
