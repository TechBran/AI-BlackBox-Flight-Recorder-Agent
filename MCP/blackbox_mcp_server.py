#!/usr/bin/env python3
"""
BlackBox MCP Server - Exposes BlackBox Flight Recorder to Claude Code

This MCP server provides tools and resources for AI agents to:
- Search and retrieve snapshots from the BlackBox memory system
- Mint new snapshots (memories)
- Access the byte-offset manifest for efficient traversal
- Direct byte-offset seeking into the snapshot volume
- Get context-enriched responses through the BlackBox chat system

Run with: python blackbox_mcp_server.py
Configure in Claude Code: ~/.claude/mcp.json

--- M1 HARDENING (2026-06-26) -----------------------------------------------
This is still a STDIO-only server on the official `mcp` SDK; the transport is
unchanged. M1 hardened the call path WITHOUT changing the wire format:
  * call_tool() dispatch is now a small set of explicit LOCAL/SPECIAL branches
    plus ONE uniform proxy dispatcher (_proxy_tool) for every remaining
    registry tool -> POST /local/tools/execute (or /gmail/execute for gmail_*).
    The valid tool-name set is computed ONCE (cached) instead of per call.
  * mint_snapshot POSTs to /chat/save (direct auto-mint persistence) and returns
    the authoritative snap_id -- no /chat round-trip, no sleep, no id guessing.
  * Errors return a structured envelope (CallToolResult isError=True + a JSON
    body carrying a machine code, the tool name, and a human message).
  * Logging uses the `logging` module to STDERR (stdout is the MCP channel),
    leveled, with a per-call request id + resolved operator + tool name.
  * Backend proxy calls are wrapped in asyncio.wait_for timeouts so a hung
    backend cannot wedge a tool call forever.

--- M2 STREAMABLE HTTP TRANSPORT (2026-06-27) -------------------------------
A REMOTE Streamable HTTP transport now sits ALONGSIDE the stdio transport. The
SAME `server` instance (and its @server.list_tools()/@server.call_tool()
handlers) serves BOTH transports -- no tool logic is duplicated. The HTTP path
hosts the official SDK's StreamableHTTPSessionManager inside a Starlette ASGI
app, mounted at `/mcp`, run by uvicorn.

  * Transport selection (stdio stays the DEFAULT so Claude Code is unaffected):
        no args / no env  -> stdio  (unchanged behaviour)
        --transport http | --http   -> Streamable HTTP
        BLACKBOX_MCP_TRANSPORT=http  -> Streamable HTTP
  * Bind: 127.0.0.1 ONLY (localhost) -- M2 is localhost-only by design.
        Port  : BLACKBOX_MCP_HTTP_PORT (default 9093)
        Host  : BLACKBOX_MCP_HTTP_HOST (default 127.0.0.1) -- M2 keeps this
                localhost; do NOT set 0.0.0.0 here (public exposure is M4).
        Path  : BLACKBOX_MCP_HTTP_PATH (default /mcp)
  * NO AUTH at this milestone -- bearer auth + operator binding is M3, public
    Tailscale exposure is M4. The HTTP app is mounted WITHOUT any auth
    middleware; it is reachable only from localhost.
  * uvicorn + starlette are already in MCP/venv (transitive deps of `mcp`); no
    new heavy dep was added (fastapi is deliberately NOT pulled in).

--- M3 BEARER AUTH + TOKEN-BOUND OPERATOR + AUDIT (2026-06-27) ---------------
The HTTP transport is now the SECURITY BOUNDARY (it becomes public in M4). M3
adds three things to the HTTP path ONLY -- stdio is locally-launched + trusted
and stays auth-free and behaviourally UNCHANGED:

  1. BEARER AUTH (Starlette middleware, edge of the /mcp route): every HTTP
     request must carry `Authorization: Bearer <token>`. Missing/malformed/
     unknown -> 401 + `WWW-Authenticate: Bearer` BEFORE the MCP handlers run.
     Validation is CONSTANT-TIME (hmac.compare_digest against every known
     token), so a wrong token leaks no timing signal about which bytes matched.
  2. TOKEN-BOUND OPERATOR (anti-spoof + anti-leak): tokens map token->operator
     (loaded from a SECURE, gitignored source -- env BLACKBOX_MCP_TOKENS JSON
     and/or Manifest/mcp_tokens.json; NEVER committed). The middleware stashes
     the bound operator in a contextvar (mirrors the M1 request-id contextvar).
     resolve_operator() and the read tools that span ALL operators (operator='')
     consult that contextvar: on the HTTP path they FORCE the bound operator and
     IGNORE any caller-asserted operator in the tool args, so a remote caller
     can neither act as another operator nor read another operator's snapshots.
     On stdio the contextvar is unset -> behaviour is exactly as before
     (operator='' still spans all; caller-asserted operator still honoured).
  3. AUDIT LOG: every HTTP request logs (at INFO) a token IDENTIFIER (a short
     sha256 prefix -- NEVER the token value), the bound operator, the method/
     path, and the request id. This is the audit trail for the public surface.

  Token store (secure, NOT committed -- both sources are gitignored):
    * env  BLACKBOX_MCP_TOKENS = {"<token>":"<operator>", ...}  (JSON), and/or
    * file BLACKBOX_MCP_TOKENS_FILE (default Manifest/mcp_tokens.json), same shape.
    Both are merged (env wins on key collision). Manifest/ is gitignored, as is
    **/*token*.json, so the file is doubly excluded from git.
"""

import argparse
import asyncio
import contextvars
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Optional, Dict, List
import httpx

# MCP SDK imports
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool,
        TextContent,
        CallToolResult,
        Resource,
        ResourceContents,
        TextResourceContents,
    )
except ImportError:
    print("MCP SDK not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)

# =============================================================================
# LOGGING (M1) -- structured, leveled, STDERR-ONLY.
#
# stdout is the MCP JSON-RPC channel; ANY write to stdout corrupts the protocol
# frame. So the logging handler is pinned to sys.stderr. A per-call request id +
# the resolved operator + the tool name are attached to each invocation log so
# the stderr stream is a usable audit trail.
# =============================================================================
LOG_LEVEL = os.getenv("BLACKBOX_MCP_LOG_LEVEL", "INFO").upper()
logger = logging.getLogger("blackbox-mcp")
if not logger.handlers:
    _h = logging.StreamHandler(stream=sys.stderr)
    _h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [blackbox-mcp] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logger.addHandler(_h)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger.propagate = False

# Per-call request id, set at the top of call_tool() and read by the loggers
# below so every line for one invocation shares an id (no threading of args).
_REQUEST_ID: "contextvars.ContextVar[str]" = contextvars.ContextVar("mcp_request_id", default="-")

# M3: the TOKEN-BOUND operator for the current HTTP request, set by the auth
# middleware (mirrors _REQUEST_ID). UNSET (None) on the stdio path -- which is
# how resolve_operator()/the read tools tell HTTP (bound) from stdio (trusted).
# A contextvar (not a global) so concurrent HTTP requests never cross operators.
_BOUND_OPERATOR: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "mcp_bound_operator", default=None
)

# M3: the token IDENTIFIER (sha256 prefix, NEVER the value) for the current HTTP
# request, set by the auth middleware. Threaded into the per-tool-call audit log
# so EVERY tool invocation on the public surface carries who-called-it. None on
# stdio (no token).
_TOKEN_ID: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "mcp_token_id", default=None
)


def _log(level: int, tool: str, msg: str, operator: Optional[str] = None) -> None:
    """Emit a structured per-call log line (stderr-only via `logger`)."""
    rid = _REQUEST_ID.get()
    op = operator if operator is not None else "-"
    logger.log(level, "rid=%s tool=%s operator=%s | %s", rid, tool, op, msg)


# Configuration - paths relative to blackbox root
BLACKBOX_ROOT = Path(os.getenv("BLACKBOX_ROOT") or Path(__file__).resolve().parent.parent)
BLACKBOX_URL = os.getenv("BLACKBOX_URL", "http://localhost:9091")

# Backend-proxy timeout (seconds). Media/generation tools return a task_id FAST
# (they do not block on the actual generation -- that runs async and is polled
# via get_task_status), so a 120s cap comfortably covers every kept/proxied tool
# including the multimodal /chat analysis paths (analyze_image/video, capped at
# 120-180s server-side -- see note on analyze_video below). Override per-deploy
# via BLACKBOX_MCP_PROXY_TIMEOUT.
PROXY_TIMEOUT = float(os.getenv("BLACKBOX_MCP_PROXY_TIMEOUT", "120"))

# --- M2: Streamable HTTP transport config -----------------------------------
# Localhost-only for M2 (auth = M3, public Tailscale = M4). The HTTP runner
# binds 127.0.0.1 by default; do NOT change the default host to 0.0.0.0 here.
BLACKBOX_MCP_HTTP_HOST = os.getenv("BLACKBOX_MCP_HTTP_HOST", "127.0.0.1")
BLACKBOX_MCP_HTTP_PORT = int(os.getenv("BLACKBOX_MCP_HTTP_PORT", "9093"))
BLACKBOX_MCP_HTTP_PATH = os.getenv("BLACKBOX_MCP_HTTP_PATH", "/mcp")

# The six per-provider web-search tools replaced the generic web_search tool.
# They require real provider API keys, which the lean MCP venv lacks, so they
# CANNOT run in-process here. They route through the generic backend executor
# (POST /local/tools/execute) which runs on the FULL backend with real keys --
# i.e. they take the UNIFORM proxy path (no dedicated branch needed anymore).
WEB_SEARCH_TOOL_NAMES = {
    "perplexity_web_search",
    "openai_web_search",
    "gemini_web_search",
    "grok_web_search",
    "grok_x_search",
    "duckduckgo_web_search",
}

# gmail_* tools route to the dedicated /gmail/execute whitelist endpoint (NOT
# the generic /local/tools/execute). The uniform dispatcher keys off this set to
# pick the route; the request/response shape is identical to /local/tools/execute.
GMAIL_TOOL_NAMES = {"gmail_search", "gmail_read", "gmail_send", "gmail_reply", "gmail_labels"}

# Operator resolution (pure decision logic lives in operator_resolution.py).
# Same-dir import: when this server runs as a bare script
# (MCP/venv/bin/python MCP/blackbox_mcp_server.py), the script's own directory
# is on sys.path[0], so the sibling module imports directly without a package root.
from operator_resolution import choose_operator

# Per-process cache of the install's operators (multi-tenant: never hard-coded).
_OPERATOR_CACHE = {"operators": None, "default": None}


async def _fetch_operators():
    """Fetch + cache the install's operators from GET /operators.

    Cached only on SUCCESS (per-process, lifetime = one MCP session). A failed
    fetch returns an empty result WITHOUT caching, so the next call retries --
    a transient API blip self-heals instead of degrading the whole session.
    """
    if _OPERATOR_CACHE["operators"] is not None:
        return _OPERATOR_CACHE["operators"], _OPERATOR_CACHE["default"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BLACKBOX_URL}/operators")
            data = r.json()
            operators = list(data.get("operators") or [])
            default = data.get("default") or ""
        _OPERATOR_CACHE["operators"] = operators
        _OPERATOR_CACHE["default"] = default
        return operators, default
    except Exception:
        return [], ""   # do NOT cache -- retry on next call


def _is_http_request() -> bool:
    """True iff we are serving an authenticated HTTP request (vs stdio).

    The middleware sets _BOUND_OPERATOR for every authenticated HTTP request, so
    a non-None value is the reliable "this is the HTTP transport" signal. (Its
    VALUE may be session-stale for a tool call -- we never trust the value; we
    only use its presence as a transport flag. The operator VALUE comes from
    _current_bound_operator() below, which reads the CURRENT request.)
    """
    return _BOUND_OPERATOR.get() is not None


def _current_bound_operator() -> Optional[str]:
    """The token-bound operator of the CURRENT request (#2B Option A).

    The SDK sets a fresh RequestContext (with `request` = the current Starlette
    Request) per JSON-RPC message, INSIDE the session run-loop, right before the
    handler runs (mcp/server/lowlevel/server.py). Our auth middleware stashes the
    request's token-operator on `request.state.bound_operator`. Reading it HERE
    therefore yields the operator of the TOKEN ON THIS REQUEST -- not the token
    that opened the session. This is what kills the session-hijack / stale-binding
    hole: a token-B request riding a token-A session executes as B.

    Returns the per-request operator on the HTTP path, or None on stdio (no
    active request context -> resolution falls back to the trusted stdio rules).

    FAIL-CLOSED: if we are demonstrably on HTTP (_is_http_request()) but the
    per-request operator cannot be read, we DENY by returning the sentinel
    _DENY_OPERATOR (an operator no snapshot can match) rather than silently
    widening scope -- a missing per-request identity must never span all data.
    """
    try:
        req = server.request_context.request
    except LookupError:
        req = None
    if req is not None:
        op = getattr(getattr(req, "state", None), "bound_operator", None)
        if op:
            return op
    # No per-request request/operator available.
    if _is_http_request():
        # On HTTP but identity is unreadable -> fail CLOSED (never span).
        logger.error("HTTP request missing per-request bound operator -- denying "
                     "(fail-closed). This should not happen; investigate.")
        return _DENY_OPERATOR
    return None


# A sentinel operator that matches NO snapshot. Used to fail closed when an HTTP
# request's per-request identity is unexpectedly unreadable.
_DENY_OPERATOR = "\x00__deny__\x00"


async def resolve_operator(provided):
    """Server-side safety net: resolve the operator for a tool call.

    HTTP path -- ANTI-SPOOF: return the CURRENT request's token-bound operator
    (_current_bound_operator) and IGNORE `provided`, so a remote caller can
    neither act as a different operator (by asserting one in the args) nor ride
    another token's session (the operator is read per-request, not per-session).
    This is the single chokepoint every write/proxy tool flows through
    (mint_snapshot, get_context, chat_with_context, _proxy_tool, ...).

    stdio (trusted) path -- UNCHANGED: when omitted, single operator -> that;
    multiple -> system default; caller-asserted operator honoured. (Interactive
    dropdown for the multiple case is handled agent-side, not here.)
    """
    bound = _current_bound_operator()
    if bound is not None:
        return bound
    operators, default = await _fetch_operators()
    resolved, _needs = choose_operator(provided, operators, default)
    return resolved


def _read_scope_operator(provided: str) -> str:
    """Operator FILTER for read tools that span ALL operators when blank.

    ANTI-LEAK: on the HTTP path, FORCE the CURRENT request's token-bound operator
    and IGNORE `provided` -- otherwise a remote caller could read every operator's
    snapshots via search_snapshots / browse_index by leaving operator blank.
    On the stdio (trusted) path no request is active, so `provided` is returned
    verbatim -- blank still means "span ALL operators on this box".
    """
    bound = _current_bound_operator()
    if bound is not None:
        return bound
    return provided


def _deny_if_not_owned(metadata) -> Optional[str]:
    """#2A ownership gate for by-id/by-offset snapshot reads.

    Returns a denial reason (str) iff we are on the HTTP/bound path AND the
    snapshot's operator != the CURRENT request's bound operator -- i.e. a
    cross-operator read that must be denied. Returns None when access is allowed
    (stdio -> always allowed; HTTP -> allowed only for the caller's own operator).
    Callers translate a non-None return into a not_found (never a 403, so the
    existence of another operator's snapshot is never confirmed).
    """
    scope = _current_bound_operator()
    if scope is None:
        return None  # stdio / trusted -- no gating
    owner = (metadata or {}).get("operator")
    if owner != scope:
        return f"cross-operator access denied (owner={owner!r} caller={scope!r})"
    return None

# Put the REPO ROOT on sys.path so the `Orchestrator` PACKAGE is importable.
# get_mcp_tools() (below) lazily runs `from Orchestrator.toolvault import registry`
# and `from Orchestrator.toolvault.resolvers import resolve_schema` -- those resolve
# the `Orchestrator` package, whose parent is the repo root. .mcp.json passes
# BLACKBOX_ROOT (env) but NOT PYTHONPATH, so without this the spawned MCP process
# fails every list_tools with "No module named 'Orchestrator'" -> client gets zero
# tools. Self-contained here so it works regardless of how the server is launched.
sys.path.insert(0, str(BLACKBOX_ROOT))

# Import web_tools directly (stdlib + requests + bs4 only)
sys.path.insert(0, str(BLACKBOX_ROOT / "Orchestrator"))
from web_tools import perform_web_fetch

# E21 (2026-05-17): load tool_registry.py as a STANDALONE module to bypass
# Orchestrator/tools/__init__.py, which re-exports blackbox_tools -- and
# blackbox_tools transitively pulls in aiohttp, Orchestrator.contacts, and
# the rest of the FastAPI request/response stack. The MCP server only needs
# get_mcp_tools (metadata generation, returns tool schemas); it never CALLS
# them locally (execution always hops through HTTP back to the BlackBox API).
# Loading the file directly via importlib lets MCP/venv stay lean -- no
# aiohttp/fastapi/starlette needed, which sidesteps the mcp-vs-fastapi
# starlette version conflict that would otherwise force a heavier venv.
import importlib.util as _ilu
_tr_path = BLACKBOX_ROOT / "Orchestrator" / "tools" / "tool_registry.py"
_tr_spec = _ilu.spec_from_file_location("blackbox_tool_registry", str(_tr_path))
_tr_module = _ilu.module_from_spec(_tr_spec)
_tr_spec.loader.exec_module(_tr_module)
get_mcp_tools = _tr_module.get_mcp_tools

# Memoized valid-tool-name set (M1): the old catch-all rebuilt the entire tool
# list (get_mcp_tools()) on EVERY unmatched call just to membership-test the name.
# Compute it ONCE, lazily, and reuse. list_tools() still calls get_mcp_tools()
# live (registry-driven, mtime-cached inside the registry) so the advertised
# catalog stays fresh; this cache is only the dispatcher's name-validity gate and
# is stable for the process lifetime (the catalog is fixed per server start).
_VALID_TOOL_NAMES: Optional[set] = None


def _valid_tool_names() -> set:
    """Return (and memoize) the set of valid MCP tool names from the registry."""
    global _VALID_TOOL_NAMES
    if _VALID_TOOL_NAMES is None:
        _VALID_TOOL_NAMES = {t.name for t in get_mcp_tools()}
    return _VALID_TOOL_NAMES

# Direct file paths for efficient byte-offset access
VOLUME_FILE = BLACKBOX_ROOT / "Volumes" / "SNAPSHOT_VOLUME.txt"
SNAPSHOT_INDEX = BLACKBOX_ROOT / "Manifest" / "snapshot_index.json"
MANIFEST_FILE = BLACKBOX_ROOT / "Manifest" / "manifest.json"

# Initialize MCP server
server = Server("blackbox-mcp")

# Cache for the snapshot index (loaded once, refreshed on demand)
_index_cache: Optional[Dict] = None
_index_cache_mtime: float = 0


# =============================================================================
# ERROR ENVELOPE (M1)
#
# A consistent, machine-readable error result. Uses the SDK's CallToolResult
# with isError=True (proper MCP error semantics) and a JSON body carrying:
#   { "error": { "code": <machine code>, "tool": <name>, "message": <human> } }
# so a backend 500, a bad argument, and a timeout are distinguishable to the
# client. Wire format is preserved (content is still TextContent).
#
# Codes: invalid_arguments | not_found | backend_error | timeout |
#        tool_error | unknown_tool | internal_error
# =============================================================================
def _error(code: str, tool: str, message: str) -> CallToolResult:
    """Build a structured MCP error result (isError=True)."""
    _log(logging.WARNING, tool, f"error code={code}: {message}")
    body = {"error": {"code": code, "tool": tool, "message": message}}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body, indent=2))],
        isError=True,
    )


def load_snapshot_index(force_refresh: bool = False) -> Dict:
    """Load snapshot index with caching for performance."""
    global _index_cache, _index_cache_mtime

    if not SNAPSHOT_INDEX.exists():
        return {}

    current_mtime = SNAPSHOT_INDEX.stat().st_mtime
    if _index_cache is None or force_refresh or current_mtime > _index_cache_mtime:
        with open(SNAPSHOT_INDEX, 'r') as f:
            _index_cache = json.load(f)
        _index_cache_mtime = current_mtime
        logger.info("Loaded snapshot index: %d entries", len(_index_cache))

    return _index_cache


def seek_snapshot_by_offset(snap_id: str) -> Optional[str]:
    """
    Efficiently retrieve a snapshot using byte offsets from the index.
    This is O(1) seek + read, not O(n) scan of the entire volume.
    """
    index = load_snapshot_index()

    if snap_id not in index:
        return None

    entry = index[snap_id]
    byte_start = entry.get("byte_start")
    byte_end = entry.get("byte_end")

    if byte_start is None or byte_end is None:
        return None

    if not VOLUME_FILE.exists():
        return None

    # Seek directly to the byte offset and read only what we need
    with open(VOLUME_FILE, 'rb') as f:
        f.seek(byte_start)
        raw_bytes = f.read(byte_end - byte_start)

    return raw_bytes.decode('utf-8', errors='replace')


def get_snapshot_metadata(snap_id: str) -> Optional[Dict]:
    """Get metadata for a snapshot without reading its content."""
    index = load_snapshot_index()
    if snap_id not in index:
        return None

    entry = index[snap_id]
    return {
        "snap_id": snap_id,
        "operator": entry.get("operator", "unknown"),
        "timestamp": entry.get("timestamp", "unknown"),
        "type": entry.get("type", "normal"),
        "byte_start": entry.get("byte_start"),
        "byte_end": entry.get("byte_end"),
        "size_bytes": entry.get("byte_end", 0) - entry.get("byte_start", 0),
        "has_embedding": "embedding" in entry and len(entry.get("embedding", [])) > 0
    }


def list_snapshots_by_operator(operator: str, limit: int = 50) -> List[Dict]:
    """List snapshots for an operator using the index (no volume reads)."""
    index = load_snapshot_index()

    results = []
    for snap_id, entry in index.items():
        if entry.get("operator") == operator:
            results.append({
                "snap_id": snap_id,
                "timestamp": entry.get("timestamp", ""),
                "type": entry.get("type", "normal"),
                "size_bytes": entry.get("byte_end", 0) - entry.get("byte_start", 0)
            })

    # Sort by timestamp descending (most recent first)
    results.sort(key=lambda x: x["timestamp"], reverse=True)
    return results[:limit]


# =============================================================================
# UNIFORM PROXY DISPATCHER (M1)
#
# Every non-local, non-special tool routes through here. gmail_* go to the
# dedicated /gmail/execute whitelist; everything else to /local/tools/execute
# (the on-device tool bridge that runs the canonical ToolVault executor on the
# FULL backend with real provider keys). Both endpoints share the request shape
# {"tool", "params", "operator"} and the response shape
# {"success": bool, "result"|"error": ...}, so one helper handles both.
#
# The whole proxy is wrapped in asyncio.wait_for(PROXY_TIMEOUT) so a hung
# backend cannot wedge the tool call forever. Returns a list[TextContent] on
# success, or a CallToolResult error envelope on any failure.
# =============================================================================
async def _proxy_tool(name: str, arguments: dict, client: httpx.AsyncClient):
    """Proxy a tool call to the backend executor (gmail or generic) -- uniform path."""
    operator = await resolve_operator(arguments.get("operator"))
    params = {k: v for k, v in arguments.items() if k != "operator"}

    if name in GMAIL_TOOL_NAMES:
        endpoint = f"{BLACKBOX_URL}/gmail/execute"
        kind = "gmail"
    else:
        endpoint = f"{BLACKBOX_URL}/local/tools/execute"
        kind = "tool"

    _log(logging.INFO, name, f"proxy -> {endpoint}", operator=operator)
    try:
        resp = await asyncio.wait_for(
            client.post(endpoint, json={"tool": name, "params": params, "operator": operator}),
            timeout=PROXY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return _error("timeout", name, f"backend did not respond within {PROXY_TIMEOUT:.0f}s")
    except httpx.HTTPError as e:
        return _error("backend_error", name, f"HTTP error reaching backend: {e}")

    if resp.status_code >= 500:
        return _error("backend_error", name, f"backend returned {resp.status_code}")
    try:
        data = resp.json()
    except Exception as e:
        return _error("backend_error", name, f"backend returned non-JSON ({resp.status_code}): {e}")

    if data.get("success"):
        return [TextContent(type="text", text=str(data.get("result", "")))]
    # Executor-level failure (e.g. bad params, provider error) -- surface it.
    return _error("tool_error", name, str(data.get("error") or data.get("result") or "unknown error"))


# =============================================================================
# TOOLS - Actions the agent can take
# =============================================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available BlackBox tools -- generated from tool_registry.py."""
    return get_mcp_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]):
    """Execute a BlackBox tool.

    Dispatch order (M1):
      1. LOCAL tools  -- read the index/volume by byte offset or hit local
         in-process helpers; NEVER proxy. (seek/get/list/browse/stats/operators/
         current_operator/refresh + web_fetch + get_media + list_devices.)
      2. SPECIAL HTTP tools -- genuine logic the generic proxy does NOT replicate
         (search/get_context -> /fossil/hybrid; chat_with_context -> /chat poll;
         mint_snapshot -> /chat/save; speech_to_text -> multipart upload).
      3. UNIFORM PROXY -- every other registry tool -> _proxy_tool (gmail/generic).

    Returns a list[TextContent] on success, or a structured CallToolResult
    (isError=True) on any failure.
    """
    # Per-call request id for the audit trail (shared by every log line below).
    # HTTP path: the auth middleware already set _REQUEST_ID (+ bound operator +
    # token id) for THIS request -- reuse it so the middleware AUDIT line and the
    # tool-call lines share one id. stdio path: no middleware ran, so mint a fresh
    # id (the contextvar is still at its "-" default).
    if _REQUEST_ID.get() == "-":
        _REQUEST_ID.set(uuid.uuid4().hex[:12])
    _tid = _TOKEN_ID.get()
    if _tid is not None:
        # AUDIT (M3): every tool invocation on the HTTP/public surface logs the
        # token id (never the value) + the CURRENT request's operator (per-request,
        # so a hijacked session is audited as the ACTUAL caller) + tool + rid.
        _log(logging.INFO, name, f"AUDIT tool-call tid={_tid}", operator=_current_bound_operator())
    _log(logging.INFO, name, "call received")

    try:
        # === LOCAL TOOLS (Direct file access - faster, no proxy) ===

        if name == "seek_snapshot_direct":
            snap_id = arguments["snap_id"]
            content = seek_snapshot_by_offset(snap_id)

            if content is None:
                return _error("not_found", name, f"Snapshot {snap_id} not found in index")

            metadata = get_snapshot_metadata(snap_id)
            # #2A: on the HTTP/bound path, a caller may only read its OWN
            # operator's snapshots. Snapshot ids are predictable, so cross-operator
            # reads are an enumeration vector. Return not_found (do NOT 403 --
            # never confirm another operator's snapshot exists).
            _gate = _deny_if_not_owned(metadata)
            if _gate is not None:
                return _error("not_found", name, f"Snapshot {snap_id} not found in index")

            result = {
                "snap_id": snap_id,
                "metadata": metadata,
                "content": content
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "get_snapshot":
            snap_id = arguments["snap_id"]
            include_content = arguments.get("include_content", True)

            metadata = get_snapshot_metadata(snap_id)
            if metadata is None:
                return _error("not_found", name, f"Snapshot {snap_id} not found")

            # #2A: cross-operator read denial (not_found, not 403) on HTTP.
            if _deny_if_not_owned(metadata) is not None:
                return _error("not_found", name, f"Snapshot {snap_id} not found")

            result = {"metadata": metadata}
            if include_content:
                result["content"] = seek_snapshot_by_offset(snap_id)

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "list_recent_snapshots":
            operator = await resolve_operator(arguments.get("operator"))
            count = arguments.get("count", 10)
            _log(logging.INFO, name, "local index read", operator=operator)

            snapshots = list_snapshots_by_operator(operator, count)
            return [TextContent(type="text", text=json.dumps(snapshots, indent=2))]

        elif name == "get_index_stats":
            index = load_snapshot_index()

            # #2A: when bound (HTTP), a caller sees stats for ONLY its operator,
            # never the whole-box roster/counts. On stdio (scope None) -> all.
            scope = _current_bound_operator()

            # Calculate stats
            operators = {}
            types = {"normal": 0, "checkpoint": 0, "summary": 0}
            total_bytes = 0
            counted = 0

            for snap_id, entry in index.items():
                op = entry.get("operator", "unknown")
                if scope is not None and op != scope:
                    continue
                counted += 1
                operators[op] = operators.get(op, 0) + 1

                snap_type = entry.get("type", "normal")
                if snap_type in types:
                    types[snap_type] += 1

                size = entry.get("byte_end", 0) - entry.get("byte_start", 0)
                total_bytes += size

            stats = {
                "total_snapshots": counted,
                "operators": operators,
                "types": types,
                "total_size_bytes": total_bytes,
                "total_size_mb": round(total_bytes / (1024 * 1024), 2),
            }
            # #2A: never leak absolute server FS paths to a remote (bound) caller.
            if scope is None:
                stats["index_file"] = str(SNAPSHOT_INDEX)
                stats["volume_file"] = str(VOLUME_FILE)
                stats["volume_exists"] = VOLUME_FILE.exists()
            return [TextContent(type="text", text=json.dumps(stats, indent=2))]

        elif name == "browse_index":
            # Read tool: omitting operator spans ALL operators on stdio (trusted).
            # On the HTTP/bound path _read_scope_operator FORCES the token's
            # operator (M3 anti-leak) so a remote caller cannot read every operator.
            operator = _read_scope_operator(arguments.get("operator", ""))
            snap_type = arguments.get("snap_type")
            limit = arguments.get("limit", 20)
            offset = arguments.get("offset", 0)

            index = load_snapshot_index()

            # Filter and collect
            results = []
            for snap_id, entry in index.items():
                if operator and entry.get("operator") != operator:
                    continue
                if snap_type and entry.get("type") != snap_type:
                    continue

                results.append({
                    "snap_id": snap_id,
                    "operator": entry.get("operator"),
                    "timestamp": entry.get("timestamp"),
                    "type": entry.get("type", "normal"),
                    "size_bytes": entry.get("byte_end", 0) - entry.get("byte_start", 0)
                })

            # Sort by timestamp descending
            results.sort(key=lambda x: x["timestamp"], reverse=True)

            # Paginate
            paginated = results[offset:offset + limit]

            return [TextContent(type="text", text=json.dumps({
                "total_matching": len(results),
                "returned": len(paginated),
                "offset": offset,
                "snapshots": paginated
            }, indent=2))]

        elif name == "list_operators":
            index = load_snapshot_index()

            # #2A: a bound (HTTP) caller sees ONLY its own operator row, never the
            # full roster of who-else-is-on-this-box. stdio (scope None) -> all.
            scope = _current_bound_operator()
            operators = {}
            for entry in index.values():
                op = entry.get("operator", "unknown")
                if scope is not None and op != scope:
                    continue
                operators[op] = operators.get(op, 0) + 1

            # Sort by count descending
            sorted_ops = sorted(operators.items(), key=lambda x: x[1], reverse=True)

            return [TextContent(type="text", text=json.dumps({
                "operators": [{"name": op, "snapshot_count": count} for op, count in sorted_ops]
            }, indent=2))]

        elif name == "get_current_operator":
            operators, default = await _fetch_operators()
            bound = _current_bound_operator()
            if bound is not None:
                # HTTP path: the operator IS the CURRENT request's token operator
                # (per-request, not session-frozen). No selection needed remotely.
                resolved, needs_selection = bound, False
                # Scope the roster to the bound operator (don't leak who else exists).
                operators, default = [bound], bound
            else:
                resolved, needs_selection = choose_operator(None, operators, default)
            result = {
                "resolved": resolved,
                "operators": operators,
                "default": default,
                "count": len(operators),
                "needs_selection": needs_selection,
            }
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "refresh_index":
            load_snapshot_index(force_refresh=True)
            index = load_snapshot_index()
            return [TextContent(type="text", text=f"Index refreshed. {len(index)} snapshots loaded.")]

        elif name == "web_fetch":
            url = arguments["url"]
            max_chars = arguments.get("max_chars", 80000)
            result = perform_web_fetch(url, max_chars)
            return [TextContent(type="text", text=result)]

        elif name == "get_media":
            import base64
            import mimetypes

            url = arguments.get("url")
            task_id = arguments.get("task_id")
            include_base64 = arguments.get("include_base64", True)
            include_metadata = arguments.get("include_metadata", True)

            # If task_id provided, look up URL from task
            if task_id and not url:
                async with httpx.AsyncClient(timeout=30) as client:
                    task_response = await client.get(f"{BLACKBOX_URL}/tasks/{task_id}")
                    if task_response.status_code == 200:
                        task_data = task_response.json()
                        url = task_data.get("result_url")
                        if not url:
                            return _error("not_found", name, f"Task {task_id} has no result_url")
                    else:
                        return _error("not_found", name, f"Task {task_id} not found")

            if not url:
                return _error("invalid_arguments", name, "No URL or task_id provided")

            # Clean URL - remove host prefix if present
            if url.startswith("http://") or url.startswith("https://"):
                from urllib.parse import urlparse
                parsed = urlparse(url)
                url = parsed.path

            # #2A: reject path traversal BEFORE the /ui/->Portal join. A `..`
            # anywhere in the url could escape Portal/ and read arbitrary files.
            if ".." in url:
                return _error("invalid_arguments", name, "Illegal path component in url")

            # #2A: when a task is involved, the media belongs to the task's
            # operator -- a bound (HTTP) caller may only fetch its OWN task's media.
            # Resolve the owning operator and deny cross-operator (not_found).
            _scope = _current_bound_operator()
            if _scope is not None and task_id:
                async with httpx.AsyncClient(timeout=30) as _oclient:
                    _tr = await _oclient.get(f"{BLACKBOX_URL}/tasks/{task_id}")
                if _tr.status_code != 200:
                    return _error("not_found", name, f"Task {task_id} not found")
                _owner = (_tr.json() or {}).get("operator")
                if _owner != _scope:
                    return _error("not_found", name, f"Task {task_id} not found")

            # Resolve to file path
            # URL format: /ui/uploads/filename.ext -> Portal/uploads/filename.ext
            if url.startswith("/ui/"):
                relative_path = url.replace("/ui/", "")
                file_path = (BLACKBOX_ROOT / "Portal" / relative_path).resolve()
                # Defense in depth: the resolved path MUST stay under Portal/.
                _portal_root = (BLACKBOX_ROOT / "Portal").resolve()
                if _portal_root not in file_path.parents and file_path != _portal_root:
                    return _error("invalid_arguments", name, "Resolved path escapes Portal/")
            else:
                return _error("invalid_arguments", name, f"Invalid URL format: {url}")

            if not file_path.exists():
                return _error("not_found", name, f"File not found: {file_path}")

            # Detect media type
            suffix = file_path.suffix.lower()
            if suffix in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                media_type = "image"
            elif suffix in ['.mp4', '.webm', '.mov']:
                media_type = "video"
            elif suffix in ['.wav', '.mp3', '.ogg', '.m4a']:
                media_type = "audio"
            else:
                media_type = "unknown"

            # Get MIME type
            mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

            result = {
                "url": url,
                "file_path": str(file_path),
                "file_size_bytes": file_path.stat().st_size,
                "media_type": media_type,
                "mime_type": mime_type
            }

            # Include base64 for images under 10MB
            if include_base64 and media_type == "image":
                if result["file_size_bytes"] < 10_000_000:
                    with open(file_path, "rb") as f:
                        result["base64"] = base64.b64encode(f.read()).decode()
                    _log(logging.INFO, name, f"included base64 for {file_path.name} ({result['file_size_bytes']} bytes)")
                else:
                    result["base64_skipped"] = "File too large (>10MB)"

            # Include metadata from task if task_id provided
            if include_metadata and task_id:
                async with httpx.AsyncClient(timeout=30) as client:
                    task_response = await client.get(f"{BLACKBOX_URL}/tasks/{task_id}")
                    if task_response.status_code == 200:
                        t = task_response.json()
                        result["metadata"] = {
                            "prompt": t.get("prompt"),
                            "task_type": t.get("task_type"),
                            "operator": t.get("operator"),
                            "created_at": t.get("created_at"),
                            "task_id": task_id
                        }
                        # Include artifact if present in result_data
                        if t.get("result_data") and "artifact" in t.get("result_data", {}):
                            result["artifact"] = t["result_data"]["artifact"]

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "list_devices":
            # In-process device registry read -- NOT an HTTP proxy. The registry
            # is a local singleton, so this stays a dedicated branch.
            from Orchestrator.device_registry import get_registry, DeviceType
            registry = get_registry()
            dtype = arguments.get("device_type")
            if dtype:
                devices = registry.get_devices_by_type(DeviceType(dtype))
            else:
                devices = registry.get_all_devices()
            device_list = [d.to_dict() for d in devices]
            return [TextContent(type="text", text=json.dumps({"devices": device_list}, indent=2))]

        # === SPECIAL HTTP TOOLS (genuine logic the generic proxy does NOT replicate) ===

        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:

            if name == "search_snapshots":
                query = arguments["query"]
                # Read tool: omitting operator spans ALL operators on stdio (trusted).
                # On the HTTP/bound path _read_scope_operator FORCES the token's
                # operator (M3 anti-leak) so a remote caller cannot read every operator.
                operator = _read_scope_operator(arguments.get("operator", ""))
                limit = arguments.get("limit", 10)
                _log(logging.INFO, name, "GET /fossil/hybrid", operator=operator)

                response = await asyncio.wait_for(client.get(
                    f"{BLACKBOX_URL}/fossil/hybrid",
                    params={"q": query, "operator": operator, "limit": limit}
                ), timeout=PROXY_TIMEOUT)
                response.raise_for_status()
                return [TextContent(type="text", text=json.dumps(response.json(), indent=2))]

            elif name == "mint_snapshot":
                # M1 FIX: POST to /chat/save (direct auto-mint persistence) -- NOT
                # /chat (a full LLM round-trip). /chat/save fires perform_mint()
                # inline and returns the AUTHORITATIVE snap_id in the body, so we
                # no longer sleep, guess the id from the index, or force-reload.
                content = arguments["content"]
                operator = await resolve_operator(arguments.get("operator"))
                _log(logging.INFO, name, "POST /chat/save", operator=operator)

                save_response = await asyncio.wait_for(client.post(
                    f"{BLACKBOX_URL}/chat/save",
                    json={
                        "operator": operator,
                        "user_message": "Memory minted via BlackBox MCP (mint_snapshot)",
                        "assistant_response": content,
                        "model": "blackbox-mcp",
                        "tokens": {"prompt": 0, "completion": 0},
                    },
                ), timeout=PROXY_TIMEOUT)
                if save_response.status_code >= 400:
                    return _error("backend_error", name,
                                  f"/chat/save returned {save_response.status_code}: {save_response.text[:300]}")
                save_result = save_response.json()
                snap_id = save_result.get("snap_id")
                minted = bool(save_result.get("minted"))

                return [TextContent(type="text", text=json.dumps({
                    "status": "minted" if minted else "saved",
                    "snap_id": snap_id,
                    "operator": operator,
                    "minted": minted,
                    "message": (f"Snapshot {snap_id} minted." if snap_id
                                else "Turn saved; auto-mint did not fire this turn (debounce/threshold)."),
                }, indent=2))]

            elif name == "get_context":
                query = arguments["query"]
                operator = await resolve_operator(arguments.get("operator"))

                # Get hybrid search results
                search_response = await asyncio.wait_for(client.get(
                    f"{BLACKBOX_URL}/fossil/hybrid",
                    params={"q": query, "operator": operator, "limit": 5}
                ), timeout=PROXY_TIMEOUT)
                search_response.raise_for_status()
                relevant = search_response.json()

                # Get recent snapshots from local index
                recent = list_snapshots_by_operator(operator, 5)

                context = {
                    "query": query,
                    "operator": operator,
                    "relevant_snapshots": relevant,
                    "recent_snapshots": recent
                }

                return [TextContent(type="text", text=json.dumps(context, indent=2))]

            elif name == "chat_with_context":
                message = arguments["message"]
                operator = await resolve_operator(arguments.get("operator"))
                provider = arguments.get("provider", "anthropic")
                model = arguments.get("model")

                payload = {
                    "operator": operator,
                    "messages": [{"role": "user", "content": message}],
                    "provider": provider
                }
                if model:
                    payload["model"] = model

                response = await client.post(
                    f"{BLACKBOX_URL}/chat",
                    json=payload,
                    timeout=120
                )
                response.raise_for_status()
                result = response.json()

                # Poll for completion if async
                if "task_id" in result:
                    task_id = result["task_id"]
                    for _ in range(120):  # Wait up to 2 minutes
                        await asyncio.sleep(1)
                        task_response = await client.get(f"{BLACKBOX_URL}/tasks/{task_id}")
                        task_data = task_response.json()
                        if task_data.get("status") == "completed":
                            # Response is in result_data.reply or result_data.ui_reply
                            result_data = task_data.get("result_data", {})
                            response_text = result_data.get("reply") or result_data.get("ui_reply") or result_data.get("text") or "No response"
                            return [TextContent(type="text", text=response_text)]
                        elif task_data.get("status") == "failed":
                            error_msg = task_data.get("error_message") or task_data.get("error") or "Unknown error"
                            return _error("tool_error", name, f"Chat failed: {error_msg}")
                    return _error("timeout", name, "Chat request timed out")

                return [TextContent(type="text", text=result.get("response", json.dumps(result)))]

            elif name == "speech_to_text":
                # SPECIAL: multipart file upload to /stt. The generic
                # /local/tools/execute proxy passes JSON params only -- it cannot
                # stream a local audio file -- so this stays a dedicated branch.
                audio_path = arguments["audio_path"]

                audio_file_path = Path(audio_path)
                if not audio_file_path.exists():
                    return _error("not_found", name, f"Audio file not found: {audio_path}")

                # Determine content type from extension
                ext = audio_file_path.suffix.lower()
                content_types = {
                    ".wav": "audio/wav",
                    ".mp3": "audio/mpeg",
                    ".m4a": "audio/mp4",
                    ".ogg": "audio/ogg",
                    ".flac": "audio/flac",
                    ".webm": "audio/webm"
                }
                content_type = content_types.get(ext, "audio/wav")

                with open(audio_file_path, "rb") as f:
                    files = {"file": (audio_file_path.name, f, content_type)}
                    response = await client.post(
                        f"{BLACKBOX_URL}/stt",
                        files=files,
                        timeout=120
                    )
                response.raise_for_status()
                result = response.json()

                return [TextContent(type="text", text=json.dumps({
                    "status": "success",
                    "transcription": result.get("text", result.get("transcription", "")),
                    "details": result
                }, indent=2))]

            # === UNIFORM PROXY DISPATCHER ===
            # Every other registry tool (multimodal generation/analysis, Workspace,
            # web search, gmail_*, status/util, roll_dice, toolvault, control_*,
            # use_computer, ...) routes through the single proxy path. The valid
            # name set is memoized (computed ONCE), not rebuilt per call.
            elif name in _valid_tool_names():
                return await _proxy_tool(name, arguments, client)

            else:
                return _error("unknown_tool", name, f"Unknown tool: {name}")

    except KeyError as e:
        return _error("invalid_arguments", name, f"missing required argument: {e}")
    except httpx.TimeoutException as e:
        return _error("timeout", name, f"backend request timed out: {e}")
    except asyncio.TimeoutError:
        return _error("timeout", name, f"backend did not respond within {PROXY_TIMEOUT:.0f}s")
    except httpx.HTTPStatusError as e:
        return _error("backend_error", name,
                      f"backend returned {e.response.status_code}: {str(e)[:300]}")
    except httpx.HTTPError as e:
        return _error("backend_error", name, f"HTTP error: {e}")
    except Exception as e:
        logger.exception("rid=%s tool=%s | unhandled exception", _REQUEST_ID.get(), name)
        return _error("internal_error", name, str(e))


# =============================================================================
# RESOURCES - Data the agent can read
# =============================================================================

@server.list_resources()
async def list_resources() -> list[Resource]:
    """List available BlackBox resources."""
    return [
        Resource(
            uri="blackbox://index/stats",
            name="Snapshot Index Statistics",
            description="Statistics about the snapshot index - counts, operators, sizes",
            mimeType="application/json"
        ),
        Resource(
            uri="blackbox://index/operators",
            name="Operator List",
            description="List of all operators with snapshot counts",
            mimeType="application/json"
        ),
        Resource(
            uri="blackbox://index/recent",
            name="Recent Snapshots",
            description="Most recent 20 snapshots across all operators",
            mimeType="application/json"
        ),
        Resource(
            uri="blackbox://volume/info",
            name="Volume Information",
            description="Information about the snapshot volume file",
            mimeType="application/json"
        )
    ]


@server.read_resource()
async def read_resource(uri: str) -> ResourceContents:
    """Read a BlackBox resource."""

    try:
        # #2A: resources run in the SAME session task and are equally reachable
        # over HTTP -- apply the SAME bound-operator filter as the matching tools.
        # scope is None on stdio (span all), or the CURRENT request's operator.
        scope = _current_bound_operator()
        if uri == "blackbox://index/stats":
            index = load_snapshot_index()

            operators = {}
            types = {"normal": 0, "checkpoint": 0, "summary": 0}
            total_bytes = 0
            counted = 0

            for entry in index.values():
                op = entry.get("operator", "unknown")
                if scope is not None and op != scope:
                    continue
                counted += 1
                operators[op] = operators.get(op, 0) + 1
                snap_type = entry.get("type", "normal")
                if snap_type in types:
                    types[snap_type] += 1
                total_bytes += entry.get("byte_end", 0) - entry.get("byte_start", 0)

            stats = {
                "total_snapshots": counted,
                "operators": operators,
                "types": types,
                "total_size_mb": round(total_bytes / (1024 * 1024), 2)
            }

            return TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(stats, indent=2))

        elif uri == "blackbox://index/operators":
            index = load_snapshot_index()
            operators = {}
            for entry in index.values():
                op = entry.get("operator", "unknown")
                if scope is not None and op != scope:
                    continue
                operators[op] = operators.get(op, 0) + 1

            return TextResourceContents(
                uri=uri,
                mimeType="application/json",
                text=json.dumps({"operators": operators}, indent=2)
            )

        elif uri == "blackbox://index/recent":
            index = load_snapshot_index()

            entries = []
            for snap_id, entry in index.items():
                if scope is not None and entry.get("operator") != scope:
                    continue
                entries.append({
                    "snap_id": snap_id,
                    "operator": entry.get("operator"),
                    "timestamp": entry.get("timestamp"),
                    "type": entry.get("type", "normal")
                })

            entries.sort(key=lambda x: x["timestamp"], reverse=True)

            return TextResourceContents(
                uri=uri,
                mimeType="application/json",
                text=json.dumps({"recent_snapshots": entries[:20]}, indent=2)
            )

        elif uri == "blackbox://volume/info":
            info = {
                "volume_path": str(VOLUME_FILE),
                "exists": VOLUME_FILE.exists(),
                "size_bytes": VOLUME_FILE.stat().st_size if VOLUME_FILE.exists() else 0,
                "size_mb": round(VOLUME_FILE.stat().st_size / (1024 * 1024), 2) if VOLUME_FILE.exists() else 0,
                "index_path": str(SNAPSHOT_INDEX),
                "index_exists": SNAPSHOT_INDEX.exists()
            }
            return TextResourceContents(uri=uri, mimeType="application/json", text=json.dumps(info, indent=2))

        else:
            return TextResourceContents(uri=uri, mimeType="text/plain", text=f"Unknown resource: {uri}")

    except Exception as e:
        return TextResourceContents(uri=uri, mimeType="text/plain", text=f"Error: {str(e)}")


# =============================================================================
# MAIN
# =============================================================================

def _log_startup_banner(transport: str) -> None:
    """Shared startup log lines (both transports)."""
    logger.info("BlackBox MCP Server starting (transport=%s)...", transport)
    logger.info("BlackBox Root: %s", BLACKBOX_ROOT)
    logger.info("BlackBox API: %s", BLACKBOX_URL)
    logger.info("Volume File: %s (exists: %s)", VOLUME_FILE, VOLUME_FILE.exists())
    logger.info("Index File: %s (exists: %s)", SNAPSHOT_INDEX, SNAPSHOT_INDEX.exists())
    logger.info("Proxy timeout: %.0fs", PROXY_TIMEOUT)
    # Pre-load the index (shared by both transports' local file tools).
    index = load_snapshot_index()
    logger.info("Loaded %d snapshots from index", len(index))


async def main():
    """Run the BlackBox MCP server over STDIO (the default transport).

    Unchanged from M1: this is the Claude Code path. Selecting it requires no
    flags/env, so existing stdio clients are entirely unaffected by M2.
    """
    _log_startup_banner("stdio")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# =============================================================================
# M2: STREAMABLE HTTP TRANSPORT
#
# The SAME `server` instance (low-level mcp.server.Server, with its already
# registered @server.list_tools()/@server.call_tool() handlers) is handed to the
# SDK's StreamableHTTPSessionManager. No tool logic is duplicated: the HTTP
# transport is purely a second way to reach the identical handlers.
#
# We use the LOW-LEVEL session manager (not FastMCP) on purpose: FastMCP owns a
# different programming model (decorator-registered @mcp.tool() functions over a
# FastMCP() object) and rebuilding the 74-tool registry-driven catalog into that
# shape would duplicate logic. StreamableHTTPSessionManager(app=server) accepts
# our exact low-level Server, so the catalog + dispatch are reused verbatim. The
# Starlette wiring below mirrors FastMCP.streamable_http_app() (the canonical
# mounting pattern) MINUS the auth branches (auth is M3).
# =============================================================================
# =============================================================================
# M3: TOKEN STORE + BEARER AUTH MIDDLEWARE (HTTP transport ONLY)
#
# The token->operator map is loaded from a SECURE source that is NOT committed:
#   * env  BLACKBOX_MCP_TOKENS       = {"<token>":"<operator>", ...} (JSON), and/or
#   * file BLACKBOX_MCP_TOKENS_FILE  (default Manifest/mcp_tokens.json), same shape.
# Both are merged (env wins on collision). Manifest/ is gitignored AND
# **/*token*.json is gitignored, so the file can never be committed by accident.
# Validation is CONSTANT-TIME (hmac.compare_digest), and the middleware runs at
# the EDGE of the /mcp route so an unauthenticated request never reaches an MCP
# handler. NONE of this touches the stdio path.
# =============================================================================
import hmac
import hashlib

BLACKBOX_MCP_TOKENS_FILE = os.getenv(
    "BLACKBOX_MCP_TOKENS_FILE", str(BLACKBOX_ROOT / "Manifest" / "mcp_tokens.json")
)


# =============================================================================
# CHUNK 6a: OAuth 2.1 DISCOVERY METADATA + DYNAMIC CLIENT REGISTRATION
#
# This chunk builds ONLY the PUBLIC OAuth bootstrap surface so claude.ai /
# Claude Desktop can DISCOVER this server and REGISTER a client. It does NOT
# build /authorize or /token (later chunks) -- but the discovery metadata and
# the middleware exemption ALREADY advertise + reserve those paths so the
# later chunks are a drop-in.
#
#   GET /.well-known/oauth-authorization-server  -- RFC 8414 AS metadata
#   GET /.well-known/oauth-protected-resource    -- RFC 9728 PR metadata
#   POST /register                               -- RFC 7591 dynamic client reg
#
# These three (and the reserved /authorize + /token) are PUBLIC: they are the
# auth bootstrap, so they must be reachable WITHOUT a bearer. The bearer-auth
# middleware EXEMPTS them; only /mcp still requires a credential. Public clients
# use PKCE (S256) with token_endpoint_auth_method=none -- NO client secret.
#
# The issuer/base URL is the PUBLIC Funnel origin (configurable via env
# BLACKBOX_MCP_PUBLIC_URL) -- NOT the localhost bind -- because that is the URL
# claude.ai actually reaches and the one that must appear in the metadata.
# =============================================================================
# Public origin as seen by claude.ai / Desktop (the Funnel front door). The
# OAuth issuer + all advertised endpoints are built from this, NOT the 127.0.0.1
# bind, because the metadata must point at the URL the remote client can reach.
BLACKBOX_MCP_PUBLIC_URL = os.getenv(
    "BLACKBOX_MCP_PUBLIC_URL",
    "https://ai-black-box-fc-a620ai-wifi.tail401fb3.ts.net:8443",
).rstrip("/")

# The MCP resource URL (issuer + the MCP path) -- the `resource` value in the
# RFC 9728 Protected Resource Metadata. Built from the public origin + the
# configured MCP path so a non-default path stays consistent across surfaces.
BLACKBOX_MCP_RESOURCE_URL = BLACKBOX_MCP_PUBLIC_URL + BLACKBOX_MCP_HTTP_PATH

# Registered OAuth clients are persisted to a GITIGNORED store (Manifest/ is
# gitignored, and **/*token*.json also covers anything token-shaped). The file
# is created 0600. Public PKCE clients have NO secret, but we still keep the
# store private (it records who registered + their redirect_uris).
BLACKBOX_MCP_OAUTH_CLIENTS_FILE = os.getenv(
    "BLACKBOX_MCP_OAUTH_CLIENTS_FILE",
    str(BLACKBOX_ROOT / "Manifest" / "mcp_oauth_clients.json"),
)


def _load_token_map() -> Dict[str, str]:
    """Load + merge the token->operator map from env JSON and/or the secure file.

    Returns {} if neither source yields a valid mapping. A {} map means the HTTP
    transport will reject EVERY request (fail closed) -- never fail open.
    The file is gitignored (Manifest/ + **/*token*.json); the env var lives in
    the deploy environment. Neither is in source.
    """
    merged: Dict[str, str] = {}
    # File source first (env overrides it on key collision).
    try:
        p = Path(BLACKBOX_MCP_TOKENS_FILE)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for tok, op in data.items():
                    if isinstance(tok, str) and tok and isinstance(op, str) and op:
                        merged[tok] = op
    except Exception as e:
        logger.error("Failed to load token file %s: %s", BLACKBOX_MCP_TOKENS_FILE, e)
    # Env source (wins on collision).
    env_raw = os.getenv("BLACKBOX_MCP_TOKENS", "").strip()
    if env_raw:
        try:
            data = json.loads(env_raw)
            if isinstance(data, dict):
                for tok, op in data.items():
                    if isinstance(tok, str) and tok and isinstance(op, str) and op:
                        merged[tok] = op
        except Exception as e:
            logger.error("Failed to parse BLACKBOX_MCP_TOKENS env JSON: %s", e)
    return merged


def _token_id(token: str) -> str:
    """A short, non-reversible IDENTIFIER for a token (audit logs only).

    sha256(token) -> first 12 hex chars. NEVER the token value -- so logs can be
    correlated to a credential without ever recording the secret.
    """
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _match_token(presented: str, token_map: Dict[str, str]):
    """Constant-time token lookup. Returns (operator, token_id) or (None, None).

    hmac.compare_digest is used for EVERY known token (no early break on the
    first non-match) so total work does not depend on which token matched --
    no timing oracle on the secret. An empty/blank presented token never matches.
    """
    if not presented:
        return None, None
    matched_op = None
    matched_tok = None
    for known, operator in token_map.items():
        if hmac.compare_digest(presented, known):
            matched_op = operator
            matched_tok = known
    if matched_op is None:
        return None, None
    return matched_op, _token_id(matched_tok)


def _extract_bearer(scope) -> Optional[str]:
    """Pull the bearer token out of an ASGI scope's Authorization header.

    Returns the token string for a well-formed `Authorization: Bearer <token>`
    (case-insensitive scheme), else None (missing OR malformed -> both -> 401).
    """
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            try:
                raw = v.decode("latin-1")
            except Exception:
                return None
            parts = raw.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                tok = parts[1].strip()
                return tok or None
            return None
    return None


class BearerAuthMiddleware:
    """Pure-ASGI bearer-auth gate wrapping the /mcp app (HTTP transport ONLY).

    Rejects any HTTP request lacking a valid `Authorization: Bearer <token>`
    with 401 + `WWW-Authenticate: Bearer` BEFORE the request reaches the MCP
    session manager. On success it resolves token->operator and sets BOTH the
    request-id and bound-operator contextvars (so call_tool() and resolve_operator
    see the bound operator), and emits one audit log line per request.

    Pure ASGI (not BaseHTTPMiddleware) so it can short-circuit with a 401
    without ever instantiating an MCP session, and so it sees the raw scope.
    The token map is loaded ONCE at construction (server start); rotating a
    token is a restart -- acceptable for a single long-lived deploy.
    """

    def __init__(self, app, token_map: Dict[str, str]):
        self.app = app
        self.token_map = token_map

    async def _reject_401(self, send, detail: str) -> None:
        body = json.dumps({"error": {"code": "unauthorized", "message": detail}}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        # Only guard HTTP requests; lifespan/other scopes pass straight through.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        rid = uuid.uuid4().hex[:12]
        _REQUEST_ID.set(rid)

        # CHUNK 6a: EXEMPT the public OAuth bootstrap surface (discovery
        # metadata, dynamic client registration, and the reserved /authorize +
        # /token). These MUST be reachable WITHOUT a bearer -- they are how a
        # client discovers + obtains a credential. Only /mcp still requires one.
        # We pass straight through to the app (which routes to the OAuth handlers)
        # WITHOUT setting _BOUND_OPERATOR/_TOKEN_ID, so these requests are never
        # mistaken for an authenticated MCP request.
        if _is_oauth_public_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        presented = _extract_bearer(scope)
        if not presented:
            logger.warning("rid=%s AUTH reject: missing/malformed bearer (%s %s)",
                           rid, scope.get("method"), scope.get("path"))
            await self._reject_401(send, "Missing or malformed Authorization: Bearer token")
            return

        # Credential check, in order:
        #   (a) STATIC bearer map (M3) -- UNCHANGED. A hit binds the static
        #       map's operator.
        #   (b) ELSE a VALID (present + UNEXPIRED) OAuth access token (chunk
        #       6c) -- binds the OAuth store's operator. Routed to the SAME
        #       request.state.bound_operator + token_id below, so the M3
        #       operator-isolation applies IDENTICALLY -- an OAuth token is
        #       NEVER a bypass of the per-operator scoping.
        #   (c) ELSE 401 (expired/bogus OAuth token falls here -> invalid).
        operator, token_id = _match_token(presented, self.token_map)
        auth_kind = "static"
        if operator is None:
            operator, token_id = _match_oauth_access_token(presented)
            auth_kind = "oauth"
        if operator is None:
            logger.warning("rid=%s AUTH reject: unknown token tid=%s (%s %s)",
                           rid, _token_id(presented), scope.get("method"), scope.get("path"))
            await self._reject_401(send, "Unknown or invalid bearer token")
            return

        # #2B (Option A): stash the operator + token id on THIS request's state.
        # The SDK threads the current Starlette Request through to the handler
        # (request_ctx.request, set fresh per JSON-RPC message), so handlers read
        # the CURRENT request's operator via _current_bound_operator() -- NOT the
        # session-frozen value. This is what defeats session hijack / stale
        # binding: a token-B request on a token-A session executes as B.
        # request.state writes persist in scope["state"], which the SDK's later
        # Request(scope, receive) reads back.
        from starlette.requests import Request as _StarletteRequest
        _req = _StarletteRequest(scope)
        _req.state.bound_operator = operator
        _req.state.token_id = token_id
        # _BOUND_OPERATOR/_TOKEN_ID remain set as the "this is HTTP" transport
        # signal + audit context. Their VALUE is NOT trusted for per-call operator
        # resolution (it can be session-stale); _current_bound_operator() is.
        _BOUND_OPERATOR.set(operator)
        _TOKEN_ID.set(token_id)
        # AUDIT (M3): token id (never the value) + bound operator + method/path + rid.
        logger.info("rid=%s AUDIT auth-ok kind=%s tid=%s operator=%s %s %s",
                    rid, auth_kind, token_id, operator, scope.get("method"), scope.get("path"))
        await self.app(scope, receive, send)


# =============================================================================
# CHUNK 6a: OAuth client store + discovery/registration handlers
# =============================================================================
# Paths exempt from bearer auth -- the OAuth bootstrap surface. The middleware
# lets these through WITHOUT a credential (they ARE how a client gets one).
# /authorize + /token are reserved here NOW (later chunks implement them) so the
# exemption is already in place when they land. Everything else (notably /mcp)
# still requires a bearer.
OAUTH_PUBLIC_PATHS = {"/register", "/authorize", "/token"}
OAUTH_PUBLIC_PREFIXES = ("/.well-known/",)


def _is_oauth_public_path(p: str) -> bool:
    """True iff the request path is part of the public OAuth bootstrap surface."""
    if p in OAUTH_PUBLIC_PATHS:
        return True
    return any(p.startswith(prefix) for prefix in OAUTH_PUBLIC_PREFIXES)


def _load_oauth_clients() -> Dict[str, dict]:
    """Load the registered-client store (client_id -> registration dict).

    Returns {} if the file is missing or unreadable -- a fresh box simply has no
    registered clients yet (registration creates the file on first POST).
    """
    try:
        p = Path(BLACKBOX_MCP_OAUTH_CLIENTS_FILE)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.error("Failed to load OAuth client store %s: %s",
                     BLACKBOX_MCP_OAUTH_CLIENTS_FILE, e)
    return {}


def _persist_oauth_client(client_id: str, record: dict) -> None:
    """Persist one registered client to the gitignored store, 0600.

    Read-modify-write the whole map (DCR is low-volume -- one record per client
    that ever connects). The file is created with 0600 perms (owner-only); the
    parent Manifest/ is gitignored, as is **/*token*.json, so the store is doubly
    excluded from git and never world-readable.
    """
    p = Path(BLACKBOX_MCP_OAUTH_CLIENTS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    clients = _load_oauth_clients()
    clients[client_id] = record
    payload = json.dumps(clients, indent=2)
    # Create with 0600 from the start (umask-independent) so the secret-bearing
    # store is never briefly world-readable. os.open + O_CREAT|O_TRUNC|O_WRONLY.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    finally:
        # Re-assert 0600 even if the file pre-existed with looser perms.
        try:
            os.chmod(str(p), 0o600)
        except OSError:
            pass


def _oauth_as_metadata() -> dict:
    """RFC 8414 Authorization Server Metadata for the public OAuth surface.

    issuer = the public Funnel origin; all endpoints are issuer + path. We
    advertise ONLY what this server supports: authorization_code + PKCE/S256,
    public clients (token_endpoint_auth_method=none, no secret).
    """
    issuer = BLACKBOX_MCP_PUBLIC_URL
    return {
        "issuer": issuer,
        "authorization_endpoint": issuer + "/authorize",
        "token_endpoint": issuer + "/token",
        "registration_endpoint": issuer + "/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def _oauth_protected_resource_metadata() -> dict:
    """RFC 9728 Protected Resource Metadata -- points the client at the AS.

    resource = the MCP URL the client calls; authorization_servers = [issuer], so
    a 401 from /mcp tells the client where to discover + run OAuth.
    """
    return {
        "resource": BLACKBOX_MCP_RESOURCE_URL,
        "authorization_servers": [BLACKBOX_MCP_PUBLIC_URL],
        "bearer_methods_supported": ["header"],
    }


async def _oauth_as_metadata_handler(request):
    """GET /.well-known/oauth-authorization-server (RFC 8414)."""
    from starlette.responses import JSONResponse
    return JSONResponse(
        _oauth_as_metadata(),
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _oauth_pr_metadata_handler(request):
    """GET /.well-known/oauth-protected-resource (RFC 9728)."""
    from starlette.responses import JSONResponse
    return JSONResponse(
        _oauth_protected_resource_metadata(),
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _oauth_register_handler(request):
    """POST /register -- RFC 7591 Dynamic Client Registration (public PKCE).

    Accepts {redirect_uris, client_name, ...}. redirect_uris is REQUIRED and must
    be a non-empty list (RFC 7591 + the auth_code flow needs at least one). We
    mint a client_id, force token_endpoint_auth_method=none (public client, NO
    secret), persist the registration to the gitignored 0600 store, and return
    the RFC 7591 client information response (201).
    """
    from starlette.responses import JSONResponse
    import secrets as _secrets
    import time as _time

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "invalid_client_metadata",
             "error_description": "Request body must be JSON"},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "invalid_client_metadata",
             "error_description": "Request body must be a JSON object"},
            status_code=400,
        )

    redirect_uris = body.get("redirect_uris")
    if (not isinstance(redirect_uris, list) or not redirect_uris
            or not all(isinstance(u, str) and u for u in redirect_uris)):
        return JSONResponse(
            {"error": "invalid_redirect_uri",
             "error_description": "redirect_uris is required and must be a "
                                  "non-empty list of strings"},
            status_code=400,
        )

    # Public PKCE client: NO secret. We accept (but normalize) the auth method.
    auth_method = body.get("token_endpoint_auth_method", "none")
    if auth_method != "none":
        return JSONResponse(
            {"error": "invalid_client_metadata",
             "error_description": "Only public clients are supported "
                                  "(token_endpoint_auth_method must be 'none')"},
            status_code=400,
        )

    client_id = "mcp-" + uuid.uuid4().hex
    issued_at = int(_time.time())
    # Echo back the standard RFC 7591 metadata fields the client sent, with our
    # enforced/derived values for the auth-method + grant/response types.
    record = {
        "client_id": client_id,
        "client_id_issued_at": issued_at,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }
    for field in ("client_name", "client_uri", "logo_uri", "scope",
                  "contacts", "tos_uri", "policy_uri", "software_id",
                  "software_version"):
        if field in body and body[field] is not None:
            record[field] = body[field]

    try:
        _persist_oauth_client(client_id, record)
    except Exception as e:
        logger.error("Failed to persist OAuth client %s: %s", client_id, e)
        return JSONResponse(
            {"error": "invalid_client_metadata",
             "error_description": "Failed to persist client registration"},
            status_code=500,
        )

    logger.info("OAuth DCR: registered client_id=%s (%d redirect_uri(s), name=%r)",
                client_id, len(redirect_uris), record.get("client_name"))
    # RFC 7591 success: 201 Created with the client information response.
    return JSONResponse(record, status_code=201)


# =============================================================================
# CHUNK 6b: OAuth 2.1 AUTHORIZATION CODE FLOW (/authorize + /token) + PKCE S256
#
# This chunk implements the two endpoints the discovery metadata (6a) already
# advertises, completing the Authorization Code + PKCE flow:
#
#   GET  /authorize  -- consent + mint a single-use authorization CODE
#   POST /token      -- exchange the code (with the PKCE verifier) for an
#                       opaque ACCESS TOKEN bound to an operator
#
# SECURITY MODEL (the review checks all of these):
#   * PKCE S256 is MANDATORY -- /authorize rejects a missing code_challenge or
#     any code_challenge_method != S256; /token re-derives S256(verifier) and
#     compares it CONSTANT-TIME against the bound challenge (hmac.compare_digest).
#   * `state` is REQUIRED at /authorize and echoed back on the redirect.
#   * redirect_uri must EXACTLY match one of the client's registered URIs (no
#     open redirect, no substring/prefix match) -- else a direct 400 (never a
#     redirect to an unvetted URI).
#   * Authorization codes are HIGH-ENTROPY, SHORT-TTL (AUTH_CODE_TTL), and
#     SINGLE-USE: /token consumes the code (pop) before doing anything else, so a
#     replay finds nothing. Each code is bound to {client_id, redirect_uri,
#     code_challenge, operator, scope}.
#   * Access tokens are opaque, high-entropy (prefix bbmcp_oat_), EXPIRE
#     (ACCESS_TOKEN_TTL), and are stored as token->{operator, expiry} so chunk
#     6c can validate them on /mcp. The token VALUE is never logged (only the
#     sha256[:12] id, matching the M3 _token_id pattern).
#   * Fail-closed: any validation gap -> reject. The operator is bound at
#     /authorize (env BLACKBOX_MCP_OAUTH_OPERATOR, default Brandon) and carried
#     through to the access token; the remote client never chooses it.
#
# Codes live IN-MEMORY only (short-lived, single process). Access tokens are
# kept in a process-local dict AND mirrored to a gitignored 0600 file
# (Manifest/mcp_oauth_tokens.json) so chunk 6c can read the binding. NO token
# value is ever written to a COMMITTED file (the store is gitignored).
# =============================================================================
import base64 as _b64
import secrets as _secrets
import threading as _threading
import time as _time

# The operator every OAuth-minted access token is bound to. The remote client
# never chooses this -- it is the box's configured OAuth operator. Default
# "Brandon" only as the unconfigured-box seed; a real deploy sets the env.
BLACKBOX_MCP_OAUTH_OPERATOR = os.getenv("BLACKBOX_MCP_OAUTH_OPERATOR", "Brandon")

# Authorization codes are single-use + short-lived (seconds). Access tokens get
# a longer (but still bounded) lifetime. Both are overridable per-deploy.
AUTH_CODE_TTL = int(os.getenv("BLACKBOX_MCP_OAUTH_CODE_TTL", "60"))          # 60s
ACCESS_TOKEN_TTL = int(os.getenv("BLACKBOX_MCP_OAUTH_TOKEN_TTL", "3600"))    # 1h

# The access-token store file -- gitignored (Manifest/ AND **/*token*.json both
# cover it). Chunk 6c reads token->{operator, expiry} from here (and/or the
# in-process mirror) to validate an OAuth access token on /mcp.
BLACKBOX_MCP_OAUTH_TOKENS_FILE = os.getenv(
    "BLACKBOX_MCP_OAUTH_TOKENS_FILE",
    str(BLACKBOX_ROOT / "Manifest" / "mcp_oauth_tokens.json"),
)

# In-memory authorization-code store: code -> binding dict. Codes are short-TTL
# and single-use, so they never need to survive a restart; keeping them only in
# memory means a replay across a restart finds nothing (fail-closed). A lock
# guards concurrent /authorize (write) vs /token (pop) across uvicorn workers in
# one process.
_AUTH_CODES: Dict[str, dict] = {}
_AUTH_CODES_LOCK = _threading.Lock()

# In-process access-token mirror: access_token -> {operator, expiry}. The
# authoritative copy is also written to the gitignored file so chunk 6c can read
# it without sharing this process's memory.
_ACCESS_TOKENS: Dict[str, dict] = {}
_ACCESS_TOKENS_LOCK = _threading.Lock()


def _s256_challenge(verifier: str) -> str:
    """Derive the PKCE S256 code_challenge from a verifier (RFC 7636 4.2).

    BASE64URL( SHA256( ASCII(verifier) ) ) with '=' padding stripped -- exactly
    the transform the SDK token handler uses, so a challenge minted by any
    compliant client validates here.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _prune_expired_auth_codes(now: float) -> None:
    """Drop expired authorization codes (called under _AUTH_CODES_LOCK)."""
    dead = [c for c, b in _AUTH_CODES.items() if b.get("expires_at", 0) < now]
    for c in dead:
        _AUTH_CODES.pop(c, None)


def _persist_access_token(token: str, operator: str, expiry: float) -> None:
    """Mirror one access token -> {operator, expiry} to the gitignored 0600 file.

    Read-modify-write the whole map (token issuance is low-volume). Expired
    tokens are pruned on every write so the file does not grow unbounded. The
    file is (re)created 0600 (owner-only) -- it carries live credentials, so it
    must never be world-readable, and Manifest/ + **/*token*.json keep it out of
    git. Chunk 6c reads this file (and/or the in-process mirror) to validate.
    """
    p = Path(BLACKBOX_MCP_OAUTH_TOKENS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Load existing, prune expired, add the new one.
    existing: Dict[str, dict] = {}
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = data
    except Exception as e:
        logger.error("Failed to read OAuth token store %s: %s",
                     BLACKBOX_MCP_OAUTH_TOKENS_FILE, e)
    now = _time.time()
    existing = {t: b for t, b in existing.items()
                if isinstance(b, dict) and b.get("expiry", 0) > now}
    existing[token] = {"operator": operator, "expiry": expiry}
    payload = json.dumps(existing, indent=2)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    finally:
        try:
            os.chmod(str(p), 0o600)
        except OSError:
            pass


def _match_oauth_access_token(presented: str):
    """Validate an OAuth access token (chunk 6c). Returns (operator, token_id)
    for a PRESENT + UNEXPIRED token, else (None, None).

    Mirrors the M3 static-bearer contract (_match_token): a hit yields the
    bound operator + the SAME sha256[:12] `_token_id`, so the OAuth branch
    feeds request.state.bound_operator on the IDENTICAL path -- OAuth tokens
    are subject to the same per-operator isolation, never a bypass.

    Expiry is enforced STRICTLY (expiry <= now -> treated as invalid -> the
    caller 401s). The in-process `_ACCESS_TOKENS` mirror is authoritative for
    this process; we fall back to the gitignored file store so a token issued
    by another worker/process still validates. Expired entries are pruned/
    ignored. The token VALUE is never logged (only its `_token_id`).
    """
    if not presented:
        return None, None
    now = _time.time()
    # 1) In-process mirror (authoritative for this process). Constant-ish work
    #    is unnecessary here: the token is a high-entropy opaque secret looked
    #    up by exact key, and a miss falls through to the file store.
    binding = None
    with _ACCESS_TOKENS_LOCK:
        b = _ACCESS_TOKENS.get(presented)
        if isinstance(b, dict):
            if b.get("expiry", 0) > now:
                binding = b
            else:
                # Expired -> prune the dead entry, treat as invalid.
                _ACCESS_TOKENS.pop(presented, None)
    # 2) File-store fallback (token issued by another worker/process).
    if binding is None:
        try:
            p = Path(BLACKBOX_MCP_OAUTH_TOKENS_FILE)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    fb = data.get(presented)
                    if isinstance(fb, dict) and fb.get("expiry", 0) > now:
                        binding = fb
                        # Warm the in-process mirror for subsequent requests.
                        with _ACCESS_TOKENS_LOCK:
                            _ACCESS_TOKENS[presented] = {
                                "operator": fb.get("operator"),
                                "expiry": fb.get("expiry"),
                            }
        except Exception as e:
            logger.error("OAuth token validation: failed to read token store %s: %s",
                         BLACKBOX_MCP_OAUTH_TOKENS_FILE, e)
    if binding is None:
        return None, None
    operator = binding.get("operator")
    if not operator:
        return None, None
    return operator, _token_id(presented)


def _consent_form_html(client_name, fields, error=None):
    """Render the /authorize consent form. The operator must enter their token."""
    import html as _html
    err = f'<p style="color:#c0392b">{_html.escape(error)}</p>' if error else ""
    hidden = "".join(
        f'<input type="hidden" name="{_html.escape(k)}" value="{_html.escape(v)}">'
        for k, v in fields.items() if v is not None
    )
    cn = _html.escape(client_name)
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Authorize BlackBox MCP</title></head>'
        '<body style="font-family:system-ui,sans-serif;max-width:460px;'
        'margin:64px auto;padding:0 16px">'
        f'<h2>Authorize {cn}</h2>{err}'
        f'<p>Enter your BlackBox token to authorize <b>{cn}</b> to access the '
        'BlackBox MCP server as your operator.</p>'
        '<form method="POST" action="/authorize">'
        f'{hidden}'
        '<input type="password" name="blackbox_token" placeholder="bbmcp_..." '
        'autocomplete="off" autofocus required '
        'style="width:100%;padding:10px;box-sizing:border-box;font-family:monospace">'
        '<button type="submit" style="margin-top:14px;padding:10px 20px">'
        'Authorize</button></form></body></html>'
    )


async def _oauth_authorize_handler(request):
    """GET /authorize -- OAuth 2.1 Authorization Code flow (PKCE S256 mandatory).

    Validates response_type=code, a registered client_id, an EXACT-match
    redirect_uri, a REQUIRED state, and a MANDATORY PKCE S256 code_challenge.
    Auto-approves (binding the box's OAuth operator) and 302-redirects to
    redirect_uri?code=<single-use code>&state=<state>.

    redirect_uri / client_id failures return a DIRECT 400 (never a redirect to
    an unvetted URI -- no open redirect). Other failures, once the redirect_uri
    is vetted, redirect the error back per RFC 6749 4.1.2.1 with state echoed.
    """
    from starlette.responses import JSONResponse, RedirectResponse, HTMLResponse
    from urllib.parse import urlencode

    is_post = request.method == "POST"
    params = (await request.form()) if is_post else request.query_params
    response_type = params.get("response_type")
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    state = params.get("state")
    code_challenge = params.get("code_challenge")
    code_challenge_method = params.get("code_challenge_method")
    scope = params.get("scope") or ""

    def _direct_error(error: str, desc: str, status: int = 400):
        # Used when we CANNOT safely redirect (bad/unknown client or redirect_uri).
        return JSONResponse(
            {"error": error, "error_description": desc},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    def _redirect_error(error: str, desc: str):
        # Used once redirect_uri is VETTED -- bounce the error back to the client.
        q = {"error": error, "error_description": desc}
        if state is not None:
            q["state"] = state
        sep = "&" if ("?" in redirect_uri) else "?"
        return RedirectResponse(
            url=f"{redirect_uri}{sep}{urlencode(q)}",
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    # 1) client_id must reference a registered client.
    if not client_id:
        return _direct_error("invalid_request", "client_id is required")
    clients = _load_oauth_clients()
    client = clients.get(client_id)
    if not client:
        return _direct_error("invalid_request",
                             f"Client ID {client_id!r} not found")

    # 2) redirect_uri must be present AND EXACTLY match a registered URI.
    #    EXACT match only -- no substring/prefix -- so there is no open redirect.
    #    This is validated BEFORE we ever redirect, and a failure is a DIRECT
    #    400 (we will not bounce to an unvetted URI).
    registered = client.get("redirect_uris") or []
    if not redirect_uri:
        return _direct_error("invalid_request", "redirect_uri is required")
    if redirect_uri not in registered:
        return _direct_error(
            "invalid_request",
            "redirect_uri does not exactly match a registered redirect URI")

    # ---- from here, redirect_uri is VETTED: errors bounce back to the client ----

    # 3) response_type must be 'code'.
    if response_type != "code":
        return _redirect_error("unsupported_response_type",
                               "response_type must be 'code'")

    # 4) state is REQUIRED (CSRF defense; echoed back).
    if not state:
        return _redirect_error("invalid_request", "state is required")

    # 5) PKCE is MANDATORY: a code_challenge MUST be present and the method MUST
    #    be S256 (we do not accept 'plain'). Reject otherwise.
    if not code_challenge:
        return _redirect_error("invalid_request",
                               "code_challenge is required (PKCE is mandatory)")
    if code_challenge_method != "S256":
        return _redirect_error(
            "invalid_request",
            "code_challenge_method must be 'S256' (PKCE S256 is mandatory)")

    # ---- consent + AUTHENTICATION (closes the public auto-approve bypass) ----
    client_name = client.get("client_name") or client_id
    _fields = {"response_type": response_type, "client_id": client_id,
               "redirect_uri": redirect_uri, "state": state,
               "code_challenge": code_challenge,
               "code_challenge_method": code_challenge_method, "scope": scope}
    if not is_post:
        # GET -> render the consent form; NO code is issued without authentication.
        return HTMLResponse(_consent_form_html(client_name, _fields),
                            headers={"Cache-Control": "no-store"})
    # POST -> the operator must prove their BlackBox bearer token; the issued code
    # binds to the AUTHENTICATED token's operator (not a fixed env operator).
    operator, _tid = _match_token(
        (params.get("blackbox_token") or "").strip(), _load_token_map())
    if not operator:
        logger.warning("OAuth authorize: rejected consent (invalid token) client=%s",
                       client_id)
        return HTMLResponse(
            _consent_form_html(client_name, _fields,
                               error="Invalid token. Enter a valid BlackBox token."),
            status_code=401, headers={"Cache-Control": "no-store"})

    # Mint a single-use, short-TTL authorization code bound to EVERYTHING.
    code = "bbmcp_code_" + _secrets.token_urlsafe(32)
    now = _time.time()
    binding = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "operator": operator,
        "scope": scope,
        "expires_at": now + AUTH_CODE_TTL,
    }
    with _AUTH_CODES_LOCK:
        _prune_expired_auth_codes(now)
        _AUTH_CODES[code] = binding

    logger.info("OAuth authorize: issued code id=%s client=%s operator=%s "
                "(consent auto-approved)",
                _token_id(code), client_id, operator)

    # Redirect back to the (vetted) redirect_uri with code + state.
    q = {"code": code, "state": state}
    sep = "&" if ("?" in redirect_uri) else "?"
    return RedirectResponse(
        url=f"{redirect_uri}{sep}{urlencode(q)}",
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


async def _oauth_token_handler(request):
    """POST /token -- exchange an authorization code (grant_type=authorization_code)
    for an opaque access token, verifying PKCE S256.

    Validates: grant_type, code exists + unexpired + SINGLE-USE (consumed up
    front), client_id matches the code, redirect_uri matches the code, and the
    PKCE code_verifier S256-hashes to the bound code_challenge (CONSTANT-TIME).
    On success: mint an opaque high-entropy access token (prefix bbmcp_oat_),
    store token->{operator, expiry} (in-process + gitignored 0600 file for
    chunk 6c), and return the RFC 6749 token response.
    """
    from starlette.responses import JSONResponse

    def _err(error: str, desc: str, status: int = 400):
        return JSONResponse(
            {"error": error, "error_description": desc},
            status_code=status,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    # Accept form-encoded (RFC 6749) and tolerate JSON bodies.
    try:
        form = await request.form()
        body = {k: v for k, v in form.items()}
    except Exception:
        body = {}
    if not body:
        try:
            j = await request.json()
            if isinstance(j, dict):
                body = j
        except Exception:
            body = {}

    grant_type = body.get("grant_type")
    if grant_type != "authorization_code":
        return _err("unsupported_grant_type",
                    "Only grant_type=authorization_code is supported")

    code = body.get("code")
    client_id = body.get("client_id")
    redirect_uri = body.get("redirect_uri")
    code_verifier = body.get("code_verifier")

    if not code:
        return _err("invalid_request", "code is required")
    if not client_id:
        return _err("invalid_request", "client_id is required")
    if not code_verifier:
        return _err("invalid_request",
                    "code_verifier is required (PKCE is mandatory)")

    # SINGLE-USE: consume (pop) the code up front under the lock. A second
    # exchange with the same code finds nothing -> invalid_grant. Expired codes
    # are also pruned here, so an expired code reads as non-existent.
    now = _time.time()
    with _AUTH_CODES_LOCK:
        _prune_expired_auth_codes(now)
        binding = _AUTH_CODES.pop(code, None)
    if binding is None:
        return _err("invalid_grant",
                    "authorization code is invalid, expired, or already used")

    # Defensive expiry recheck (pop happened under lock; this is belt-and-braces).
    if binding.get("expires_at", 0) < now:
        return _err("invalid_grant", "authorization code has expired")

    # client_id bound to the code must match.
    if binding.get("client_id") != client_id:
        return _err("invalid_grant",
                    "authorization code was not issued to this client")

    # redirect_uri must match the one bound at /authorize (RFC 6749 10.6).
    if (redirect_uri or None) != (binding.get("redirect_uri") or None):
        return _err("invalid_grant",
                    "redirect_uri does not match the one used at /authorize")

    # PKCE VERIFY: S256(verifier) must equal the bound challenge -- CONSTANT-TIME.
    expected = binding.get("code_challenge") or ""
    derived = _s256_challenge(code_verifier)
    if not hmac.compare_digest(derived, expected):
        return _err("invalid_grant", "PKCE code_verifier does not match")

    # ---- success: mint + store an opaque, expiring access token ----
    operator = binding.get("operator") or BLACKBOX_MCP_OAUTH_OPERATOR
    access_token = "bbmcp_oat_" + _secrets.token_urlsafe(40)
    expiry = now + ACCESS_TOKEN_TTL
    with _ACCESS_TOKENS_LOCK:
        # Prune expired in-process entries too, then add the new one.
        for t in [t for t, b in _ACCESS_TOKENS.items() if b.get("expiry", 0) <= now]:
            _ACCESS_TOKENS.pop(t, None)
        _ACCESS_TOKENS[access_token] = {"operator": operator, "expiry": expiry}
    try:
        _persist_access_token(access_token, operator, expiry)
    except Exception as e:
        # If we cannot persist the binding, fail CLOSED -- an access token chunk
        # 6c cannot validate is worse than no token.
        with _ACCESS_TOKENS_LOCK:
            _ACCESS_TOKENS.pop(access_token, None)
        logger.error("OAuth token: failed to persist access token id=%s: %s",
                     _token_id(access_token), e)
        return _err("server_error", "failed to persist issued token", status=500)

    logger.info("OAuth token: issued access token id=%s client=%s operator=%s "
                "expires_in=%ds (code id=%s consumed)",
                _token_id(access_token), client_id, operator,
                ACCESS_TOKEN_TTL, _token_id(code))

    # RFC 6749 5.1 success token response (no refresh token -- the public PKCE
    # MCP clients re-run the short auth_code flow; a refresh token would be one
    # more long-lived secret to store with no current consumer).
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "scope": binding.get("scope") or "",
        },
        status_code=200,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )



def build_http_app(path: str = None):
    """Build the Starlette ASGI app hosting the SAME `server` over Streamable HTTP.

    Returns (starlette_app, session_manager). The session manager's run() is the
    Starlette lifespan, so the manager's task group lives for the app's lifetime.

    M3 wraps the /mcp endpoint in BearerAuthMiddleware. CHUNK 6a adds the PUBLIC
    OAuth bootstrap routes (discovery metadata + dynamic client registration) as
    SEPARATE, UN-wrapped routes -- so they are reachable WITHOUT a bearer, while
    /mcp stays guarded. (The middleware ALSO exempts these paths as defense in
    depth, in case a later chunk routes everything through one wrapper.)
    """
    # Imported lazily so the stdio path never depends on these (they ARE in the
    # lean MCP/venv as transitive deps of `mcp`, but the import stays scoped to
    # the HTTP runner to keep the default path minimal).
    import contextlib
    from starlette.applications import Starlette
    from starlette.routing import Route
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    if path is None:
        path = BLACKBOX_MCP_HTTP_PATH

    # One session manager per app (SDK contract). Stateful (default): a session
    # id is issued on initialize and carried by the client thereafter.
    session_manager = StreamableHTTPSessionManager(app=server)

    # A RAW ASGI app (a CLASS INSTANCE with __call__(scope, receive, send)) --
    # this is exactly what the SDK's own StreamableHTTPASGIApp is, and how
    # FastMCP.streamable_http_app() mounts it. Starlette's Route treats a class
    # instance (NOT a function) as a raw 3-arg ASGI app -- the contract
    # handle_request expects. (A plain `async def` passed to Route would be
    # mis-detected as a func(request)->response endpoint -> TypeError; a Mount
    # would 307-redirect /mcp -> /mcp/. The class-instance-on-Route avoids both
    # and serves the endpoint at EXACTLY `path`.)
    class _MCPASGIApp:
        def __init__(self, sm):
            self._sm = sm

        async def __call__(self, scope, receive, send):
            await self._sm.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            logger.info("Streamable HTTP session manager running at path %s", path)
            yield

    # M3: load the token->operator map ONCE and wrap the MCP endpoint in the
    # bearer-auth gate. A loaded map of size 0 means the transport FAILS CLOSED
    # (every request 401s) -- we log that loudly so a misconfigured deploy is
    # obvious rather than silently rejecting everything.
    token_map = _load_token_map()
    if not token_map:
        logger.error("M3 AUTH: token map is EMPTY -- the HTTP transport will reject "
                     "EVERY request (fail-closed). Set BLACKBOX_MCP_TOKENS (env JSON) "
                     "or %s.", BLACKBOX_MCP_TOKENS_FILE)
    else:
        logger.info("M3 AUTH: loaded %d bearer token(s) bound to operator(s): %s",
                    len(token_map), sorted(set(token_map.values())))

    guarded_endpoint = BearerAuthMiddleware(_MCPASGIApp(session_manager), token_map)

    # CHUNK 6a: the PUBLIC OAuth bootstrap routes. These are plain function
    # endpoints (Starlette func(request)->Response), registered SEPARATELY from
    # the bearer-guarded /mcp endpoint -- so they are reachable WITHOUT a token.
    # /authorize + /token are NOT built yet (later chunks); only discovery +
    # registration ship here. The metadata advertises /authorize + /token so the
    # client knows where they WILL be.
    oauth_routes = [
        Route("/.well-known/oauth-authorization-server",
              endpoint=_oauth_as_metadata_handler, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource",
              endpoint=_oauth_pr_metadata_handler, methods=["GET"]),
        Route("/register",
              endpoint=_oauth_register_handler, methods=["POST"]),
        # CHUNK 6b: the Authorization Code flow endpoints (PKCE S256).
        # Public (no bearer) -- they ARE how a client obtains a credential.
        Route("/authorize",
              endpoint=_oauth_authorize_handler, methods=["GET", "POST"]),
        Route("/token",
              endpoint=_oauth_token_handler, methods=["POST"]),
    ]

    app = Starlette(
        debug=False,
        routes=[
            Route(path, endpoint=guarded_endpoint,
                  methods=["GET", "POST", "DELETE"]),
            *oauth_routes,
        ],
        lifespan=lifespan,
    )
    return app, session_manager


def run_http(host: str = None, port: int = None, path: str = None):
    """Run the Streamable HTTP transport via uvicorn (localhost-only for M2)."""
    import uvicorn

    host = host or BLACKBOX_MCP_HTTP_HOST
    port = port if port is not None else BLACKBOX_MCP_HTTP_PORT
    path = path or BLACKBOX_MCP_HTTP_PATH

    _log_startup_banner("streamable-http")
    logger.info("Streamable HTTP transport binding http://%s:%d%s (M3: bearer auth + "
                "token-bound operator + audit)", host, port, path)
    if host not in ("127.0.0.1", "localhost", "::1"):
        # M2 guard-rail: we are not supposed to expose beyond localhost yet.
        logger.warning("HTTP host is %s -- M2 is localhost-only; non-localhost bind is "
                       "premature (auth is M3, public exposure is M4).", host)

    app, _sm = build_http_app(path)
    config = uvicorn.Config(app, host=host, port=port, log_level=LOG_LEVEL.lower())
    uvicorn.Server(config).run()


def _select_transport() -> str:
    """Resolve the transport: CLI flag > env > default(stdio).

    --transport {stdio,http} or --http (flag alias for http). The env var
    BLACKBOX_MCP_TRANSPORT=http selects HTTP when no CLI flag is given. Default
    is stdio so Claude Code (and every existing stdio client) is unaffected.
    """
    parser = argparse.ArgumentParser(
        prog="blackbox_mcp_server",
        description="BlackBox MCP server (stdio default; opt-in Streamable HTTP).",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=None,
        help="Transport to run. Default: stdio (env BLACKBOX_MCP_TRANSPORT also honored).",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Alias for --transport http (run the Streamable HTTP transport).",
    )
    args = parser.parse_args()

    if args.transport:
        return args.transport
    if args.http:
        return "http"
    env = os.getenv("BLACKBOX_MCP_TRANSPORT", "").strip().lower()
    if env in ("http", "stdio"):
        return env
    return "stdio"


if __name__ == "__main__":
    _transport = _select_transport()
    if _transport == "http":
        # uvicorn owns the event loop; run_http() is synchronous.
        run_http()
    else:
        asyncio.run(main())
