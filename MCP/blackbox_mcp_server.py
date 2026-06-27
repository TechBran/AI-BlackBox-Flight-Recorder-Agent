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
"""

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
    """Server-side safety net: resolve the operator for a tool call. When omitted,
    single operator -> that; multiple -> system default. (Interactive dropdown for
    the multiple case is handled agent-side, not here.)"""
    operators, default = await _fetch_operators()
    resolved, _needs = choose_operator(provided, operators, default)
    return resolved

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
    _REQUEST_ID.set(uuid.uuid4().hex[:12])
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
            # Read tool: omitting operator means ALL operators on this box (do NOT force-resolve).
            operator = arguments.get("operator", "")
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
                # Read tool: omitting operator means ALL operators on this box (do NOT force-resolve).
                operator = arguments.get("operator", "")
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

async def main():
    """Run the BlackBox MCP server."""
    logger.info("BlackBox MCP Server starting...")
    logger.info("BlackBox Root: %s", BLACKBOX_ROOT)
    logger.info("BlackBox API: %s", BLACKBOX_URL)
    logger.info("Volume File: %s (exists: %s)", VOLUME_FILE, VOLUME_FILE.exists())
    logger.info("Index File: %s (exists: %s)", SNAPSHOT_INDEX, SNAPSHOT_INDEX.exists())
    logger.info("Proxy timeout: %.0fs", PROXY_TIMEOUT)

    # Pre-load the index
    index = load_snapshot_index()
    logger.info("Loaded %d snapshots from index", len(index))

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
