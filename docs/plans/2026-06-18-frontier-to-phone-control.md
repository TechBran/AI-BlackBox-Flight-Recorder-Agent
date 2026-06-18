# Frontier → On-Device Gemma `control_phone` — Implementation Plan

> **For Claude:** Build DIRECTLY on `main` in this dev sandbox — NO worktree, NO
> feature branch ([[feedback-staging-box-as-production]]). Edit → test → commit →
> push. Optionally subagent-driven (fresh subagent + two-stage review per task),
> but all on `main`.

**Goal:** A ToolVault tool `control_phone` that lets a frontier model delegate a
device-control task to the on-device Gemma on the originating phone (Phase 2: any
reachable registered device), over Direct Tailscale HTTP, blocking with wake-tracking,
bounded by a remote allowlist (YOLO for safe actions, high-consequence refused).

**Architecture:** Inverts the call direction (BlackBox→phone). The phone's foreground
`LocalModelService` hosts a tailnet-bound HTTP listener (`/task`, `/status`,
`/healthz`); the BlackBox enumerates the tailnet (`tailscale status` ⋈ attestations)
to resolve + reach it; the remote task reuses the on-device agent loop with an
allowlist filter. Design: `docs/plans/2026-06-18-frontier-to-phone-control-design.md`.

**Tech:** Python/FastAPI + ToolVault module (`Orchestrator/`), pytest; Kotlin/Android
(`AI_BlackBox_Portal_Android_MVP/.../portal/`), JUnit; Tailscale CLI/API; the existing
on-device native agent loop + `LocalProviderRegistry`.

**Conventions:** TDD per task. Explicit `git add <paths>` — NEVER `git add -A`.
Backend tests: `Orchestrator/venv/bin/python -m pytest <path> -q`. Android gate:
`./gradlew :app:testDebugUnitTest --offline`. `ChatViewModel.kt` is CRLF — preserve it.

---

## Phase 1 — Backend: registry, tailnet resolution, attest

### Task 1: record the phone's tailnet name at attest
**Files:** `Orchestrator/local_provider/registry.py`, `Orchestrator/routes/local_routes.py`, `Orchestrator/tests/test_local_provider_registry.py`
- Extend the attestation record + `/local/device/attest` to accept + store `tailnet_name` (optional, backward-compatible). It's the join key to `tailscale status`.
- TDD: attest with `tailnet_name` → `status()`/record carries it; attest WITHOUT it still works (existing tests green).

### Task 2: tailnet mesh resolution (`tailscale status` ⋈ attestations)
**Files:** Create `Orchestrator/local_provider/mesh.py`, `Orchestrator/tests/test_local_mesh.py`
- `parse_tailscale_status(json_str) -> list[Node]` (name, ip, online) — PURE, tested against a captured `tailscale status --json` sample.
- `reachable_devices(operator=None) -> list[dict]` = online nodes ⋈ attestations (has-Gemma), joined on `tailnet_name`; `resolve_origin(operator) -> Node|None` (v1).
- TDD: fake `tailscale status` + a fake registry → correct join, online-only, operator filter. (Shell-out to `tailscale status --json` behind a seam so it's mockable.)

---

## Phase 2 — Backend: the `control_phone` tool

### Task 3: `control_phone` ToolVault module (schema + executor)
**Files:** Create `ToolVault/tools/control_phone/schema.json` + `executor.py`; `Orchestrator/tests/test_control_phone_tool.py`
- `schema.json`: `name=control_phone`, params `{task: str}` (Phase 2: + optional `device`). Description STEERS the frontier model: pre-announce "Waking Gemma on your phone — I'll report back…", note it BLOCKS + may take a minute, and that only safe device actions run remotely.
- `executor.py` (`async def execute(params, ctx)`): `resolve_origin(operator)` → POST `https://<node>/task` `{task, operator}` → poll `GET /status` until `done`/`error`/timeout → return the result text. Structured errors for: no reachable device, wake-fail, timeout, refused-only outcome.
- Validate via `python -m Orchestrator.toolvault.validate`; `POST /toolvault/reload`.
- TDD: mock the mesh resolution + the phone HTTP (a stub returning waking→working→done) → executor returns the result; unreachable → error; timeout → error. (`tool_injection`/`/local/tools/search` will surface it to the on-device path too — confirm it's discoverable.)

### Task 4: blocking/poll/timeout semantics
**Files:** `executor.py` (+ tests)
- Poll interval (e.g. 2-3s) + total timeout (config, default ~5 min). Distinguish `waking` (model load) vs `working` (executing) in the error/progress text. Cancellation-safe.
- TDD: stub phone that stays `waking` past the cap → timeout error; flips to `done` → result.

---

## Phase 3 — Phone: listener + remote runner

### Task 5: tailnet-bound HTTP listener in `LocalModelService`
**Files:** new `data/remote/RemoteControlServer.kt` (+ test for the pure request/route logic); wire start/stop into `LocalModelService`; `AndroidManifest` perms if needed.
- Embedded HTTP server (NanoHTTPD or Ktor-embedded) bound to the tailnet interface: `POST /task`, `GET /status/{id}`, `GET /healthz`. Started by the FG service; stopped on destroy.
- TDD: the pure request-parse + route-dispatch logic (JDK17-testable); the socket binding is device/compile-verified.

### Task 6: remote task runner + allowlist filter
**Files:** new `data/remote/RemoteTaskRunner.kt` + `data/remote/RemoteAllowlist.kt` (pure) + tests
- `RemoteAllowlist` (PURE): `isAllowedRemote(toolName): Boolean` from the design's safe/refused split. TDD the split.
- `RemoteTaskRunner`: wake/load Gemma (reuse the engine holder) → run the native agent loop on the task, dispatching through a filter that runs allowlisted tools (YOLO, no ConfirmGate) and REFUSES high-consequence ones with a clear result. Reuses the snapshot-ledger loop. TDD the filter wrapping (a fake tool set → safe dispatched, refused returns the refusal).

### Task 7: status tracking (`waking → working → done|error`)
**Files:** `RemoteTaskRunner.kt` / a small state holder + test
- Per-task state machine surfaced by `GET /status/{id}`: `waking` (loading), `working` (loop running, optional step count), `done` (result), `error` (message). TDD the transitions.

### Task 8: listener auth/scope (paired hub + operator)
**Files:** `RemoteControlServer.kt` (+ test)
- Accept `/task` only from the paired hub (token from pairing / Tailscale source) + for the bound operator; reject others (403). Tailscale is the perimeter; this is blast-radius scoping. TDD the accept/reject decision (pure).

---

## Phase 4 — Integration, device validation, ship

### Task 9: backend integration smoke
- Run the backend; point `control_phone` at a STUB phone listener (local) → confirm resolve→POST→poll→result + the error paths. Confirm the tool is discoverable in ToolVault + executable.

### Task 10: device validation (watched session, Brandon)
Real Fold 6: in BlackBox chat ask a frontier model (Gemini/Claude) to "open Maps to X on my phone" → it pre-announces, calls `control_phone`, the phone wakes Gemma, runs the allowlisted action, reports back. Verify: an allowlisted task completes + reports; a high-consequence task (send SMS) is REFUSED; an unreachable phone → clean error → frontier retries/ends; the device list reflects tailnet liveness.

### Task 11: final review + ship
- superpowers:code-reviewer over the diff (security pass: allowlist enforced server-AND-phone-side, auth scope, no unattended high-consequence, Tailscale perimeter). Fix findings.
- Ship per [[feedback-staging-box-as-production]]: it's already on `main` — commit + `git push`; redeploy the live tree (restart) + reinstall the APK; device-validate.

---

## Risks / watch-items
- **Direction inversion = new attack surface**: the phone now accepts inbound tasks. The allowlist (enforced BOTH phone-side AND ideally echoed server-side) + Tailscale perimeter + operator scope are the guards. Review hard.
- **Allowlist is the blast radius** — get the split right; default-deny (unknown tool → refused remote).
- **Wake latency** — model load 10-75s; the blocking poll + generous timeout + the pre-announce UX absorb it; never hang forever.
- **Phone listener lifecycle** — only alive while the FG service runs; reflect liveness honestly in the list (don't offer a device whose `/healthz` fails). FCM-wake is Phase 2.
- **CRLF** on `ChatViewModel.kt` if touched; embedded-server lib choice + tailnet-interface binding need a device check.
