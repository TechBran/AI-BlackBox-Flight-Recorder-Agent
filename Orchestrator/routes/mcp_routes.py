"""Backend routes for remote MCP-server onboarding.

Mounted at /mcp/* by Orchestrator/app.py. Covers token mint/list/revoke, the
public URL (auto-derived from Tailscale, written to a writable Manifest/ file the
MCP server reads -- no /etc, no sudo), connection info + paste-ready client
configs, and a 4-signal status probe. (Privileged box actuators -- service
control + Funnel-up -- land in M7.4.)

The token store Manifest/mcp_tokens.json is the SAME flat {token: operator} 0600
file the MCP server loads. A fresh token / new public URL is made live via the MCP
server's localhost /internal/reload (best-effort; else takes effect on next restart).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import stat
import subprocess
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from Orchestrator.config import USERS_LIST

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])

MCP_TOKENS_FILE = Path("Manifest/mcp_tokens.json")
MCP_RUNTIME_FILE = Path("Manifest/mcp_runtime.json")
MCP_SERVER_URL = os.getenv("BLACKBOX_MCP_INTERNAL_URL", "http://127.0.0.1:9093")
MCP_FUNNEL_PORT = "8443"
MCP_LOCAL_PORT = "9093"
TOKEN_PREFIX = "bbmcp_"


# --------------------------------------------------------------------------- #
# Token store helpers
# --------------------------------------------------------------------------- #
def _live_operators() -> list:
    """This box's live operator roster -- the SAME source GET /operators serves,
    so a token can only bind to an operator the user actually has."""
    return list(USERS_LIST)


def _token_id(token: str) -> str:
    """Mirror MCP/blackbox_mcp_server.py::_token_id -- sha256:+12hex, never the secret."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _load_tokens() -> dict:
    try:
        if MCP_TOKENS_FILE.exists():
            data = json.loads(MCP_TOKENS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: v for k, v in data.items()
                        if isinstance(k, str) and isinstance(v, str)}
    except Exception as e:
        logger.error("MCP tokens load failed: %s", e)
    return {}


def _write_tokens(tokens: dict) -> None:
    """Atomic 0600 write (tmp -> os.replace), mirroring the MCP store's perms."""
    MCP_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(MCP_TOKENS_FILE) + ".tmp")
    tmp.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(tmp, MCP_TOKENS_FILE)
    os.chmod(MCP_TOKENS_FILE, stat.S_IRUSR | stat.S_IWUSR)


def _trigger_reload() -> bool:
    """Best-effort: ask the running MCP server to hot-reload its token map + URL.
    True if reloaded; False if the endpoint is not up yet (changes then take effect
    on the next blackbox-mcp.service restart)."""
    try:
        r = httpx.post(f"{MCP_SERVER_URL}/internal/reload", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Public-URL: derive from Tailscale, persist to the writable runtime file
# --------------------------------------------------------------------------- #
def _public_url() -> str:
    """Currently-effective URL: ENV-pinned wins; else the runtime file; else ""."""
    env = (os.getenv("BLACKBOX_MCP_PUBLIC_URL") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        if MCP_RUNTIME_FILE.exists():
            data = json.loads(MCP_RUNTIME_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("public_url"), str):
                return data["public_url"].strip().rstrip("/")
    except Exception:
        pass
    return ""


def _derive_public_url() -> str:
    """Auto-derive the public Funnel origin from `tailscale status --json`
    (Self.DNSName + the known Funnel port). No sudo; read-only. "" if unavailable."""
    try:
        out = subprocess.run(["tailscale", "status", "--json"],
                             capture_output=True, text=True, timeout=8)
        if out.returncode != 0:
            return ""
        host = ((json.loads(out.stdout).get("Self") or {}).get("DNSName") or "").rstrip(".")
        return f"https://{host}:{MCP_FUNNEL_PORT}" if host else ""
    except Exception:
        return ""


def _write_runtime_public_url(url: str) -> None:
    """Atomic 0644 write of Manifest/mcp_runtime.json (public_url is not a secret)."""
    MCP_RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "public_url": url.strip().rstrip("/"), "updated_by": "onboarding"}
    tmp = Path(str(MCP_RUNTIME_FILE) + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, MCP_RUNTIME_FILE)


# --------------------------------------------------------------------------- #
# Status probes (all read-only; no sudo for the no-grant signals)
# --------------------------------------------------------------------------- #
def _mcp_up() -> bool:
    """MCP HTTP server reachable on localhost (401 = up + auth-gated)."""
    try:
        return httpx.get(f"{MCP_SERVER_URL}/mcp", timeout=4).status_code in (401, 406)
    except Exception:
        return False


def _oauth_ready() -> bool:
    """OAuth discovery serving (200) -- i.e. the public URL is set."""
    try:
        r = httpx.get(f"{MCP_SERVER_URL}/.well-known/oauth-authorization-server", timeout=4)
        return r.status_code == 200
    except Exception:
        return False


def _funnel_up() -> bool:
    """Is the Funnel exposing :8443 -> :9093? Parse `tailscale funnel status`
    (try no-sudo, then `sudo -n`). Lenient: the config mentions our two ports."""
    for cmd in (["tailscale", "funnel", "status"],
                ["sudo", "-n", "/usr/bin/tailscale", "funnel", "status", "--json"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
            if out.returncode == 0 and out.stdout.strip():
                txt = out.stdout
                return MCP_FUNNEL_PORT in txt and MCP_LOCAL_PORT in txt
        except Exception:
            continue
    return False


# --------------------------------------------------------------------------- #
# Client-config snippets
# --------------------------------------------------------------------------- #
def _per_app_configs(public_url: str, token: Optional[str]) -> dict:
    url = (public_url or "https://<your-funnel-host>.ts.net:8443") + "/mcp"
    tok = token or "<your-token>"
    return {
        "claude_code":
            f"claude mcp add --transport http blackbox {url} "
            f"--header 'Authorization: Bearer {tok}'",
        "codex":
            "# ~/.codex/config.toml\n"
            "experimental_use_rmcp_client = true\n\n"
            "[mcp_servers.blackbox]\n"
            f'url = "{url}"\n'
            f'http_headers = {{ Authorization = "Bearer {tok}" }}',
        "antigravity": json.dumps(
            {"mcpServers": {"blackbox": {"serverUrl": url,
                                         "headers": {"Authorization": f"Bearer {tok}"}}}},
            indent=2),
        "claude_desktop_oauth":
            f"Add a custom connector with URL  {url}  -- it uses OAuth, so no token "
            "is pasted here; you enter your BlackBox token at the consent screen.",
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
class MintBody(BaseModel):
    operator: str


@router.post("/tokens")
def mint_token(body: MintBody):
    """Mint a bearer token bound to a LIVE operator. Returns the token ONCE."""
    operator = (body.operator or "").strip()
    if not operator:
        raise HTTPException(400, "operator is required")
    if operator.lower() == "system":
        raise HTTPException(400, "'system' cannot be a remote token operator "
                                 "(span-all is a local-only capability)")
    operators = _live_operators()
    if operator not in operators:
        raise HTTPException(400, f"operator {operator!r} is not a live operator on "
                                 f"this box. Valid operators: {sorted(operators)}")
    token = TOKEN_PREFIX + secrets.token_urlsafe(48)
    tokens = _load_tokens()
    tokens[token] = operator           # merge -- never clobber other operators' tokens
    _write_tokens(tokens)
    reloaded = _trigger_reload()
    return {
        "token": token,
        "operator": operator,
        "token_id": _token_id(token),
        "live": reloaded,
        "note": None if reloaded else
                "Token saved. It activates on the next blackbox-mcp.service reload/restart.",
    }


@router.get("/tokens")
def list_tokens(operator: Optional[str] = None):
    """List tokens as {token_id, operator} -- NEVER the secret."""
    tokens = _load_tokens()
    out = [{"token_id": _token_id(tok), "operator": op}
           for tok, op in tokens.items()
           if not operator or op == operator]
    return {"tokens": out}


@router.delete("/tokens")
def revoke_token(token_id: str):
    """Revoke by token_id (sha256:+12hex) so the secret never transits the wire."""
    tid = (token_id or "").strip()
    if not tid:
        raise HTTPException(400, "token_id is required")
    tokens = _load_tokens()
    match = [tok for tok in tokens if _token_id(tok) == tid]
    if not match:
        raise HTTPException(404, "no token with that token_id")
    for tok in match:
        del tokens[tok]
    _write_tokens(tokens)
    _trigger_reload()
    return {"ok": True, "revoked": len(match)}


class PublicUrlBody(BaseModel):
    url: Optional[str] = None


@router.post("/public-url")
def set_public_url(body: PublicUrlBody):
    """Set the public URL (auto-derived from Tailscale if not given), persist it to
    the writable runtime file, and hot-reload the MCP server. No sudo, no /etc."""
    url = (body.url or "").strip() or _derive_public_url()
    if not url:
        raise HTTPException(400, "could not derive a public URL from Tailscale; "
                                 "pass one explicitly or check `tailscale status`")
    _write_runtime_public_url(url)
    reloaded = _trigger_reload()
    return {"public_url": url.rstrip("/"), "live": reloaded}


@router.get("/status")
def status():
    """4-signal health for the onboarding card + hub tile."""
    public_url = _public_url()
    return {
        "mcp_up": _mcp_up(),
        "funnel_up": _funnel_up(),
        "tokens_present": bool(_load_tokens()),
        "oauth_ready": _oauth_ready(),
        "public_url": public_url,
        "derived_public_url": _derive_public_url(),
    }


@router.get("/connection")
def connection(operator: Optional[str] = None):
    """Connection info for the card: public URL, whether a token exists, and the
    4 client-config snippets (token placeholders -- never re-served)."""
    public_url = _public_url()
    tokens = _load_tokens()
    has_token = any((operator is None or op == operator) for op in tokens.values())
    return {
        "public_url": public_url,
        "has_token": has_token,
        "per_app_configs": _per_app_configs(public_url, None),
    }
