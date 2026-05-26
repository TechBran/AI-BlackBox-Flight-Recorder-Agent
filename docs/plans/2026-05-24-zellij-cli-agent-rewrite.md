# Zellij-Based CLI Agent Architecture — Comprehensive Replacement Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (same session, fresh subagent per task, two-stage review). Phase 0 is a hard decision gate — DO NOT proceed past it without Brandon's go-ahead.

**Goal:** Replace the current PtyBridge + tmux + xterm.js CLI Agent architecture with Zellij. The Portal modal becomes an iframe to Zellij's web client; the orchestrator becomes the auth/launcher/proxy layer; power users can `zellij attach <url>` from their local gnome-terminal and see the SAME session as Portal users.

**Why:** Today's debugging surfaced that our double-PTY layering (PtyBridge → tmux → CLI) is unique to us and accumulates provider-specific hacks. Every CLI provider needs a different POST_ATTACH_HOOK (Claude `/tui fullscreen`, Codex `--no-alt-screen`, Gemini nothing). New providers will keep adding to the table. Zellij's web client is the productized version of what we're trying to build by hand. Research findings in `docs/plans/2026-05-24-cli-agent-terminal-architecture-research.md`.

**Outcome we want:**
1. **Same experience in Portal modal and in gnome-terminal.** Power user opens terminal locally, runs `zellij attach <session-url> --token <token>`, sees identical state. Non-technical user opens Portal CLI Agents modal, clicks Launch, sees the same.
2. **Page scrolling works for ALL providers** — Claude, Gemini, Codex, Antigravity, and any future CLI we add. No per-provider hooks required.
3. **Sessions persist** across orchestrator restarts (Zellij-server is its own systemd unit).
4. **Adding a new CLI provider is just** appending to a list — no new PATH shims, no new POST_ATTACH_HOOKS, no new env-var injection logic.
5. **Mobile-usable** in Android WebView (today's Android MVP wraps the Portal in a WebView).

**Tech stack changes:**
- ADD: `zellij` binary (Rust, single static binary, ~10 MB), `zellij-web.service` systemd unit (manages only the HTTP daemon — per-session backends spawn under Zellij's own control)
- REMOVE (eventually, after stable): `Orchestrator/cli_agent/pty_bridge.py`, `Orchestrator/cli_agent/session_manager.py` (tmux-specific code), `Orchestrator/cli_agent/path_shims/`, the xterm.js wiring in `Portal/modules/cli-agents-modal.js`
- KEEP: `Orchestrator/cli_agent/operator_config.py` (still drives per-operator config dir), `Orchestrator/routes/cli_agent_routes.py` (refactored to mint Zellij tokens instead of WebSocket-bridging), Android `cli_agent/*` (refactored to load Zellij URL in WebView)

---

## Phase 0 RESULTS — PASSED 2026-05-24

**Status: 5/5 must-pass gates green. Plan proceeds to Phase 1.** Spike conducted with Zellij v0.44.3 on dev box. S7 (Android WebView) and S9 (stress) skipped per Brandon confidence call after S6 passed — both are acceptable-degradation/should-pass tier, not blocking.

| Gate | Result | Validated mechanism |
|---|---|---|
| S3 CLI rendering (must-pass) | ✅ | All 4 CLIs (claude/gemini/codex/agy) render with native page-scrollback in Zellij web client; ZERO post-attach hooks applied. |
| S4 multi-client attach (must-pass) | ✅ | Browser session + `zellij attach` in gnome-terminal mirror bidirectionally on the same backend socket. |
| S5 persistence (must-pass) | ✅ | Killing `zellij web` HTTP daemon does NOT kill per-session backend processes; restart resumes cleanly with scrollback intact. |
| S6 Tauri webview (must-pass) | ✅ | webkit2gtk renders Zellij identically to Firefox, no CSP/WebSocket issues. Patched `src-tauri/src/main.rs` URL + `lib.rs` nav-interceptor port allowlist for spike; reverted after test. |
| S8 token mint (must-pass) | ✅ | `zellij web --create-token` works programmatically. **Caveat:** `--token-name` is mutually exclusive with `--create-token` in 0.44.3 — tokens auto-named `token_1`, `token_2`. AC2 must own the operator→name mapping. |

### 11 spike findings (folded into ACs/Tracks below)

1. **Web server is opt-in via config.** `~/.config/zellij/config.kdl` must declare `web_server true`, `web_server_port <chosen>`, `web_server_ip "127.0.0.1"`. Daemon silently no-ops without it. → Track A.
2. **HTTPS-enforcement defaults ON for localhost.** `enforce_https_for_localhost true` by default. Production install must decide cert source (Tailscale funnel cert, self-signed, or orchestrator-proxy TLS termination). → AC2/AC3.
3. **`zellij web --daemonize` is broken in 0.44.3.** Reports "Web Server started" but forked child silently dies. Use systemd `Type=simple` (no `-d`). Worth upstream issue. → Track A T2.
4. **Sha256 verification trap.** Released `.sha256sum` hashes the EXTRACTED binary (under build path `target/x86_64-unknown-linux-musl/release/zellij`), NOT the tarball. Verify-after-extract, never verify-tarball. → Track A T3.
5. **Token names auto-assigned.** `--create-token --token-name X` rejected (mutex). Orchestrator must mint, parse stdout for `(name, value)`, and own the `(operator, provider, app) → name` mapping in our own state. → Track B.
6. **Tokens stored as hash only.** `~/.local/share/zellij/tokens.db` (SQLite) keeps `token_hash` not the raw token. AC2 decides: persist raw tokens in our state (encrypted) OR mint short-lived tokens per Portal modal load + revoke after.
7. **Per-session backend processes, not single global zellij-server.** Each session has its own `zellij --server <socket>` process. systemd manages only `zellij web` daemon; session backends spawn under Zellij's own control. → AC1 clarification.
8. **Sessions survive HTTP-daemon restart → zero-downtime update path.** Killing `zellij web` doesn't touch session backends. T22 (update pipeline) inherits this for free; CLI agent sessions don't disconnect during BlackBox updates. → Track F upside.
9. **Skills, slash commands, `~/.claude/` all visible to CLI inside Zellij.** No sandboxing, no env stripping. AC1 simplifies — no `CLAUDE_CONFIG_DIR` redirection beyond what `operator_config.py` already does for tmux. → AC1.
10. **Port collisions matter.** Plan's original port 9092 is held by `Orchestrator/asterisk/audio_subprocess.py` on dev box. Track A T2 must pick a production port deliberately (spike used 9097 successfully). → Track A T1 add.
11. **xterm.js + all addons (webgl/clipboard/web-links) ship in Zellij's web client.** Zero porting needed for what the research doc called the "~50 KB of code we should copy by hand." Same xterm.js library we already use — we inherit the production wiring.

### Architectural pivot from Phase 0 — Portal OWNS the session-launch UX

After watching Brandon's S3 walkthrough, a UX gap surfaced that the original plan's "Portal modal becomes an iframe" framing didn't address: **today, the user has to remember to type `claude`, `gemini`, `codex`, or `agy` at a shell prompt inside the terminal.** That's fine for power users, friction for everyone else.

**New direction (confirmed with Brandon 2026-05-24):**
- Portal modal renders **per-provider launch buttons** ("Launch Claude session", "Launch Gemini session", etc.) — one click creates a fresh, named Zellij session AND auto-runs the CLI binary inside it.
- Portal owns the **session-switcher chrome** (list of operator's active sessions, click to switch). Zellij's native session-picker UI is bypassed — iframe always loads a specific session URL directly.
- Multiple sessions per operator allowed (parallel claude + gemini, etc.).
- **Android terminal-key overlay (Ctrl/Esc/Tab/arrows/etc.) already exists in the current Android app** — Zellij rewrite just inherits/adapts it. NO new design work for on-screen keys.

This pivot expands Track C and tightens AC3, codified in new **AC9** below.

---

## Phase 0.5 RESULTS — Production-install audit (2026-05-24)

**Status: 13 of 18 audit findings folded into plan. 5 MINOR findings deferred (acceptable).** After Phase 0 passed, the plan underwent an independent production-install completeness audit (independent code-reviewer subagent + main-session reality-check against `install.sh`). Found that the original plan was drafted from an aspirational understanding of how BlackBox actually installs in production. All CRITICAL gaps and IMPORTANT gaps closed before Phase 1 starts.

### CRITICAL fixes folded in (production-install blockers)

| ID | Gap | Resolution location |
|---|---|---|
| C0 | `bbx` user is a fiction — production runs as `$REAL_USER` | Track A (`User=$REAL_USER`); narrative sweeps |
| C1 | TLS cert source unresolved | **REFRAMED in Phase 1 T5** — AC1 was over-engineering. Customer traffic flows Tailscale MagicDNS HTTPS → orchestrator (edge TLS termination) → reverse-proxy to `127.0.0.1:9097` (HTTP). Internal localhost hop is NEVER customer-visible; HTTPS there adds zero attack-surface reduction. AC2 updated to document the corrected architecture. T1.5 cert generation removed from install.sh in commit `5ebc840` |
| C2 | First-boot ordering — orchestrator may mint before Zellij listens | Track A (systemd `After=`/`Wants=`); Track B (`web_server_healthy()` retries with backoff) |
| C3 | Reinstall semantics for `tokens.db` unspecified | Track A T3 + Track B (`zellij_state.reconcile_or_wipe()` at startup) |
| C4 | Customer-update has no health-gate or auto-fallback | Track F + Track G (code-default; `get_backend()` health-fallback to tmux on Zellij failure) |
| C5 | Sudoers grant scope incomplete for binary install + dispatcher pattern | Track A (new `blackbox-install-zellij-binary` helper + extends `blackbox-write-systemd` with `zellij-web-unit` target_kind) |

### IMPORTANT fixes folded in (first-customer-experience)

| ID | Gap | Resolution location |
|---|---|---|
| I6 | Onboarding wizard no Zellij progress UI | Track E T23.5 (retry button + status surface) |
| I7 | AC2 long-vs-short token decision not binding | AC2 (Option A stricken; Option B locked — mint-per-launch, revoke-on-kill) |
| I8 | Per-operator scope leakage in launch/delete endpoints | AC9 + Track B T8/T9 (operator-prefix gate enforcement + unit tests) |
| I9 | Customer observability outside the modal | Track C (Portal status-bar Zellij health indicator) |
| I10 | AC10 inject endpoint can write arbitrary shell as user | AC10 (CSRF token required + restrict to `provider="terminal"` + journalctl audit log) |
| I11 | tmux → Zellij flip day abandons in-progress sessions | Track F (pre-flip check for active tmux sessions; defer or banner-prompt) |
| I12 | Coexistence `.env.template` flip doesn't reach existing customers | Track G (default in code, not `.env.template`; per-customer rollout via feature flag) |

### MINOR — deferred to Phase 1 implementation

- **M13** (rollback if new Zellij binary breaks) — handle in T22 implementation.
- **M14** (zellij-server.service ↔ zellij-web.service inconsistency) — already swept globally.
- **M15** (config.kdl install-time only) — fold into Track B `ensure_config()` during T6.
- **M16** (xterm.js dep check before Track H delete) — Track H T31 implementation detail.
- **M17** (power-user `zellij attach` via Tailscale funnel routing) — Track A T2 documents the proxy mount, full wiring lands in Phase 1.

### Audit method

- Subagent: `superpowers:code-reviewer` against full plan against `install.sh`/`installer/templates/` reality.
- Main session: independent grep-and-verify against actual production systemd unit + sudoers template.
- Findings cross-referenced — every CRITICAL caught by at least one channel, several by both.

---

## Phase 1 RESULTS — PASSED 2026-05-24

**Status: 11 commits, 6 files, +550/-4 lines. T5 smoke test green: zellij-web.service active on 127.0.0.1:9097 (HTTP), sudoers grants installed + visudo-validated, end-to-end install.sh completed.**

### Final commit chain

| Commit | Task |
|---|---|
| `20f5f97` | T1 — pin v0.44.3 + document port 9097 |
| `f5b6644` | T2 — zellij-web.service unit + blackbox.service ordering |
| `48f38ba` | T2 polish — rate-limit + stable docs URL |
| `672acdb` | T4 — sudoers grants |
| `2544e37` | T4 polish — daemon-reload general-purpose note |
| `5fc785e` | T4.5 — binary-install dispatcher + zellij-web-unit target_kind |
| `8270cc8` | T4.5 fix — concurrency lock + verify restart success |
| `46c8d8d` | T3+T1.5 — install.sh step_2c_install_zellij (244 LoC) |
| `daeb4db` | T3 polish — trap-cleanup + settle delay + overwrite log |
| `e61fa0d` | T5 fix — config.kdl perm + trap unbound TMPDIR |
| `5ebc840` | T5 reframe — HTTP on localhost (AC1 was over-engineering, see AC2) |

### Two T5-discovered bugs (caught by smoke test, would have shipped otherwise)

1. **config.kdl write permission** — `sudo -u $REAL_USER cp` couldn't read root-owned mktemp file. Fixed with atomic `sudo install -m 0644 -o $REAL_USER -g $REAL_USER`. Three reviewer passes missed it because they reviewed CODE PATH, not EXECUTION CONTEXT (only surfaces when install.sh actually runs as root).
2. **TMPDIR unbound on trap** — `local TMPDIR` has function scope; when `set -e` triggered abnormal return, EXIT trap fired AFTER function scope ended, `set -u` killed it. Fixed with `"${TMPDIR:-}"` default.

### One architectural reframe surfaced by T5

**Audit C1 was over-engineering.** Original lock: install.sh generates self-signed cert + `enforce_https_for_localhost true`. Empirical reality: Zellij 0.44.3 refuses to honor `web_server_cert`/`web_server_key` config keys regardless of format. BUT — the deeper insight is that internal-hop TLS adds zero attack-surface reduction; the customer's HTTPS terminates at the Tailscale funnel, never reaches localhost. Architecture corrected: HTTP on localhost, TLS at orchestrator edge. See AC2 for full rationale.

### What's now installed on a customer box after install.sh

- `/usr/local/bin/zellij` — 0.44.3 musl binary, sha256-verified against extracted-binary hash
- `/usr/local/sbin/blackbox-install-zellij-binary` — future-update dispatcher (with flock + restart verification)
- `/etc/systemd/system/zellij-web.service` — systemd unit running as `$REAL_USER`, depends on `network-online.target`, ordered after by `blackbox.service`
- `/etc/sudoers.d/blackbox-system` — extended with Zellij grants (restart + daemon-reload + dispatcher wildcard)
- `~/.config/zellij/config.kdl` — `web_server true` on `127.0.0.1:9097`, `enforce_https_for_localhost false`
- `~/.local/share/zellij/tokens.db` — empty (reconciled per audit C3)

### Verification evidence

- `systemctl is-active zellij-web.service` → `active`
- `ss -tlnp | grep 9097` → bound by zellij PID
- `curl http://127.0.0.1:9097/` → HTTP 200 (xterm.js landing page)
- `sudo visudo -cf /etc/sudoers.d/blackbox-system` → `parsed OK`
- install.sh exit code 0; final log: "Done. Reboot to launch BlackBox Setup..."

### Pickup point for next session

Read this Phase 1 RESULTS section + Phase 2 task list (T6-T10 — Backend orchestration). First action: **T6 — write `Orchestrator/cli_agent/zellij_client.py`** per the AC API defined in Track B + `zellij_state.py` with `reconcile_or_wipe()` startup hook. All design decisions still locked from Phase 0.5 audit; subagent-driven-development continues.

---

## Phase 0 — Validation spike (DECISION GATE — 1-2 days) [HISTORICAL — see RESULTS above]

**No code commits in this phase.** Just install, test, decide. If Phase 0 fails any of the must-pass criteria below, this plan is abandoned and we fall back to A+B in the research doc.

### Spike tasks (in order)

1. **S1: Install Zellij on dev box.**
   - `cargo install --locked zellij` OR `apt install zellij` (whichever's current — check Zellij release notes for 2026-05 status)
   - Verify: `zellij --version` returns ≥0.42 (web client landed in 0.41).
2. **S2: Run web client locally.**
   - `zellij web --start --port 9092` (or whatever flag is current per docs)
   - Open http://localhost:9092 in Firefox. Confirm landing page renders.
   - Generate a token, attach to a new session via URL `/spike-test?token=...`.
3. **S3: Launch each CLI inside Zellij and validate rendering.** This is the must-pass test.
   - In Zellij web client: open new tab, run `claude`. Verify Claude TUI renders fully, can type, page scrolling works (PgUp/PgDn or trackpad scroll).
   - Same for `gemini`, `codex`, `agy`.
   - **Must-pass criterion**: ALL FOUR CLIs render correctly with working scrollback in Zellij's web client. If even ONE doesn't, document the symptom and abandon (or escalate the bug to Zellij upstream first).
4. **S4: Validate multi-client attach.**
   - Keep Zellij web client open with a `claude` session active.
   - In a gnome-terminal: `zellij attach spike-test`.
   - **Must-pass criterion**: gnome-terminal client shows the SAME claude session, can type, both clients see each other's keystrokes.
5. **S5: Validate persistence.**
   - Detach both clients. `systemctl stop zellij-server` (or whatever the right command is). `systemctl start zellij-server`. Re-attach via web client.
   - **Must-pass criterion**: Session resumes with claude still running. (If Zellij sessions die on server restart, the persistence story is broken and we lose a major reason to adopt.)
6. **S6: Validate Tauri webview compatibility.**
   - Launch the existing BlackBox Setup Tauri app (`installer/src-tauri/`). In its webview, manually navigate to http://localhost:9092 (cheat — we don't have the iframe wired yet).
   - **Must-pass criterion**: Zellij web client renders inside Tauri's webkit2gtk, can type, scroll works. If Tauri's webview blocks Zellij's WebSocket or CSP, we have an integration problem to solve (workable but adds work).
7. **S7: Validate Android WebView compatibility.**
   - Build the Android MVP. In the in-app WebView, navigate to `https://<dev-box-tailscale>:9092` (or whatever the funnel URL is). Verify rendering + typing + scrolling on a phone-sized viewport.
   - **Must-pass criterion**: same as S6 but for Android System WebView (Chromium-based). If touch input or virtual-keyboard handling is broken, Android UX is degraded but probably acceptable for v1 (Portal/desktop is primary).
8. **S8: Spike-test the token mint API.**
   - Read Zellij's web client docs on how tokens are issued/validated. Confirm we can mint a Zellij token from a Python orchestrator (HTTP POST, CLI invocation, or config-file write — whichever Zellij supports).
   - **Must-pass criterion**: we can programmatically create a token for a named session from outside Zellij (no manual login required). If Zellij only supports interactive token creation, our SSO plumbing gets ugly fast.
9. **S9: Run a stress test.**
   - Open 3 Zellij web client sessions simultaneously (different CLIs). Type, scroll, switch between them.
   - **Must-pass criterion**: no resource exhaustion, no rendering glitches between sessions. Acceptable if there's some latency under load — we just need to confirm we won't ship a broken product.

### Phase 0 exit criteria (ALL must hold) — MET 2026-05-24

- [x] S3, S4, S5, S6, S8 must-pass criteria all met
- [~] S7 deferred to Phase 4 (existing Android terminal-key overlay confirmed; WebView spike rolled into Phase 4 build)
- [x] No security blocker found
- [x] Brandon's S3 walkthrough produced an unsolicited "it just works just like I'm sitting at my computer" — preference vs current Portal modal confirmed

---

## Architectural choices (assume Phase 0 passes)

### AC1: Zellij as a separate systemd service, not a child of blackbox.service

`zellij-web.service` (the HTTP daemon) runs as its own systemd unit owned by **`$REAL_USER`** — the same user that owns `blackbox.service` (per existing install.sh pattern at line 248). Per-session backends spawn under Zellij's own control and are NOT directly managed by systemd (Phase 0 finding #7). Reasons for the separate unit:

- Survives orchestrator restarts independently (matches the persistence win + Phase 0 finding #8: zero-downtime updates for in-progress sessions).
- `ProtectSystem=strict` on `blackbox.service` doesn't affect Zellij (separate cgroup, separate mount namespace) — avoids the failure mode documented in memory `protectsystem_strict_blast_radius.md`.
- Independent restart semantics — if orchestrator OOMs, Zellij sessions live on.
- Easier to debug (separate journal, separate failure mode).

**Why `User=$REAL_USER` and NOT a dedicated `bbx` user (audit C0):** production `blackbox.service` already runs as `$REAL_USER`; shared user namespace means orchestrator can read/write `~/.local/share/zellij/tokens.db` directly with no sudo, no group-permission gymnastics, no special API. Any other user choice forces a sudo path for every orchestrator → Zellij CLI invocation, which the existing security model (`ProtectSystem=strict` + bounded NOPASSWD grants) is hostile to. The shared-user model is what makes "the BlackBox is automatically an authorized user" trivially true.

Unit: `/etc/systemd/system/zellij-web.service`. Binds 127.0.0.1 only (security-by-network). Web client exposed externally via orchestrator-fronted reverse proxy on the Tailscale funnel (existing TLS termination + auth flow at port 9091).

### AC2: Auth via Zellij CLI-minted tokens, mapped operator→name in orchestrator state

**Validated mechanism (S8):** `zellij web --create-token` mints a UUID token, prints it once on stdout, stores only its SHA-hash in `~/.local/share/zellij/tokens.db`. The `--token-name` flag is mutex with `--create-token` in 0.44.3 — tokens get auto-named `token_1`, `token_2`, etc. There is no "show me the token again" command; the raw value is irrecoverable after the initial print.

**Adapter pattern (Track B):**
1. Orchestrator runs `zellij web --create-token`, parses stdout for `(auto_name, uuid_value)`.
2. Orchestrator stores `(operator, provider, app) → (zellij_token_name, zellij_token_value)` in its own state file.
3. When the Portal modal needs to load an iframe for a session, orchestrator fetches the stored token value and embeds it in the URL.
4. On session teardown, orchestrator runs `zellij web --revoke-token <auto_name>`.

**Token lifecycle — LOCKED (audit I7): short-lived, mint-per-launch.**
- Orchestrator mints a fresh token via `zellij web --create-token` on every `POST /cli-agent/zellij/launch`.
- Token value is returned in the launch-response payload + embedded in the iframe URL.
- After the iframe successfully loads (or after a 5-minute TTL ceiling, whichever first), orchestrator revokes the token via `zellij web --revoke-token <name>`.
- `zellij_state.py` stores ONLY `token_name` (the Zellij-assigned `token_1`, `token_2`, etc.) — NEVER the raw token value. Acceptance criterion: grep `Orchestrator/cli_agent/state/` post-launch finds zero UUID-shaped strings.
- Per-open latency: sub-100ms (single CLI subprocess). Acceptable tradeoff for "no persistent raw tokens at rest" security posture.

(The previously-considered "Option A long-lived encrypted tokens" was rejected — defense-in-depth wins; the per-open latency is invisible to users.)

**TLS — RE-LOCKED in Phase 1 T5: HTTP on localhost; TLS terminated at orchestrator edge.**

Original audit C1 lock specified install.sh-generated self-signed cert at `/etc/blackbox/zellij/{cert,key}.pem` + `enforce_https_for_localhost true`. **Phase 1 T5 smoke test revealed this was over-engineering, not a bug worth fixing.**

**Why over-engineering:** The customer's traffic flow makes the internal HTTPS hop architecturally redundant. The customer's browser never reaches `127.0.0.1:9097`:

```
Customer phone/laptop
       │
       │ HTTPS (Tailscale MagicDNS cert, Tailscale-terminated)
       ▼
https://<customer>.tailnet-name.ts.net/...
       │
       │ Tailscale funnel terminates TLS
       ▼
http://localhost:9091/  (orchestrator on the BlackBox device)
       │
       │ orchestrator serves Portal HTML/JS
       │ Portal iframe src = same-origin /zellij/<session>?token=...
       │ orchestrator reverse-proxies /zellij/* → 127.0.0.1:9097
       ▼
http://127.0.0.1:9097/  (zellij-web daemon — never customer-visible)
```

The localhost-to-localhost connection between orchestrator and zellij-web is INTERNAL. TLS there is defense-in-depth — but here it adds zero attack-surface reduction (anyone with localhost access already owns the entire stack: tokens.db, session sockets, the orchestrator's own state).

**Mechanical observation from T5:** Zellij 0.44.3 refuses to honor `web_server_cert`/`web_server_key` config keys (and the `--cert`/`--key` CLI flags), citing "Cannot bind without an SSL certificate" regardless of cert path/format. No matching GitHub issues. Even if we wanted defense-in-depth TLS, the Zellij side wasn't going to cooperate cheaply.

**Locked architecture (committed `5ebc840`):**
- install.sh's `step_2c_install_zellij` writes `config.kdl` with `enforce_https_for_localhost false`.
- No cert generation. `/etc/blackbox/zellij/` directory unused (left in place from earlier audit C1 attempts; future Phase 1 polish can remove).
- zellij-web binds plain HTTP on `127.0.0.1:9097`.
- Sanity check uses `curl http://...` not `https://`.

**Customer-facing crypto is unchanged:** Tailscale MagicDNS HTTPS + orchestrator's Tailscale-funnel TLS termination at port 9091. Customer never sees the cert mismatch because they never see the cert.

**Phase 3 wiring requirement:** Portal iframe `src` must use same-origin `/zellij/<session>?token=...` (orchestrator reverse-proxies), NOT `http://localhost:9097/...` (mixed-content blocking + cross-origin issues + customer's browser isn't on the BlackBox). The existing `/app-proxy/{port}/{path:path}` route in `Orchestrator/routes/agent_routes.py:1300` is the pattern — Track C T11.6 either reuses it directly (iframe src = `/app-proxy/9097/...`) or adds a dedicated `/zellij/*` route for cleaner URLs. **One caveat for Phase 3:** the existing app-proxy is HTTP-only (httpx); Zellij's WebSocket terminal traffic will need WebSocket-aware proxying added.

**Defense-in-depth note (deferred to v1.1):** Should we choose to add internal-hop TLS later, the path is either (a) figure out the Zellij 0.44.3 cert config that the docs imply but the runtime rejects (likely an upstream bug or undocumented requirement), or (b) wait for a Zellij version that fixes it. Not blocking v1 ship.

### AC3: Portal owns the session-launcher and session-switcher UX; Zellij is the rendering substrate

**Revised after Phase 0 walkthrough (2026-05-24).** Original framing was "Portal modal becomes an iframe." Spike surfaced that users today have to remember to type `claude`/`gemini`/`codex`/`agy` at a shell prompt inside a Zellij terminal — fine for power users, friction for everyone else. Portal must own the launch + switcher chrome; the iframe loads a *specific* session (with the CLI already running), not Zellij's landing page.

**Portal modal structure (replaces today's `cli-agents-modal.js`):**

```
┌─ Portal CLI Agents modal ────────────────────────────────────────┐
│ [Launch buttons row]                                             │
│   [+ Claude] [+ Gemini] [+ Codex] [+ Antigravity] [+ Terminal]   │
│ ────────────────────────────────────────────────────────────────│
│ [Session switcher panel — left rail]                             │
│   ● claude (active)        2m ago                                │
│   ○ gemini                 8m ago    [×]                         │
│   ○ codex                  31m ago   [×]                         │
│   ○ terminal               1h ago    [×]   ← AC10                │
│ ────────────────────────────────────────────────────────────────│
│ [Optional: Shortcut palette dropdown]  ← AC10, terminal-mode only│
│ ────────────────────────────────────────────────────────────────│
│ [iframe — fills remaining modal body]                            │
│   <iframe src="http://.../session/Brandon__claude__default       │
│           ?token=..." width=100% height=100%>                    │
└──────────────────────────────────────────────────────────────────┘
```

The fifth launch button `[+ Terminal]` opens BlackBox Terminal mode — a raw shell + shortcut palette aimed at advanced users. See **AC10** for the full design.

**Launch flow (single button click):**
1. User clicks "+ Claude" → frontend POSTs to `/cli-agent/zellij/launch` with `{operator, provider}`.
2. Orchestrator:
   a. Generates session name `{operator}__{provider}__{timestamp}` (or `__root` if no app context).
   b. Creates new Zellij session via `zellij action new-tab --name <session> --layout-string '<auto-run CLI binary>'` OR `zellij --session <name> -- <binary>` (validated mechanism TBD in Phase 2 T6).
   c. Mints fresh Zellij token (or reuses operator's, per AC2 sub-decision).
   d. Returns `{session_name, session_url, token}` to frontend.
3. Frontend appends session to left-rail switcher, sets iframe `src` to the new session's URL, focuses it.
4. CLI is already running inside the session when the iframe renders — no prompt, no typing required.

**Switcher flow:**
- Click another session in left rail → frontend swaps iframe `src` to that session's URL (Portal-controlled, NOT Zellij's native picker).
- × button on session → DELETE `/cli-agent/zellij/sessions/{name}` → orchestrator calls `zellij kill-session <name>` + revokes token.
- Session list auto-refreshes via existing polling pattern in `cli-agents-modal.js` (no new transport needed).

**Bypassed:** Zellij's native session-picker UI is never shown to the user. The iframe always lands directly in a specific session. (`zellij web` does have a built-in picker, but rendering Portal chrome AROUND it doubles the UX; we render Portal chrome INSTEAD of it.)

**Token never in URL bar:** use POST + form-data iframe, OR pass token via `postMessage` after iframe load, OR use Zellij's auth cookie mechanism if it supports cross-origin setup (TBD Phase 2 T6).

**See also AC9** for the launcher/switcher contract in more detail.

### AC4: Session-per-(operator, provider, app) — same naming as today

Zellij session name = `{operator}__{provider}__{app_or_root}` (matches current tmux convention). Lets us preserve the cli_agent_routes API shape: existing endpoints like `/cli-agent/sessions?op=Brandon` keep working but query Zellij instead of tmux.

### AC5: Power user CLI attach — documented, supported

`zellij attach https://<host>/<session>?token=<token>` works out of the box per Zellij's design. Document in Portal: "Power user? Run this command in your local terminal to attach the same session."

Token in URL is acceptable here — it's the user's own token, in their own terminal. Same token they'd see in the Portal iframe.

### AC6: Provider list refactor — pure data

Today's per-provider scaffolding (PROVIDER_ARGS, POST_ATTACH_HOOKS, INSTALL_COMMANDS, _CLI_AUTH_CMD, _PROVIDER_BINARY_NAMES, SUPPORTED_PROVIDERS) collapses to ONE table:

```python
PROVIDERS = {
    "claude":      {"binary": "claude",  "package": "@anthropic-ai/claude-code", "install_cmd": ["npm", "install", "-g", "@anthropic-ai/claude-code"]},
    "gemini":      {"binary": "gemini",  "package": "@google/gemini-cli",        "install_cmd": ["npm", "install", "-g", "@google/gemini-cli"]},
    "codex":       {"binary": "codex",   "package": "@openai/codex",             "install_cmd": ["npm", "install", "-g", "@openai/codex"]},
    "antigravity": {"binary": "agy",     "package": None,                        "install_cmd": ["bash", "-c", "curl -fsSL https://antigravity.google/cli/install.sh | bash"]},
}
```

POST_ATTACH_HOOKS becomes empty (Zellij handles scroll natively for everything — that's the whole point). PROVIDER_ARGS becomes empty (no `--no-alt-screen` needed; Zellij's scrollback works regardless). Adding a 5th CLI = one line in this table.

### AC7: Migration path — coexistence during transition

For 1-2 release cycles, keep BOTH paths available:
- Old tmux/PtyBridge path stays default
- Zellij path opt-in via env var (`CLI_AGENT_BACKEND=zellij` in `.env`)
- Portal modal renders different content based on which backend is active
- After a stable period (1-2 weeks of customer use with Zellij flag enabled), make Zellij default
- One more cycle later, delete tmux code

This is non-trivial code but the cost of a botched cut-over is high (Brandon's customer would have a hard regression). Coexistence buys reversibility.

### AC8: Update pipeline — Zellij version managed like other system deps

Zellij version pinned in `installer/templates/zellij-version`. Update pipeline's apt-install bucket categorization triggers if that file changes. install.sh's Zellij install step checks version, upgrades if mismatched.

This means we control Zellij version per release. No surprise upgrades from upstream breaking our integration.

**Free win surfaced in Phase 0:** killing `zellij web` does NOT kill per-session backends. So Zellij version updates that only touch the web daemon are zero-downtime for active CLI sessions — the customer's claude session stays alive while we hot-swap the binary + restart `zellij web.service`. Document in T22.

### AC9: Launcher/switcher contract — Portal-side, Zellij-bypass

This codifies the launcher pattern referenced in AC3.

**Contract between Portal frontend and orchestrator backend:**

| Action | Portal call | Orchestrator behavior |
|---|---|---|
| Page load — get my sessions | `GET /cli-agent/zellij/sessions?op=<op>` | Lists this operator's active Zellij sessions with `{name, provider, created_at, last_activity}` |
| Click + Launch button | `POST /cli-agent/zellij/launch` body `{operator, provider, app?}` | (1) Generate session name; (2) Create Zellij session with CLI binary auto-run; (3) Mint token; (4) Return `{session_name, session_url, token, expires_at}` |
| Click session in switcher | (frontend-only — swap iframe `src`) | n/a |
| Click × on session | `DELETE /cli-agent/zellij/sessions/{name}` | Kill Zellij session + revoke token + remove from operator state |
| Backend health check (status indicator) | `GET /cli-agent/zellij/backend-status` | Returns `{web_daemon_running, session_count_total, my_session_count}` |

**State the orchestrator owns (not Zellij):**
- `(operator, provider, app) → (zellij_session_name, zellij_token_name, zellij_token_value)` mapping.
- Token expiry/revocation policy (per AC2 short-lived option).
- Session list visible to a given operator (Zellij itself has no operator concept — orchestrator filters).

**Why Portal owns the switcher chrome:**
- Operator filtering: Zellij's session list is global; Portal must filter to current operator only.
- Per-session metadata Portal needs (provider name, friendly created-at, app context) doesn't live in Zellij.
- Consistent feel with the rest of the Portal (matches today's CLI agent modal styling, dark theme, button placement).
- Avoids nested chrome: if iframe loaded Zellij's picker first, user would see TWO levels of session-list UI (Portal's outer + Zellij's inner).

**Operator-prefix gate (audit I8, security-critical):**
The orchestrator filters Zellij's global session list to the requesting operator. BUT the launch + delete + inject endpoints MUST also enforce that any `session_name` parameter starts with the current operator's prefix — otherwise Operator A could iframe-load (or kill, or inject into) Operator B's session by guessing or scraping a session name.

- `POST /cli-agent/zellij/launch` — only generates names with `{current_operator}__` prefix; can't be overridden by request.
- `DELETE /cli-agent/zellij/sessions/{name}` — rejects with 403 if `name` doesn't start with `{current_operator}__`.
- `POST /cli-agent/zellij/inject` (AC10) — same prefix check + the extra restrictions in AC10.
- `GET /cli-agent/zellij/sessions?op=X` — orchestrator IGNORES the query `op=X` and uses session-authenticated operator. (Pre-existing operator-auth pattern; the `?op=X` parameter is informational only.)

Documented limitation: a malicious operator with shell access on the device could `zellij attach <other-op-session>` directly from their gnome-terminal — accepted (single-tenant device); the Portal-side path is what we close. Unit test in Track B T9 verifies cross-operator attempts return 403.

**Android port of this UX:** Track D inherits the launcher button row, switcher panel, AND the existing on-screen terminal-key overlay (confirmed already present in Android Portal). Touch ergonomics: launch buttons stacked vertically on phone aspect ratio; switcher collapsible (hamburger menu) when not in use.

### AC10: BlackBox Terminal — raw shell + extensible shortcut palette (added 2026-05-24)

**A FIFTH launch button alongside the four CLI-provider buttons** — `[+ BlackBox Terminal]` — targeting advanced users who want full Zellij power without being scoped to a single CLI. Born from Brandon's observation that the four CLI buttons cover the curated-product path well but leave no door open for the power-user workflow of "I want a raw shell on this device with all my aliases handy."

**Differs from the four CLI-provider buttons:**

| | CLI provider button ("+ Claude") | BlackBox Terminal button |
|---|---|---|
| Session creation | Fresh session named `{op}__claude__<ts>` per click | Single session named `{op}__terminal` (reused per operator) — TBD Phase 1 sub-decision |
| Auto-launched binary | The CLI binary (claude/gemini/codex/agy) | None — default user shell (bash) |
| Session list shown | This operator's CLI sessions only | All operator's sessions + ability to attach to any existing Zellij session via the prompt |
| Shortcut palette | Hidden | Visible — dropdown of system + user shortcuts |
| Audience | Non-technical → power-user crossover | Power-user / advanced mode |

**Shortcut palette UX:**
- Dropdown widget pinned at the top of the BlackBox Terminal pane (above the iframe; T11.7 finalizes exact placement).
- Click → grouped menu:
  - **System group** (shipped with install): `claude`, `gemini`, `codex`, `agy`, `sudo systemctl restart blackbox.service`, etc.
  - **User group** (operator-defined): custom aliases the operator has added.
- Selecting a shortcut → orchestrator POSTs `zellij action write-chars <text>` to the active session → text appears at the user's prompt.
- **No auto-execute.** User reviews the populated text and presses Enter manually. Prevents accidental destructive commands and gives review before commit.
- `+ Add shortcut` button at bottom of dropdown → operator-scoped add (label + command); persists to operator state.
- `× Delete` available on user-group entries (system entries are read-only).

**Shortcut storage:**
- **Scope:** per-operator. Brandon's aliases ≠ another operator's aliases. Aligns with rest of operator-config plumbing (`operator_config.py`).
- **Format:** YAML. Single-line aliases v1. Multi-line scripts deferred to v2 — for now, put the script in `~/scripts/` and add an alias that invokes it.
- **Location:**
  - System defaults: `installer/templates/shortcuts/system.yaml` (shipped in install, never edited by operator)
  - User additions: `Orchestrator/cli_agent/state/shortcuts/{operator}.yaml`
- **Example file:**
  ```yaml
  shortcuts:
    - label: "Launch Claude"
      command: "claude"
    - label: "Restart BlackBox"
      command: "sudo systemctl restart blackbox.service"
    - label: "SSH into MSO2"
      command: "ssh bbx@192.168.1.171"
  ```
- **Parameters (variable substitution like `ssh {host}`):** deferred to v2. v1 is literal strings only.

**Backend endpoints (Track B addition):**
- `GET /cli-agent/zellij/shortcuts?op=X` — returns merged `{system: [...], user: [...]}` shortcut lists for operator X.
- `POST /cli-agent/zellij/shortcuts` body `{label, command}` → adds user shortcut for current operator (write to `state/shortcuts/{op}.yaml`).
- `DELETE /cli-agent/zellij/shortcuts/{id}` → removes user shortcut.
- `POST /cli-agent/zellij/inject` body `{session_name, text}` → orchestrator runs `zellij --session <session_name> action write-chars <text>`. Returns `{success, error?}`. **Security gates (audit I10):**
  - **CSRF token required** in request header (existing Portal CSRF pattern; reject without it).
  - **Operator-prefix gate** (AC9): `session_name` must start with `{current_operator}__`; reject 403 otherwise.
  - **Provider restriction:** target session must have `provider="terminal"` in orchestrator state — CLI-provider sessions (claude/gemini/codex/agy) reject inject. Prevents an attacker from injecting `rm -rf /` into the user's running claude session.
  - **Sanitization:** escape but DO NOT strip — the user explicitly authored this text via the shortcut palette UI. Trust intent, but log everything.
  - **Audit log:** every inject call writes a journalctl entry with `(operator, session_name, text_first_120_chars, timestamp)`. Customer + support can review post-incident.

**Cross-session-attach affordance:** because the BlackBox Terminal is just a raw shell, the user can type `zellij attach <other-session-name>` themselves to jump to any existing session — same power they get from gnome-terminal. Surface this in the shortcut palette as a system shortcut: `"Attach to session"` → populates `zellij attach ` (with trailing space, user fills in the name).

**Sub-decisions deferred to Phase 1 T1:**
- Single reused `{op}__terminal` session per operator OR fresh `{op}__terminal__<ts>` per click? Reused is cleaner (user's shell history persists) but requires "already exists" handling.
- Shortcut palette placement: top toolbar above iframe vs floating button vs left-rail addition. T11.7 decides via design pass.
- Whether system shortcuts include `+ Claude` / `+ Gemini` / etc. — feels redundant with the dedicated launch buttons, BUT lets the user re-launch a CLI inside the SAME raw terminal session (no new Zellij session). Probably yes, with a note in the label distinguishing semantics.

---

## Tracks (parallel work streams — assignable to subagents)

Track names match the antigravity plan convention. Subagent execution per `superpowers:subagent-driven-development`.

### Track A — install.sh + systemd unit + Zellij binary deployment

**Files:**
- Modify: `Scripts/install.sh` — new step `step_2c_install_zellij` (after MCP venv, before sudoers)
- Create: `installer/templates/zellij-web.service` — systemd unit for the `zellij web` HTTP daemon (NOT a per-session server — see Phase 0 finding #7). `User=$REAL_USER` (matches `blackbox.service` pattern — see audit C0).
- Create: `installer/templates/zellij-config.kdl` — opt-in config template (see Phase 0 finding #1)
- Create: `installer/templates/zellij-version` — single line, pinned version (start at `0.44.3` — validated)
- Create: `installer/templates/blackbox-install-zellij-binary.sh` — root-owned dispatcher (audit C5; mirrors `blackbox-apt-install`/`blackbox-write-systemd` pattern). Validates target version against pinned `zellij-version`, downloads from GitHub, sha256-verifies extracted binary, installs to `/usr/local/bin/zellij`. Update pipeline calls this; orchestrator namespace can't write `/usr/local/bin` directly per `ProtectSystem=strict`.
- Modify: `installer/templates/blackbox-write-systemd.sh` — add `zellij-web-unit` to whitelisted target_kinds (audit C5).
- Modify: `installer/templates/sudoers-blackbox-system` — add NOPASSWD grants for: `systemctl restart zellij-web.service`, `systemctl reload-daemon`, `/usr/local/sbin/blackbox-install-zellij-binary *`. All use `REAL_USER_PLACEHOLDER` per existing template convention.
- Modify: `Scripts/install.sh` — add `After=zellij-web.service` + `Wants=zellij-web.service` to the `blackbox.service` unit it generates (audit C2 — orchestrator startup ordering).

**Behavior (revised per Phase 0 findings + production audit C0/C1/C2/C3/C5):**

1. **Download:** fetch `https://github.com/zellij-org/zellij/releases/download/v{ver}/zellij-x86_64-unknown-linux-musl.tar.gz` + `.sha256sum`. Use the `musl` build (statically linked, no glibc concerns across customer distros).
2. **Verify:** extract tarball FIRST, then sha256sum the extracted binary against the `.sha256sum` file. The `.sha256sum` hashes the EXTRACTED binary (under CI path `target/x86_64-unknown-linux-musl/release/zellij`), NOT the tarball — verify-tarball will always fail. → Phase 0 finding #4.
3. **Install:** move binary to `/usr/local/bin/zellij` mode 0755.
4. **TLS posture (REFRAMED in Phase 1 T5 — see AC2):** no cert generation. The customer's browser never touches `127.0.0.1:9097`; TLS terminates at the Tailscale funnel before reaching the orchestrator, and orchestrator reverse-proxies same-origin to localhost. Internal-hop TLS adds zero attack-surface reduction. install.sh just logs `[install] TLS posture: HTTP on localhost (TLS terminated at orchestrator edge — see plan AC2)`.
5. **Write config:** install `~$REAL_USER/.config/zellij/config.kdl` with `web_server true`, `web_server_ip "127.0.0.1"`, `web_server_port <PORT>`, `web_sharing "on"`, `enforce_https_for_localhost false`. Daemon silently no-ops without `web_server true`. → Phase 0 finding #1, #2.
6. **Choose port deliberately (audit M14):** port 9092 (originally in plan) collides with `Orchestrator/asterisk/audio_subprocess.py`. **Locked: port 9097** (validated in spike, adjacent to orchestrator's 9091, far from Apps range 8060-8099 and UGV's 8082). Document choice in `zellij-version` template comment. → Phase 0 finding #10.
7. **systemd unit (zellij-web.service):** `Type=simple` running `/usr/local/bin/zellij web` with NO `--daemonize` flag. The `--daemonize` flag is broken in 0.44.3 — forked child silently dies. → Phase 0 finding #3. **`User=$REAL_USER` (audit C0)**, `Restart=always`, no `PrivateTmp` or `ProtectSystem` (avoid the namespace traps documented in memory `cli_agent_systemd_hardening.md`).
8. **No service for session backends.** Per-session `zellij --server <socket>` processes spawn under Zellij's own control when the first client connects to a new session. systemd does not manage them. → Phase 0 finding #7.
9. **First-boot ordering (audit C2):** `blackbox.service` gets `After=zellij-web.service` + `Wants=zellij-web.service`. Orchestrator does not attempt token operations until `zellij_client.web_server_healthy()` returns True (with retries — see Track B). If Zellij is unhealthy after retries, orchestrator boots in degraded mode and surfaces this via `cli_agent_status`.
10. **Reinstall reconciliation (audit C3):** `step_2c_install_zellij` checks for mismatch between `~$REAL_USER/.local/share/zellij/tokens.db` (Zellij's hash store) and `Orchestrator/cli_agent/state/zellij_sessions.json` (orchestrator's mapping). If one exists without the other, both are wiped clean. Factory-reset of either is treated as "reset both." Orchestrator also runs `zellij_state.reconcile_or_wipe()` at startup — install-time AND runtime defense.
11. **Customer-update integration (audit C5):** the update pipeline (Track F) does NOT invoke `install.sh` directly for Zellij binary swaps. Instead it calls `sudo blackbox-install-zellij-binary <version>` — the dispatcher validates the version matches the new pinned `zellij-version` and handles download + verify + install + `systemctl restart zellij-web.service`. Orchestrator namespace stays clean of `/usr/local/bin/` writes.
12. **Idempotent overall:** skip download if `zellij --version` already matches pinned version. config.kdl assertion moves to orchestrator startup (`ensure_config()` in `zellij_client.py` — audit M15) so version bumps with new required config fields get refreshed without re-running install.sh. install.sh seeds the initial file for first-boot ordering.

### Track B — Backend orchestration

**Files:**
- Create: `Orchestrator/cli_agent/zellij_client.py` — Python adapter for Zellij CLI invocations + state-mapping owner + `ensure_config()` (audit M15) + `web_server_healthy()` with backoff retry (audit C2)
- Create: `Orchestrator/cli_agent/zellij_state.py` — persists `(operator, provider, app) → (session_name, token_name)` mapping. **Per audit I7: NEVER stores raw token values.** Includes `reconcile_or_wipe()` called at orchestrator startup (audit C3) to detect tokens.db ↔ state-file mismatch and clean-slate both if needed.
- Modify: `Orchestrator/routes/cli_agent_routes.py` — add Zellij-based endpoints (parallel to existing tmux endpoints during coexistence). All new endpoints enforce operator-prefix gate (audit I8).
- Modify: `Orchestrator/cli_agent/path_extension.py` — keep PATH augmentation logic (still useful for Zellij child processes — Zellij does NOT sandbox env, so existing nvm-aware bin resolution still applies)
- Keep: `Orchestrator/cli_agent/operator_config.py` (unchanged, still used for `CLAUDE_CONFIG_DIR` per-operator; Phase 0 finding #9 confirms `~/.claude/` visible inside Zellij sessions)

**`zellij_client.py` API (validated mechanisms from Phase 0):**

```python
# Token operations — CLI-driven, Option B short-lived per audit I7
def mint_token() -> tuple[str, str]:
    """Run `zellij web --create-token`, parse stdout, return (auto_name, uuid_value).
    Auto_name will be 'token_1', 'token_2', etc. — Zellij assigns.

    SECURITY: caller MUST embed uuid_value in launch-response payload and forget
    it. NEVER persist uuid_value to disk. State stores auto_name only.
    """

def revoke_token(name: str) -> None:
    """Run `zellij web --revoke-token <name>`. Idempotent. Called after iframe
    successfully loads OR after 5-minute TTL ceiling (whichever first) per
    Option B short-lived policy.
    """

def list_tokens() -> list[dict]:
    """Run `zellij web --list-tokens`. Returns [{name, created_at}, ...].
    Note: actual token values are not retrievable post-mint (hash-only storage,
    Phase 0 finding #6).
    """

# Session operations
def launch_session(name: str, binary: str | None = None, args: list[str] = None) -> None:
    """Create a new Zellij session named `name`. If `binary` provided, auto-run it
    in the session (CLI provider path); if None, default user shell (BlackBox
    Terminal path per AC10).
    Mechanism: `zellij --session <name> -- <binary> <args...>` (validated in
    Phase 2 T6 alongside layout-string-injection fallback).
    """

def list_sessions() -> list[dict]:
    """Run `zellij list-sessions --no-formatting`. Returns [{name, created_at}, ...].
    Global list — orchestrator filters by operator via name prefix (audit I8).
    """

def kill_session(name: str) -> None:
    """Run `zellij kill-session <name>`. Idempotent."""

def inject(session_name: str, text: str) -> None:
    """Run `zellij --session <session_name> action write-chars <text>`. Used by
    AC10 shortcut palette. Caller must enforce I10 security gates (CSRF check,
    operator-prefix gate, provider=terminal restriction, audit log) BEFORE
    calling this — adapter itself only does the mechanical inject.
    """

# Health + config (audit C2 + M15)
def web_server_healthy(retries: int = 5, backoff_seconds: float = 2.0) -> bool:
    """Curl http://127.0.0.1:<port>/ with retries. Returns True on first HTTP 200.
    On first orchestrator boot, Zellij may still be starting; retry with backoff
    for up to ~10 seconds. Lighter than `zellij web --status` which has a
    `--port`-flag-respect bug in 0.44.3.
    """

def ensure_config() -> None:
    """Idempotently assert ~/.config/zellij/config.kdl has all required fields
    for the current Zellij version. Called at orchestrator startup so version
    bumps with new required fields get applied without re-running install.sh
    (audit M15). install.sh seeds the initial file; orchestrator owns updates.
    """
```

**`zellij_state.py` — orchestrator-owned mapping (per Phase 0 finding #5 + audit I7):**
- State file: `Orchestrator/cli_agent/state/zellij_sessions.json` (or sqlite, TBD T6)
- Schema per row: `{operator, provider, app, session_name, token_name, created_at, expires_at}`
- **NO `token_value` column.** Option A (long-lived) was rejected per audit I7; Option B locked. Raw token UUIDs exist only transiently in launch-response payloads.
- **`reconcile_or_wipe()` called at orchestrator startup** (audit C3): compares this state file vs Zellij's `tokens.db`. If session names exist here but their token_names are missing from Zellij's list OR vice-versa, wipe both clean. Logs the wipe to journalctl so operators see why their sessions disappeared.

**New endpoints (revised per AC9 contract):**
- `POST /cli-agent/zellij/launch` — body `{operator, provider, app?}` → creates session + mints token + returns `{session_name, session_url, token, expires_at}`. `provider` accepts `claude`, `gemini`, `codex`, `agy`, OR `terminal` (raw shell — AC10).
- `GET /cli-agent/zellij/sessions?op=X` — lists this operator's active sessions with provider/created_at metadata
- `DELETE /cli-agent/zellij/sessions/{name}` — kills session + revokes its token
- `GET /cli-agent/zellij/backend-status` — `{web_daemon_running, session_count_total, my_session_count}`

**AC10 endpoints (shortcut palette, terminal mode):**
- `GET /cli-agent/zellij/shortcuts?op=X` — returns `{system: [...], user: [...]}` merged shortcut list (system from `installer/templates/shortcuts/system.yaml`, user from `Orchestrator/cli_agent/state/shortcuts/{op}.yaml`)
- `POST /cli-agent/zellij/shortcuts` body `{label, command}` → adds user shortcut for current operator
- `DELETE /cli-agent/zellij/shortcuts/{id}` → removes user shortcut
- `POST /cli-agent/zellij/inject` body `{session_name, text}` → orchestrator runs `zellij --session <session_name> action write-chars <text>`. Escape but DO NOT strip — user explicitly authored this text.

**Additional files for AC10:**
- Create: `Orchestrator/cli_agent/shortcuts.py` — load/merge/persist shortcut YAML
- Create: `installer/templates/shortcuts/system.yaml` — shipped defaults (5 CLI launches + BlackBox restart + SSH-into-session template, etc.)
- Create dir: `Orchestrator/cli_agent/state/shortcuts/` — per-operator user shortcut files land here

**Old endpoints kept:** `/cli-agent/sessions` (tmux-backed), `/cli-agent/ws/{id}` (tmux-backed) — still functional, used when `CLI_AGENT_BACKEND=tmux` (the default during coexistence).

### Track C — Portal modal (launcher + switcher + Zellij iframe target)

**Revised after Phase 0 (2026-05-24).** Original framing was "iframe variant" — a dumb iframe wrapping Zellij's UI. Now Portal owns the launcher buttons and session-switcher chrome (per AC3/AC9); the iframe targets a specific session URL.

**Files:**
- Modify: `Portal/modules/cli-agents-modal.js` — branch on backend: if Zellij, render new launcher+switcher+iframe layout; if tmux, render xterm.js (existing path during coexistence)
- Create: `Portal/modules/cli-agents-zellij-launcher.js` — provider launch buttons + POST to `/cli-agent/zellij/launch` + iframe `src` swap
- Create: `Portal/modules/cli-agents-zellij-switcher.js` — left-rail session list (polls `GET /cli-agent/zellij/sessions`), click-to-switch, × to kill
- Create: `Portal/modules/cli-agents-zellij-iframe.js` — iframe lifecycle (initial load, error/disconnect states, optional token-refresh)
- Create: `Portal/modules/cli-agents-zellij-shortcuts.js` — AC10 shortcut palette (dropdown widget, click-to-populate via POST `/cli-agent/zellij/inject`, add/delete user shortcuts). Only mounted when active session is `provider="terminal"`. CSRF token attached to every inject POST per audit I10.
- Modify: `Portal/styles/features/_cli_agents_modal.css` — three-region layout (launcher row top, switcher rail left, iframe fills rest), iframe sizing, dark theme to match terminal background, shortcut palette dropdown styling
- **Add (audit I9): top-level Portal status-bar health indicator** (separate from in-modal indicator). Place near the operator-name pill in the existing status-bar pattern. Polls `/cli-agent/zellij/backend-status` + `/cli-agent/backend` together; displays:
  - Green dot: Zellij active + healthy
  - Yellow dot: configured as Zellij but `get_backend()` is fallback-returning `tmux` (audit C4) — customer sees "CLI Agent backend degraded" tooltip
  - Red dot: backend offline — modal won't open
  - Hidden: customer is on default tmux (no UI clutter for non-power users during coexistence)

**UX preservation:**
- Same provider radios become **launch buttons**: one click creates a session AND auto-runs the CLI binary (no shell prompt to remember).
- Same disconnect/× button (closes Zellij session if user opts in, otherwise just hides from switcher).
- NEW: per-session metadata in switcher (provider name, "started 2m ago", app context if any).
- NEW: small "Open in terminal" CTA showing `zellij attach <session-name>` command for power users to attach from gnome-terminal.
- NEW: Zellij-server health indicator at top of modal (green dot / red dot, fed by `/cli-agent/zellij/backend-status`).

**What's intentionally NOT in Portal (handled by Zellij iframe):**
- Terminal rendering (xterm.js is inside Zellij now, not Portal).
- Scrollback (Zellij's native scrollback, works for all providers).
- Copy/paste (Zellij's clipboard addon).
- Pane splitting / tabs (Zellij's native — Portal doesn't expose, but power users see it in the iframe).

### Track D — Android MVP integration (REVISED 2026-05-25)

**Architectural correction (Brandon 2026-05-25):** the original Track D sketch assumed "WebView wraps Portal." That was wrong. The Android app at `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/` is a **full native Kotlin/Compose frontend** that mirrors the Portal but is built separately. The terminal itself is rendered via **Termux's native `TerminalView`** (NOT WebView) with a Kotlin WebSocket client feeding bytes directly to `TerminalEmulator.append()`. WebView is only a fallback. So Track D is not "swap iframe URL" — it's "**add native zellij protocol client + native session-switcher UI**".

**Current Android files (15 native components for CLI Agents):**
- `ui/cli_agent/CliAgentScreen.kt` (92 lines) — top-level state machine: picker → terminal → back
- `ui/cli_agent/TerminalScreen.kt` (697 lines) — Termux `TerminalView` wrapper, WebSocket bytes → `TerminalEmulator.append()`, keystroke forwarding
- `ui/cli_agent/ExtraKeysBar.kt` (445 lines) — scrollable bottom keyboard bar (Ctrl/Esc/Tab/arrows/F-keys) — **untouched, dispatches `KeyEvent` to parent**
- `ui/cli_agent/CliAgentWebSocket.kt` — current tmux-bytes WebSocket client. Hits `/cli-agent/ws/{session_id}`. **Replaced by ZellijWebSocketClient in T18.**
- `ui/cli_agent/CliAgentSessionRepository.kt` (63 lines) — session state, list/spawn/kill
- `ui/cli_agent/AppFolderPicker.kt` — provider+folder picker (current empty state) — **demoted to optional flow accessible via launch-button long-press**
- `ui/cli_agent/CliAgentToolsButton.kt`, `WhisperMicButton.kt` — auxiliary
- `data/model/CliAgentModels.kt` (88 lines) — provider enum, session DTOs
- `data/api/BlackBoxApi.kt` (search and confirm in T17) — REST client
- `navigation/NavGraph.kt` — top-level navigation

**Brandon's UX decisions (2026-05-25):**

1. **Terminal substrate**: keep Termux `TerminalView`. Write a Kotlin client for zellij's web-client WebSocket protocol that swaps in where `CliAgentWebSocket` is today.
2. **Session-switcher entry point**: tap top-bar session name (e.g. `Brandon__claude__root`) → dropdown showing all active sessions + "+ Terminal" + "Shortcuts ▼".
3. **CLI Agents in nav**: ADD as a new hamburger menu item alongside the existing Apps Folder item (do not replace).
4. **Empty state**: centered "+ Terminal" + "Shortcuts ▼" buttons (mirrors Portal's empty modal). AppFolderPicker becomes optional, reachable via long-press on "+ Terminal" for "choose folder" flow.

**Layout sketch:**
```
┌─────────────────────────────────┐
│ ☰  Brandon__claude__root  ▼     │  ← top bar — tap = dropdown
├─────────────────────────────────┤
│                                 │
│  <Termux TerminalView fills>    │
│                                 │
│                                 │
│                                 │
├─────────────────────────────────┤
│ Ctrl Esc Tab Shift  ← → ↑ ↓ ... │  ← ExtraKeysBar (unchanged)
└─────────────────────────────────┘

Dropdown when tapped:
┌────────────────────────────┐
│ ● Brandon__claude__root    │ ← current
│ ○ Brandon__gemini__root    │
│ ○ Brandon__terminal        │
│ ────────────────────────── │
│ ➕ + Terminal              │
│ ⚡ Shortcuts ▼             │
└────────────────────────────┘
```

**Coexistence with the legacy tmux path (audit 2026-05-25):** `/cli-agent/ws/{session_id}` does NOT call `_require_zellij_backend()` (see `cli_agent_routes.py:160`), so the existing Android app's tmux WebSocket bridge keeps working against ANY orchestrator regardless of that orchestrator's `CLI_AGENT_BACKEND` setting. The Android app is a portable client — it connects to whatever orchestrator `origin` URL it's pointed at, and the two backend endpoint families (`/cli-agent/ws/*` for legacy tmux and `/cli-agent/zellij/*` for Phase 2+) coexist server-side. Phase 4 is therefore upgrade-by-choice, not break-and-fix.

**Tasks (10-15 dev days total):**

| # | Task | Files | Days | Notes |
|---|------|-------|------|-------|
| T17 | Probe + document zellij web-client WS protocol (websocat capture against dev box + cross-ref zellij rust source `zellij-server/src/web_server/...`) | spike doc → `docs/notes/2026-05-25-zellij-ws-protocol.md` | 1-2 | De-risks T18. Document frame layout, auth handshake (cookie or query token), control messages, resize semantics. |
| T18 | Implement `ZellijWebSocketClient.kt` (new) — `connect(sessionUrl, token)`, `sendBytes(bytes)`, `sendResize(cols, rows)`, `onBytes(callback)`, `close()`. Mirror surface of `CliAgentWebSocket.kt` so the swap in `TerminalScreen.kt` is one constructor line. | new `data/api/ZellijWebSocketClient.kt`, unit tests | 2-3 | Lock to zellij 0.44.3; add version probe + warn if mismatch. |
| T19 | Extend `CliAgentSessionRepository.kt` — add `launchSession(provider): Session` (POST `/cli-agent/zellij/launch?op={op}`), `listSessions()` (GET `/cli-agent/zellij/sessions?op={op}`), `killSession(name)` (DELETE `/cli-agent/zellij/sessions/{name}?op={op}`). Update `Session` model to carry `session_url` + `token`. | modify repo + DTOs | 1 | Reuse existing operator-resolution helpers. |
| T20 | `SessionSwitcherTopBar.kt` (new composable) — current session label + ▼ chevron, dropdown rendered via Material 3 `DropdownMenu` or custom bottom sheet, hosts active-sessions list + "+ Terminal" + "Shortcuts ▼" entries. Wire to repository. | new composable + modify CliAgentScreen | 2 | Touch target ≥48 dp; honor system status bar inset. |
| T21 | Empty-state launch buttons — replace/wrap the picker branch of CliAgentScreen with centered "+ Terminal" + "Shortcuts ▼" big buttons (mirror Portal). On tap: call repository.launchSession() + transition to TerminalScreen. Long-press "+ Terminal" → fall through to existing AppFolderPicker for "choose folder first" flow. | modify CliAgentScreen + new EmptyStateLaunchButtons composable | 1 | |
| T22 | Hamburger menu integration — add "CLI Agents" entry to NavGraph nav rail/drawer alongside existing Apps Folder item. Tap → CliAgentScreen. | modify NavGraph + the menu source | 0.5 | |
| T23 | Device QA on Brandon's Z Fold 6 (`100.111.152.112`) pointing at the dev-box orchestrator first, then any reachable production box: all 4 shortcuts launch + render, switcher dropdown opens/switches, ExtraKeysBar still works, suspend/resume preserves session, dropdown closes on outside-tap. The same APK should work against either backend host. | none (validation) | 1-2 | |
| T24 | Plan + memory updates — close out Track D in this plan with file-paths + commit refs; mint snapshot capturing Phase 4 architecture. | docs only | 0.5 | |

**Critical reuse (no-touch):**
- `TerminalScreen.kt` Termux bridge — keep its `TerminalView` setup, `TerminalEmulator.append()` byte feeding, IME wiring, and key-event forwarding. Only the WebSocket source swaps.
- `ExtraKeysBar.kt` (445 lines) — dispatches `KeyEvent` to the TerminalView regardless of bytes source.
- `WhisperMicButton.kt`, `CliAgentToolsButton.kt` — auxiliary, untouched.

**Risks + mitigations:**
- **Risk:** zellij's web-client WS protocol is undocumented + rust-internal; may shift between minor versions. **Mitigation:** lock to 0.44.3 in `installer/templates/zellij-version` (already done — Phase 1); T17 captures actual frame layout from running daemon; T18 adds version probe + WARN if remote daemon ≠ pinned version.
- **Risk:** zellij's protocol may include framing/auth that's not trivial in vanilla Kotlin WebSocket clients (`okhttp.WebSocket`). **Mitigation:** T17 spike resolves before T18 starts. If protocol is too complex, fall back to WebView for the terminal area only (still keeps native switcher/launcher chrome — hybrid model).
- **Risk:** existing customers on MSO2-like deployments who already use the tmux Android app could be confused by two parallel CLI flows (legacy tmux still works, new zellij is the recommended path). **Mitigation:** when Phase 4 ships, hide the legacy entry point + auto-route all CLI flows through Zellij; tmux path remains available only via direct WebSocket URL for backwards compat.
- **Risk:** TerminalView resize behavior may differ from xterm.js, causing Zellij to send mis-sized frames. **Mitigation:** test in T23 with phone rotation, foldable unfold (Z Fold 6 is a real concern here), and IME open/close.

**Test plan (T23 acceptance):**
1. Open hamburger → tap "CLI Agents" → see empty state with "+ Terminal" + "Shortcuts ▼".
2. Tap "+ Terminal" → bash session launches in Termux view; ExtraKeysBar functional; type `ls`, see output.
3. Tap top-bar session name → dropdown opens; tap "Shortcuts ▼" → expand list of Claude/Gemini/Codex/Antigravity.
4. Tap "Claude" → new claude session launches; switcher dropdown now shows 2 sessions (terminal + claude).
5. Tap the terminal session in dropdown → switch back; verify terminal output preserved.
6. Long-press "+ Terminal" → AppFolderPicker opens; pick a folder; launch terminal with `cwd` set.
7. Suspend app (home button) + resume → terminal output intact; ExtraKeysBar functional.
8. Rotate phone (or unfold Z Fold 6) → terminal resizes correctly; zellij sees new size.
9. Kill session via dropdown → session list updates; if it was current, transition back to empty state.
10. Point the SAME APK at the dev-box origin AND any production-box origin (Tailscale-reachable) to confirm the app is genuinely portable — no per-host config required.

### Track E — Onboarding wizard

**Files:**
- Modify: `Portal/onboarding/steps/cli_agents.js` — install detection no longer needs to verify CLI binary works in tmux; just needs binary exists + Zellij-web daemon is healthy
- Update copy: remove references to "tmux", remove mentions of per-provider scrollback hacks
- Add: small status indicator showing Zellij-web is running (green check) or down (red, with fix CTA)
- **Add (audit I6): "Retry Zellij install" remediation button.** When Zellij failed to install during install.sh (network down at install time, sha256 mismatch, etc.), the customer's wizard step shows red with a "Retry" button. Button POSTs to `/onboarding/install-zellij` → orchestrator invokes `sudo blackbox-install-zellij-binary <pinned_version>` (the Track A C5 helper) + streams status back to the wizard. Mirrors the existing CLI binary install pattern at `onboarding_routes.py:756` (`INSTALL_COMMANDS`).

### Track F — Update pipeline integration

**Files:**
- Modify: `Orchestrator/update/changes.py` — new bucket `zellij` triggered when `installer/templates/zellij-version` or `zellij-web.service` files change
- Modify: `Orchestrator/update/runner.py` — handle `zellij` bucket via `sudo blackbox-install-zellij-binary <ver>` (Track A C5 helper) — NOT raw sudo or direct binary writes. Helper handles download + verify + install + restart in one bounded call.
- Modify: sudoers grants (already covered by Track A's NOPASSWD grants for `systemctl restart zellij-web.service` + `blackbox-install-zellij-binary *`).

**Flip-day handling (audit I11):** when Phase 7 T29 flips a customer from tmux to Zellij, the update runner first checks for active tmux CLI sessions via existing `cli_agent_routes` introspection. If any are present:
- **Option A (deferred flip):** runner notes "Backend upgrade pending" in journal + Portal banner; flip applies on next orchestrator restart when no tmux sessions are active.
- **Option B (banner-prompted flip):** Portal shows a banner "CLI Agent backend upgrade ready — close active sessions to apply" with a "Close & upgrade" CTA. Customer-driven flip.
- Recommend Option B for v1 — explicit customer consent beats silent state-loss.

**Backend health-fallback (audit C4):** if `CLI_AGENT_BACKEND=zellij` is set (in code default or in customer `.env`) but `zellij_client.web_server_healthy()` returns False after retries, `get_backend()` (Track G) returns `tmux` + logs a LOUD warning to journalctl + surfaces an alert in the Portal status bar (audit I9). Prevents a Zellij outage from silently killing the customer's CLI Agent functionality.

### Track G — Coexistence + migration

**Files:**
- Modify: `Orchestrator/cli_agent/__init__.py` — export `get_backend()` that returns `zellij` IF code-default says so AND `web_server_healthy()` returns True (audit C4 health-fallback). Customer `.env` can override with explicit `CLI_AGENT_BACKEND=tmux` to opt out.
- Modify: All endpoint dispatchers to branch on backend (via `get_backend()` not direct env-var read)
- Add: `/cli-agent/backend` GET endpoint returning current backend AND why (for Portal frontend to decide which UI to render + surface fallback state)
- Add: `/cli-agent/migrate-tmux-to-zellij` POST endpoint — for ops, walks current tmux sessions and recreates them in Zellij (if backend just switched). Customer-facing flow uses banner-prompted explicit-consent flip per Track F.

**Default-flip mechanic (audit C4 + I12):** the default-backend value lives in CODE, not `.env.template`. `get_backend()` returns the code default unless customer's `.env` explicitly overrides. This means:
- Existing customer `.env` files (which predate the `CLI_AGENT_BACKEND` variable) silently pick up the new default on update.
- T28 in Phase 7 is a one-line code change (`DEFAULT_BACKEND = "zellij"`) + a feature-flag rollout (per-customer via existing config infra, not a file edit).
- Customers who DON'T want auto-flip add `CLI_AGENT_BACKEND=tmux` to their `.env` BEFORE the rollout reaches them.

**Coexistence period: minimum 2 weeks.** Default-in-code stays `tmux` during this period. Brandon manually flips dev box to Zellij first, then his own MSO2, then customers via per-customer feature flag.

### Track H — Documentation + decommission (LAST, after Zellij is default for 2+ weeks)

**Files:**
- DELETE: `Orchestrator/cli_agent/pty_bridge.py`
- DELETE: `Orchestrator/cli_agent/session_manager.py` (or shrink to a no-op compat shim if anything still imports it)
- DELETE: `Orchestrator/cli_agent/path_shims/` (gone — Zellij doesn't need it for these CLIs)
- DELETE: `POST_ATTACH_HOOKS` table from session_manager
- DELETE: `PROVIDER_ARGS` table from cli_agent_routes
- DELETE: Old WebSocket endpoint `/cli-agent/ws/{id}` and its xterm.js consumer code in Portal
- DELETE: All tmux-specific diagnostic endpoints (`/onboarding/cli-agent/reset-tmux`, `/onboarding/cli-agent/pane-content`, etc.) — they reference tmux directly
- KEEP for archaeology: 1-paragraph note in `docs/architecture/cli-agent-history.md` explaining the tmux era

---

## Critical reuse

| Need | Existing pattern | File:line |
|---|---|---|
| Operator config dir per-CLI | `OperatorConfig.env_for(op)` | `Orchestrator/cli_agent/operator_config.py:42` |
| Provider install commands | `INSTALL_COMMANDS` table | `Orchestrator/routes/onboarding_routes.py:756` |
| Auth status detection | `cli_agent_status()` endpoint | `Orchestrator/routes/onboarding_routes.py:808` |
| Update pipeline bucket categorization | `changes.py:categorize()` | `Orchestrator/update/changes.py` |
| Helper-script sudoers pattern | `blackbox-write-systemd` + bounded NOPASSWD grant | `installer/templates/blackbox-write-systemd.sh` + `sudoers-blackbox-system` |
| Onboarding step wiring | Existing CLI agents step | `Portal/onboarding/steps/cli_agents.js` |
| Tailscale funnel for HTTPS exposure | E1-era infrastructure | install.sh Step 4 |
| Per-operator session naming | `cli-agent-{op}__{prov}__{app}` | `Orchestrator/cli_agent/session_manager.py:82` (reuse the format string, just point at Zellij instead of tmux) |
| **Background-task pattern for non-blocking ops** | `asyncio.create_task` in update runner | `Orchestrator/update/runner.py` |
| **Status-aware button (works during transitions)** | Restart Service button on done step | `Portal/onboarding/steps/done.js` |

---

## Tasks (executable order)

### Phase 0 (DECISION GATE)

| T# | Task | Owner | Deliverable |
|---|---|---|---|
| T0 | Spike S1–S9 per Phase 0 above | Brandon (with claude assist if needed) | Pass/fail decision + spike notes |

### Phase 1 (Infrastructure, assuming Phase 0 passes)

| T# | Task | Track | Depends on |
|---|---|---|---|
| T1 | Pin Zellij version + add `installer/templates/zellij-version` (start at 0.44.3, validated). Document port 9097 choice in comment | A | T0 |
| T1.5 | **(audit C1)** Add TLS cert generation to `step_2c_install_zellij`: openssl self-signed at `/etc/blackbox/zellij/{cert,key}.pem`, CN=localhost, 10-year, mode 0600, owner `$REAL_USER:$REAL_USER`. Idempotent skip if present + valid + non-expired | A | T1 |
| T2 | Write `installer/templates/zellij-web.service` systemd unit with `User=$REAL_USER` + `After=network-online.target` + (in blackbox.service) `After=zellij-web.service`/`Wants=zellij-web.service` per audit C0+C2 | A | T0 |
| T3 | Write `step_2c_install_zellij` in install.sh (download + extract-then-verify-sha256 of inner binary + install + config write + systemd write/enable + tokens.db reconciliation per audit C3) | A | T1, T1.5, T2 |
| T4 | **(audit C5)** Add Zellij sudoers grants to template: `systemctl restart zellij-web.service`, `systemctl reload-daemon`, `/usr/local/sbin/blackbox-install-zellij-binary *`. All use `REAL_USER_PLACEHOLDER` per existing convention | A | (parallel) |
| T4.5 | **(audit C5)** Write `installer/templates/blackbox-install-zellij-binary.sh` (mirror `blackbox-write-systemd` pattern: validate version against pinned, download, sha256-verify extracted binary, atomic move to `/usr/local/bin/zellij`, restart `zellij-web.service`). Add `zellij-web-unit` target_kind to `blackbox-write-systemd.sh` | A | T2, T3 |
| T5 | Smoke-test install.sh on dev box — `sudo bash Scripts/install.sh` → zellij-web.service active, cert files present, sudoers valid via `visudo -c`, orchestrator can curl `https://127.0.0.1:9097/` | A | T3, T4, T4.5 |

### Phase 2 (Backend — minimal Zellij integration)

| T# | Task | Track | Depends on |
|---|---|---|---|
| T6 | Write `Orchestrator/cli_agent/zellij_client.py` per the audited API: `mint_token() -> (name, value)` (audit I7 Option B), `revoke_token(name)`, `launch_session(name, binary?, args?)`, `list_sessions()`, `kill_session(name)`, `inject(session, text)`, `web_server_healthy(retries, backoff)` (audit C2), `ensure_config()` (audit M15). Write `zellij_state.py` with `reconcile_or_wipe()` startup hook (audit C3). State schema stores `token_name` only, NEVER `token_value` | B | T5 |
| T7 | Add `get_backend()` helper in `Orchestrator/cli_agent/__init__.py` (audit C4+I12: default in CODE, customer `.env` override only; health-fallback to `tmux` if configured as `zellij` but `web_server_healthy()` False after retries; LOUD warning on fallback) | B, G | T6 |
| T8 | Add new Zellij endpoints to `cli_agent_routes.py` (launch, sessions, backend-status, shortcuts, inject). **All endpoints enforce operator-prefix gate per audit I8** — session_name params must start with `{current_operator}__`, else 403 | B | T6, T7 |
| T9 | Unit tests for zellij_client.py + cli_agent_routes.py: (1) mock Zellij CLI, validate mint/launch/kill, (2) **operator-prefix gate rejects cross-operator session_name with 403** (audit I8), (3) inject rejects non-`terminal`-provider sessions (audit I10), (4) inject without CSRF header rejects (audit I10), (5) state file post-launch contains zero UUID-shaped strings (audit I7 acceptance) | B | T6 |
| T10 | curl-test the new endpoints on dev box with `CLI_AGENT_BACKEND=zellij` | B | T8 |

### Phase 3 (Portal — launcher + switcher + iframe)

| T# | Task | Track | Depends on |
|---|---|---|---|
| T11 | Write `Portal/modules/cli-agents-zellij-iframe.js` (iframe lifecycle — initial load, error/disconnect, optional token refresh) | C | T8 |
| T11.5 | Write `Portal/modules/cli-agents-zellij-launcher.js` — provider launch buttons + POST `/cli-agent/zellij/launch` + iframe `src` swap on success | C | T8 |
| T11.6 | Write `Portal/modules/cli-agents-zellij-switcher.js` — left-rail session list (poll `GET /cli-agent/zellij/sessions`), click-to-switch (frontend iframe `src` swap), × button (DELETE session) | C | T8 |
| T12 | Modify `cli-agents-modal.js` to branch on backend: if Zellij, compose launcher+switcher+iframe; if tmux, render existing xterm.js path | C, G | T11, T11.5, T11.6 |
| T13 | Add CSS for three-region layout in `_cli_agents_modal.css` (launcher row top, switcher rail left, iframe fills rest) | C | T11 |
| T14 | Add "Open in terminal" CTA showing `zellij attach <session-name>` command for power users | C | T11.6 |
| T14.5 | Add Zellij-server health indicator (green/red dot at top of modal, fed by `/cli-agent/zellij/backend-status`) | C | T8 |
| T14.6 | **AC10:** add `[+ Terminal]` button to launcher row + extend `/cli-agent/zellij/launch` to accept `provider="terminal"` (creates session named `{op}__terminal`, no auto-launched binary, default shell) | B, C | T8, T11.5 |
| T14.7 | **AC10:** write `Orchestrator/cli_agent/shortcuts.py` + system + user YAML loaders/writers, plus `installer/templates/shortcuts/system.yaml` (5 CLI launches, BlackBox restart, attach-to-session template, etc.) | B | T6 |
| T14.8 | **AC10:** add shortcuts endpoints — `GET /cli-agent/zellij/shortcuts`, `POST /cli-agent/zellij/shortcuts`, `DELETE /cli-agent/zellij/shortcuts/{id}`, `POST /cli-agent/zellij/inject` | B | T14.7 |
| T14.9 | **AC10:** write `Portal/modules/cli-agents-zellij-shortcuts.js` — dropdown widget, mounted only when active session is `terminal`. Click-to-populate via inject endpoint; add/delete user shortcuts via shortcuts endpoints | C | T14.6, T14.8 |
| T15 | Smoke-test Portal modal with `CLI_AGENT_BACKEND=zellij` on dev box — verify (1) launch buttons (incl. Terminal) create sessions with correct binary/shell, (2) switcher swaps iframe correctly, (3) × kills cleanly, (4) multiple concurrent sessions work, (5) **AC10 shortcut palette injects text into prompt, user-add/delete persists** | C | T12, T13, T14, T14.5, T14.6, T14.9 |
| T16 | Cross-browser smoke-test (Firefox, Chromium) | C | T15 |

### Phase 4 (Android MVP)

| T# | Task | Track | Depends on |
|---|---|---|---|
| T17 | Audit existing Android CLI agent activity + on-screen terminal-key overlay (Brandon confirmed both exist Phase 0). Locate filenames, confirm keystroke-injection target is rewireable from xterm.js → WebView | D | T8 |
| T17.5 | Add Zellij URL pattern to Android `Constants.kt` | D | T17 |
| T18 | Modify CLI agent activity to load Zellij URL in WebView instead of WebSocket-driven custom view. Rewire existing on-screen key overlay to send keystrokes to the WebView | D | T17, T17.5 |
| T19 | Token plumbing (URL query param or POST + form-data per AC2 decision in T6) | D | T17.5 |
| T19.5 | Port launcher + switcher UI from Portal to Android (launch buttons as touch targets; switcher collapses to hamburger on phone, stays as rail on tablet) | D | T18 |
| T20 | Build APK + smoke-test on Brandon's Z Fold — verify (1) launch buttons work, (2) on-screen key overlay sends Ctrl+C/Esc/arrows correctly to Zellij, (3) switcher works, (4) virtual keyboard + overlay co-exist without UI overlap | D | T18, T19, T19.5 |

### Phase 5 (Update pipeline + onboarding)

| T# | Task | Track | Depends on |
|---|---|---|---|
| T21 | Add `zellij` bucket to `changes.py` categorizer (triggers on `installer/templates/zellij-version` or `zellij-web.service` changes) | F | T6 |
| T22 | Wire `zellij` bucket into runner.py: invoke `sudo blackbox-install-zellij-binary <ver>` (T4.5 dispatcher per audit C5), NOT raw sudo. Pre-flip check for active tmux sessions (audit I11): if present, defer flip OR raise banner-prompt CTA. Post-restart health-check the new daemon | F | T21, T4.5 |
| T23 | Update onboarding `cli_agents.js` step to show Zellij-web health via `/cli-agent/zellij/backend-status` | E | T8 |
| T23.5 | **(audit I6)** Add "Retry Zellij install" remediation button to onboarding step. POSTs to new `/onboarding/install-zellij` endpoint → orchestrator invokes `sudo blackbox-install-zellij-binary <pinned>` + streams status. Surfaces install failure with actionable CTA | E | T4.5, T23 |
| T24 | Update wizard copy (remove tmux references in user-facing strings) | E | T23 |

### Phase 6 (Brandon-dogfooded validation, 1 week)

| T# | Task | Track | Depends on |
|---|---|---|---|
| T25 | Flip Brandon's dev box to `CLI_AGENT_BACKEND=zellij`, use daily for 1 week | F0 | T15, T22 |
| T26 | Flip MSO2 to Zellij, run customer-scenario tests (Antigravity OAuth, etc.) | F0 | T25 |
| T27 | Decision point: Zellij stable enough to be the default? | F0 | T25, T26 |

### Phase 7 (Roll out to customers as default)

| T# | Task | Track | Depends on |
|---|---|---|---|
| T28 | Flip default `CLI_AGENT_BACKEND` to `zellij` in `.env.template` | F0 | T27 |
| T29 | Push update — customers receive Zellij default on next update | F0 | T28 |
| T30 | Watch for 2 weeks; any rollbacks → flag for investigation | F0 | T29 |

### Phase 8 (Decommission, only after Phase 7 stable for 2+ weeks)

| T# | Task | Track | Depends on |
|---|---|---|---|
| T31 | Delete tmux/PtyBridge code per Track H | H | T30 |
| T32 | Delete xterm.js wiring from cli-agents-modal.js | H | T30 |
| T33 | Remove `CLI_AGENT_BACKEND` env var (Zellij always) | H | T31, T32 |
| T34 | Snapshot the milestone + write 1-paragraph archaeology note | H | T33 |

---

## Verification

### Per-phase verification

**Phase 0:** Brandon's subjective experience using Zellij with all 4 CLIs. Documented in spike notes.

**Phase 1:** `systemctl status zellij-web.service` shows active on dev box. `zellij --version` returns pinned version. `curl http://localhost:<zellij-port>` returns Zellij landing page.

**Phase 2:** `curl -s http://localhost:9091/cli-agent/zellij/start -d '{"operator":"Brandon","provider":"claude"}' | jq` returns `{url, token, session_name}`. Visiting that URL+token in browser opens a working Claude session.

**Phase 3:** Open Portal → CLI Agents → Launch claude → claude UI renders in iframe with scrolling. Repeat for gemini, codex, antigravity.

**Phase 4:** Android app's CLI agent picker → select Claude → claude renders in WebView with on-screen keyboard working + scrollback.

**Phase 5:** Push a fake `installer/templates/zellij-version` bump → trigger update → orchestrator downloads new Zellij + restarts service → version reflects new value.

**Phase 6:** Brandon successfully uses Zellij-backed CLI Agents for 1 week with no regressions vs. current tmux-backed experience.

**Phase 7:** Customer base receives Zellij default without support tickets spiking.

**Phase 8:** Test suite passes after tmux code deletion. No imports of deleted modules.

### End-to-end ship verification

```bash
# On fresh install
sudo bash Scripts/install.sh
sleep 5
systemctl is-active blackbox.service zellij-web.service
# both should be active

# Portal flow
curl -s http://localhost:9091/cli-agent/backend
# {"backend":"zellij"}

curl -s -X POST http://localhost:9091/cli-agent/zellij/start \
    -H "Content-Type: application/json" \
    -d '{"operator":"Brandon","provider":"claude"}'
# {"url":"http://localhost:9092/Brandon__claude___root","token":"<jwt>","session_name":"Brandon__claude___root"}

# Open returned URL in browser → working Claude session with scrolling

# Power-user attach
zellij attach http://localhost:9092/Brandon__claude___root --token <jwt>
# Same session in gnome-terminal
```

---

## Rollback strategy

**If Phase 6 or Phase 7 fails badly (regressions on customer hardware):**

- Customer's `.env` flips `CLI_AGENT_BACKEND=tmux` (one-line change)
- Service restart loads old code path
- Customer's experience reverts to today's tmux-backed Portal modal — fully functional, just back to today's known limitations

This is why coexistence (AC7) matters. Without it, a Zellij regression would require an emergency hotfix push + customer recovery flow (we just lived that today and know it's painful).

**If Zellij itself becomes problematic post-deployment:**

- Coexistence period bought us reversibility
- Brandon ships an update that flips the default back to tmux
- Subsequent update can remove Zellij entirely if needed (it's a separate systemd unit + binary; cleanup is mechanical)

---

## Out of scope (Deferred)

- **Migration of existing tmux sessions to Zellij**: kill them, user starts fresh. Pre-Zellij sessions weren't persistent across major architecture changes anyway. (Documented behavior, not a regression.)
- **Multi-tenant Zellij**: each operator has separate Zellij namespace via session naming. We don't need true multi-tenancy isolation in v1.
- **Custom Zellij theme / branding**: web client UI is Zellij's — we accept that for v1. Branding refinement is a v2 task.
- **WhatsApp/SMS integration with Zellij sessions**: this customer (Eric) asked; not the right architecture layer.
- **Zellij plugin system** (their WASM plugins): interesting but not needed for v1; would add complexity.
- **Replacing the Tauri Debian app's terminal**: the Tauri app already loads Portal HTML in its webview; once Portal modal uses Zellij iframe, Tauri benefits automatically. No separate Tauri rework needed.

---

## References

- Research doc: `docs/plans/2026-05-24-cli-agent-terminal-architecture-research.md`
- Zellij: https://github.com/zellij-org/zellij
- Zellij web client docs: https://zellij.dev/documentation/web-client
- VS Code PtyService (reference for flow control + persistence patterns): https://github.com/microsoft/vscode/blob/main/src/vs/platform/terminal/node/ptyService.ts
- Memory: `protectsystem_strict_blast_radius.md` (relevant — Zellij as separate unit sidesteps this entirely)
- Memory: `xdg-open-mime-defaults-drift.md` (irrelevant under Zellij — Zellij is its own browser-opener context)
- Memory: `cli_agent_systemd_hardening.md` (relevant — Zellij doesn't have the same hardening conflicts since it's its own unit)
- Plan precedent: `docs/plans/2026-05-22-antigravity-cli-integration.md` (followed for format consistency)
