# BlackBox MCP — Remote Deployment

Exposes the MCP tool server to external apps (Claude Code/Desktop, Codex, Antigravity)
over a public, bearer/OAuth-authed HTTPS endpoint via Tailscale Funnel.

## Topology (important)
- Backend (`:9091`) stays **tailnet-only** (no auth — relies on the tailnet boundary).
- MCP server (`:9093`) runs the Streamable HTTP transport, **bearer/OAuth authed**.
- Tailscale Funnel exposes **only** the MCP server, on a **separate port (8443)** —
  so the unauthenticated backend is never public.

## 1. Token store (auth)
```
python3 -c "import secrets; print('bbmcp_'+secrets.token_urlsafe(48))"   # generate
# write Manifest/mcp_tokens.json  ->  {"bbmcp_YOUR_TOKEN":"Brandon"}
chmod 600 Manifest/mcp_tokens.json     # gitignored (Manifest/ + **/*token*.json)
```
Each token maps to one operator; the server binds every request to that operator
(reads are scoped to it — no cross-operator access).

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

## 4. Verify over the internet
```
H=<host>.ts.net
curl -s -o /dev/null -w '%{http_code}\n' https://$H:8443/mcp                       # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer bbmcp_YOUR_TOKEN" https://$H:8443/mcp  # 406 (auth ok; needs MCP headers)
```
