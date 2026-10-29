# BlackBox MCP Server

Exposes the BlackBox Flight Recorder — snapshot memory, multimodal generation/analysis,
Workspace, telephony, and the ToolVault `mcp` group — to AI agents over the Model Context Protocol.

The advertised tool list is **registry-derived** from the ToolVault `mcp` group
(`Orchestrator/toolvault`), so it tracks the live catalog automatically. Most tools execute
by proxying to the BlackBox backend (`/local/tools/execute`, `/gmail/execute`); snapshot
byte-offset reads run in-process.

## Two transports

| Transport | Use | Auth |
|---|---|---|
| **stdio** (default) | Local Claude Code on this box (subprocess) | none (local) |
| **Streamable HTTP** (`--transport http`) | Remote apps over the network | bearer token / OAuth 2.1 |

- Local: launched per `claude_mcp_config.example.json` (command + args; replace
  `<REPO_ROOT>` with this checkout's absolute path).
- Remote: a persistent service on `127.0.0.1:9093` (`blackbox-mcp.service`, rendered and
  enabled by `Scripts/install.sh`), exposed publicly via Tailscale Funnel, path by path.
  See [`deploy/REMOTE_SETUP.md`](deploy/REMOTE_SETUP.md).

On either transport the server refuses to start, loudly, when its tool catalog is empty
or fails to build (for example an import the lean venv lacks), instead of serving zero
tools. On stdio only the SDK writes to stdout; any other print goes to stderr.

## Auth & isolation (HTTP transport)

- **Bearer token** — `Authorization: Bearer <token>`; tokens live in the gitignored
  `Manifest/mcp_tokens.json` as `{"<token>":"<operator>"}`.
- **Token → operator binding** — every request acts as its token's bound operator
  (caller-asserted operator is ignored); snapshot reads are **scoped to that operator**
  (no cross-operator access). Validated per-request.
- **Audit logging** — each call logs `sha256(token)[:12]` + operator + tool (never the token).
- **OAuth 2.1** (DCR + PKCE/S256) — for Claude Desktop / claude.ai web; discovery at
  `/.well-known/oauth-authorization-server`. The operator authorizes a connection once by
  entering a bearer token at the consent form; the client then gets a 1-hour access token
  and a rotating 30-day refresh token. A refresh token presented twice revokes the whole
  connection, `POST /revoke` (RFC 7009) revokes on request, and revoking the bearer that
  consented ends every connection made with it. Refresh tokens are stored as hashes, and a
  revoked connection leaves a tombstone in their store, so a restart does not revive it.
- OAuth bootstrap endpoints (`/.well-known/*`, `/register`, `/authorize`, `/token`,
  `/revoke`) are public; only `/mcp` requires a credential. They are rate-limited per client
  address, their bodies are capped, and the client registry is bounded.
- `/internal/reload` (onboarding's hot reload of tokens and the public URL) is not on this
  listener: it is served on `127.0.0.1:9094` only (`BLACKBOX_MCP_CONTROL_PORT`) and needs the
  per-boot secret in `credentials/mcp_reload.secret`.

## Connecting external apps

Public URL: `https://<host>.ts.net:8443/mcp`. Replace `bbmcp_YOUR_TOKEN` with your real token.

**Claude Code**
```
claude mcp add --transport http blackbox https://<host>.ts.net:8443/mcp \
  --header "Authorization: Bearer bbmcp_YOUR_TOKEN"
```

**OpenAI Codex** (`~/.codex/config.toml`) — the experimental flag is REQUIRED:
```toml
experimental_use_rmcp_client = true
[mcp_servers.blackbox]
url = "https://<host>.ts.net:8443/mcp"
bearer_token_env_var = "BLACKBOX_MCP_TOKEN"
```

**Google Antigravity** — key is `serverUrl` (not `url`):
```json
{"mcpServers":{"blackbox":{"serverUrl":"https://<host>.ts.net:8443/mcp","headers":{"Authorization":"Bearer bbmcp_YOUR_TOKEN"}}}}
```

**Claude Desktop** — via the `mcp-remote` npx shim (or OAuth once complete):
```json
{"mcpServers":{"blackbox":{"command":"npx","args":["-y","mcp-remote","https://<host>.ts.net:8443/mcp","--header","Authorization: Bearer bbmcp_YOUR_TOKEN"]}}}
```

## Resources

- `blackbox://index/stats` — snapshot index stats
- `blackbox://index/operators` — operator roster
- `blackbox://index/recent` — recent snapshots
- `blackbox://volume/info` — snapshot volume file info

A remote (token-bound) caller sees only its own operator's data in each.

## Notes

- Slow media tools return a `task_id` fast; poll with `get_task_status` (don't block — Claude
  Code aborts idle tool calls after ~5 min).
- The lean `MCP/venv` (mcp, httpx, requests, bs4, starlette, uvicorn) is deliberately separate
  from the backend venv: it carries only what the server imports, with its own pins.
