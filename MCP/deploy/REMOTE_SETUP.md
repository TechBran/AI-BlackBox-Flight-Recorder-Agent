# BlackBox MCP — Remote Deployment

Exposes the MCP tool server to external apps (Claude Code/Desktop, Codex, Antigravity)
over a public, bearer/OAuth-authed HTTPS endpoint via Tailscale Funnel.

## Topology (important)
- Backend (`:9091`) stays **tailnet-only** (no auth — relies on the tailnet boundary).
- MCP server (`:9093`) runs the Streamable HTTP transport, **bearer/OAuth authed**.
- Tailscale Funnel exposes **only** the MCP server, on a **separate port (8443)**,
  and only its public paths (section 3) — so the unauthenticated backend is never public.
- The MCP server's control endpoint (`POST /internal/reload`, used by onboarding to
  hot-load a new token or public URL) is on its **own loopback-only listener**,
  `127.0.0.1:9094` (`BLACKBOX_MCP_CONTROL_PORT`), which no Funnel or serve mapping
  names. It also requires the per-boot secret in `credentials/mcp_reload.secret`.

## 1. Token store (auth)
Mint a token bound to one of **your** live operators. The helper reads this box's
roster from `GET /operators`, lets you pick (auto-selects if there's only one),
**validates** the choice, and writes the gitignored 0600 store for you:
```
python3 MCP/deploy/mint_token.py                  # interactive operator picker
python3 MCP/deploy/mint_token.py --operator Alice # non-interactive (must be a live operator)
```
Each token maps to one operator; the server binds every request to that operator
(reads are scoped to it — no cross-operator access). The operator **must** be a
real name returned by `GET /operators` on **this** box — an unknown name (e.g. a
copied `"Brandon"` from another install) is **rejected at startup** (fail-closed),
because it would silently mint snapshots under a phantom operator. `"Brandon"` is
only the seed on an unconfigured box, never a default to copy.

> Manual fallback (discouraged): `curl -s http://localhost:9091/operators | jq .operators`
> to see the valid names, then write `Manifest/mcp_tokens.json -> {"<token>":"<ONE_OF_THOSE>"}`
> and `chmod 600`. The mint helper does this for you and can't typo the operator.

Revoking a token (onboarding's MCP panel, or removing it from the store and
restarting) also ends every OAuth connection that was authorized with it: their
access and refresh tokens stop working at once.

## 2. Persistent HTTP service
`Scripts/install.sh` renders `blackbox-mcp.service` for **this** box (its user and
install path) and enables it; there is no hand-maintained unit file to copy. On a
box installed before that, re-run `sudo bash Scripts/install.sh`. Then:
```
sudo systemctl status blackbox-mcp.service
ss -tlnp | grep -E '9093|9094'   # 127.0.0.1:9093 (MCP) and 127.0.0.1:9094 (control)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9093/mcp   # -> 401 (no token)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:9093/internal/reload  # -> 404
```
The service refuses to start (and logs why) if its tool catalog is empty or fails
to build, rather than serving clients zero tools.

## 3. Public exposure (Tailscale Funnel)
Publish **only** the MCP endpoint and the OAuth paths, each mounted at the same
path on `:8443` (Funnel strips the mount and prepends the target's path):
```
tailscale funnel --bg --https=8443 --set-path=/mcp http://127.0.0.1:9093/mcp
tailscale funnel --bg --https=8443 --set-path=/.well-known http://127.0.0.1:9093/.well-known
tailscale funnel --bg --https=8443 --set-path=/register http://127.0.0.1:9093/register
tailscale funnel --bg --https=8443 --set-path=/authorize http://127.0.0.1:9093/authorize
tailscale funnel --bg --https=8443 --set-path=/token http://127.0.0.1:9093/token
tailscale funnel --bg --https=8443 --set-path=/revoke http://127.0.0.1:9093/revoke
tailscale funnel status     # confirm the six :8443 paths -> 9093; :443 still tailnet-only
```
If the box was exposed with the older whole-origin command
(`tailscale funnel --bg --https=8443 http://127.0.0.1:9093`, which is also what the
onboarding "Funnel up" button runs), drop that root mapping after adding the paths:
```
tailscale funnel --https=8443 --set-path=/ off
```
Do **not** use `tailscale funnel reset` for this: it clears the whole serve config,
including the tailnet-only `:443` mapping of the Portal.

Public URL: `https://<host>.ts.net:8443/mcp`

**For OAuth clients (claude.ai web / native Claude Desktop)** set this box's public
origin so the discovery metadata points at **your** host (it has no hard-coded
default — OAuth discovery returns `503 server_not_configured` until you set it;
bearer-token clients don't need it). Onboarding's MCP panel writes it to
`Manifest/mcp_runtime.json` and hot-loads it; to pin it instead, add
`Environment=BLACKBOX_MCP_PUBLIC_URL=https://<host>.ts.net:8443` to the unit with
`sudo systemctl edit blackbox-mcp.service`, then `sudo systemctl restart blackbox-mcp.service`.

OAuth clients get a 1-hour access token and a 30-day refresh token
(`BLACKBOX_MCP_OAUTH_TOKEN_TTL` / `BLACKBOX_MCP_OAUTH_REFRESH_TTL`, seconds). Refresh
tokens rotate on every use; a refresh token presented twice revokes its whole
connection, and `POST /revoke` (RFC 7009) revokes one on request.

The public OAuth endpoints are rate-limited per client address (`/register` 10 an
hour per address and 60 an hour in all, `/authorize` 30 and `/revoke` 30 per
10 minutes, `/token` 60 per 10 minutes; override with
`BLACKBOX_MCP_OAUTH_RATE_LIMITS='{"register": [10, 3600], ...}'`), their bodies are
capped at 16 KiB, and the client registry is bounded (a registration that never
completes a flow expires after a day).

## 4. Verify over the internet
```
H=<host>.ts.net
curl -s -o /dev/null -w '%{http_code}\n' https://$H:8443/mcp                       # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer bbmcp_YOUR_TOKEN" https://$H:8443/mcp  # 406 (auth ok; needs MCP headers)
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://$H:8443/internal/reload  # 404 (not published)
```
