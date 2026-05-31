#!/usr/bin/env python3
"""
gmail_routes.py - Gmail OAuth and status endpoints

Endpoints:
  GET  /auth/gmail/authorize?operator={op}  — redirect to Google OAuth
  GET  /auth/gmail/callback                 — OAuth callback, save tokens
  GET  /gmail/status/{operator}             — connection status + email
  POST /gmail/disconnect/{operator}         — remove tokens (logout)
"""

import os
import json
import base64

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from Orchestrator.checkpoint import app
from Orchestrator.config import GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
from Orchestrator.gmail.service import (
    SCOPES, save_tokens, load_tokens, delete_tokens, is_connected, get_gmail_service
)

# Allow HTTP for local dev (OAuth library requires HTTPS by default)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# In-memory store for PKCE code verifiers (keyed by state)
_pending_verifiers: dict = {}


# ---------------------------------------------------------------------------
# GET /auth/gmail/authorize?operator={operator}
# ---------------------------------------------------------------------------
@app.get("/auth/gmail/authorize")
async def gmail_authorize(request: Request, operator: str = "Brandon"):
    """Generate Google OAuth URL and redirect the user to Google sign-in."""
    if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth credentials not configured in .env")

    from google_auth_oauthlib.flow import Flow

    # Build redirect URI dynamically from the incoming request (supports localhost + Tailscale)
    redirect_uri = str(request.url_for("gmail_callback"))

    flow = Flow.from_client_config(
        {"web": {
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )

    # Encode operator in the state so callback knows who to save tokens for
    state = base64.urlsafe_b64encode(json.dumps({"operator": operator}).encode()).decode()

    authorization_url, generated_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )

    # Store the code_verifier so the callback can use it for PKCE token exchange
    _pending_verifiers[state] = flow.code_verifier

    return RedirectResponse(url=authorization_url)


# ---------------------------------------------------------------------------
# GET /auth/gmail/callback
# ---------------------------------------------------------------------------
@app.get("/auth/gmail/callback")
async def gmail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle the OAuth callback from Google. Exchange code for tokens."""
    if error:
        return HTMLResponse(
            content=f"<html><body><h2>Gmail Authorization Failed</h2><p>{error}</p>"
                    f"<p>You can close this window.</p></body></html>",
            status_code=400,
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state parameter")

    # Decode operator from state
    try:
        state_data = json.loads(base64.urlsafe_b64decode(state))
        operator = state_data.get("operator", "Brandon")
    except Exception:
        operator = "Brandon"

    from google_auth_oauthlib.flow import Flow

    # Rebuild redirect URI from the current request (same origin that started the flow)
    redirect_uri = str(request.url_for("gmail_callback"))

    flow = Flow.from_client_config(
        {"web": {
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )

    # Restore the PKCE code_verifier from the authorize step
    code_verifier = _pending_verifiers.pop(state, None)
    if code_verifier:
        flow.code_verifier = code_verifier

    # Exchange authorization code for tokens
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        return HTMLResponse(
            content=f"<html><body><h2>Token Exchange Failed</h2><p>{str(e)}</p>"
                    f"<p>You can close this window.</p></body></html>",
            status_code=500,
        )

    creds = flow.credentials
    token_data = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else list(SCOPES),
    }
    save_tokens(operator, token_data)

    print(f"[Gmail] OAuth tokens saved for operator: {operator}")

    return HTMLResponse(
        content=f"<html><body style='font-family: sans-serif; text-align: center; padding-top: 80px;'>"
                f"<h2>Gmail connected for {operator}!</h2>"
                f"<p>You can close this window.</p></body></html>"
    )


# ---------------------------------------------------------------------------
# GET /gmail/status/{operator}
# ---------------------------------------------------------------------------
@app.get("/gmail/status/{operator}")
async def gmail_status(operator: str):
    """Check Gmail connection status for an operator. Returns email if connected."""
    connected = is_connected(operator)
    email = ""

    if connected:
        try:
            service = get_gmail_service(operator)
            if service:
                profile = service.users().getProfile(userId="me").execute()
                email = profile.get("emailAddress", "")
        except Exception as e:
            print(f"[Gmail] Status check error for {operator}: {e}")

    return JSONResponse(content={
        "connected": connected,
        "email": email,
        "operator": operator,
    })


# ---------------------------------------------------------------------------
# POST /gmail/disconnect/{operator}
# ---------------------------------------------------------------------------
@app.post("/gmail/disconnect/{operator}")
async def gmail_disconnect(operator: str):
    """Remove Gmail tokens for an operator (logout)."""
    was_connected = is_connected(operator)
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
    body = await request.json()
    tool = body.get("tool")
    if tool not in _GMAIL_MCP_TOOLS:
        return JSONResponse({"success": False, "error": f"not a gmail tool: {tool}"}, status_code=400)
    operator = (body.get("operator") or "system")
    params = dict(body.get("params") or {})
    params["operator"] = operator
    try:
        result = await execute_tool(tool, params, operator)
        return {"success": bool(result.success), "result": result.result}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
