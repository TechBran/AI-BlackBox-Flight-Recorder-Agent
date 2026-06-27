# BlackBox MCP — Remote Deployment

Exposes the MCP tool server to external apps (Claude Code/Desktop, Codex, Antigravity)
over a public, bearer/OAuth-authed HTTPS endpoint via Tailscale Funnel.

## Topology (important)
- Backend (`:9091`) stays **tailnet-only** (no auth — relies on the tailnet boundary).
- MCP server (`:9093`) runs the Streamable HTTP transport, **bearer/OAuth authed**.
- Tailscale Funnel exposes **only** the MCP server, on a **separate port (8443)** —
  so the unauthenticated backend is never public.

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

## 2. Persistent HTTP service
```
sudo cp MCP/deploy/blackbox-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now blackbox-mcp.service
ss -tlnp | grep 9093          # listening on 127.0.0.1:9093
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9093/mcp   # -> 401 (no token)
```

## 3. Public exposure (Tailscale Funnel)
```
tailscale funnel --bg --https=8443 http://127.0.0.1:9093
tailscale funnel status     # confirm :8443 Funnel on -> 9093; :443 still tailnet-only
```
Public URL: `https://<host>.ts.net:8443/mcp`

**For OAuth clients (claude.ai web / native Claude Desktop)** set this box's public
origin so the discovery metadata points at **your** host (it has no hard-coded
default — OAuth discovery returns `503 server_not_configured` until you set it;
bearer-token clients don't need it):
```
# in blackbox-mcp.service:  Environment=BLACKBOX_MCP_PUBLIC_URL=https://<host>.ts.net:8443
sudo systemctl daemon-reload && sudo systemctl restart blackbox-mcp.service
```

## 4. Verify over the internet
```
H=<host>.ts.net
curl -s -o /dev/null -w '%{http_code}\n' https://$H:8443/mcp                       # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer bbmcp_YOUR_TOKEN" https://$H:8443/mcp  # 406 (auth ok; needs MCP headers)
```
