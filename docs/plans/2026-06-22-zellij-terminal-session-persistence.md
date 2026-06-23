# Zellij/CLI Terminal — Persistent Sessions Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development to execute this plan task-by-task.

**Goal:** Terminal sessions in the Android MVP persist until the user explicitly kills them with the X button — surviving screen navigation, app backgrounding, app kill/relaunch, AND an orchestrator restart. Opening a terminal that already has a session **resumes** it; a separate "+ New" forks a fresh one. Terminals stay warm in the background via a foreground service.

**Architecture:** The durable substrate already exists server-side (sessions decoupled from the WebSocket; name-keyed registry with no TTL; tmux `killmode.conf` + `attach_or_create`; zellij detach/attach; kill only on explicit DELETE). The bug is entirely about **client-side ownership**: the WebSocket + reattach handle are welded to the Compose composition and dropped on navigation, and re-entry always creates-new. Fix = hoist connection/session ownership out of the composition into a process-lived **TerminalSessionManager** (owned by a foreground service), add **reattach-by-id**, make the backend **attach-if-exists** + **survive its own restart**, and wire the **X button** to the explicit kill.

**Tech stack:** Android Jetpack Compose + Kotlin (OkHttp WebSocket, foreground service), FastAPI/Python backend (tmux + zellij CLI bridges).

---

## Root Cause (from the 6-agent investigation — evidence on file)

The server session does NOT die on navigation; the app orphans it via THREE client-side facts:
1. **Close-on-dispose.** `ZellijTerminalScreen.kt:205-211` `DisposableEffect.onDispose { client.close() }`; `close()` sets `userClosed` (permanent) and defeats the built-in reconnect (`ZellijWebSocketClient.kt:207-216,151-155`).
2. **Handle dropped + reset.** Back → `clearCurrent()` + `EmptyState` (`CliAgentScreen.kt:316-327`); the reattach token lives only in screen-scoped `liveSessionsByName` (`CliAgentScreenState.kt:119`).
3. **Always create-new.** Re-entry mints a new session (`ZellijWebSocketClient.kt:158-164`); selecting an existing row toasts "kill and relaunch to reattach" (`CliAgentScreen.kt:177-188`). No reattach-by-id exists.

Backend already persists: WS proxy kills nothing on disconnect (`agent_routes.py:1581-1593`); registry idempotent + no TTL (`zellij_state.py`); tmux `attach_or_create` (`session_manager.py:160`) + explicit-only `kill` (`:344`); zellij detach keeps backend alive.

## Decisions (locked with the user 2026-06-22)
- **Persistence boundary:** survive navigation + background + app-kill + **orchestrator restart**. Only the X button kills (plus the existing 7-day idle reaper as a safety net — keep it).
- **Foreground service:** YES — terminals stay warm in the background.
- **Open behavior:** resume existing (attach-if-exists on deterministic name) + a separate "+ New" to fork.

---

## Target Architecture

```
Compose screen (render only)  <-- binds/unbinds, never closes the socket
        |
TerminalSessionManager  (process singleton; map<name, ZellijWebSocketClient + handle>)
        |  owned/kept-alive by
TerminalForegroundService  (connectedDevice-style FGS; persistent notification; lives across nav + background)
        |  OkHttp WS (terminal + control)
Backend WS proxy (relay; kills nothing)  -->  tmux/zellij session (survives detach + systemd restart)
        |
Session registry (zellij_state) -- survives orchestrator restart (reconcile keeps live terminal rows)
```

- **Connection + session handles** move OUT of the composable into `TerminalSessionManager` (process-lived). Navigating away **unbinds the renderer**; the socket stays open (or, if dropped, the existing reconnect machinery re-attaches by name).
- **Foreground service** owns/anchors the manager so the process stays warm while backgrounded; started when the first terminal opens, stopped when the last session is killed.
- **Reattach-by-id**: on app launch (or when the manager has no live client for a name), `GET /sessions` lists server sessions and the manager opens a proxy WS **by name** (master-token model — no per-session token needed; confirm G4).
- **Backend resume**: `launch` becomes attach-if-exists on the deterministic name `cli-agent-{operator}__{provider}__{slug}`; a distinct "+ New" path forks.
- **Survive restart**: `reconcile_or_wipe()` must RECONCILE (preserve) live terminal rows (`expires_at=None`) whose underlying tmux/zellij session still exists, instead of wiping.
- **X button** → explicit DELETE/kill (the only kill path). Plain back/nav never kills.

---

## Phase 1 — Stop the bleed: hoist ownership, stop close-on-dispose, wire X (client-only, fixes the reported bug)

**Files:**
- Create: `ui/cli_agent/TerminalSessionManager.kt` (process singleton: `object` or Application-scoped; holds `map<sessionName, LiveClient>` where LiveClient = ZellijWebSocketClient + handle + last-known cols/rows + a render-bound flag).
- Modify: `ui/cli_agent/ZellijTerminalScreen.kt` — get the client from the manager (bind), render; in `onDispose` **unbind only** (stop forwarding output to the dead TerminalView), do NOT `close()`. Keep `BackHandler` → `onBack()` (no close).
- Modify: `ui/cli_agent/CliAgentScreen.kt` — `onBack` no longer drops the session; switcher row select reattaches via the manager (live client OR reconnect-by-name) instead of toasting.
- Modify: `ui/cli_agent/CliAgentScreenState.kt` — `liveSessionsByName` delegates to the manager (survives navigation); `kill()` stays the only removal.
- Modify: `data/api/ZellijWebSocketClient.kt` — split "detach" (stop rendering, keep socket / allow reconnect) from "close" (permanent, only on kill). Ensure reconnect-by-`currentSessionName` is reachable (don't set `userClosed` on detach).
- Modify: X button wiring (SessionSwitcherTopBar onKillSession / a per-terminal X) → explicit kill (`sendKill` control frame + DELETE).

**Tests (Robolectric/JUnit + the existing `test/.../cli_agent` suite):**
- Manager survives a simulated screen dispose→recreate: same session name → same live client, socket NOT closed.
- `onDispose` does not call `close()`; only an explicit kill does.
- Switcher reattaches to a live manager client without a re-`POST /session`.
- X kill removes the session from the manager AND sends the kill frame/DELETE.

**Risk:** owner lifetime — must be Application/FGS-scoped, NOT NavBackStackEntry-scoped (the cli_agent route is popped on back). Confirm the nav pop behavior (`NavGraph.kt:175-177`).

## Phase 2 — Durability: reattach-by-id + backend attach-if-exists + survive orchestrator restart

**Files (Android):**
- `TerminalSessionManager.kt` — on first access with no live client for a name, **reattach**: `GET /sessions` → open proxy WS by name. On app cold start, hydrate the session list.
- `CliAgentSessionRepository.kt` — add resume/reattach call; `GET /sessions` already returns operator rows.
- `CliAgentModels.kt` — confirm name (not per-session token) suffices to reattach (master-token; G4).

**Files (backend):**
- `routes/cli_agent_routes.py` — `launch` attach-if-exists on the deterministic name (today mints timestamped, `:528`); add/confirm a **resume** path that re-opens a proxy WS by name; guard the launch-failure cleanup (`:583`) from force-deleting an existing session on a name collision (G3).
- `cli_agent/zellij_state.py` — `reconcile_or_wipe()` (`:248-335`): RECONCILE (keep) terminal rows (`expires_at=None`) whose tmux/zellij session is still live; wipe only genuinely-orphaned/short-lived rows. This is the survive-restart change.
- `cli_agent/zellij_client.py` / `session_manager.py` — confirm `attach_or_create` (tmux `:160`) + the zellij attach path resume by name under the master token.

**Tests:**
- Backend: launch twice with same (operator,provider,slug) → second ATTACHES (same session), not a new one. `GET /sessions` lists it. DELETE kills it.
- Backend: `reconcile_or_wipe()` after a restart with a live terminal session present → row PRESERVED (not wiped); a stale/missing-backend row → wiped.
- Android: cold start with an existing server session → manager hydrates + reattaches by name (no new session).

**Risk / confirm (G1-G4):** which backend serves the live UI on this box (`CLI_AGENT_BACKEND`); reaper 7-day behavior acceptable; launch-failure-cleanup collision; reattach-by-name-only under master token.

## Phase 3 — Foreground service: warm terminals in the background

**Files:**
- Create: `TerminalForegroundService.kt` — `connectedDevice`-type FGS (like `LocalModelService`, which dodges the dataSync 6h timeout), holds/anchors `TerminalSessionManager`. START when first terminal opens; STOP when last session killed. Persistent notification ("N terminals running") with a tap-to-open + (optional) kill-all action.
- Modify: `AndroidManifest.xml` — FGS `<service>` entry + `FOREGROUND_SERVICE` / `FOREGROUND_SERVICE_CONNECTED_DEVICE` permissions (Android 14+ typed FGS).
- Modify: `NativeMainActivity.kt` / nav — start/stop the service around terminal lifecycle; bind the screen to the service-held manager.

**Tests:**
- Service starts on first launch, stops on last kill; manager + sockets survive backgrounding (instrumented or manual on-device).
- Notification reflects live session count.

**Risk:** Android 14+ FGS type restrictions; battery/notification UX; ensure swipe-away path falls through to Phase-2 reattach (FGS dies with process on force-stop → relaunch reattaches).

---

## Sequencing & verification
1. Phase 1 → device-verify: open terminal, run a long command, back to Portal, return → **same session, still running**. X kills it. (Fixes the reported bug.)
2. Phase 2 → device-verify: kill the app (swipe), relaunch → session resumes; restart `blackbox.service` → session preserved.
3. Phase 3 → device-verify: background the app during a long run → terminal stays warm; notification shows it.

Each phase: build + install + on-device validation BEFORE commit/push (per the project workflow). Snapshot after each phase.

## Open items to confirm during implementation
- G1: `CLI_AGENT_BACKEND` value on this box (tmux vs zellij) — both persist, but the resume/attach code path differs.
- G4: master-token reattach works by session **name** alone (no per-session token) — gates how thin the reattach is.
- Reaper: keep 7-day idle reap as the safety net (recommended) or make terminals never-reap.
- "+ New" naming: deterministic name + a uniqueness suffix when forking a second concurrent session for the same app.
