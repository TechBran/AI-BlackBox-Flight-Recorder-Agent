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
  4. SESSION BINDING (2026-10, CVE-2026-52869): an mcp-session-id is bound to
     the token whose initialize opened it; any other token presenting it (or an
     id this process never issued) gets the SDK's unknown-session 404.

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
import re
import stat
import sys
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional, Dict, List
import httpx

# MCP SDK imports
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.server.lowlevel.helper_types import ReadResourceContents
    from mcp.shared.exceptions import McpError
    from mcp.types import (
        INTERNAL_ERROR,
        CallToolResult,
        ErrorData,
        Resource,
        TextContent,
        Tool,
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
# including the analysis tools, which answer within their own budgets below it
# (analyze_image/video ~100s: Orchestrator/media_analysis.py; analyze_audio
# ~90s, then a task_id). Override per-deploy via BLACKBOX_MCP_PROXY_TIMEOUT.
PROXY_TIMEOUT = float(os.getenv("BLACKBOX_MCP_PROXY_TIMEOUT", "120"))

# How long chat_with_context polls its /chat task (seconds). One non-stream
# Claude call may take up to (30 s connect + 600 s read) in the backend
# (chat_routes.ANTHROPIC_NONSTREAM_TIMEOUT, audit W21), so the old 120 s poll
# reported a timeout for replies that then arrived unseen. Override per-deploy
# via BLACKBOX_MCP_CHAT_TIMEOUT.
CHAT_POLL_TIMEOUT = float(os.getenv("BLACKBOX_MCP_CHAT_TIMEOUT", "660"))
CHAT_POLL_INTERVAL = 1.0

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
    that opened the session. The middleware now refuses a session id presented
    with any token other than the one that opened it (CVE-2026-52869), so the
    two always agree; reading per request stays as defense in depth.

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


def _request_audit_ids():
    """(request id, token id) of the CURRENT request for the audit trail.

    HTTP: both read from request.state, where the auth middleware put them for
    this request -- never the session-frozen contextvars. A request whose state
    lacks them still logs, as "unknown", so the line is never attributed to
    another request. stdio: (None, None) -- no token, and the caller mints the
    request id.
    """
    try:
        req = server.request_context.request
    except LookupError:
        req = None
    state = getattr(req, "state", None) if req is not None else None
    if state is None:
        if _is_http_request():
            return None, "unknown"
        return None, None
    return (getattr(state, "request_id", None),
            getattr(state, "token_id", None) or "unknown")


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


# #2A: Device fields a remote (bound) caller never sees -- the tailnet address,
# the control ports and the metadata block (Tailscale DNS name + hostname).
_REMOTE_DEVICE_HIDDEN_FIELDS = frozenset(
    {"tailscale_ip", "adb_port", "vnc_port", "rdp_port", "metadata"})


# #2A: what a remote (bound) caller may put in a /tasks/{task_id} path. Every id
# the backend issues (uuid4, "ttsq-<hex>", "update-<ts>-<hex>", ...) fits; a
# "../" id would otherwise steer the ownership lookup to another backend route.
_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _media_url_path(u):
    """A media url as its /ui/... path (drops any scheme + host prefix)."""
    if isinstance(u, str) and (u.startswith("http://") or u.startswith("https://")):
        from urllib.parse import urlparse
        return urlparse(u).path
    return u


def _task_media_urls(task_data) -> list:
    """Every media path a task produced, primary (result_url) first.

    Multi-output tasks (numberOfImages 2-4, Lyria variations) keep their other
    outputs only in result_data.all_urls and result_data.artifact.all_urls
    (Orchestrator/tasks.py), so the owner of the task owns all of them.
    """
    rd = task_data.get("result_data")
    rd = rd if isinstance(rd, dict) else {}
    artifact = rd.get("artifact")
    candidates = [task_data.get("result_url")]
    for source in (rd, artifact):
        if isinstance(source, dict) and isinstance(source.get("all_urls"), list):
            candidates.extend(source["all_urls"])
    out = []
    for u in candidates:
        if isinstance(u, str) and u:
            path = _media_url_path(u)
            if path not in out:
                out.append(path)
    return out


async def _owned_task(client, task_id: str, scope: str):
    """GET /tasks/{task_id} for a bound caller: the task dict iff `scope` owns it.

    None for a missing task, a task of another operator and a task with no
    recorded operator alike, so a denial never confirms that the task exists.
    """
    resp = await client.get(f"{BLACKBOX_URL}/tasks/{task_id}")
    if resp.status_code != 200:
        return None
    task_data = resp.json() or {}
    owner = task_data.get("operator")
    if not owner:
        logger.warning("rid=%s /tasks/%s returned no operator -- denying "
                       "(the ownership gate cannot be enforced without it)",
                       _REQUEST_ID.get(), task_id)
    if owner != scope:
        return None
    return task_data


def _read_device_registry():
    """A fresh, READ-ONLY view of the backend's device store.

    The backend owns devices.json. DeviceRegistry's load path can rewrite it
    (legacy migration, primary de-dup), which would make this process a second
    writer, and the get_registry() singleton would never see a device registered
    after this process started. Load fresh per call with saving disabled, and
    keep the registry's stdout prints off the stdio JSON-RPC channel.
    """
    import contextlib
    from Orchestrator.device_registry import DeviceRegistry

    class _ReadOnlyDeviceRegistry(DeviceRegistry):
        def _save_to_file(self):
            pass

    with contextlib.redirect_stdout(sys.stderr):
        return _ReadOnlyDeviceRegistry()

# Put the REPO ROOT on sys.path so the `Orchestrator` PACKAGE is importable.
# get_mcp_tools() (below) lazily runs `from Orchestrator.toolvault import registry`
# and `from Orchestrator.toolvault.resolvers import resolve_schema` -- those resolve
# the `Orchestrator` package, whose parent is the repo root. .mcp.json passes
# BLACKBOX_ROOT (env) but NOT PYTHONPATH, so without this the spawned MCP process
# fails every list_tools with "No module named 'Orchestrator'" -> client gets zero
# tools. Self-contained here so it works regardless of how the server is launched.
sys.path.insert(0, str(BLACKBOX_ROOT))

# Upload-path containment (stdlib only -- safe in the lean venv). Shared with
# the Orchestrator so both sides refuse the same traversal spellings.
from Orchestrator.utils.paths import (  # noqa: E402
    AUDIO_EXTS, DEFAULT_MAX_UPLOAD_BYTES, UploadNotFoundError, UploadPathError,
    resolve_upload_path,
)
# The per-boot /internal/reload secret (stdlib only), shared with the
# Orchestrator caller (Orchestrator/routes/mcp_routes.py::_trigger_reload).
from Orchestrator.utils import internal_secret  # noqa: E402
# The atomic, locked, corrupt-safe store helper (stdlib only) the backend's
# stores use too: the OAuth client and access-token stores below go through it.
from Orchestrator.utils import atomic_json  # noqa: E402

# Import web_tools directly (stdlib + requests + bs4 only)
sys.path.insert(0, str(BLACKBOX_ROOT / "Orchestrator"))
from web_tools import perform_web_fetch

# web_fetch blocks for up to its whole time budget. Its own small pool keeps a
# burst of slow fetches from a Funnel caller from starving the default
# executor, which asyncio's getaddrinfo (every proxied tool call) runs on.
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
_WEB_FETCH_POOL = _ThreadPoolExecutor(max_workers=4, thread_name_prefix="web-fetch")

# The catalog: tool_registry's MCP converter over the ToolVault registry. The
# MCP server only needs the schemas -- execution always hops through HTTP back
# to the BlackBox API. Orchestrator/tools/__init__.py and
# Orchestrator/toolvault/__init__.py resolve their re-exports lazily, so this
# import loads the registry, the resolvers and the availability gate only, and
# none of the tool executor / injector / embeddings stack the lean venv lacks.
# (It replaced an importlib load of tool_registry.py under a second module
# name, which kept a second copy of the registry snapshot and stopped keeping
# anything out once toolvault/__init__ imported the injector: audit
# O20-mcp-server-17. MCP/test_mcp_hardening.py holds the import closure.)
from Orchestrator.tools.tool_registry import get_mcp_tools  # noqa: E402

# Memoized valid-tool-name set (M1): the old catch-all rebuilt the entire tool
# list (get_mcp_tools()) on EVERY unmatched call just to membership-test the name.
# Compute it lazily and reuse it. list_tools() still calls get_mcp_tools() live
# (registry-driven, mtime-cached inside the registry, availability gates re-read
# from .env), so the advertised catalog can grow while the server runs: a
# provider key saved in the wizard, or a hot-reloaded tool module. A name the
# memo lacks is therefore looked up once more in the live catalog before it is
# an unknown tool (_is_catalog_tool), and the memo is refreshed.
_VALID_TOOL_NAMES: Optional[set] = None


def _valid_tool_names(refresh: bool = False) -> set:
    """Return (and memoize) the set of valid MCP tool names from the registry;
    refresh=True rebuilds it from the live catalog."""
    global _VALID_TOOL_NAMES
    if _VALID_TOOL_NAMES is None or refresh:
        _VALID_TOOL_NAMES = {t.name for t in get_mcp_tools()}
    return _VALID_TOOL_NAMES


def _is_catalog_tool(name: str) -> bool:
    """Is `name` a tool tools/list would advertise now? The memo answers the
    common case; a miss re-reads the live catalog once."""
    return name in _valid_tool_names() or name in _valid_tool_names(refresh=True)


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


def _local_audio_file(value) -> Path:
    """speech_to_text on stdio: an upload reference, or any regular local audio
    file (the stdio caller is the trusted local CLI with its own filesystem
    access). An absolute path is used as given and a relative one is resolved
    against the working directory, as ``Path(audio_path)`` always did; a /ui/
    or http(s) URL, or a relative path the working directory has no file for,
    goes to resolve_upload_path. Raises UploadPathError / UploadNotFoundError
    like resolve_upload_path."""
    if (isinstance(value, str) and value and "\x00" not in value
            and "://" not in value and not value.startswith("/ui/")):
        path = Path(value)
        if path.is_absolute():
            return _checked_local_audio(path, value)
        try:
            in_cwd = path.exists()
        except OSError:  # e.g. a name too long for the filesystem
            in_cwd = False
        if in_cwd:
            return _checked_local_audio(path.absolute(), value)
    return resolve_upload_path(value, allowed_exts=AUDIO_EXTS,
                               root=BLACKBOX_ROOT / "Portal" / "uploads")


def _checked_local_audio(path: Path, value: str) -> Path:
    """A stdio-named local file: it must be a regular audio file under the cap."""
    try:
        st = path.stat()
    except FileNotFoundError:
        raise UploadNotFoundError(f"audio file not found: {value!r}") from None
    except OSError:
        raise UploadPathError(f"unreadable audio path: {value!r}") from None
    if not stat.S_ISREG(st.st_mode):
        raise UploadPathError(f"not a regular file: {value!r}")
    if path.suffix.lower() not in AUDIO_EXTS:
        raise UploadPathError(f"not an audio file: {value!r}")
    if st.st_size > DEFAULT_MAX_UPLOAD_BYTES:
        raise UploadPathError(f"audio file is larger than {DEFAULT_MAX_UPLOAD_BYTES} bytes")
    return path


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


def _file_base64(path) -> str:
    import base64
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


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
            client.post(endpoint, json={"tool": name, "params": params, "operator": operator}, headers={"X-BlackBox-Caller": "mcp-gateway"}),
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

def _scope_operator_enums(tools: list, bound: str) -> list:
    """Narrow every resolved operator enum to the bound operator (HTTP only).

    The x-source "operators" resolver expands to the box's whole roster, which a
    remote caller must never see (#2A already scopes list_operators,
    get_index_stats and get_current_operator). The value is forced to the bound
    operator on this path anyway. The fail-closed sentinel is never advertised:
    the enum is dropped instead. get_mcp_tools() builds fresh Tool objects on
    every call, so editing them in place cannot leak into another request.
    """
    for tool in tools:
        prop = ((tool.inputSchema or {}).get("properties") or {}).get("operator")
        if isinstance(prop, dict) and "enum" in prop:
            if bound == _DENY_OPERATOR:
                prop.pop("enum")
            else:
                prop["enum"] = [bound]
    return tools


# What a remote caller's get_media accepts (the bound branch in call_tool).
_REMOTE_GET_MEDIA_NOTE = (
    " Remote callers fetch media by task_id (required); url is optional and must"
    " be one of that task's own outputs (its result_url or any of its all_urls).")


def _scope_remote_get_media(tools: list) -> list:
    """Advertise the bound get_media contract: task_id is required (HTTP only)."""
    for tool in tools:
        if tool.name == "get_media":
            tool.description = (tool.description or "") + _REMOTE_GET_MEDIA_NOTE
            schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
            schema["required"] = sorted(set(schema.get("required") or []) | {"task_id"})
    return tools


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available BlackBox tools -- generated from tool_registry.py."""
    tools = get_mcp_tools()
    bound = _current_bound_operator()
    if bound is not None:
        tools = _scope_operator_enums(tools, bound)
        tools = _scope_remote_get_media(tools)
    return tools


# validate_input=False: the SDK would jsonschema-check arguments against a tool
# cache it shares across every session, filled by whichever caller listed tools
# last -- with per-operator enums above that rejects valid calls, and on stdio it
# rejects the documented operator='' ("span all") before call_tool runs. Every
# branch below reads its own arguments (proxied tools are checked again by the
# backend executor) and turns a bad one into a structured error envelope.
@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]):
    """Execute a BlackBox tool.

    Dispatch order (M1):
      1. LOCAL tools  -- read the index/volume by byte offset or hit local
         in-process helpers; NEVER proxy. (seek/get/list/browse/stats/operators/
         current_operator/refresh + web_fetch + get_media + list_devices.)
      2. SPECIAL HTTP tools -- genuine logic the generic proxy does NOT replicate
         (search/get_context -> /fossil/hybrid; chat_with_context -> /chat poll;
         mint_snapshot -> /chat/save; speech_to_text -> multipart upload;
         get_task_status when bound -> /tasks ownership check, then the proxy).
      3. UNIFORM PROXY -- every other registry tool -> _proxy_tool (gmail/generic).

    Returns a list[TextContent] on success, or a structured CallToolResult
    (isError=True) on any failure.
    """
    # Per-call request id for the audit trail (shared by every log line below).
    # HTTP path: the auth middleware stashed THIS request's rid + token id on
    # request.state -- reuse them so the middleware AUDIT line and the tool-call
    # lines share one id. (The contextvars hold the values of the request that
    # opened the session, copied into the session task: audit
    # O20-mcp-server-14.) stdio path: no middleware ran, so mint a fresh id.
    _rid, _tid = _request_audit_ids()
    _REQUEST_ID.set(_rid or uuid.uuid4().hex[:12])
    if _tid is not None:
        # AUDIT (M3): every tool invocation on the HTTP/public surface logs the
        # token id (never the value) + the CURRENT request's operator (read per
        # request, never the session-frozen value) + tool + rid.
        _log(logging.INFO, name, f"AUDIT tool-call tid={_tid}", operator=_current_bound_operator())
    _log(logging.INFO, name, "call received")

    try:
        # === LOCAL TOOLS (Direct file access - faster, no proxy) ===

        # The index helpers re-parse the 16 MB index after every mint and read
        # the volume: in worker threads, so one session's read never stalls the
        # others (audit O20-mcp-server-10).
        if name == "seek_snapshot_direct":
            snap_id = arguments["snap_id"]
            content = await asyncio.to_thread(seek_snapshot_by_offset, snap_id)

            if content is None:
                return _error("not_found", name, f"Snapshot {snap_id} not found in index")

            metadata = await asyncio.to_thread(get_snapshot_metadata, snap_id)
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

            metadata = await asyncio.to_thread(get_snapshot_metadata, snap_id)
            if metadata is None:
                return _error("not_found", name, f"Snapshot {snap_id} not found")

            # #2A: cross-operator read denial (not_found, not 403) on HTTP.
            if _deny_if_not_owned(metadata) is not None:
                return _error("not_found", name, f"Snapshot {snap_id} not found")

            result = {"metadata": metadata}
            if include_content:
                result["content"] = await asyncio.to_thread(seek_snapshot_by_offset, snap_id)

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "list_recent_snapshots":
            operator = await resolve_operator(arguments.get("operator"))
            count = arguments.get("count", 10)
            _log(logging.INFO, name, "local index read", operator=operator)

            snapshots = await asyncio.to_thread(list_snapshots_by_operator, operator, count)
            return [TextContent(type="text", text=json.dumps(snapshots, indent=2))]

        elif name == "get_index_stats":
            index = await asyncio.to_thread(load_snapshot_index)

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

            index = await asyncio.to_thread(load_snapshot_index)

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
            index = await asyncio.to_thread(load_snapshot_index)

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
            index = await asyncio.to_thread(load_snapshot_index, True)
            return [TextContent(type="text", text=f"Index refreshed. {len(index)} snapshots loaded.")]

        elif name == "web_fetch":
            url = arguments["url"]
            max_chars = arguments.get("max_chars", 80000)
            # perform_web_fetch is the SSRF-guarded fetch (Orchestrator/utils/
            # safe_fetch.py): this runs in-process for Funnel callers, so a
            # loopback/LAN/tailnet URL is refused, never fetched. Blocking I/O,
            # so it runs off the event loop, on its own pool.
            result = await asyncio.get_running_loop().run_in_executor(
                _WEB_FETCH_POOL, perform_web_fetch, url, max_chars)
            return [TextContent(type="text", text=result)]

        elif name == "get_media":
            import mimetypes

            url = arguments.get("url")
            task_id = arguments.get("task_id")
            include_base64 = arguments.get("include_base64", True)
            include_metadata = arguments.get("include_metadata", True)

            # #2A: a bound (HTTP) caller reaches media ONLY through a task it
            # owns -- a bare url would serve any file under Portal/ (other
            # operators' uploads and generations). The url, if given, must be
            # one of that task's own outputs. Every denial is not_found (never
            # confirm that another operator's task exists).
            _scope = _current_bound_operator()
            if _scope is not None:
                if not task_id:
                    return _error("invalid_arguments", name,
                                  "task_id is required: remote callers fetch media by the task that produced it")
                if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
                    return _error("invalid_arguments", name, "Malformed task_id")

            # ONE task lookup serves the url, the ownership gate and the metadata.
            task_data = None
            if task_id:
                async with httpx.AsyncClient(timeout=30) as client:
                    if _scope is not None:
                        task_data = await _owned_task(client, task_id, _scope)
                    else:
                        task_response = await client.get(f"{BLACKBOX_URL}/tasks/{task_id}")
                        if task_response.status_code == 200:
                            task_data = task_response.json() or {}

            if _scope is not None:
                if task_data is None:
                    return _error("not_found", name, f"Task {task_id} not found")
                task_urls = _task_media_urls(task_data)
                if not task_urls:
                    return _error("not_found", name, f"Task {task_id} has no result_url")
                if url and _media_url_path(url) not in task_urls:
                    return _error("not_found", name, f"Task {task_id} has no media at that url")
                url = _media_url_path(url) if url else task_urls[0]
            elif task_id and not url:
                # If task_id provided, look up URL from task
                if task_data is None:
                    return _error("not_found", name, f"Task {task_id} not found")
                url = task_data.get("result_url")
                if not url:
                    return _error("not_found", name, f"Task {task_id} has no result_url")

            if not url:
                return _error("invalid_arguments", name, "No URL or task_id provided")

            url = _media_url_path(url)

            # #2A: reject path traversal BEFORE the /ui/->Portal join. A `..`
            # anywhere in the url could escape Portal/ and read arbitrary files.
            if ".." in url:
                return _error("invalid_arguments", name, "Illegal path component in url")

            # Resolve to file path through the shared upload guard:
            # /ui/uploads/filename.ext -> Portal/uploads/filename.ext, and it
            # must stay there (no '..', no symlink escape, regular files only).
            try:
                file_path = resolve_upload_path(url, root=BLACKBOX_ROOT / "Portal" / "uploads")
            except UploadNotFoundError:
                return _error("not_found", name, f"File not found: {url}")
            except UploadPathError as e:
                return _error("invalid_arguments", name, f"Invalid URL: {e}")

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
                "file_size_bytes": file_path.stat().st_size,
                "media_type": media_type,
                "mime_type": mime_type
            }
            # #2A: the absolute server path is for the trusted stdio caller only
            # (get_index_stats withholds its paths from bound callers the same way).
            if _scope is None:
                result["file_path"] = str(file_path)

            # Include base64 for images under 10MB
            if include_base64 and media_type == "image":
                if result["file_size_bytes"] < 10_000_000:
                    # Up to 10 MB read + encode: in a worker thread.
                    result["base64"] = await asyncio.to_thread(_file_base64, file_path)
                    _log(logging.INFO, name, f"included base64 for {file_path.name} ({result['file_size_bytes']} bytes)")
                else:
                    result["base64_skipped"] = "File too large (>10MB)"

            # Include metadata from task if task_id provided
            if include_metadata and task_data is not None:
                t = task_data
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
            # In-process read of the backend's device store -- NOT an HTTP proxy.
            from Orchestrator.device_registry import DeviceType
            dtype = arguments.get("device_type")
            try:
                dtype = DeviceType(dtype) if dtype else None
            except ValueError:
                return _error("invalid_arguments", name, f"Unknown device_type: {dtype!r}")
            registry = _read_device_registry()
            # #2A: a bound (HTTP) caller sees only its OWN devices, and never
            # their tailnet addresses, ports or DNS names. stdio (scope None) -> all.
            scope = _current_bound_operator()
            if scope is not None:
                devices = registry.get_devices_by_owner(scope)
            else:
                devices = registry.get_all_devices()
            if dtype is not None:
                devices = [d for d in devices if d.device_type == dtype]
            device_list = [d.to_dict() for d in devices]
            if scope is not None:
                device_list = [{k: v for k, v in d.items() if k not in _REMOTE_DEVICE_HIDDEN_FIELDS}
                               for d in device_list]
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

                # Get recent snapshots from local index (a thread: the index
                # re-parses after every mint)
                recent = await asyncio.to_thread(list_snapshots_by_operator, operator, 5)

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
                    "provider": provider,
                    # An MCP turn says so: health data is never read for an
                    # MCP client in v1, household mode or not (spec §4.5).
                    "wellness_surface": "mcp",
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
                    polls = max(1, int(CHAT_POLL_TIMEOUT / CHAT_POLL_INTERVAL))
                    for _ in range(polls):
                        await asyncio.sleep(CHAT_POLL_INTERVAL)
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
                    # The task keeps running: name it, so the caller can poll it.
                    return _error("timeout", name,
                                  f"Chat still running after {CHAT_POLL_TIMEOUT:.0f}s; "
                                  f"poll get_task_status with task_id {task_id}")

                return [TextContent(type="text", text=result.get("response", json.dumps(result)))]

            elif name == "speech_to_text":
                # SPECIAL: multipart file upload to /stt. The generic
                # /local/tools/execute proxy passes JSON params only -- it cannot
                # stream a local audio file -- so this stays a dedicated branch.
                audio_path = arguments["audio_path"]

                # The file's bytes go to the STT provider. A token-bound (HTTP,
                # Funnel-exposed) caller has no files of its own on this box, so
                # it may only name an audio upload -- a /ui/uploads URL or a path
                # inside Portal/uploads (sessions/ and terminal/ included). The
                # stdio caller is the local, trusted CLI with its own filesystem
                # access: any local path, but still a regular audio file.
                try:
                    if _current_bound_operator() is not None:
                        audio_file_path = resolve_upload_path(
                            audio_path, allowed_exts=AUDIO_EXTS,
                            root=BLACKBOX_ROOT / "Portal" / "uploads")
                    else:
                        audio_file_path = _local_audio_file(audio_path)
                except UploadNotFoundError:
                    return _error("not_found", name, f"Audio file not found: {audio_path}")
                except UploadPathError as e:
                    return _error("invalid_arguments", name, f"Audio file refused: {e}")

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

                # Streamed: httpx reads the handle in chunks as it uploads (the
                # file may be up to the 500 MB upload cap; never slurp it).
                with open(audio_file_path, "rb") as f:  # noqa: ASYNC230
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

            elif name == "get_task_status" and _current_bound_operator() is not None:
                # #2A: a remote caller polls only its OWN tasks -- the full task
                # JSON carries result urls, result_data, reasoning and the owner.
                # Denials are not_found, exactly like get_media's.
                task_id = arguments.get("task_id")
                if not task_id or not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
                    return _error("invalid_arguments", name, "A well-formed task_id is required")
                if await _owned_task(client, task_id, _current_bound_operator()) is None:
                    return _error("not_found", name, f"Task not found: {task_id}")
                return await _proxy_tool(name, arguments, client)

            # === UNIFORM PROXY DISPATCHER ===
            # Every other registry tool (multimodal generation/analysis, Workspace,
            # web search, gmail_*, status/util, roll_dice, toolvault, control_*,
            # use_computer, ...) routes through the single proxy path. The valid
            # name set is memoized, rebuilt only when a name misses it.
            elif _is_catalog_tool(name):
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


def _json_resource(payload) -> list:
    return [ReadResourceContents(content=json.dumps(payload, indent=2),
                                 mime_type="application/json")]


# MCP's "resource not found" error code (spec: Resources > Error Handling).
RESOURCE_NOT_FOUND = -32002


@server.read_resource()
async def read_resource(uri) -> list:
    """Read a BlackBox resource.

    The SDK hands over the URI as a pydantic AnyUrl, which never equals a str,
    and expects Iterable[ReadResourceContents] back; comparing the AnyUrl and
    returning a TextResourceContents made every read fail (audit
    O20-mcp-server-04). An unknown URI is the spec's resource-not-found error.
    """
    uri = str(uri)
    try:
        # #2A: resources run in the SAME session task and are equally reachable
        # over HTTP -- apply the SAME bound-operator filter as the matching tools.
        # scope is None on stdio (span all), or the CURRENT request's operator.
        scope = _current_bound_operator()
        if uri == "blackbox://index/stats":
            index = await asyncio.to_thread(load_snapshot_index)

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

            return _json_resource({
                "total_snapshots": counted,
                "operators": operators,
                "types": types,
                "total_size_mb": round(total_bytes / (1024 * 1024), 2)
            })

        elif uri == "blackbox://index/operators":
            index = await asyncio.to_thread(load_snapshot_index)
            operators = {}
            for entry in index.values():
                op = entry.get("operator", "unknown")
                if scope is not None and op != scope:
                    continue
                operators[op] = operators.get(op, 0) + 1

            return _json_resource({"operators": operators})

        elif uri == "blackbox://index/recent":
            index = await asyncio.to_thread(load_snapshot_index)

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

            entries.sort(key=lambda x: x["timestamp"] or "", reverse=True)

            return _json_resource({"recent_snapshots": entries[:20]})

        elif uri == "blackbox://volume/info":
            info = {
                "volume_path": str(VOLUME_FILE),
                "exists": VOLUME_FILE.exists(),
                "size_bytes": VOLUME_FILE.stat().st_size if VOLUME_FILE.exists() else 0,
                "size_mb": round(VOLUME_FILE.stat().st_size / (1024 * 1024), 2) if VOLUME_FILE.exists() else 0,
                "index_path": str(SNAPSHOT_INDEX),
                "index_exists": SNAPSHOT_INDEX.exists()
            }
            # #2A: absolute server paths are for the trusted stdio caller only
            # (get_index_stats and get_media withhold them the same way).
            if scope is not None:
                info.pop("volume_path")
                info.pop("index_path")
            return _json_resource(info)

    except Exception:
        logger.exception("rid=%s resource=%s | read failed", _REQUEST_ID.get(), uri)
        raise McpError(ErrorData(code=INTERNAL_ERROR,
                                 message=f"Failed to read resource {uri}")) from None
    raise McpError(ErrorData(code=RESOURCE_NOT_FOUND, message=f"Resource not found: {uri}"))


# =============================================================================
# MAIN
# =============================================================================

def _require_tool_catalog() -> int:
    """Refuse to start without a tool catalog (audit O20-mcp-server-17).

    A catalog that fails to build (an import the lean venv cannot satisfy, a
    broken registry) or builds empty used to surface only as a JSON-RPC error
    on tools/list, or as zero tools, in every client -- silently. Exit loudly
    instead, so the stdio client reports a dead server and systemd a failed
    unit. Returns the number of tools.
    """
    try:
        count = len(get_mcp_tools())
    except Exception:
        logger.critical("Refusing to start: the MCP tool catalog failed to build "
                        "(an import the MCP venv lacks, or a broken ToolVault "
                        "registry). Clients would see no tools.", exc_info=True)
        raise SystemExit(1) from None
    if count == 0:
        logger.critical("Refusing to start: the MCP tool catalog is EMPTY (no valid "
                        "ToolVault module lists the 'mcp' group under %s). Clients "
                        "would see no tools.", BLACKBOX_ROOT / "ToolVault" / "tools")
        raise SystemExit(1)
    logger.info("Tool catalog: %d tools", count)
    return count


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

    stdout is the JSON-RPC channel, and code this server calls (web_fetch, the
    device registry, ToolVault) prints. The SDK gets the real stdout handle and
    every other write to sys.stdout goes to stderr, so a stray print can never
    split or corrupt a frame (audit O20-mcp-server-05).
    """
    import anyio
    from io import TextIOWrapper

    protocol_out = anyio.wrap_file(TextIOWrapper(sys.stdout.buffer, encoding="utf-8"))
    sys.stdout = sys.stderr
    _log_startup_banner("stdio")
    _require_tool_catalog()
    async with stdio_server(stdout=protocol_out) as (read_stream, write_stream):
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
# NO host default: shipping a real box's hostname would point every other
# install's OAuth discovery at it. The deploy MUST set BLACKBOX_MCP_PUBLIC_URL to
# THIS box's public Funnel origin; OAuth discovery fails loudly (503) until it is
# set. Bearer-token auth does not need it.
# Onboarding writes the public URL to a WRITABLE Manifest/ file (no /etc edit), so
# the box-setup flow sets it with zero sudo. Precedence: ENV (deploy-pinned) >
# the runtime file > "" (OAuth 503). /internal/reload re-reads this at runtime.
BLACKBOX_MCP_RUNTIME_FILE = os.getenv(
    "BLACKBOX_MCP_RUNTIME_FILE", str(BLACKBOX_ROOT / "Manifest" / "mcp_runtime.json"))


def _load_runtime_public_url() -> str:
    env = os.getenv("BLACKBOX_MCP_PUBLIC_URL", "").strip()
    if env:
        return env.rstrip("/")
    try:
        p = Path(BLACKBOX_MCP_RUNTIME_FILE)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                url = data.get("public_url")
                if isinstance(url, str) and url.strip():
                    return url.strip().rstrip("/")
    except Exception as e:
        logger.error("Failed to load MCP runtime file %s: %s", BLACKBOX_MCP_RUNTIME_FILE, e)
    return ""


BLACKBOX_MCP_PUBLIC_URL = _load_runtime_public_url()

# The MCP resource URL (issuer + the MCP path) -- the `resource` value in the
# RFC 9728 Protected Resource Metadata. Built from the public origin + the
# configured MCP path so a non-default path stays consistent across surfaces.
BLACKBOX_MCP_RESOURCE_URL = BLACKBOX_MCP_PUBLIC_URL + BLACKBOX_MCP_HTTP_PATH

# Module-level handle to the live auth middleware so the control listener's
# /internal/reload can hot-swap the token map (mint/revoke) with no restart, and
# the OAuth endpoints read the same live map. Set in build_http_app.
_AUTH_MIDDLEWARE = {"instance": None}


def _reload_secret_path():
    return internal_secret.mcp_reload_secret_path(BLACKBOX_ROOT)


async def _internal_reload_handler(request):
    """POST /internal/reload: re-read the runtime public URL + the token map from
    disk and apply them IN PLACE, so a freshly minted token / a newly set public
    URL goes live with no restart. It only re-reads on-disk config -- it cannot
    set arbitrary values.

    It is served ONLY by the control listener (_start_control_listener), a
    second uvicorn server bound to 127.0.0.1:<BLACKBOX_MCP_CONTROL_PORT>, never
    by the public listener that Tailscale Funnel forwards the internet to
    (audit O20-mcp-server-08). The gate is the per-boot reload secret in
    credentials/ (0600, readable only by the service user), sent by the
    Orchestrator and compared in constant time (audit M3 U20); the loopback
    check below is defense in depth."""
    from starlette.responses import JSONResponse
    client = getattr(request, "client", None)
    if client is not None and client.host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    presented = request.headers.get(internal_secret.SECRET_HEADER, "")
    if not internal_secret.verify(presented, _reload_secret_path()):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    global BLACKBOX_MCP_PUBLIC_URL, BLACKBOX_MCP_RESOURCE_URL
    BLACKBOX_MCP_PUBLIC_URL = _load_runtime_public_url()
    BLACKBOX_MCP_RESOURCE_URL = BLACKBOX_MCP_PUBLIC_URL + BLACKBOX_MCP_HTTP_PATH
    tokens = 0
    mw = _AUTH_MIDDLEWARE["instance"]
    if mw is not None:
        # The roster fetch is a blocking HTTP call (10 s timeout): in a thread.
        new_map = await asyncio.to_thread(_load_validated_token_map)
        mw.token_map.clear()        # in-place: middleware reads the dict by reference
        mw.token_map.update(new_map)
        tokens = len(mw.token_map)
    logger.info("internal/reload: public_url=%r tokens=%d", BLACKBOX_MCP_PUBLIC_URL, tokens)
    return JSONResponse({"public_url": BLACKBOX_MCP_PUBLIC_URL,
                         "resource": BLACKBOX_MCP_RESOURCE_URL, "tokens": tokens})

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
                    if not (isinstance(tok, str) and tok and isinstance(op, str) and op):
                        continue
                    if op.strip().lower() in ("system", "flight recorder"):
                        # "system"/"Flight Recorder"/blank = span-ALL operators.
                        # That is a trusted stdio/local capability, NEVER a
                        # remote bearer token -- else one token reads every
                        # operator's snapshots. ("Flight Recorder" is the
                        # permanent overseer operator, design 2026-07-23 §6:
                        # remote cross-operator access stays CLOSED in v1;
                        # string literal here per the MCP lean-venv rule.)
                        logger.error("M3 AUTH: refusing token tid=%s bound to "
                                     "span-all operator %r (not a remote "
                                     "capability).", _token_id(tok), op.strip())
                        continue
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
                    if not (isinstance(tok, str) and tok and isinstance(op, str) and op):
                        continue
                    if op.strip().lower() in ("system", "flight recorder"):
                        # "system"/"Flight Recorder"/blank = span-ALL operators.
                        # That is a trusted stdio/local capability, NEVER a
                        # remote bearer token -- else one token reads every
                        # operator's snapshots. ("Flight Recorder" is the
                        # permanent overseer operator, design 2026-07-23 §6:
                        # remote cross-operator access stays CLOSED in v1;
                        # string literal here per the MCP lean-venv rule.)
                        logger.error("M3 AUTH: refusing token tid=%s bound to "
                                     "span-all operator %r (not a remote "
                                     "capability).", _token_id(tok), op.strip())
                        continue
                    merged[tok] = op
        except Exception as e:
            logger.error("Failed to parse BLACKBOX_MCP_TOKENS env JSON: %s", e)
    return merged


def _fetch_operators_sync():
    """Blocking roster fetch for STARTUP-time token validation. Returns the live
    operator list, or None when the roster is unavailable (so the caller fails
    OPEN rather than dropping every token on a transient backend blip)."""
    try:
        r = httpx.get(f"{BLACKBOX_URL}/operators", timeout=10)
        ops = r.json().get("operators")
        return list(ops) if isinstance(ops, list) else None
    except Exception:
        return None


def _roster_enforce_enabled() -> bool:
    """BLACKBOX_MCP_ROSTER_ENFORCE (default ON). Off only for tests that bind
    synthetic operators not present in a real box's roster."""
    return os.getenv("BLACKBOX_MCP_ROSTER_ENFORCE", "1").strip().lower() not in (
        "0", "false", "no", "off", "")


def _load_validated_token_map() -> Dict[str, str]:
    """The token map from disk, checked against the live roster. Blocking (a
    file read and the roster fetch): async handlers run it in a thread."""
    return _validate_token_operators(_load_token_map())


def _validate_token_operators(token_map: Dict[str, str]) -> Dict[str, str]:
    """Anti-ghost guard (the production-portability enforcement): every token must
    bind to an operator that EXISTS in THIS box's live GET /operators roster.
    A token bound to a non-roster operator mints snapshots under a phantom name
    (invisible to the real team) and reads back nothing -- the silent-misrouting
    failure mode. Nothing is hard-coded: the bound operator is checked against the
    customer's OWN live roster.

      * roster unavailable (backend down at startup) -> fail OPEN, keep all, warn;
        a restart once the backend is reachable re-validates.
      * roster available + enforce ON (default) -> DROP + log any non-roster token
        (it then 401s -- fail closed).
      * enforce OFF -> keep all, but still log the drift so it is diagnosable.

    Tokens should be minted with deploy/mint_token.py, which validates the chosen
    operator against the live roster AT MINT TIME (the primary control)."""
    if not token_map:
        return token_map
    roster = _fetch_operators_sync()
    if roster is None:
        logger.warning("M3 AUTH: live roster unavailable at startup -- skipping "
                       "token->operator roster validation (tokens kept as-is). "
                       "Restart once the backend is reachable to re-validate.")
        return token_map
    enforce = _roster_enforce_enabled()
    kept: Dict[str, str] = {}
    for tok, op in token_map.items():
        if op in roster:
            kept[tok] = op
        else:
            logger.error("M3 AUTH: token tid=%s bound to operator %r NOT in this "
                         "box's live roster %s -- %s. Re-mint with "
                         "deploy/mint_token.py against a real operator.",
                         _token_id(tok), op, sorted(roster),
                         "REJECTED (fail-closed)" if enforce else "kept (enforce off)")
    return kept if enforce else token_map


def _token_bytes(token: str) -> bytes:
    """A token as UTF-8 bytes, for hashing and comparison. surrogatepass keeps
    the mapping one-to-one for any str a form or header can carry."""
    return token.encode("utf-8", "surrogatepass")


def _token_id(token: str) -> str:
    """A short, non-reversible IDENTIFIER for a token (audit logs only).

    sha256(token) -> first 12 hex chars. NEVER the token value -- so logs can be
    correlated to a credential without ever recording the secret.
    """
    return "sha256:" + hashlib.sha256(_token_bytes(token)).hexdigest()[:12]


def _match_token(presented: str, token_map: Dict[str, str]):
    """Constant-time token lookup. Returns (operator, token_id) or (None, None).

    hmac.compare_digest is used for EVERY known token (no early break on the
    first non-match) so total work does not depend on which token matched --
    no timing oracle on the secret. An empty/blank presented token never matches.
    Both sides are compared as UTF-8 bytes: compare_digest raises TypeError
    for a str with non-ASCII characters, which made a hostile Authorization
    header or consent-form token a 500 (audit O20-mcp-server-11).
    """
    if not presented or not isinstance(presented, str):
        return None, None
    presented_b = _token_bytes(presented)
    matched_op = None
    matched_tok = None
    for known, operator in token_map.items():
        if hmac.compare_digest(presented_b, _token_bytes(known)):
            matched_op = operator
            matched_tok = known
    if matched_op is None:
        return None, None
    return matched_op, _token_id(matched_tok)


# A bearer token is printable ASCII (RFC 6750's b64token is a subset); anything
# else -- control bytes, latin-1 high bytes -- is malformed, never compared.
_BEARER_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,4096}")


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
                return tok if _BEARER_TOKEN_RE.fullmatch(tok) else None
            return None
    return None


# Session binding (CVE-2026-52869, audit X01-deps-python-04): a Streamable HTTP
# session id is only valid with the credential that opened it. The SDK matches
# requests to sessions by id alone (and only checks the principal from 1.27.2
# on, and only when scope["user"] is set), so without this a holder of token B
# could attach to token A's session -- including A's server-to-client SSE stream
# -- just by presenting A's mcp-session-id. The cap matches the SDK's own
# DEFAULT_MAX_SESSIONS; the least recently used binding is dropped first, and a
# dropped session simply answers 404 so the client re-initializes.
MCP_SESSION_HEADER = b"mcp-session-id"
MAX_BOUND_SESSIONS = 10_000


def _token_fingerprint(token: str) -> str:
    """Full sha256 of a token: the session-binding key (never the token value)."""
    return hashlib.sha256(_token_bytes(token)).hexdigest()


def _request_header(scope, name: bytes) -> Optional[str]:
    """One request header from an ASGI scope (latin-1, as the wire carries it)."""
    for k, v in scope.get("headers", []):
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def _sdk_principal(fingerprint: str):
    """An SDK AuthenticatedUser for scope["user"], so mcp>=1.27.2's own
    session-owner check binds sessions to the same per-token identity. None on
    an SDK without the auth module -- the middleware binding still applies."""
    try:
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        from mcp.server.auth.provider import AccessToken
    except ImportError:
        return None
    return AuthenticatedUser(AccessToken(token=fingerprint, client_id=fingerprint, scopes=[]))


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
        # mcp-session-id -> fingerprint of the token whose initialize opened it.
        self.session_owners: "OrderedDict[str, str]" = OrderedDict()

    def live_consents(self) -> Dict[str, str]:
        """fingerprint -> operator of every static token in the live map: an
        OAuth token is valid only while the bearer that consented to it is."""
        return {_token_fingerprint(t): op for t, op in self.token_map.items()}

    def _bind_session(self, session_id: str, fingerprint: str) -> None:
        self.session_owners[session_id] = fingerprint
        self.session_owners.move_to_end(session_id)
        while len(self.session_owners) > MAX_BOUND_SESSIONS:
            self.session_owners.popitem(last=False)

    async def _reject_unknown_session(self, send) -> None:
        # The SDK's own answer for an unknown session (404 + JSON-RPC error), so
        # a foreign token cannot even confirm that the session exists.
        body = json.dumps({"jsonrpc": "2.0", "id": "server-error",
                           "error": {"code": -32600, "message": "Session not found"}}).encode("utf-8")
        await send({"type": "http.response.start", "status": 404,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    async def _reject_401(self, send, detail: str) -> None:
        body = json.dumps({"error": {"code": "unauthorized", "message": detail}}).encode("utf-8")
        # RFC 9728 / MCP auth: the 401 MUST advertise WHERE the OAuth discovery
        # lives (resource_metadata) so a spec-compliant client (claude.ai / Claude
        # Desktop) can find the authorization server and start the flow. Without it
        # the client sees a bare `Bearer` 401 and reports "Couldn't connect." Read
        # the public URL at call time so /internal/reload updates are reflected.
        if BLACKBOX_MCP_PUBLIC_URL:
            wa = ('Bearer resource_metadata="' + BLACKBOX_MCP_PUBLIC_URL
                  + '/.well-known/oauth-protected-resource"').encode("utf-8")
        else:
            wa = b"Bearer"
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", wa),
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
            operator, token_id = _match_oauth_access_token(presented, self.live_consents())
            auth_kind = "oauth"
        if operator is None:
            logger.warning("rid=%s AUTH reject: unknown token tid=%s (%s %s)",
                           rid, _token_id(presented), scope.get("method"), scope.get("path"))
            await self._reject_401(send, "Unknown or invalid bearer token")
            return

        # Session binding: a request naming a session must carry the token that
        # opened it. Unknown ids are refused here too -- every live session was
        # opened through this middleware, so an id it never bound is stale or
        # forged (the SDK would 404 it anyway).
        fingerprint = _token_fingerprint(presented)
        session_id = _request_header(scope, MCP_SESSION_HEADER)
        if session_id is not None:
            owner = self.session_owners.get(session_id)
            if owner is None or not hmac.compare_digest(owner, fingerprint):
                logger.warning("rid=%s AUDIT session-reject tid=%s operator=%s session=%r... (%s) %s %s",
                               rid, token_id, operator, session_id[:12],
                               "unknown session" if owner is None else "opened by a different token",
                               scope.get("method"), scope.get("path"))
                await self._reject_unknown_session(send)
                return
            self.session_owners.move_to_end(session_id)

        # #2B (Option A): stash the operator + token id on THIS request's state.
        # The SDK threads the current Starlette Request through to the handler
        # (request_ctx.request, set fresh per JSON-RPC message), so handlers read
        # the CURRENT request's operator via _current_bound_operator() -- NOT the
        # session-frozen value. With the session binding above every request on
        # a session carries the same token, so this is now defense in depth.
        # request.state writes persist in scope["state"], which the SDK's later
        # Request(scope, receive) reads back.
        from starlette.requests import Request as _StarletteRequest
        _req = _StarletteRequest(scope)
        _req.state.bound_operator = operator
        _req.state.token_id = token_id
        _req.state.request_id = rid      # the tool-call audit line reads it back
        # _BOUND_OPERATOR/_TOKEN_ID remain set as the "this is HTTP" transport
        # signal + audit context. Their VALUE is NOT trusted for per-call operator
        # resolution (it can be session-stale); _current_bound_operator() is.
        _BOUND_OPERATOR.set(operator)
        _TOKEN_ID.set(token_id)
        principal = _sdk_principal(fingerprint)
        if principal is not None:
            scope["user"] = principal
        # AUDIT (M3): token id (never the value) + bound operator + method/path + rid.
        logger.info("rid=%s AUDIT auth-ok kind=%s tid=%s operator=%s %s %s",
                    rid, auth_kind, token_id, operator, scope.get("method"), scope.get("path"))

        is_delete = scope.get("method") == "DELETE"

        async def send_and_track(message):
            if message.get("type") == "http.response.start" and 200 <= message.get("status", 0) < 300:
                if session_id is None:
                    # A successful opening request: bind the id the SDK issued
                    # BEFORE the client sees it. A failed open never binds.
                    issued = _request_header(message, MCP_SESSION_HEADER)
                    if issued:
                        self._bind_session(issued, fingerprint)
                elif is_delete:
                    self.session_owners.pop(session_id, None)
            await send(message)

        await self.app(scope, receive, send_and_track)


# =============================================================================
# CHUNK 6a: OAuth client store + discovery/registration handlers
# =============================================================================
# Paths exempt from bearer auth -- the OAuth bootstrap surface. The middleware
# lets these through WITHOUT a credential (they ARE how a client gets one).
# /authorize + /token are reserved here NOW (later chunks implement them) so the
# exemption is already in place when they land. Everything else (notably /mcp)
# still requires a bearer.
OAUTH_PUBLIC_PATHS = {"/register", "/authorize", "/token", "/revoke"}
OAUTH_PUBLIC_PREFIXES = ("/.well-known/",)


# =============================================================================
# THE PUBLIC OAUTH EDGE (audit O20-mcp-server-09)
#
# /register, /authorize, /token and /revoke take no credential, and Tailscale
# Funnel puts them on the internet. Every request to them is rate-limited per
# client address (a token bucket per rule, kept in memory and mirrored to a
# small ledger so a restart does not hand out fresh buckets), and its body is
# capped before a handler parses it. /mcp is never touched here: it streams,
# and the bearer gate answers it.
#
# The client address is scope["client"]: uvicorn rewrites it from Funnel's
# X-Forwarded-For only for a loopback peer (run_http pins forwarded_allow_ips),
# taking the address the proxy appended. An IPv6 client is keyed by its /64.
# =============================================================================
OAUTH_MAX_BODY_BYTES = int(os.getenv("BLACKBOX_MCP_OAUTH_MAX_BODY", str(16 * 1024)))

# rule -> (capacity, window_s): a bucket holds `capacity` requests and refills
# them evenly over `window_s` seconds. "register_all" is ONE bucket for every
# address: it bounds how fast the client store can grow at all. Override with
# BLACKBOX_MCP_OAUTH_RATE_LIMITS='{"register": [10, 3600], ...}'.
_DEFAULT_OAUTH_RATE_LIMITS = {
    "register": (10, 3600),
    "register_all": (60, 3600),
    "authorize": (30, 600),
    "token": (60, 600),
    "revoke": (30, 600),
}

# path -> the buckets a request to it draws from ("*" = the shared bucket).
_OAUTH_RATE_RULES = {
    "/register": (("register", None), ("register_all", "*")),
    "/authorize": (("authorize", None),),
    "/token": (("token", None),),
    "/revoke": (("revoke", None),),
}

BLACKBOX_MCP_OAUTH_RATE_LEDGER_FILE = os.getenv(
    "BLACKBOX_MCP_OAUTH_RATE_LEDGER_FILE",
    str(BLACKBOX_ROOT / "Manifest" / "mcp_oauth_rate_ledger.json"),
)


def _rate_limits_from_env(defaults: dict) -> dict:
    out = dict(defaults)
    raw = os.getenv("BLACKBOX_MCP_OAUTH_RATE_LIMITS", "").strip()
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except ValueError:
        logger.error("BLACKBOX_MCP_OAUTH_RATE_LIMITS is not JSON; using the defaults")
        return out
    for rule, spec in (data.items() if isinstance(data, dict) else ()):
        if (rule in out and isinstance(spec, list) and len(spec) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0
                        for x in spec)):
            out[rule] = (spec[0], spec[1])
        else:
            logger.error("BLACKBOX_MCP_OAUTH_RATE_LIMITS: ignoring %r=%r "
                         "(want a known rule -> [capacity, window_seconds])", rule, spec)
    return out


OAUTH_RATE_LIMITS = _rate_limits_from_env(_DEFAULT_OAUTH_RATE_LIMITS)


def _client_tag(host) -> str:
    """The rate-limit / registration key for a client address: a hash, so the
    ledger and the client store never hold a raw address. IPv6 is keyed by its
    /64 (one subscriber's prefix), an IPv4-mapped address as the IPv4 one."""
    import ipaddress
    text = host if isinstance(host, str) else ""
    try:
        ip = ipaddress.ip_address(text)
        if ip.version == 6 and ip.ipv4_mapped is not None:
            text = str(ip.ipv4_mapped)
        elif ip.version == 6:
            text = str(ipaddress.ip_network(f"{ip}/64", strict=False))
    except ValueError:
        text = text or "unknown"
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


class _RateLimiter:
    """Token buckets keyed "rule|client", in memory, mirrored to a ledger.

    At most `max_keys` buckets are kept (least recently used dropped first), so
    a flood of addresses cannot grow memory or the ledger without bound. Only
    buckets that are not full are written: a full one carries no state.
    """

    def __init__(self, rules: dict, ledger_path=None, max_keys: int = 4096,
                 flush_interval: float = 5.0, clock=None):
        self.rules = dict(rules)
        self.ledger_path = ledger_path
        self.max_keys = max_keys
        self.flush_interval = flush_interval
        self._clock = clock or _time.time
        self._buckets: "OrderedDict[str, list]" = OrderedDict()   # key -> [tokens, stamp]
        self._lock = _threading.Lock()
        self._dirty = False
        self._last_flush = 0.0

    def _level(self, key: str, rule: str, now: float) -> float:
        capacity, window = self.rules[rule]
        tokens, stamp = self._buckets.get(key, (capacity, now))
        return min(capacity, tokens + max(0.0, now - stamp) * capacity / window)

    def take(self, draws, now: float = None) -> float:
        """Take one token from each (rule, client) bucket, all or none.

        Returns 0.0 when the request may proceed, else the seconds until every
        bucket it needs holds a token again (the Retry-After)."""
        now = self._clock() if now is None else now
        with self._lock:
            keyed = [(f"{rule}|{client}", rule) for rule, client in draws]
            levels = {key: self._level(key, rule, now) for key, rule in keyed}
            short = [(key, rule) for key, rule in keyed if levels[key] < 1]
            if short:
                wait = 0.0
                for key, rule in short:
                    capacity, window = self.rules[rule]
                    wait = max(wait, (1 - levels[key]) * window / capacity)
                return max(wait, 0.001)
            for key, _rule in keyed:
                self._buckets[key] = [levels[key] - 1, now]
                self._buckets.move_to_end(key)
            # Evict the least recently used per-client buckets; the shared
            # ("rule|*") ones are never evicted, or a flood of addresses would
            # refill them.
            if len(self._buckets) > self.max_keys:
                for key in list(self._buckets):
                    if len(self._buckets) <= self.max_keys:
                        break
                    if not key.endswith("|*"):
                        del self._buckets[key]
            self._dirty = True
            return 0.0

    def load(self) -> None:
        """Restore the ledger (a missing or corrupt one starts empty)."""
        if not self.ledger_path:
            return
        data = atomic_json.read_json(self.ledger_path, {},
                                     valid=lambda d: isinstance(d, dict))
        now = self._clock()
        with self._lock:
            for key, value in list(data.items())[-self.max_keys:]:
                rule = key.split("|", 1)[0] if isinstance(key, str) else None
                if (rule in self.rules and isinstance(value, list) and len(value) == 2
                        and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                for x in value)):
                    tokens, stamp = float(value[0]), min(float(value[1]), now)
                    self._buckets[key] = [max(0.0, min(tokens, self.rules[rule][0])), stamp]

    def flush(self, force: bool = False) -> None:
        """Write the not-full buckets to the ledger (at most every
        flush_interval seconds unless forced). Never raises."""
        if not self.ledger_path:
            return
        now = self._clock()
        with self._lock:
            if not self._dirty or (not force and now - self._last_flush < self.flush_interval):
                return
            snapshot = {key: [round(value[0], 4), value[1]]
                        for key, value in self._buckets.items()
                        if self._level(key, key.split("|", 1)[0], now) < self.rules[key.split("|", 1)[0]][0]}
            self._dirty = False
            self._last_flush = now
        try:
            atomic_json.write_json(self.ledger_path, snapshot, indent=None)
        except Exception as e:
            logger.error("OAuth rate ledger %s not written: %s", self.ledger_path, e)


async def _send_json(send, status: int, payload: dict, extra_headers=()) -> None:
    body = json.dumps(payload).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
               (b"content-length", str(len(body)).encode("ascii"))]
    headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _OAuthEdgeMiddleware:
    """Pure ASGI, in front of the router: per-client rate limits and a body cap
    on the public OAuth endpoints. Everything else passes straight through."""

    def __init__(self, app, limiter: "_RateLimiter", max_body: int = None):
        self.app = app
        self.limiter = limiter
        self.max_body = OAUTH_MAX_BODY_BYTES if max_body is None else max_body

    async def __call__(self, scope, receive, send):
        draws = _OAUTH_RATE_RULES.get(scope.get("path")) if scope.get("type") == "http" else None
        if draws is None:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        tag = _client_tag(client[0] if client else None)
        retry = self.limiter.take([(rule, "*" if who == "*" else tag) for rule, who in draws])
        if retry:
            logger.warning("OAuth edge: rate limit on %s %s from client=%s (retry in %.0fs)",
                           scope.get("method"), scope.get("path"), tag, retry)
            await _send_json(send, 429, {
                "error": "temporarily_unavailable",
                "error_description": "Too many requests; retry later."},
                [(b"retry-after", str(max(1, int(retry + 0.999))).encode("ascii"))])
            return

        # Cap the body: a declared Content-Length over the cap is refused
        # unread, and a chunked body is read only up to the cap.
        length = None
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    length = int(v)
                except ValueError:
                    length = -1
        too_large = {"error": "invalid_request",
                     "error_description": f"Request body exceeds {self.max_body} bytes."}
        if length is not None and (length < 0 or length > self.max_body):
            await _send_json(send, 413, too_large)
            return
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                await _send_json(send, 413, too_large)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        try:
            await self.app(scope, replay, send)
        finally:
            await asyncio.to_thread(self.limiter.flush)


def _is_oauth_public_path(p: str) -> bool:
    """True iff the request path is part of the public OAuth bootstrap surface."""
    if p in OAUTH_PUBLIC_PATHS:
        return True
    return any(p.startswith(prefix) for prefix in OAUTH_PUBLIC_PREFIXES)


def _load_oauth_clients() -> Dict[str, dict]:
    """Load the registered-client store (client_id -> registration dict).

    Returns {} if the file is missing or unreadable -- a fresh box simply has no
    registered clients yet (registration creates the file on first POST). A
    store that does not parse is quarantined (<name>.corrupt-<ts>) rather than
    left for the next registration to overwrite.
    """
    return atomic_json.read_json(BLACKBOX_MCP_OAUTH_CLIENTS_FILE, {},
                                 valid=lambda d: isinstance(d, dict),
                                 cross_process=True)


# Client store bounds (audit O20-mcp-server-09). A registration that never
# completes a flow is "pending"; it expires after a day, and one address may
# hold only a few at once. The store as a whole is capped; when full, the oldest
# pending registration makes room. Registrations made before these bounds
# (no "registered_from") are kept as they were.
MAX_OAUTH_CLIENTS = int(os.getenv("BLACKBOX_MCP_OAUTH_MAX_CLIENTS", "500"))
MAX_PENDING_CLIENTS_PER_ADDRESS = 10
PENDING_CLIENT_TTL = 24 * 3600


def _is_pending_client(record) -> bool:
    return (isinstance(record, dict) and "registered_from" in record
            and "last_token_at" not in record)


def _register_oauth_client(client_id: str, record: dict, tag: str, now: float):
    """Add one registration within the store bounds, under the store lock.

    Returns None when stored, else (status, error, description) to refuse it.
    """
    stored = dict(record, registered_from=tag)
    with atomic_json.update_json(BLACKBOX_MCP_OAUTH_CLIENTS_FILE, {},
                                 valid=lambda d: isinstance(d, dict),
                                 cross_process=True) as clients:
        for cid in [cid for cid, rec in clients.items()
                    if _is_pending_client(rec)
                    and now - (rec.get("client_id_issued_at") or 0) > PENDING_CLIENT_TTL]:
            del clients[cid]
        pending = [(rec.get("client_id_issued_at") or 0, cid) for cid, rec in clients.items()
                   if _is_pending_client(rec)]
        if sum(1 for _at, cid in pending
               if clients[cid].get("registered_from") == tag) >= MAX_PENDING_CLIENTS_PER_ADDRESS:
            return (429, "temporarily_unavailable",
                    "Too many unfinished registrations from this address; complete or wait for them to expire.")
        if len(clients) >= MAX_OAUTH_CLIENTS:
            if not pending:
                return (503, "temporarily_unavailable", "The client registry is full.")
            _at, oldest = min(pending)
            del clients[oldest]
            logger.warning("OAuth DCR: client store full (%d); dropped the oldest unfinished "
                           "registration %s", MAX_OAUTH_CLIENTS, oldest)
        clients[client_id] = stored
    return None


def _touch_oauth_client(client_id: str, now: float) -> None:
    """Record that a client completed a flow: it is no longer pending."""
    with atomic_json.update_json(BLACKBOX_MCP_OAUTH_CLIENTS_FILE, {},
                                 valid=lambda d: isinstance(d, dict),
                                 cross_process=True) as clients:
        rec = clients.get(client_id)
        if isinstance(rec, dict):
            rec["last_token_at"] = int(now)


def _persist_oauth_client(client_id: str, record: dict) -> None:
    """Persist one registered client to the gitignored store, 0600.

    Read-modify-write the whole map (DCR is low-volume -- one record per client
    that ever connects). The file is created with 0600 perms (owner-only); the
    parent Manifest/ is gitignored, as is **/*token*.json, so the store is doubly
    excluded from git and never world-readable.
    """
    # Locked read-modify-write, then an atomic replace from a temp file that
    # is 0600 from creation (audit O20-mcp-server-12): a crash mid-write can
    # no longer leave a truncated store that silently reads as {}, and two
    # concurrent /register calls can no longer drop each other's client.
    with atomic_json.update_json(BLACKBOX_MCP_OAUTH_CLIENTS_FILE, {},
                                 valid=lambda d: isinstance(d, dict),
                                 cross_process=True) as clients:
        clients[client_id] = record


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
        "revocation_endpoint": issuer + "/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
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


_OAUTH_UNCONFIGURED = {
    "error": "server_not_configured",
    "error_description": (
        "OAuth discovery requires BLACKBOX_MCP_PUBLIC_URL to be set to this box's "
        "public Funnel origin (e.g. https://<your-host>:8443). Bearer-token auth "
        "still works without it."
    ),
}


async def _oauth_as_metadata_handler(request):
    """GET /.well-known/oauth-authorization-server (RFC 8414)."""
    from starlette.responses import JSONResponse
    if not BLACKBOX_MCP_PUBLIC_URL:
        return JSONResponse(_OAUTH_UNCONFIGURED, status_code=503,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(
        _oauth_as_metadata(),
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _oauth_pr_metadata_handler(request):
    """GET /.well-known/oauth-protected-resource (RFC 9728)."""
    from starlette.responses import JSONResponse
    if not BLACKBOX_MCP_PUBLIC_URL:
        return JSONResponse(_OAUTH_UNCONFIGURED, status_code=503,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(
        _oauth_protected_resource_metadata(),
        headers={"Cache-Control": "public, max-age=3600"},
    )


# RFC 7591 string metadata kept for a client -> its length cap.
_CLIENT_STRING_FIELDS = {
    "client_name": 256, "client_uri": 2048, "logo_uri": 2048, "scope": 512,
    "tos_uri": 2048, "policy_uri": 2048, "software_id": 256, "software_version": 64,
}
MAX_CLIENT_CONTACTS = 10
MAX_REDIRECT_URIS = 10
MAX_REDIRECT_URI_LENGTH = 2048
# Never a redirect target: they run code or read local data in the browser.
_FORBIDDEN_REDIRECT_SCHEMES = {"javascript", "data", "vbscript", "file", "blob", "about"}


def _is_text(value) -> bool:
    """A str that is valid Unicode text. JSON can spell a lone surrogate
    ("\\ud800"), which no response, log line or store can encode as UTF-8."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _redirect_uris_error(uris: list) -> Optional[str]:
    """Why a registration's redirect_uris are unacceptable, or None."""
    from urllib.parse import urlsplit
    if len(uris) > MAX_REDIRECT_URIS:
        return f"at most {MAX_REDIRECT_URIS} redirect_uris"
    for uri in uris:
        if not _is_text(uri):
            return "a redirect_uri is not valid Unicode text"
        if len(uri) > MAX_REDIRECT_URI_LENGTH:
            return f"a redirect_uri is longer than {MAX_REDIRECT_URI_LENGTH} characters"
        if "#" in uri or any(ord(ch) < 0x21 or ord(ch) == 0x7f for ch in uri):
            return "a redirect_uri may not contain a fragment, spaces or control characters"
        try:
            parts = urlsplit(uri)
        except ValueError:
            return "a redirect_uri is not a valid URI"
        scheme = parts.scheme.lower()
        if not scheme or scheme in _FORBIDDEN_REDIRECT_SCHEMES:
            return "a redirect_uri needs an absolute URI with an allowed scheme"
        if scheme in ("http", "https") and not parts.netloc:
            return "an http(s) redirect_uri needs a host"
    return None


async def _oauth_register_handler(request):
    """POST /register -- RFC 7591 Dynamic Client Registration (public PKCE).

    Accepts {redirect_uris, client_name, ...}. redirect_uris is REQUIRED and must
    be a non-empty list (RFC 7591 + the auth_code flow needs at least one). We
    mint a client_id, force token_endpoint_auth_method=none (public client, NO
    secret), persist the registration to the gitignored 0600 store, and return
    the RFC 7591 client information response (201).

    Anyone on the internet can call this (the edge caps the body and the rate),
    so every stored field is type- and length-checked and the store is bounded
    (_register_oauth_client; audit O20-mcp-server-09).
    """
    from starlette.responses import JSONResponse

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
    bad_uri = _redirect_uris_error(redirect_uris)
    if bad_uri:
        return JSONResponse({"error": "invalid_redirect_uri", "error_description": bad_uri},
                            status_code=400)

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
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    for field, limit in _CLIENT_STRING_FIELDS.items():
        value = body.get(field)
        if value is None:
            continue
        if not _is_text(value) or len(value) > limit:
            return JSONResponse(
                {"error": "invalid_client_metadata",
                 "error_description": f"{field} must be a string of at most {limit} characters"},
                status_code=400)
        record[field] = value
    contacts = body.get("contacts")
    if contacts is not None:
        if (not isinstance(contacts, list) or len(contacts) > MAX_CLIENT_CONTACTS
                or not all(_is_text(c) and len(c) <= 320 for c in contacts)):
            return JSONResponse(
                {"error": "invalid_client_metadata",
                 "error_description": f"contacts must be a list of at most "
                                      f"{MAX_CLIENT_CONTACTS} strings"},
                status_code=400)
        record["contacts"] = contacts

    client = getattr(request, "client", None)
    try:
        refused = await asyncio.to_thread(
            _register_oauth_client, client_id, record,
            _client_tag(client.host if client else None), _time.time())
    except Exception as e:
        logger.error("Failed to persist OAuth client %s: %s", client_id, e)
        return JSONResponse(
            {"error": "invalid_client_metadata",
             "error_description": "Failed to persist client registration"},
            status_code=500,
        )
    if refused:
        status, error, description = refused
        logger.warning("OAuth DCR: refused a registration (%s): %s", status, description)
        return JSONResponse({"error": error, "error_description": description},
                            status_code=status, headers={"Cache-Control": "no-store"})

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
#     (ACCESS_TOKEN_TTL), and are stored as token->{operator, expiry, client_id,
#     family, consent} so chunk 6c can validate them on /mcp. The token VALUE is
#     never logged (only the sha256[:12] id, matching the M3 _token_id pattern).
#   * Refresh tokens (prefix bbmcp_ort_) come with every access token, rotate on
#     every use, are stored by hash only, and a replayed one revokes its whole
#     family (audit O20-mcp-server-13). Every token of a family dies with the
#     bearer that consented to it.
#   * Fail-closed: any validation gap -> reject. The operator is bound at
#     /authorize from the AUTHENTICATED bearer token's operator (the consent gate)
#     and carried through to the access token; the remote client never chooses it.
#
# Codes live IN-MEMORY only (short-lived, single process). Access tokens are
# kept in a process-local dict AND mirrored to a gitignored 0600 file
# (Manifest/mcp_oauth_tokens.json) so chunk 6c can read the binding; refresh
# tokens live in Manifest/mcp_oauth_refresh_tokens.json. NO token value is ever
# written to a COMMITTED file (the stores are gitignored).
# =============================================================================
import base64 as _b64
import secrets as _secrets
import threading as _threading
import time as _time

# The operator an OAuth-minted access token binds to is the operator of the
# BlackBox bearer token AUTHENTICATED at the consent form (/authorize POST) -- see
# the consent gate below. There is deliberately NO server-wide default operator:
# a real person's name must never be a fail-open fallback (a fresh customer box
# has no dev-box operator). A code with no bound operator is a bug -> fail closed.

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

# In-process access-token mirror: access_token -> {operator, expiry, client_id,
# family, consent}. The authoritative copy is also written to the gitignored
# file so chunk 6c can read it without sharing this process's memory.
_ACCESS_TOKENS: Dict[str, dict] = {}
_ACCESS_TOKENS_LOCK = _threading.Lock()

# Token families revoked in this process (a refresh token reused, its consent
# withdrawn, or /revoke): checked on every match, so an access token read back
# from the file store in the instant of a revocation can never be warmed again.
# The durable copy is a tombstone in the refresh store (_tombstone_family),
# which a restart does not forget.
_REVOKED_FAMILIES: set = set()


class _FamilyRevoked(Exception):
    """A token pair was about to be issued into a revoked family."""


# Refresh tokens (audit O20-mcp-server-13): without them claude.ai / Claude
# Desktop lost the connection every hour and the operator pasted the long-lived
# bbmcp_ secret into the consent form again. A refresh token is single-use and
# rotates: each refresh returns a new one in the same family, and presenting a
# used one again (a replay of a stolen token) revokes the whole family. The
# family also dies with the bearer that consented. Stored by sha256 only, in a
# gitignored 0600 file, so the store never holds a usable refresh token.
BLACKBOX_MCP_OAUTH_REFRESH_FILE = os.getenv(
    "BLACKBOX_MCP_OAUTH_REFRESH_FILE",
    str(BLACKBOX_ROOT / "Manifest" / "mcp_oauth_refresh_tokens.json"),
)
REFRESH_TOKEN_TTL = int(os.getenv("BLACKBOX_MCP_OAUTH_REFRESH_TTL", str(30 * 24 * 3600)))  # 30 d
# How long a rotated token is remembered, to catch its reuse.
REFRESH_REUSE_WINDOW = 24 * 3600

# RFC 7636: a verifier is 43-128 unreserved characters; an S256 challenge is
# the unpadded base64url of a SHA-256 digest. The tokens this server issues
# are prefix + token_urlsafe.
_PKCE_VERIFIER_RE = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_S256_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_OPAQUE_TOKEN_RE = re.compile(r"[A-Za-z0-9._~-]{1,512}")


def _str_params(items) -> Dict[str, str]:
    """Form / query / JSON fields that are plain strings of valid text (others
    dropped)."""
    try:
        pairs = items.items()
    except AttributeError:
        return {}
    return {k: v for k, v in pairs if _is_text(k) and _is_text(v)}


def _refresh_key(token: str) -> str:
    return hashlib.sha256(_token_bytes(token)).hexdigest()


def _valid_store(d) -> bool:
    return isinstance(d, dict)


def _live_consents() -> Dict[str, str]:
    """fingerprint -> operator of every bearer the live gate accepts now."""
    mw = _AUTH_MIDDLEWARE["instance"]
    if mw is not None:
        return mw.live_consents()
    return {_token_fingerprint(t): op for t, op in _load_token_map().items()}


def _consent_alive(binding: dict, consents: Dict[str, str]) -> bool:
    consent = binding.get("consent")
    if consent is None:
        return True          # issued before consent binding: lives out its TTL
    return consents.get(consent) == binding.get("operator")


def _persist_refresh_token(token: str, record: dict, now: float) -> None:
    """Store one refresh token (by hash), dropping the expired ones. Raises
    _FamilyRevoked, storing nothing, when its family has been revoked."""
    with atomic_json.update_json(BLACKBOX_MCP_OAUTH_REFRESH_FILE, {},
                                 valid=_valid_store, cross_process=True) as store:
        for key in [k for k, r in store.items()
                    if not (isinstance(r, dict) and r.get("expiry", 0) > now)]:
            del store[key]
        if _family_revoked(record.get("family"), store):
            raise _FamilyRevoked(record.get("family"))
        store[_refresh_key(token)] = record


# A revoked family's tombstone in the refresh store: keyed apart from the
# sha256 hex keys of the tokens, and kept as long as any token of the family
# could otherwise still be presented.
_TOMBSTONE_PREFIX = "revoked-family:"


def _tombstone_family(store: dict, family: str, now: float) -> None:
    """Inside an open edit of the refresh store: mark `family` revoked and drop
    its refresh tokens, in one write. Issuance checks the tombstone under the
    same lock, so no refresh token of the family can be stored after it."""
    _REVOKED_FAMILIES.add(family)
    store[_TOMBSTONE_PREFIX + family] = {
        "revoked_at": now, "expiry": now + max(REFRESH_TOKEN_TTL, ACCESS_TOKEN_TTL)}
    for key in [k for k, r in store.items()
                if isinstance(r, dict) and r.get("family") == family]:
        del store[key]


def _family_revoked(family, store: Optional[dict] = None) -> bool:
    """Is `family` revoked: in this process, or by a tombstone in the refresh
    store (`store` when the caller holds it open, else read fresh)?"""
    if not family:
        return False
    if family in _REVOKED_FAMILIES:
        return True
    if store is None:
        store = atomic_json.read_json(BLACKBOX_MCP_OAUTH_REFRESH_FILE, {},
                                      valid=_valid_store, cross_process=True)
    rec = store.get(_TOMBSTONE_PREFIX + family)
    if isinstance(rec, dict) and rec.get("expiry", 0) > _time.time():
        _REVOKED_FAMILIES.add(family)
        return True
    return False


def _drop_family_access_tokens(family: str) -> None:
    """Remove every access token of one family (file and memory)."""
    with atomic_json.update_json(BLACKBOX_MCP_OAUTH_TOKENS_FILE, {},
                                 valid=_valid_store, cross_process=True) as store:
        for tok in [t for t, b in store.items()
                    if isinstance(b, dict) and b.get("family") == family]:
            del store[tok]
    with _ACCESS_TOKENS_LOCK:
        for tok in [t for t, b in _ACCESS_TOKENS.items() if b.get("family") == family]:
            _ACCESS_TOKENS.pop(tok, None)


def _revoke_family(family: str, why: str) -> None:
    """End every refresh and access token of one family (file and memory),
    durably: the tombstone outlives a restart."""
    if not family:
        return
    with atomic_json.update_json(BLACKBOX_MCP_OAUTH_REFRESH_FILE, {},
                                 valid=_valid_store, cross_process=True) as store:
        _tombstone_family(store, family, _time.time())
    _drop_family_access_tokens(family)
    logger.warning("OAuth: revoked token family %s (%s)", family[:12], why)


def _redeem_refresh_token(presented: str, client_id: str, consents: Dict[str, str],
                          now: float):
    """Spend a refresh token. Returns (binding, None) for a live one -- marked
    used, so it can never be spent again -- or (None, (error, description)).

    A token that was already spent is a replay: the whole family is revoked.
    A token whose consenting bearer is gone revokes its family too. Either
    revocation is written in the same locked edit that read the token, so a
    concurrent refresh of the same family cannot slip a new pair in between.
    """
    key = _refresh_key(presented)
    revoked, why, binding = None, None, None
    dead = ("invalid_grant", "refresh token is no longer valid; reconnect to authorize again")
    with atomic_json.update_json(BLACKBOX_MCP_OAUTH_REFRESH_FILE, {},
                                 valid=_valid_store, cross_process=True) as store:
        for k in [k for k, r in store.items()
                  if not (isinstance(r, dict) and r.get("expiry", 0) > now)]:
            del store[k]
        rec = store.get(key)
        if not isinstance(rec, dict) or not rec.get("family"):
            return None, ("invalid_grant", "refresh token is invalid or expired")
        if rec.get("client_id") != client_id:
            return None, ("invalid_grant", "refresh token was not issued to this client")
        family = rec["family"]
        if _family_revoked(family, store):
            why = "a token of an already revoked family was presented"
        elif rec.get("used_at") is not None:
            why = "a spent refresh token was presented again"
        elif not _consent_alive(rec, consents):
            why = "the bearer that authorized it is no longer valid"
        if why is not None:
            _tombstone_family(store, family, now)
            revoked = family
        else:
            rec["used_at"] = now
            rec["expiry"] = min(rec.get("expiry", now), now + REFRESH_REUSE_WINDOW)
            binding = {k: rec.get(k) for k in ("client_id", "operator", "scope", "consent", "family")}
    if revoked is not None:
        _drop_family_access_tokens(revoked)
        logger.warning("OAuth: revoked token family %s (%s)", revoked[:12], why)
        return None, dead
    return binding, None


def _issue_token_pair(binding: dict, now: float):
    """Mint an access token and a refresh token for one consented binding
    ({client_id, operator, scope, consent, family?}). Returns
    (access_token, refresh_token); raises if either cannot be stored."""
    family = binding.get("family") or uuid.uuid4().hex
    access_token = "bbmcp_oat_" + _secrets.token_urlsafe(40)
    refresh_token = "bbmcp_ort_" + _secrets.token_urlsafe(40)
    common = {"operator": binding["operator"], "client_id": binding["client_id"],
              "family": family}
    if binding.get("consent"):
        common["consent"] = binding["consent"]
    # The refresh token first: one stored but never handed out just expires.
    _persist_refresh_token(refresh_token, dict(
        common, scope=binding.get("scope") or "", issued_at=now,
        expiry=now + REFRESH_TOKEN_TTL, used_at=None), now)
    expiry = now + ACCESS_TOKEN_TTL
    access_binding = dict(common, expiry=expiry)
    with _ACCESS_TOKENS_LOCK:
        # Prune expired in-process entries too, then add the new one.
        for t in [t for t, b in _ACCESS_TOKENS.items() if b.get("expiry", 0) <= now]:
            _ACCESS_TOKENS.pop(t, None)
        _ACCESS_TOKENS[access_token] = access_binding
    try:
        _persist_access_token(access_token, binding["operator"], expiry,
                              **{k: v for k, v in common.items() if k != "operator"})
        # A revocation that ran after the refresh token was stored removed it,
        # but may have cleaned the access-token store before this one landed.
        # It marks the family before it cleans, so looking again now catches
        # every such interleaving.
        if _family_revoked(family):
            _drop_family_access_tokens(family)
            raise _FamilyRevoked(family)
    except Exception:
        # Fail CLOSED: an access token chunk 6c cannot validate is worse than none.
        with _ACCESS_TOKENS_LOCK:
            _ACCESS_TOKENS.pop(access_token, None)
        raise
    return access_token, refresh_token


def _revoke_token(token: str, client_id: str) -> None:
    """RFC 7009: revoke a refresh token (its whole family) or an access token
    issued to `client_id`. A token of another client, or none, is a no-op."""
    now = _time.time()
    rec = atomic_json.read_json(BLACKBOX_MCP_OAUTH_REFRESH_FILE, {},
                                valid=_valid_store, cross_process=True).get(_refresh_key(token))
    if isinstance(rec, dict) and rec.get("expiry", 0) > now and rec.get("client_id") == client_id:
        _revoke_family(rec.get("family"), "revoked by its client")
        return
    with _ACCESS_TOKENS_LOCK:
        binding = _ACCESS_TOKENS.get(token)
    if binding is None:
        binding = atomic_json.read_json(BLACKBOX_MCP_OAUTH_TOKENS_FILE, {},
                                        valid=_valid_store, cross_process=True).get(token)
    if isinstance(binding, dict) and binding.get("client_id") == client_id:
        with atomic_json.update_json(BLACKBOX_MCP_OAUTH_TOKENS_FILE, {},
                                     valid=_valid_store, cross_process=True) as store:
            store.pop(token, None)
        with _ACCESS_TOKENS_LOCK:
            _ACCESS_TOKENS.pop(token, None)
        logger.info("OAuth: access token id=%s revoked by its client", _token_id(token))


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


def _persist_access_token(token: str, operator: str, expiry: float, **binding) -> None:
    """Mirror one access token -> {operator, expiry, + its family binding} to
    the gitignored 0600 file.

    Read-modify-write the whole map (token issuance is low-volume). Expired
    tokens are pruned on every write so the file does not grow unbounded. The
    file is (re)created 0600 (owner-only) -- it carries live credentials, so it
    must never be world-readable, and Manifest/ + **/*token*.json keep it out of
    git. Chunk 6c reads this file (and/or the in-process mirror) to validate.
    """
    # Load existing, prune expired, add the new one: locked, atomic, 0600
    # from creation (see _persist_oauth_client).
    now = _time.time()
    with atomic_json.update_json(BLACKBOX_MCP_OAUTH_TOKENS_FILE, {},
                                 valid=lambda d: isinstance(d, dict),
                                 cross_process=True) as existing:
        for t in [t for t, b in existing.items()
                  if not (isinstance(b, dict) and b.get("expiry", 0) > now)]:
            del existing[t]
        existing[token] = dict(binding, operator=operator, expiry=expiry)


def _match_oauth_access_token(presented: str, consents: Optional[Dict[str, str]] = None):
    """Validate an OAuth access token (chunk 6c). Returns (operator, token_id)
    for a PRESENT + UNEXPIRED token, else (None, None).

    `consents` (fingerprint -> operator of the live bearers): a token whose
    consenting bearer has left the map, or whose family was revoked, is
    invalid. Tokens minted before consent binding carry none and live out
    their TTL.

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
                    # A family tombstoned before a restart stays dead: the
                    # in-memory set starts empty, and this is the only way
                    # into the mirror.
                    if (isinstance(fb, dict) and fb.get("expiry", 0) > now
                            and not _family_revoked(fb.get("family"))):
                        binding = fb
                        # Warm the in-process mirror for subsequent requests.
                        with _ACCESS_TOKENS_LOCK:
                            _ACCESS_TOKENS[presented] = dict(fb)
        except Exception as e:
            logger.error("OAuth token validation: failed to read token store %s: %s",
                         BLACKBOX_MCP_OAUTH_TOKENS_FILE, e)
    if binding is None:
        return None, None
    operator = binding.get("operator")
    if not operator:
        return None, None
    if binding.get("family") in _REVOKED_FAMILIES:
        return None, None
    if consents is not None and not _consent_alive(binding, consents):
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
    # Only plain string fields count: a multipart file part is never a param.
    params = _str_params((await request.form()) if is_post else request.query_params)
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
    if not _S256_CHALLENGE_RE.fullmatch(code_challenge):
        return _redirect_error(
            "invalid_request",
            "code_challenge must be a base64url SHA-256 digest (43 characters)")

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
    # Authenticate against the SAME roster-validated map the /mcp gate uses (the
    # live one, hot-reloaded in place), so a ghost-operator token (dropped at
    # /mcp) cannot mint an OAuth token here either, and the tokens minted from
    # this consent stay valid exactly as long as that bearer does.
    consent_token = (params.get("blackbox_token") or "").strip()
    mw = _AUTH_MIDDLEWARE["instance"]
    live_tokens = (mw.token_map if mw is not None
                   else await asyncio.to_thread(_load_validated_token_map))
    operator, _tid = _match_token(consent_token, live_tokens)
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
        # The bearer that consented: the OAuth tokens from this code live only
        # as long as it stays in the token map (revoking it ends them).
        "consent": _token_fingerprint(consent_token),
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
    for an opaque access token + a rotating refresh token, verifying PKCE S256;
    or spend a refresh token (grant_type=refresh_token) for a new pair.

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

    # Accept form-encoded (RFC 6749) and tolerate JSON bodies. Only string
    # fields count (a JSON number or a file part is never a token or a code).
    try:
        body = _str_params(await request.form())
    except Exception:
        body = {}
    if not body:
        try:
            body = _str_params(await request.json())
        except Exception:
            body = {}

    grant_type = body.get("grant_type")
    if grant_type == "refresh_token":
        return await _oauth_refresh_grant(body, _err)
    if grant_type != "authorization_code":
        return _err("unsupported_grant_type",
                    "grant_type must be authorization_code or refresh_token")

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
    if not _PKCE_VERIFIER_RE.fullmatch(code_verifier):
        return _err("invalid_grant",
                    "code_verifier must be 43-128 unreserved characters (RFC 7636)")

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
    if not hmac.compare_digest(derived.encode("ascii"), _token_bytes(expected)):
        return _err("invalid_grant", "PKCE code_verifier does not match")

    # ---- success: mint + store an access token and a refresh token ----
    operator = binding.get("operator")
    if not operator:
        # The consent gate always binds the authenticated operator; a missing one
        # is a server bug, not an excuse to fall back to someone's name.
        return _err("server_error", "no operator bound to authorization code")
    return await _token_response(
        {"client_id": client_id, "operator": operator, "scope": binding.get("scope") or "",
         "consent": binding.get("consent")},
        now, _err, f"code id={_token_id(code)} consumed")


async def _token_response(binding: dict, now: float, _err, how: str):
    """Issue a token pair for `binding` and answer with RFC 6749 5.1."""
    from starlette.responses import JSONResponse
    try:
        access_token, refresh_token = await asyncio.to_thread(_issue_token_pair, binding, now)
    except _FamilyRevoked:
        # A replay of the same refresh token revoked the family mid-request.
        logger.warning("OAuth token: refused a pair for a revoked family (client=%s)",
                       binding.get("client_id"))
        return _err("invalid_grant",
                    "refresh token is no longer valid; reconnect to authorize again")
    except Exception as e:
        logger.error("OAuth token: failed to persist issued tokens (client=%s): %s",
                     binding.get("client_id"), e)
        return _err("server_error", "failed to persist issued token", status=500)
    try:
        # The client completed a flow: it is no longer an unfinished registration.
        await asyncio.to_thread(_touch_oauth_client, binding["client_id"], now)
    except Exception as e:
        logger.error("OAuth token: could not mark client %s as used: %s",
                     binding.get("client_id"), e)
    logger.info("OAuth token: issued access token id=%s client=%s operator=%s "
                "expires_in=%ds (%s)", _token_id(access_token), binding["client_id"],
                binding["operator"], ACCESS_TOKEN_TTL, how)
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "refresh_token": refresh_token,
            "scope": binding.get("scope") or "",
        },
        status_code=200,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _oauth_refresh_grant(body: dict, _err):
    """grant_type=refresh_token: spend the presented refresh token (rotation)
    and issue a new pair in the same family, for the same client and operator."""
    refresh_token = body.get("refresh_token")
    client_id = body.get("client_id")
    if not refresh_token:
        return _err("invalid_request", "refresh_token is required")
    if not client_id:
        return _err("invalid_request", "client_id is required")
    if not _OPAQUE_TOKEN_RE.fullmatch(refresh_token):
        return _err("invalid_grant", "refresh token is invalid or expired")
    now = _time.time()
    binding, error = await asyncio.to_thread(
        _redeem_refresh_token, refresh_token, client_id, _live_consents(), now)
    if error is not None:
        logger.warning("OAuth token: refresh refused client=%s: %s", client_id, error[1])
        return _err(*error)
    return await _token_response(binding, now, _err, "refresh token rotated")


async def _oauth_revoke_handler(request):
    """POST /revoke -- RFC 7009 token revocation for public clients.

    Revokes a refresh token (and with it every token of its family) or one
    access token, if it was issued to `client_id`. Answers 200 whether or not
    anything matched, so it confirms nothing about a token it did not issue.
    """
    from starlette.responses import JSONResponse, Response
    try:
        body = _str_params(await request.form())
    except Exception:
        body = {}
    token = body.get("token")
    client_id = body.get("client_id")
    if not token or not client_id:
        return JSONResponse({"error": "invalid_request",
                             "error_description": "token and client_id are required"},
                            status_code=400, headers={"Cache-Control": "no-store"})
    if _OPAQUE_TOKEN_RE.fullmatch(token):
        await asyncio.to_thread(_revoke_token, token, client_id)
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


# =============================================================================
# THE CONTROL LISTENER (audit O20-mcp-server-08)
#
# POST /internal/reload is served on its own loopback-only listener,
# 127.0.0.1:<BLACKBOX_MCP_CONTROL_PORT> (default 9094, the address
# Orchestrator/utils/internal_secret.mcp_control_url() names), never on the
# public listener Tailscale Funnel forwards the internet to. It is a second
# uvicorn server on the same event loop, started and stopped by the public
# app's lifespan; the public server keeps the signal handlers.
# =============================================================================
class _ControlListener:
    def __init__(self, server, task, address):
        self._server = server
        self._task = task
        self.address = address

    async def stop(self) -> None:
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, 10)
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("internal/reload control listener did not stop cleanly: %s", e)


async def _start_control_listener(port: int = None):
    """Serve /internal/reload on 127.0.0.1 only. Returns the listener, or None
    (logged) when the port cannot be bound: token and public-URL changes then
    take effect on the next restart."""
    import contextlib
    import socket
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Route

    if port is None:
        port = internal_secret.mcp_control_port()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))   # loopback, whatever BLACKBOX_MCP_HTTP_HOST says
    except OSError as e:
        sock.close()
        logger.error("internal/reload control listener NOT started: cannot bind "
                     "127.0.0.1:%d (%s). Token and public-URL changes take effect on "
                     "the next restart.", port, e)
        return None

    class _QuietServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield      # the public server owns SIGINT/SIGTERM

    app = Starlette(routes=[Route("/internal/reload", endpoint=_internal_reload_handler,
                                  methods=["POST"])])
    # log_config=None and no log_level: a uvicorn Config reconfigures the
    # process-wide "uvicorn.*" loggers when it is built, and access_log=False
    # would strip the access handler the PUBLIC listener logs every Funnel
    # request through. This one reuses whatever run_http() set up.
    config = uvicorn.Config(app, lifespan="off", log_config=None, log_level=None,
                            proxy_headers=False)
    control = _QuietServer(config)
    task = asyncio.create_task(control.serve(sockets=[sock]))
    while not control.started and not task.done():
        await asyncio.sleep(0.01)
    if task.done():
        logger.error("internal/reload control listener failed to start: %r",
                     task.exception() if not task.cancelled() else "cancelled")
        return None
    address = sock.getsockname()
    logger.info("internal/reload control listener on http://%s:%d (loopback only)", *address)
    return _ControlListener(control, task, address)


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

    # The public OAuth edge's rate limiter, restored from its ledger.
    limiter = _RateLimiter(OAUTH_RATE_LIMITS, ledger_path=BLACKBOX_MCP_OAUTH_RATE_LEDGER_FILE)
    limiter.load()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            logger.info("Streamable HTTP session manager running at path %s", path)
            control = await _start_control_listener()
            try:
                yield
            finally:
                if control is not None:
                    await control.stop()
                await asyncio.to_thread(limiter.flush, True)

    # M3: load the token->operator map ONCE and wrap the MCP endpoint in the
    # bearer-auth gate. A loaded map of size 0 means the transport FAILS CLOSED
    # (every request 401s) -- we log that loudly so a misconfigured deploy is
    # obvious rather than silently rejecting everything.
    token_map = _validate_token_operators(_load_token_map())
    if not token_map:
        logger.error("M3 AUTH: token map is EMPTY -- the HTTP transport will reject "
                     "EVERY request (fail-closed). Set BLACKBOX_MCP_TOKENS (env JSON) "
                     "or %s.", BLACKBOX_MCP_TOKENS_FILE)
    else:
        logger.info("M3 AUTH: loaded %d bearer token(s) bound to operator(s): %s",
                    len(token_map), sorted(set(token_map.values())))

    if not BLACKBOX_MCP_PUBLIC_URL:
        logger.warning("OAuth DISABLED: BLACKBOX_MCP_PUBLIC_URL is unset -- the "
                       "/.well-known/oauth-* endpoints return 503, so claude.ai / "
                       "native Claude Desktop cannot connect via OAuth. Bearer-token "
                       "clients are unaffected. Set it to this box's public Funnel "
                       "origin to enable OAuth.")

    guarded_endpoint = BearerAuthMiddleware(_MCPASGIApp(session_manager), token_map)
    _AUTH_MIDDLEWARE["instance"] = guarded_endpoint   # for the control listener's /internal/reload

    # Whichever side starts first writes this boot's reload secret (the
    # Orchestrator also creates it on demand), so it exists before a reload.
    try:
        internal_secret.ensure_secret(_reload_secret_path())
    except OSError as e:
        logger.warning("internal/reload secret unavailable (%s); the Orchestrator "
                       "writes it on its first reload", e)

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
        Route("/revoke",
              endpoint=_oauth_revoke_handler, methods=["POST"]),
        # /internal/reload is NOT here: Funnel forwards the internet to this
        # listener. It lives on the loopback control listener (lifespan above).
    ]

    from starlette.middleware import Middleware
    app = Starlette(
        debug=False,
        routes=[
            Route(path, endpoint=guarded_endpoint,
                  methods=["GET", "POST", "DELETE"]),
            *oauth_routes,
        ],
        middleware=[Middleware(_OAuthEdgeMiddleware, limiter=limiter)],
        lifespan=lifespan,
    )
    app.state.oauth_limiter = limiter
    return app, session_manager


def run_http(host: str = None, port: int = None, path: str = None):
    """Run the Streamable HTTP transport via uvicorn (localhost-only for M2)."""
    import uvicorn

    host = host or BLACKBOX_MCP_HTTP_HOST
    port = port if port is not None else BLACKBOX_MCP_HTTP_PORT
    path = path or BLACKBOX_MCP_HTTP_PATH

    _log_startup_banner("streamable-http")
    _require_tool_catalog()
    logger.info("Streamable HTTP transport binding http://%s:%d%s (M3: bearer auth + "
                "token-bound operator + audit)", host, port, path)
    if host not in ("127.0.0.1", "localhost", "::1"):
        # M2 guard-rail: we are not supposed to expose beyond localhost yet.
        logger.warning("HTTP host is %s -- M2 is localhost-only; non-localhost bind is "
                       "premature (auth is M3, public exposure is M4).", host)

    app, _sm = build_http_app(path)
    # The client address the OAuth edge keys its rate limits on comes from
    # X-Forwarded-For only when the peer is loopback (the local Funnel/serve
    # proxy), whatever FORWARDED_ALLOW_IPS the environment carries.
    config = uvicorn.Config(app, host=host, port=port, log_level=LOG_LEVEL.lower(),
                            proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1")
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
