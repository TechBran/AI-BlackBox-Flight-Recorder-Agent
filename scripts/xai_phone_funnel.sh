#!/usr/bin/env bash
# xAI sovereign phone line — public webhook exposure via Tailscale Funnel.
#
# TOPOLOGY (mirrors MCP/deploy/REMOTE_SETUP.md):
#   * Backend :9091 stays TAILNET-ONLY (it has no app-layer auth by design —
#     Tailscale is the perimeter).
#   * Funnel exposes ONLY the path /xai/voice/incoming, on its own funnel
#     port (10000; 8443 is the MCP server). That endpoint is HMAC-authed
#     (Standard Webhooks signatures, 401 fail-closed), so it — and nothing
#     else on :9091 — is safe to make public.
#
# ORDER OF OPERATIONS (first-time setup):
#   1. scripts/xai_phone_funnel.sh up            -> prints the public webhook URL
#   2. curl -s -X POST http://localhost:9091/xai/phone/provision \
#        -H 'Content-Type: application/json' \
#        -d '{"name":"BlackBox line","webhook_url":"<URL FROM STEP 1>"}'
#   3. scripts/xai_phone_funnel.sh status        -> preflight must report ok:true
#
# Usage: scripts/xai_phone_funnel.sh {up|down|status}
set -euo pipefail

PORT=10000
WEBHOOK_PATH="/xai/voice/incoming"
BACKEND="http://127.0.0.1:9091${WEBHOOK_PATH}"

cmd="${1:-status}"
host=$(tailscale status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
public_url="https://${host}:${PORT}${WEBHOOK_PATH}"

case "$cmd" in
  up)
    tailscale funnel --bg --https="${PORT}" --set-path="${WEBHOOK_PATH}" "${BACKEND}"
    echo "Public webhook URL: ${public_url}"
    echo "Verify (expect 503 before provisioning, 401 after):"
    echo "  curl -s -o /dev/null -w '%{http_code}\\n' -X POST ${public_url}"
    ;;
  down)
    tailscale funnel --https="${PORT}" --set-path="${WEBHOOK_PATH}" off
    echo "Funnel route removed for ${WEBHOOK_PATH}"
    ;;
  status)
    tailscale funnel status
    echo "--- backend preflight (GET /xai/phone/status?preflight=true) ---"
    curl -s "http://127.0.0.1:9091/xai/phone/status?preflight=true" | python3 -m json.tool
    ;;
  *)
    echo "usage: $0 {up|down|status}" >&2
    exit 1
    ;;
esac
