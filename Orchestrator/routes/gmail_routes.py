#!/usr/bin/env python3
"""
gmail_routes.py - Gmail OAuth and status endpoints

Endpoints:
  GET  /auth/gmail/authorize?operator={op}  — redirect to Google OAuth
  GET  /auth/gmail/callback                 — OAuth callback, save tokens
  GET  /gmail/status/{operator}             — connection status + email
  POST /gmail/disconnect/{operator}         — remove tokens (logout)
"""

import asyncio
import html
import os
import secrets
import time
from typing import Dict, Optional
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from Orchestrator import config as _cfg
from Orchestrator.checkpoint import app
from Orchestrator.config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
from Orchestrator.gmail.service import (
    SCOPES, save_tokens, delete_tokens, is_connected, probe_connection,
    token_data_from_credentials,
)

# Allow HTTP for local dev (OAuth library requires HTTPS by default)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# Google auto-grants openid + userinfo.email + userinfo.profile alongside any
# Workspace/Gmail scopes, so the returned scope set never matches the requested
# set exactly. Relax oauthlib's strict scope-equality check, which would
# otherwise raise "Scope has changed from ... to ..." and fail fetch_token on
# every multi-scope (re-)consent. RFC 6749 3.3 allows the changed scope set;
# we persist creds.scopes (the actual grant), so this loses no information.
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

# Pending authorizations, keyed by the OAuth `state` we hand Google:
#   state -> {operator, code_verifier, redirect_uri, created_at}
# The state is an unguessable secrets.token_urlsafe(32), so a callback can only
# complete a flow THIS server started (no login-CSRF binding someone else's
# Google account to an operator), and each flow keeps its own PKCE verifier
# (two concurrent sign-ins for one operator used to share a state and clobber
# each other's). Entries are single-use and expire after _PENDING_TTL_S;
# _PENDING_MAX bounds the dict however often /authorize is hit.
_pending_flows: Dict[str, dict] = {}
_PENDING_TTL_S = 10 * 60
_PENDING_MAX = 64
_now = time.monotonic  # module-level so tests can drive the clock

# The callback pages carry no script at all; this makes any value that ever
# slipped past html.escape inert as well.
_CALLBACK_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def _client_config() -> dict:
    return {"web": {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}


def _authorize_path(operator: str) -> str:
    """Root-relative sign-in URL for an operator (each client resolves it
    against the origin it already talks to: LAN, localhost or tailnet)."""
    return f"/auth/gmail/authorize?operator={quote(operator, safe='')}"


def _prune_pending() -> None:
    now = _now()
    for key in [k for k, v in _pending_flows.items() if now - v["created_at"] > _PENDING_TTL_S]:
        del _pending_flows[key]
    while len(_pending_flows) >= _PENDING_MAX:
        del _pending_flows[min(_pending_flows, key=lambda k: _pending_flows[k]["created_at"])]


def _take_pending(state: str) -> Optional[dict]:
    """Pop the flow for `state`: single use, and None once it has expired."""
    entry = _pending_flows.pop(state, None) if state else None
    if entry is None or _now() - entry["created_at"] > _PENDING_TTL_S:
        return None
    return entry


def _callback_page(title: str, message: str, status_code: int = 200) -> HTMLResponse:
    """The small page the OAuth popup lands on. Every value is escaped."""
    return HTMLResponse(
        content=(
            "<html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title></head>"
            "<body style='font-family: sans-serif; text-align: center; padding-top: 80px;'>"
            f"<h2>{html.escape(title)}</h2><p>{html.escape(message)}</p>"
            "<p>You can close this window.</p></body></html>"
        ),
        status_code=status_code,
        headers=_CALLBACK_HEADERS,
    )


# ---------------------------------------------------------------------------
# GET /auth/gmail/authorize?operator={operator}
# ---------------------------------------------------------------------------
@app.get("/auth/gmail/authorize")
async def gmail_authorize(request: Request, operator: str = ""):
    """Generate Google OAuth URL and redirect the user to Google sign-in."""
    operator = (operator or "").strip()
    if not operator:
        raise HTTPException(status_code=400, detail="operator is required")
    if operator not in _cfg.USERS_LIST:
        raise HTTPException(status_code=400, detail="Unknown operator — add the operator first, then connect Google")
    if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth credentials not configured in .env")

    from google_auth_oauthlib.flow import Flow

    # Build redirect URI dynamically from the incoming request (supports localhost + Tailscale)
    redirect_uri = str(request.url_for("gmail_callback"))

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=redirect_uri)

    _prune_pending()
    state = secrets.token_urlsafe(32)

    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )

    # The callback needs this flow's operator, PKCE verifier and redirect URI.
    _pending_flows[state] = {
        "operator": operator,
        "code_verifier": flow.code_verifier,
        "redirect_uri": redirect_uri,
        "created_at": _now(),
    }

    return RedirectResponse(url=authorization_url)


# ---------------------------------------------------------------------------
# GET /auth/gmail/callback
# ---------------------------------------------------------------------------
@app.get("/auth/gmail/callback")
async def gmail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle the OAuth callback from Google. Exchange code for tokens."""
    # Consume the state first: an error response (e.g. access_denied) still
    # uses up its flow.
    pending = _take_pending(state)

    if error:
        return _callback_page("Gmail Authorization Failed", error, status_code=400)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state parameter")

    if pending is None:
        return _callback_page(
            "Sign-in link expired",
            "This Google sign-in link is unknown, expired or already used. "
            "Start again from Google Workspace in the Portal or the Android app.",
            status_code=400,
        )

    operator = pending["operator"]
    if operator not in _cfg.USERS_LIST:
        return _callback_page("Unknown operator", f"Operator {operator} no longer exists.", status_code=400)

    from google_auth_oauthlib.flow import Flow

    # Same redirect URI the authorize step sent Google (they must match).
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=pending["redirect_uri"])

    # Restore the PKCE code_verifier from the authorize step
    if pending.get("code_verifier"):
        flow.code_verifier = pending["code_verifier"]

    # Exchange authorization code for tokens (a blocking HTTPS call)
    try:
        await asyncio.to_thread(flow.fetch_token, code=code)
    except Exception as e:
        return _callback_page("Token Exchange Failed", str(e), status_code=500)

    save_tokens(operator, token_data_from_credentials(flow.credentials))

    print(f"[Gmail] OAuth tokens saved for operator: {operator}")

    return _callback_page(f"Gmail connected for {operator}!", "Google Workspace is ready for this operator.")


# ---------------------------------------------------------------------------
# GET /gmail/status/{operator}
# ---------------------------------------------------------------------------
@app.get("/gmail/status/{operator}")
async def gmail_status(operator: str):
    """Connection status for an operator, PROVEN with a live Google call.

    `connected` is true only when Google accepted the stored grant (or could
    not be reached to ask — state "unverified"). A revoked/expired grant is
    connected:false + needs_reauth + reauth_url, never "connected" because a
    token file happens to exist.
    """
    probe = await asyncio.to_thread(probe_connection, operator)
    needs_reauth = probe["state"] == "needs_reauth"
    return JSONResponse(content={
        "connected": probe["state"] in ("connected", "unverified"),
        "email": probe["email"],
        "operator": operator,
        "state": probe["state"],
        "needs_reauth": needs_reauth,
        "error": probe["error"],
        "error_message": probe["error_message"],
        "reauth_url": _authorize_path(operator) if needs_reauth else None,
    })


# ---------------------------------------------------------------------------
# POST /gmail/disconnect/{operator}
# ---------------------------------------------------------------------------
@app.post("/gmail/disconnect/{operator}")
async def gmail_disconnect(operator: str):
    """Remove Gmail tokens for an operator (logout)."""
    try:
        was_connected = is_connected(operator)
    except (OSError, ValueError):
        # An unreadable token file (status: needs_reauth/token_unreadable) is
        # exactly what Disconnect is offered for: remove it regardless.
        was_connected = False
    delete_tokens(operator)

    print(f"[Gmail] Disconnected operator: {operator} (was_connected={was_connected})")

    return JSONResponse(content={
        "disconnected": True,
        "operator": operator,
    })


# ---------------------------------------------------------------------------
# POST /gmail/execute  — whitelisted gmail_* tool execution for the BlackBox MCP
# ---------------------------------------------------------------------------
# Gmail tools exposed to the MCP / CLI agents. Whitelist — do NOT route arbitrary
# tools through this endpoint (keeps the HTTP surface scoped to gmail only).
_GMAIL_MCP_TOOLS = {"gmail_search", "gmail_read", "gmail_send", "gmail_reply", "gmail_labels"}


@app.post("/gmail/execute")
async def gmail_execute(request: Request):
    """Execute a whitelisted gmail_* tool (for the BlackBox MCP / CLI agents).
    Body: {"tool": "gmail_search", "params": {...}, "operator": "<name>"}."""
    from Orchestrator.tools.blackbox_tools import execute_tool
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "error": "body must be a JSON object"}, status_code=400)
    tool = body.get("tool")
    if not isinstance(tool, str) or tool not in _GMAIL_MCP_TOOLS:
        return JSONResponse({"success": False, "error": f"not a gmail tool: {tool}"}, status_code=400)
    operator = (body.get("operator") or "system")
    params = body.get("params")
    if not isinstance(params, dict):
        params = {}
    params = dict(params)
    params["operator"] = operator
    try:
        result = await execute_tool(tool, params, operator)
        return {"success": bool(result.success), "result": result.result}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
