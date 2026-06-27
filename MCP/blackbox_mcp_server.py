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


async def resolve_operator(provided):
    """Server-side safety net: resolve the operator for a tool call.

    HTTP (bound) path -- M3 ANTI-SPOOF: when a token-bound operator is set
    (_BOUND_OPERATOR contextvar), RETURN IT and IGNORE `provided` entirely, so a
    remote caller cannot act as a different operator by asserting one in the
    tool args. This is the single chokepoint every write/proxy tool flows
    through (mint_snapshot, get_context, chat_with_context, _proxy_tool, ...).

    stdio (trusted) path -- UNCHANGED: when omitted, single operator -> that;
    multiple -> system default; caller-asserted operator honoured. (Interactive
    dropdown for the multiple case is handled agent-side, not here.)
    """
    bound = _BOUND_OPERATOR.get()
    if bound is not None:
        return bound
    operators, default = await _fetch_operators()
    resolved, _needs = choose_operator(provided, operators, default)
    return resolved


def _read_scope_operator(provided: str) -> str:
    """Operator FILTER for read tools that span ALL operators when blank.

    M3 ANTI-LEAK: on the HTTP (bound) path, FORCE the token's bound operator and
    IGNORE `provided` -- otherwise a remote caller could read every operator's
    snapshots via search_snapshots / browse_index by leaving operator blank.
    On the stdio (trusted) path the contextvar is unset, so `provided` is
    returned verbatim -- blank still means "span ALL operators on this box".
    """
    bound = _BOUND_OPERATOR.get()
    if bound is not None:
        return bound
    return provided

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
        # token id (never the value) + bound operator + tool name + request id.
        _log(logging.INFO, name, f"AUDIT tool-call tid={_tid}", operator=_BOUND_OPERATOR.get())
    _log(logging.INFO, name, "call received")

    try:
        # === LOCAL TOOLS (Direct file access - faster, no proxy) ===

        if name == "seek_snapshot_direct":
            snap_id = arguments["snap_id"]
            content = seek_snapshot_by_offset(snap_id)

            if content is None:
                return _error("not_found", name, f"Snapshot {snap_id} not found in index")

            metadata = get_snapshot_metadata(snap_id)
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

            # Calculate stats
            operators = {}
            types = {"normal": 0, "checkpoint": 0, "summary": 0}
            total_bytes = 0

            for snap_id, entry in index.items():
                op = entry.get("operator", "unknown")
                operators[op] = operators.get(op, 0) + 1

                snap_type = entry.get("type", "normal")
                if snap_type in types:
                    types[snap_type] += 1

                size = entry.get("byte_end", 0) - entry.get("byte_start", 0)
                total_bytes += size

            stats = {
                "total_snapshots": len(index),
                "operators": operators,
                "types": types,
                "total_size_bytes": total_bytes,
                "total_size_mb": round(total_bytes / (1024 * 1024), 2),
                "index_file": str(SNAPSHOT_INDEX),
                "volume_file": str(VOLUME_FILE),
                "volume_exists": VOLUME_FILE.exists()
            }
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

            operators = {}
            for entry in index.values():
                op = entry.get("operator", "unknown")
                operators[op] = operators.get(op, 0) + 1

            # Sort by count descending
            sorted_ops = sorted(operators.items(), key=lambda x: x[1], reverse=True)

            return [TextContent(type="text", text=json.dumps({
                "operators": [{"name": op, "snapshot_count": count} for op, count in sorted_ops]
            }, indent=2))]

        elif name == "get_current_operator":
            operators, default = await _fetch_operators()
            bound = _BOUND_OPERATOR.get()
            if bound is not None:
                # HTTP/bound path: the operator IS the token's bound operator.
                # No selection is ever needed remotely (identity is the credential).
                resolved, needs_selection = bound, False
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

            # Resolve to file path
            # URL format: /ui/uploads/filename.ext -> Portal/uploads/filename.ext
            if url.startswith("/ui/"):
                relative_path = url.replace("/ui/", "")
                file_path = BLACKBOX_ROOT / "Portal" / relative_path
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
        if uri == "blackbox://index/stats":
            index = load_snapshot_index()

            operators = {}
            types = {"normal": 0, "checkpoint": 0, "summary": 0}
            total_bytes = 0

            for entry in index.values():
                op = entry.get("operator", "unknown")
                operators[op] = operators.get(op, 0) + 1
                snap_type = entry.get("type", "normal")
                if snap_type in types:
                    types[snap_type] += 1
                total_bytes += entry.get("byte_end", 0) - entry.get("byte_start", 0)

            stats = {
                "total_snapshots": len(index),
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

        presented = _extract_bearer(scope)
        if not presented:
            logger.warning("rid=%s AUTH reject: missing/malformed bearer (%s %s)",
                           rid, scope.get("method"), scope.get("path"))
            await self._reject_401(send, "Missing or malformed Authorization: Bearer token")
            return

        operator, token_id = _match_token(presented, self.token_map)
        if operator is None:
            logger.warning("rid=%s AUTH reject: unknown token tid=%s (%s %s)",
                           rid, _token_id(presented), scope.get("method"), scope.get("path"))
            await self._reject_401(send, "Unknown or invalid bearer token")
            return

        # Bind the operator for the LIFETIME of this request (contextvar) so
        # resolve_operator()/read tools FORCE this operator (anti-spoof/anti-leak).
        # The token id is threaded the same way for the per-tool-call audit line.
        _BOUND_OPERATOR.set(operator)
        _TOKEN_ID.set(token_id)
        # AUDIT (M3): token id (never the value) + bound operator + method/path + rid.
        logger.info("rid=%s AUDIT auth-ok tid=%s operator=%s %s %s",
                    rid, token_id, operator, scope.get("method"), scope.get("path"))
        await self.app(scope, receive, send)


def build_http_app(path: str = None):
    """Build the Starlette ASGI app hosting the SAME `server` over Streamable HTTP.

    Returns (starlette_app, session_manager). The session manager's run() is the
    Starlette lifespan, so the manager's task group lives for the app's lifetime.
    NO auth middleware is attached (M2 is localhost-only, no auth).
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

    app = Starlette(
        debug=False,
        routes=[Route(path, endpoint=guarded_endpoint,
                      methods=["GET", "POST", "DELETE"])],
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
