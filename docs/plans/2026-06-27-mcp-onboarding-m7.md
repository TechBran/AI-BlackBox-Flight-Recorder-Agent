# M7 — One-Click MCP Onboarding Implementation Plan

> **For Claude:** execute milestone-by-milestone; each step leaves the app launchable. Backend = pytest. Adversarial-review the privileged milestones (M7.3 MCP reload, M7.4 sudoers/actuators) before commit.

**Goal:** Make standing up the remote MCP server (service running + public Funnel + `BLACKBOX_MCP_PUBLIC_URL` + per-operator tokens + ready-to-paste client configs) one guided onboarding flow for any new user, "as easy as possible."

**Architecture:** New `mcp` onboarding section in the existing Portal wizard/hub (4-place lockstep contract). New backend `/mcp/*` router for token mint/list/revoke + connection + status. The MCP server gains a writable-config source (`Manifest/mcp_runtime.json`) + a localhost `/internal/reload` so the public URL and tokens go live with no restart. Privileged box actions (service start/restart, Funnel-up) run via six narrow new sudoers grants mirroring the existing tailscale-serve perimeter; the unit *install*+enable is installer-time (root, once) and onboarding only **detects** it.

**Decisions (Brandon):** one-click automation; token **hot-reload** (no restart). Funnel-up keeps the locked explicit public-exposure confirm.

**Key constraint:** backend runs `ProtectSystem=strict` → `/etc` read-only in its namespace (even via sudo). So all `/etc` writes (unit install, `systemctl enable`) are installer-only; runtime is `/etc`-free (tailscaled socket, PID-1 transitions, `Manifest/` writes).

**Spec of record:** the mechanism design in the workflow output (sudoers lines, install.sh heredoc, the `_load_runtime_public_url`/`/internal/reload` code, the automate-vs-installer table). This plan is the execution order.

---

## M7.1 — Backend token + connection API (no UI)
**Files:** Create `Orchestrator/routes/mcp_routes.py` (`APIRouter(prefix="/mcp")`); mount in `Orchestrator/app.py:125-126`; reuse mint logic from `MCP/deploy/mint_token.py:70-110,129`. Test: `Orchestrator/tests/test_mcp_routes.py`.
- `POST /mcp/tokens {operator}` → `{token}` once. Validate `operator ∈ GET /operators` (400 if not); `"bbmcp_"+secrets.token_urlsafe(48)`; merge-write `Manifest/mcp_tokens.json` 0600 (never clobber other operators); refuse `"system"`/blank.
- `GET /mcp/tokens?operator=<op>` → `[{token_id, operator}]` (sha256:+12 via the server's `_token_id` convention; NEVER the secret).
- `DELETE /mcp/tokens {token_id}` → `{ok}`; re-chmod 0600.
- `GET /mcp/connection?operator=<op>` → `{public_url, has_token, per_app_configs}` (4 paste snippets pre-filled; token only on a fresh mint, never re-served in connection).
- **Test:** mint→list(token_id only)→delete; non-roster operator→400; store stays 0600 and other operators' tokens preserved.

## M7.2 — Public-URL auto-derivation + `GET /mcp/status`
**Files:** `Orchestrator/routes/mcp_routes.py` (+ a small `Orchestrator/onboarding/mcp_probe.py` helper); write `Manifest/mcp_runtime.json`.
- Derive public URL: `tailscale status --json` → `Self.DNSName` (strip trailing dot) + `:8443` → `https://…`; fallback to `Manifest/mcp_runtime.json` then `""`.
- `POST /mcp/public-url` (or fold into funnel-up) → atomic-write `Manifest/mcp_runtime.json` (0644) then `POST 127.0.0.1:9093/internal/reload`.
- `GET /mcp/status` → `{mcp_up, funnel_up, tokens_present, oauth_ready, public_url}` via: `curl 127.0.0.1:9093/mcp`→401; `tailscale funnel status --json` parse `:8443→9093`; read token store; `curl 127.0.0.1:9093/.well-known/oauth-authorization-server`→200.
- **Test:** pytest with `tailscale`/`curl` subprocess mocked — derived URL matches; 4 booleans map to mocked probes.

## M7.3 — MCP server writable-config + `/internal/reload` (URL + token hot-reload) — REVIEW
**Files:** `MCP/blackbox_mcp_server.py` (CRLF → Python byte-splice). Test: `MCP/test_mcp_runtime_reload.py`.
- `BLACKBOX_MCP_RUNTIME_FILE` + `_load_runtime_public_url()` (precedence ENV > file > `""`); `BLACKBOX_MCP_PUBLIC_URL = _load_runtime_public_url()` at `:1303`.
- Module-level handle to the `BearerAuthMiddleware` instance (set in `build_http_app`).
- `POST /internal/reload` (localhost-only + host-check 403): rebind `BLACKBOX_MCP_PUBLIC_URL` **and** `BLACKBOX_MCP_RESOURCE_URL` together; `mw.token_map.clear(); mw.token_map.update(_validate_token_operators(_load_token_map()))` (in-place — middleware reads by reference). Register in `oauth_routes` (`:2354-2367`).
- **Test:** write runtime file → reload → discovery returns new URL (not 503); mint token to store → reload → `_match_token` finds it (no reconstruction). **Existing 71 MCP tests stay green.** Adversarial review: localhost-only enforced; reload can't set arbitrary values (only re-reads disk); both globals rebound together.

## M7.4 — Privileged actuators: sudoers + Funnel + service control + installer — REVIEW
**Files:** `installer/templates/sudoers-blackbox-system` (+6 lines); `Scripts/install.sh` (blackbox-mcp.service heredoc-tee install+enable at bootstrap, after the zellij step ~:470); `Orchestrator/onboarding/mcp_actuator.py` (mirror `tailscale_actuator.py:272-292`); endpoints `POST /mcp/service/{start,restart}`, `POST /mcp/funnel/up` (explicit-confirm body), detection in `GET /mcp/status`.
- 6 grants (literal-arg, `/etc`-free): `tailscale funnel --bg --https=8443 9093`, `funnel status --json`, `funnel reset`, `systemctl {start,restart,stop} blackbox-mcp.service`. **NOT** `enable` (installer-only).
- Funnel-up: requires `{confirm:true}`; runs `sudo -n /usr/bin/tailscale funnel --bg --https=8443 9093`; then write runtime file + `/internal/reload`.
- **Validate** `visudo -c` on the rendered template. **Caveat:** this dev box's `NOPASSWD: ALL` masks the perimeter — document that the fresh-box grants are template-validated, not live-verified here.
- Adversarial security review of every grant (blast radius, no wildcards, confirm gate on Funnel).
- **Test:** actuator argv exactly matches the grant tokens; funnel-up without `confirm` → rejected.

## M7.5 — Frontend MCP card + 4-place lockstep
**Files:** Create `Portal/onboarding/steps/mcp.js` (mirror `steps/web_search.js`); `Portal/onboarding/onboarding.js:5-28` (`STEPS`+`STEP_LABELS`, slot `mcp` after `cli_agents`); `Portal/onboarding/status.js:20-31` (`SECTIONS`); `Orchestrator/onboarding/state.py:30-51` (`StepName`+`ALL_STEPS`).
- Card: operator picker (from `/operators`); status pips (`GET /mcp/status`); one-click **Set up service** / **Expose via Funnel** (confirm modal) / **Mint token** (reveal masked + copy); auto-filled public URL; 4 client-config snippets (Claude Code/Codex/Antigravity from `mint_token.py:146`/`REMOTE_SETUP.md`; claude.ai Desktop = URL only). Guided fallback when `LoadState=not-found` (re-run installer).
- **Test:** `pytest Orchestrator/tests/test_onboarding_steps_parity.py` green; card renders + copies in-browser.

## M7.6 — Status-rollup integration (hub tile live)
**Files:** `Orchestrator/onboarding/status_rollup.py:32-43` (`SECTIONS` `mcp`, group `network`) + `_derive_mcp`; `Orchestrator/routes/onboarding_routes.py:600-678` (`mcp` input in `_collect_status_inputs`) + `:712-737` (`elif key=="mcp":` live probe in SSE).
- `_derive_mcp`: ready iff service up + token present (+ funnel up + oauth ready → ready; missing pieces → attention; funnel/oauth optional pre-exposure).
- **Test:** pytest — `build_status` yields `mcp` section; `GET /onboarding/status` includes it; SSE re-emits after probe.

## M7.7 — End-to-end (Brandon)
Fresh-operator path on his box: pick operator → mint → reveal+copy → paste into Claude Code → connect → list tools (operator-isolated). Status grid all-green; OAuth/Desktop connects via URL only; mint a 2nd operator's token → it works with no restart (hot-reload proof).

---

**Three-surfaces note:** onboarding home is Portal → build the card Portal-only; `/mcp/*` API is surface-agnostic so Android/WebView can adopt later without a contract change (matches the uplift design's Portal-only-hub non-goal).
