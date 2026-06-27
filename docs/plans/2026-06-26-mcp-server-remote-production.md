# BlackBox MCP Server — Remote Production Plan

> **For Claude:** after Brandon locks the OPEN DECISIONS, execute via subagent-driven-development on main. M0 (Twilio removal) is independent and can land immediately. Backed by a 6-angle multi-agent audit (2026-06-26); mcp 1.23.1 SDK facts + all file:line cites are code-verified.

**Goal:** Make the BlackBox MCP tool server production-quality and REMOTELY CONNECTABLE by URL + auth over Tailscale, so Claude Code, Claude Desktop, OpenAI Codex, and Google Antigravity can each register it and use the BlackBox tools. Plus: remove the Twilio SMS path (TG200 is the only supported pathway).

## CURRENT STATE
`MCP/blackbox_mcp_server.py` (~1149 lines) is STDIO-ONLY on the official `mcp` SDK (v1.23.1, lean MCP/venv). Exposes the ToolVault `mcp` group — 74 of 85 modules — as flat MCP tools via get_mcp_tools().
- list_tools() is registry-driven (healthy) — tracks the live catalog (tool_registry.py:242,297).
- call_tool() is a hand-maintained 770-line if/elif chain (messy half, 231-1010) — ~30 bespoke branches + a catch-all (985) re-deriving the whole list per unmatched call. Dispatch/schema-drift surface.
- Execution split: local byte-offset file tools read the volume directly; everything else proxies to the backend (POST /local/tools/execute, /gmail/execute).
HARD LIMITS: (1) no remote transport — stdio subprocess only; (2) zero auth; (3) no exposure policy — all 74 flat incl. action/spend tools; (4) mint_snapshot mis-implemented (POSTs /chat not /chat/save); (5) coarse Error:{str(e)} + print-only logging; (6) dual-venv coupling (lean venv dodges mcp-starlette vs fastapi-0.118 conflict); (7) stale README. (send_sms is NOT in the mcp group — not currently MCP-exposed.)

## TARGET ARCHITECTURE
Transport: STREAMABLE HTTP, served by the already-installed SDK — no new heavy dep. mcp 1.23.1 already ships streamable_http.py, streamable_http_manager.py, fastmcp/, and a full auth/ submodule (confirmed in MCP/venv). NOT SSE (deprecated in Claude Code; not the Codex/Antigravity path).
Topology — recommend (A):
- (A) STANDALONE remote MCP process [recommend] — host StreamableHTTPSessionManager in a uvicorn/Starlette app on a dedicated port (e.g. 9093), bound 0.0.0.0, behind `tailscale serve` TLS at https://<host>.ts.net/mcp. Preserves lean-venv isolation; keeps proxying execution to backend :9091 over localhost; decouples MCP uptime from backend restarts.
- (B) Mount /mcp inside the FastAPI Orchestrator — single-process but re-introduces the mcp-starlette vs fastapi conflict the lean venv avoids. NOT recommended.
Backend reach: collapse the 770-line if/elif into a thin UNIFORM DISPATCHER — keep local file tools, route all others through /local/tools/execute (+ /gmail/execute); add asyncio.wait_for timeouts + a forwarded Authorization header.
REACHABILITY CAVEAT: `tailscale serve` is TAILNET-ONLY — reachable by Claude Code/Codex/Antigravity + the Claude Desktop app on tailnet devices, but NOT claude.ai WEB (Anthropic cloud), which needs Tailscale FUNNEL (public).

## AUTH OVER TAILSCALE
Single long-lived static BEARER TOKEN, validated at the transport edge, over Tailscale — matches the "Tailscale IS the boundary" stance, satisfies 3 of 4 apps natively. High-entropy token -> .env BLACKBOX_MCP_TOKEN; reject non-matching Authorization: Bearer (constant-time). BIND OPERATOR TO THE TOKEN server-side (not caller-asserted) so a remote caller can't spoof an operator + read another's snapshots. Claude Desktop is the exception (OAuth-only UI).

## PER-APP CONNECTION
- Claude Code (CLI) — YES native. `claude mcp add --transport http blackbox https://<host>.ts.net/mcp --header "Authorization: Bearer $BLACKBOX_MCP_TOKEN"`. Note 60s first-byte min + 5-min idle abort -> slow media tools must return a task_id + be polled (get_task_status exists), not block.
- OpenAI Codex (CLI) — YES native, REQUIRES a flag. ~/.codex/config.toml: experimental_use_rmcp_client = true (#1 silent-failure trap — without it HTTP servers silently fail), [mcp_servers.blackbox] url=... bearer_token_env_var="BLACKBOX_MCP_TOKEN".
- Google Antigravity (IDE) — YES native. JSON config: key is serverUrl (NOT url) + headers.Authorization. UNCERTAIN: exact config-file path is community-sourced (~/.gemini/config/mcp_config.json) — verify live; schema is high-confidence.
- Claude Desktop / claude.ai web — PARTIALLY BLOCKED. Connector UI does OAuth 2.1 (DCR/PKCE) ONLY — no static-bearer/header field (Anthropic issue #112 "closed as not planned"). Recommended: mcp-remote (npx) local stdio SHIM that injects the bearer header + presents the remote server to Desktop locally. claude.ai web can't reach a tailnet-only URL at all (needs Funnel).
NET: one static bearer over Tailscale natively serves Code + Codex + Antigravity; Desktop via mcp-remote shim; web out of scope under the tailnet boundary.

## EXPOSURE POLICY (security)
NOT "all 74 tools, system operator." Recommended:
- Separate mcp_external ALLOWLIST, READ-ONLY DEFAULT — snapshot read/search, media analysis, get_*/list_*, web search safe by default.
- OPT-IN for side-effecting + spend tools — gmail_send, control_phone, control_android_device, use_computer, destructive Workspace tools, generation (image/video/music = real API spend) behind an explicit enable flag.
- OPERATOR-BOUND TOKEN — today read tools pass operator='' (span ALL operators); fine locally, an exfiltration vector remotely.

## TWILIO REMOVAL (M0 — independent, tiny)
In ToolVault/tools/send_sms/executor.py: (1) delete the Twilio block lines 51-83; (2) delete `import aiohttp` (line 2, Twilio-only); (3) reword docstring (line 7) TG200-only; (4) KEEP from_number (drives SIM/gateway selection in send_manual, not Twilio); (5) optional: fix schema.json's wrong "160-char/10-segment" wording (actual: 1600-char truncate, single send); (6) README.md:93 -> TG200-only. Do NOT touch config.py TWILIO_* or the make_phone_call/voice stack (voice calls, still in use). Graceful failure already holds: Asterisk-down -> "TG200 SMS failed: No gateway available" (no silent fallback) — verify post-edit.

## WORK BREAKDOWN (cheapest/least-risky first)
1. M0 — Twilio removal. The 6 edits. One commit. Independent of all MCP work.
2. M1 — Dispatcher collapse + mint fix + error/logging hardening (no transport change, testable over stdio). Uniform /local/tools/execute dispatcher; mint_snapshot -> /chat/save; structured error envelope (isError + code); leveled/structured logs w/ request+operator IDs; timeouts. Biggest correctness win, de-risks everything after.
3. M2 — Streamable HTTP transport (standalone process, localhost-only, NO auth yet). Prove tool list + a read tool + a proxied tool over HTTP.
4. M3 — Bearer auth + operator binding + mcp_external allowlist (read-only default).
5. M4 — Tailscale exposure + per-app smoke. `tailscale serve` -> https://<host>.ts.net/mcp; live-connect + run one tool from EACH of Claude Code, Codex, Antigravity, Claude Desktop (via mcp-remote). Confirm the Codex flag + the Antigravity config path live.
6. M5 — Docs. Rewrite README to the 74-tool catalog + real URIs; ship per-app config snippets.
7. M6 (optional, gated) — OAuth 2.1 for native Claude Desktop / web (only if the shim is insufficient; pulls in the SDK auth/ provider + DCR/PKCE + Funnel for web).

## OPEN DECISIONS FOR BRANDON
- Serving topology? (A) standalone uvicorn process [recommend] / (B) mount /mcp in FastAPI.
- Auth model? (A) static bearer over Tailscale now [recommend] / (B) OAuth 2.1 up front.
- Claude Desktop support? (A) mcp-remote shim [recommend] / (B) OAuth 2.1 + Funnel for web / (C) skip Desktop.
- External tool surface? (A) mcp_external allowlist, read-only default, opt-in for action/spend [recommend] / (B) expose all 74.
- Operator on remote calls? (A) token->fixed operator [recommend] / (B) keep caller-asserted.
- Smoke scope before "production"? (A) all four apps live on the real box [recommend] / (B) Claude Code only.

## Uncertainty flags
Antigravity config-FILE PATH is community-sourced (verify live); the serverUrl+headers schema is high-confidence. Claude Desktop OAuth-only + no static-bearer field is doc-verified (Anthropic issue #112). claude.ai web != tailnet-reachable is a network fact. (Two audit angles — transport-design + auth-tailscale — hit the structured-output cap; their content was recovered via the server-audit + per-app angles + synthesis.)

## LOCKED DECISIONS (Brandon, 2026-06-26)
- **Tool surface: FULL 74-tool catalog externally** (no allowlist) — all action/spend tools exposed. Mitigations that do NOT reduce capability: per-credential OPERATOR BINDING (a token/OAuth identity can only act as its bound operator) + FULL per-call AUDIT LOGGING + strong auth. Note for build: still scope reads to the bound operator (no operator='' span externally).
- **Claude Desktop: OAuth 2.1 + Tailscale FUNNEL (public)** — enables native Desktop AND claude.ai web. So M6 (OAuth 2.1: DCR + PKCE/S256, callback https://claude.ai/api/mcp/auth_callback) is now IN-SCOPE (not optional), and M4 uses Tailscale FUNNEL (public internet) not just `serve` (tailnet). Auth must support BOTH static bearer (Code/Codex/Antigravity) AND OAuth (Desktop/web).
- Defaults taken: standalone process topology; token/OAuth-bound operator; smoke all four apps.
- **Order:** M0 Twilio removal FIRST (in progress), then M1 hardening -> M2 transport -> M3 auth -> M4 Funnel -> M5 docs -> M6 OAuth.
- **SECURITY GATE:** because Funnel = public internet exposure of the full action surface, RE-CONFIRM with Brandon immediately before flipping on public Funnel (start of M4). Everything M0-M3 is non-public and builds safely up to that gate.
