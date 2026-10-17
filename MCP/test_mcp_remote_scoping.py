#!/usr/bin/env python3
"""
Hostile-input regression tests for the internet-facing MCP surface (audit
2026-10-09 ranks 22 + 33: X01-deps-python-04, O20-mcp-server-03/-06/-07/-15/-16).

Every test is HERMETIC: no backend at :9091, no network, no real token / OAuth /
device / snapshot store. The HTTP transport is driven IN-PROCESS through
httpx.ASGITransport against the real Starlette app + the real SDK
StreamableHTTPSessionManager, and backend HTTP calls go to a fake client.

  (1) session binding  -- an mcp-session-id only works with the token that
      opened it; any other token (and any unknown id) gets the SDK's own
      "Session not found" 404 before the session manager runs.
  (2) tools/list       -- a bound caller's operator enums name only itself.
  (3) validate_input   -- the documented operator='' (span all) reaches the
      handler on stdio instead of dying in SDK schema validation.
  (4) list_devices     -- bound: own devices only, no IPs/ports/DNS metadata,
      and the backend's devices.json is never rewritten by this process.
  (5) get_media        -- bound: task_id required, ownership from the task's
      operator, the url must be one of the task's own outputs (any of its
      all_urls), no server paths; tools/list says so.
  (6) toolvault read   -- the proxied meta-tool's schema read names only the
      bound operator (the roster must not leak around tools/list).
  (7) get_task_status  -- bound: only the caller's own tasks.
  (8) volume/info      -- bound: no absolute server paths.

RUN (supported path -- the lean MCP venv, no pytest needed):
    cd MCP && BLACKBOX_ROOT=<repo-root> venv/bin/python test_mcp_remote_scoping.py
Also pytest-discoverable in any venv that has both `mcp` and `pytest`:
    <venv>/bin/python -m pytest MCP/test_mcp_remote_scoping.py -q
"""

import asyncio
import atexit
import base64
import contextlib
import importlib.util as _ilu
import inspect
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_TMP = Path(tempfile.mkdtemp(prefix="bbmcp_scoping_"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

TOKEN_A = "scoping-test-token-alice-" + "a" * 24
TOKEN_B = "scoping-test-token-bob-" + "b" * 24
OP_A = "alice"
OP_B = "bob"

# Import the server with every credential/store path pointed at a temp dir, so
# nothing here can read or write the box's real Manifest/ stores. The module
# binds these at import time; the environment is restored right after.
_ENV = {
    "BLACKBOX_ROOT": str(_REPO_ROOT),
    "BLACKBOX_MCP_TOKENS_FILE": str(_TMP / "no_tokens.json"),
    "BLACKBOX_MCP_OAUTH_TOKENS_FILE": str(_TMP / "no_oauth_tokens.json"),
    "BLACKBOX_MCP_OAUTH_CLIENTS_FILE": str(_TMP / "no_oauth_clients.json"),
    "BLACKBOX_MCP_RUNTIME_FILE": str(_TMP / "no_runtime.json"),
    "BLACKBOX_MCP_PUBLIC_URL": "",
    "BLACKBOX_MCP_LOG_LEVEL": os.environ.get("BLACKBOX_MCP_LOG_LEVEL", "WARNING"),
}
_saved_env = {k: os.environ.get(k) for k in _ENV}
os.environ.update(_ENV)
sys.path.insert(0, str(_HERE))
try:
    _spec = _ilu.spec_from_file_location("bbmcp_remote_scoping", str(_HERE / "blackbox_mcp_server.py"))
    srv = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(srv)
finally:
    for _k, _v in _saved_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

# The startup roster guard would GET /operators on the live backend.
srv._fetch_operators_sync = lambda: None

from mcp import types as mcp_types  # noqa: E402  (after the server import, like the server)

_ACCEPT = "application/json, text/event-stream"
_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "scoping-test", "version": "0"}}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hdrs(token, sid=None):
    h = {"Authorization": f"Bearer {token}", "Accept": _ACCEPT,
         "Content-Type": "application/json"}
    if sid:
        h["mcp-session-id"] = sid
    return h


def _sse_json(resp):
    """First JSON-RPC message of an SSE or JSON response body."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return None
    try:
        return resp.json()
    except Exception:
        return None


async def _with_http_app(fn):
    """Run `fn(client)` against the real HTTP app (SDK session manager live)."""
    real_loader = srv._load_token_map
    srv._load_token_map = lambda: {TOKEN_A: OP_A, TOKEN_B: OP_B}
    try:
        app, sm = srv.build_http_app()
    finally:
        srv._load_token_map = real_loader
    async with sm.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await asyncio.wait_for(fn(c), 30)


async def _open_session(c, token):
    r = await c.post("/mcp", json=_INIT, headers=_hdrs(token))
    assert r.status_code == 200, (r.status_code, r.text[:200])
    sid = r.headers.get("mcp-session-id")
    assert sid, "initialize must issue an mcp-session-id"
    r2 = await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                      headers=_hdrs(token, sid))
    assert r2.status_code == 202, r2.status_code
    return sid


@contextlib.contextmanager
def _bound(operator):
    """Simulate an authenticated HTTP request bound to `operator`.

    Sets exactly what the bearer middleware + SDK set for a real request: the
    SDK request context carrying a request whose state holds the bound operator,
    and the transport flag contextvar.
    """
    from mcp.server.lowlevel.server import request_ctx
    from mcp.shared.context import RequestContext
    req = SimpleNamespace(state=SimpleNamespace(bound_operator=operator, token_id="sha256:test"))
    t_ctx = request_ctx.set(RequestContext(request_id=1, meta=None, session=None,
                                           lifespan_context=None, request=req))
    t_op = srv._BOUND_OPERATOR.set(operator)
    try:
        yield
    finally:
        srv._BOUND_OPERATOR.reset(t_op)
        request_ctx.reset(t_ctx)


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = json.dumps(self._json)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(str(self.status_code), request=None, response=None)


class _FakeClient:
    def __init__(self, router, calls):
        self._router = router
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _route(self, method, url, kw):
        self.calls.append((method, url, kw))
        res = self._router(method, url, kw)
        return (await res) if inspect.isawaitable(res) else res

    async def get(self, url, **kw):
        return await self._route("GET", url, kw)

    async def post(self, url, **kw):
        return await self._route("POST", url, kw)


@contextlib.contextmanager
def _fake_backend(router):
    calls = []
    real = srv.httpx.AsyncClient
    srv.httpx.AsyncClient = lambda *a, **kw: _FakeClient(router, calls)
    try:
        yield calls
    finally:
        srv.httpx.AsyncClient = real


@contextlib.contextmanager
def _roster(names):
    from Orchestrator.toolvault import resolvers
    real = resolvers._list_operators
    resolvers._list_operators = lambda ctx=None: list(names)
    try:
        yield
    finally:
        resolvers._list_operators = real


def _payload(result):
    """(isError, parsed body) for a call_tool result."""
    if isinstance(result, srv.CallToolResult):
        return bool(result.isError), json.loads(result.content[0].text)
    text = result[0].text
    try:
        return False, json.loads(text)
    except ValueError:
        return False, text


# ===========================================================================
# (1) Session binding (CVE-2026-52869 / #2B)
# ===========================================================================
def test_session_id_refused_for_a_different_token():
    """Token B presenting token A's session id is refused before the session
    manager runs -- on tools/list, tools/call and the GET stream alike."""
    async def run(c):
        sid = await _open_session(c, TOKEN_A)
        out = {}
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                         headers=_hdrs(TOKEN_B, sid))
        out["list_b"] = (r.status_code, r.text)
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "get_index_stats", "arguments": {}}},
                         headers=_hdrs(TOKEN_B, sid))
        out["call_b"] = (r.status_code, r.text)
        try:
            # An accepted GET opens A's server-to-client SSE stream and never
            # completes, so a timeout here IS the leak.
            r = await asyncio.wait_for(c.get("/mcp", headers=_hdrs(TOKEN_B, sid)), 5)
            out["get_b"] = (r.status_code, r.text)
        except asyncio.TimeoutError:
            out["get_b"] = (200, "token B attached to token A's SSE stream")
        # The owner keeps working on its own session.
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                         headers=_hdrs(TOKEN_A, sid))
        out["list_a"] = (r.status_code, _sse_json(r))
        return out

    out = asyncio.run(_with_http_app(run))
    for k in ("list_b", "call_b", "get_b"):
        status, text = out[k]
        assert status == 404, f"{k}: token B rode token A's session (status {status}): {text[:200]}"
        assert "Session not found" in text, f"{k}: refusal must look like an unknown session: {text[:200]}"
        assert "tools" not in text and "total_snapshots" not in text, f"{k} leaked a result"
    status, body = out["list_a"]
    assert status == 200 and body and body.get("result", {}).get("tools"), out["list_a"]


def test_unknown_session_id_is_refused():
    async def run(c):
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                         headers=_hdrs(TOKEN_A, uuid.uuid4().hex))
        return r.status_code, r.text

    status, text = asyncio.run(_with_http_app(run))
    assert status == 404 and "Session not found" in text, (status, text[:200])


def test_delete_ends_the_binding():
    async def run(c):
        sid = await _open_session(c, TOKEN_A)
        d = await c.delete("/mcp", headers=_hdrs(TOKEN_A, sid))
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                         headers=_hdrs(TOKEN_A, sid))
        return d.status_code, r.status_code, sid

    deleted, after, sid = asyncio.run(_with_http_app(run))
    assert deleted == 200, deleted
    assert after == 404, f"a deleted session must not answer again (got {after})"
    assert sid not in srv._AUTH_MIDDLEWARE["instance"].session_owners


def _drive_middleware(mw, method, token, sid=None):
    """One request through the bearer middleware; returns (status, headers)."""
    headers = [(b"authorization", f"Bearer {token}".encode())]
    if sid:
        headers.append((b"mcp-session-id", sid.encode()))
    scope = {"type": "http", "method": method, "path": "/mcp", "headers": headers,
             "query_string": b"", "client": ("127.0.0.1", 1)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], {k.lower(): v for k, v in start.get("headers", [])}


def test_binding_is_enforced_by_the_middleware_itself():
    """The guard must not depend on the installed SDK version: a fake inner app
    that would happily serve ANY session id is still never reached by token B."""
    reached = []
    open_status = {"code": 200}

    async def inner(scope, receive, send):
        reached.append(scope)
        sid = dict(scope["headers"]).get(b"mcp-session-id")
        hdrs = [(b"content-type", b"application/json")]
        status = 200
        if sid is None:
            hdrs.append((b"mcp-session-id", uuid.uuid4().hex.encode()))
            status = open_status["code"]
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = srv.BearerAuthMiddleware(inner, {TOKEN_A: OP_A, TOKEN_B: OP_B})
    status, hdrs = _drive_middleware(mw, "POST", TOKEN_A)
    sid = hdrs[b"mcp-session-id"].decode()
    assert status == 200 and len(reached) == 1

    status, _ = _drive_middleware(mw, "POST", TOKEN_B, sid)
    assert status == 404, f"token B reached a session opened by token A (status {status})"
    assert len(reached) == 1, "a refused request must never reach the session manager"

    status, _ = _drive_middleware(mw, "POST", TOKEN_A, sid)
    assert status == 200 and len(reached) == 2

    # A failed opening request (non-2xx) never binds the id it was handed.
    open_status["code"] = 400
    status, hdrs = _drive_middleware(mw, "POST", TOKEN_A)
    assert status == 400
    assert hdrs[b"mcp-session-id"].decode() not in mw.session_owners

    # The SDK's own principal check (mcp>=1.27.2) sees a per-token identity.
    users = [s.get("user") for s in reached]
    assert all(u is not None for u in users), "scope['user'] must be set for the SDK"


# ===========================================================================
# (2) tools/list operator enum (O20-mcp-server-15)
# ===========================================================================
def _operator_enums(tools):
    out = {}
    for t in tools:
        op = ((t.inputSchema or {}).get("properties") or {}).get("operator")
        if isinstance(op, dict) and "enum" in op:
            out[t.name] = op["enum"]
    return out


def test_bound_tools_list_names_only_the_bound_operator():
    with _roster([OP_A, OP_B, "carol"]):
        stdio = _operator_enums(asyncio.run(srv.list_tools()))
        with _bound(OP_A):
            remote = _operator_enums(asyncio.run(srv.list_tools()))
    assert stdio, "fixture: some tools must carry a resolved operator enum"
    assert all(set(e) == {OP_A, OP_B, "carol"} for e in stdio.values()), stdio
    assert set(remote) == set(stdio)
    leaked = {n: e for n, e in remote.items() if e != [OP_A]}
    assert not leaked, f"bound tools/list leaked the operator roster: {leaked}"


# ===========================================================================
# (3) SDK input validation must not pre-empt the handler (O20-mcp-server-16)
# ===========================================================================
def test_operator_blank_reaches_the_handler_on_stdio():
    def router(method, url, kw):
        assert url.endswith("/fossil/hybrid"), url
        return _FakeResponse(200, {"results": [], "operator": kw["params"]["operator"] or "all"})

    handler = srv.server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(
            name="search_snapshots", arguments={"query": "anything", "operator": ""}))
    with _roster([OP_A, OP_B]), _fake_backend(router) as calls:
        result = asyncio.run(handler(req)).root
    text = result.content[0].text
    assert not result.isError, f"operator='' was rejected before the handler ran: {text[:200]}"
    assert calls and calls[0][2]["params"]["operator"] == "", calls


# ===========================================================================
# (4) list_devices (O20-mcp-server-06)
# ===========================================================================
_DEVICES = {"devices": [
    {"id": "alice-phone", "name": "Alice Phone", "tailscale_ip": "100.64.0.11",
     "device_type": "android", "protocol": "adb", "owner": OP_A, "adb_port": 5555,
     "is_primary": True, "metadata": {"tailscale_dns": "alice-phone.example.ts.net"}},
    {"id": "alice-laptop", "name": "Alice Laptop", "tailscale_ip": "100.64.0.12",
     "device_type": "linux", "protocol": "vnc", "owner": OP_A, "vnc_port": 5901,
     "is_primary": True},  # second primary for alice: the registry would "fix" + save
    {"id": "bob-desktop", "name": "Bob Desktop", "tailscale_ip": "100.64.0.21",
     "device_type": "windows", "protocol": "rdp", "owner": OP_B, "rdp_port": 3389,
     "metadata": {"tailscale_dns": "bob-desktop.example.ts.net"}},
]}


@contextlib.contextmanager
def _device_store():
    from Orchestrator.device_registry import registry as reg
    d = Path(tempfile.mkdtemp(prefix="devices_", dir=_TMP))
    store = d / "devices.json"
    store.write_text(json.dumps(_DEVICES))
    saved = (reg.DEVICES_FILE, reg._LEGACY_DEVICES_FILE, reg._registry)
    reg.DEVICES_FILE = store
    reg._LEGACY_DEVICES_FILE = store
    reg._registry = None
    try:
        yield store
    finally:
        reg.DEVICES_FILE, reg._LEGACY_DEVICES_FILE, reg._registry = saved


_ADDRESS_KEYS = {"tailscale_ip", "adb_port", "vnc_port", "rdp_port", "metadata"}


def test_bound_list_devices_shows_only_own_devices_without_addresses():
    with _device_store() as store:
        before = store.read_bytes()
        with _bound(OP_A):
            err, body = _payload(asyncio.run(srv.call_tool("list_devices", {})))
        after = store.read_bytes()
    assert not err, body
    owners = {d["owner"] for d in body["devices"]}
    assert owners == {OP_A}, f"bound list_devices leaked other owners: {owners}"
    assert {d["id"] for d in body["devices"]} == {"alice-phone", "alice-laptop"}
    for d in body["devices"]:
        leaked = _ADDRESS_KEYS & set(d)
        assert not leaked, f"remote device row carries tailnet addresses: {sorted(leaked)}"
        assert "100.64." not in json.dumps(d) and ".ts.net" not in json.dumps(d)
    assert before == after, "the MCP process must never rewrite the backend's devices.json"


def test_bound_list_devices_type_filter_stays_scoped():
    with _device_store():
        with _bound(OP_A):
            err, body = _payload(asyncio.run(srv.call_tool("list_devices", {"device_type": "windows"})))
    assert not err and body["devices"] == [], body


def test_stdio_list_devices_is_unchanged():
    with _device_store():
        err, body = _payload(asyncio.run(srv.call_tool("list_devices", {})))
    assert not err, body
    assert {d["owner"] for d in body["devices"]} == {OP_A, OP_B}
    assert all("tailscale_ip" in d for d in body["devices"])


# ===========================================================================
# (5) get_media (O20-mcp-server-03/-07)
# ===========================================================================
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

_TASKS = {
    "T-ALICE": {"task_id": "T-ALICE", "task_type": "image", "status": "completed",
                "result_url": "http://127.0.0.1:9091/ui/uploads/alice.png", "result_data": {},
                "operator": OP_A, "created_at": "2026-10-01T00:00:00Z"},
    "T-BOB": {"task_id": "T-BOB", "task_type": "image", "status": "completed",
              "result_url": "/ui/uploads/bob.png", "result_data": {},
              "operator": OP_B, "created_at": "2026-10-01T00:00:00Z"},
    "T-NOOWNER": {"task_id": "T-NOOWNER", "task_type": "image", "status": "completed",
                  "result_url": "/ui/uploads/alice.png", "result_data": {}},
}


def _task_router(method, url, kw):
    tid = url.rsplit("/", 1)[-1]
    if method == "GET" and "/tasks/" in url and tid in _TASKS:
        return _FakeResponse(200, _TASKS[tid])
    return _FakeResponse(404, {"detail": "Task not found"})


@contextlib.contextmanager
def _portal_root():
    root = Path(tempfile.mkdtemp(prefix="root_", dir=_TMP))
    (root / "Portal" / "uploads").mkdir(parents=True)
    (root / "Portal" / "uploads" / "alice.png").write_bytes(_PNG)
    (root / "Portal" / "uploads" / "bob.png").write_bytes(_PNG)
    saved = srv.BLACKBOX_ROOT
    srv.BLACKBOX_ROOT = root
    try:
        yield root
    finally:
        srv.BLACKBOX_ROOT = saved


def _get_media(args, operator=None):
    with _portal_root() as root, _fake_backend(_task_router) as calls:
        if operator is None:
            res = asyncio.run(srv.call_tool("get_media", args))
        else:
            with _bound(operator):
                res = asyncio.run(srv.call_tool("get_media", args))
    err, body = _payload(res)
    return err, body, calls, root


def test_bound_get_media_requires_task_id():
    err, body, calls, _ = _get_media({"url": "/ui/uploads/bob.png"}, operator=OP_A)
    assert err and body["error"]["code"] == "invalid_arguments", body
    assert "base64" not in body


def test_bound_get_media_refuses_another_operators_task():
    err, body, _, _ = _get_media({"task_id": "T-BOB"}, operator=OP_A)
    assert err and body["error"]["code"] == "not_found", body


def test_bound_get_media_refuses_task_without_owner():
    err, body, _, _ = _get_media({"task_id": "T-NOOWNER"}, operator=OP_A)
    assert err and body["error"]["code"] == "not_found", body


def test_bound_get_media_url_must_be_the_tasks_own_media():
    err, body, _, _ = _get_media({"task_id": "T-ALICE", "url": "/ui/uploads/bob.png"}, operator=OP_A)
    assert err and body["error"]["code"] == "not_found", body
    assert "base64" not in body


def test_bound_get_media_serves_own_task_without_server_paths():
    err, body, calls, root = _get_media({"task_id": "T-ALICE"}, operator=OP_A)
    assert not err, body
    assert body["url"] == "/ui/uploads/alice.png"
    assert base64.b64decode(body["base64"]) == _PNG
    assert body["metadata"]["operator"] == OP_A
    assert "file_path" not in body and str(root) not in json.dumps(body), body
    task_gets = [c for c in calls if c[0] == "GET" and "/tasks/" in c[1]]
    assert len(task_gets) == 1, f"one /tasks lookup per call, got {len(task_gets)}"


def test_stdio_get_media_still_refuses_traversal():
    for url in ("/ui/../../etc/passwd", "/ui/uploads/../../config.ini"):
        err, body, _, _ = _get_media({"url": url})
        assert err and body["error"]["code"] == "invalid_arguments", (url, body)


def test_stdio_get_media_by_url_still_works():
    err, body, calls, root = _get_media({"url": "/ui/uploads/bob.png"})
    assert not err, body
    assert body["file_path"] == str((root / "Portal" / "uploads" / "bob.png").resolve())
    assert calls == []


# A numberOfImages=2 generation (tasks.py stores every output in result_data and
# its artifact) and a Lyria-style task whose extra outputs live in the artifact.
_TASKS["T-ALICE-MULTI"] = {
    "task_id": "T-ALICE-MULTI", "task_type": "image", "status": "completed",
    "result_url": "/ui/uploads/alice.png",
    "result_data": {"all_urls": ["/ui/uploads/alice.png", "/ui/uploads/alice2.png"],
                    "artifact": {"type": "image", "url": "/ui/uploads/alice.png",
                                 "all_urls": ["/ui/uploads/alice.png", "/ui/uploads/alice2.png"]}},
    "operator": OP_A, "created_at": "2026-10-01T00:00:00Z"}
_TASKS["T-ALICE-ART"] = {
    "task_id": "T-ALICE-ART", "task_type": "music", "status": "completed",
    "result_url": "/ui/uploads/alice.png",
    "result_data": {"artifact": {"type": "music", "all_urls": ["/ui/uploads/alice.png",
                                                              "/ui/uploads/alice3.png"]}},
    "operator": OP_A, "created_at": "2026-10-01T00:00:00Z"}


def _get_media_multi(args, operator):
    with _portal_root() as root, _fake_backend(_task_router) as calls:
        (root / "Portal" / "uploads" / "alice2.png").write_bytes(_PNG)
        (root / "Portal" / "uploads" / "alice3.png").write_bytes(_PNG)
        with _bound(operator):
            res = asyncio.run(srv.call_tool("get_media", args))
    err, body = _payload(res)
    return err, body, calls


def test_bound_get_media_serves_every_output_of_an_own_multi_output_task():
    for task_id, url in (("T-ALICE-MULTI", "/ui/uploads/alice2.png"),
                         ("T-ALICE-MULTI", "https://box.example.ts.net/ui/uploads/alice2.png"),
                         ("T-ALICE-ART", "/ui/uploads/alice3.png")):
        err, body, _ = _get_media_multi({"task_id": task_id, "url": url}, OP_A)
        assert not err, f"owner refused its own output {url} of {task_id}: {body}"
        assert body["url"] == "/ui/uploads/" + url.rsplit("/", 1)[-1]
        assert base64.b64decode(body["base64"]) == _PNG
    # No url still means the primary output.
    err, body, _ = _get_media_multi({"task_id": "T-ALICE-MULTI"}, OP_A)
    assert not err and body["url"] == "/ui/uploads/alice.png", body


def test_bound_get_media_multi_output_task_still_refuses_foreign_urls():
    for url in ("/ui/uploads/bob.png", "/ui/uploads/alice3.png"):
        err, body, _ = _get_media_multi({"task_id": "T-ALICE-MULTI", "url": url}, OP_A)
        assert err and body["error"]["code"] == "not_found", (url, body)
    # Another operator cannot reach alice's extra outputs either.
    err, body, _ = _get_media_multi({"task_id": "T-ALICE-MULTI", "url": "/ui/uploads/alice2.png"}, OP_B)
    assert err and body["error"]["code"] == "not_found", body


def test_bound_get_media_refuses_a_malformed_task_id_without_a_lookup():
    for task_id in ("../agent/apps", "T-ALICE/../T-BOB", "a b", "x" * 200):
        err, body, calls, _ = _get_media({"task_id": task_id}, operator=OP_A)
        assert err and body["error"]["code"] == "invalid_arguments", (task_id, body)
        assert calls == [], f"a malformed task_id must never steer a backend request: {calls}"


def test_bound_tools_list_tells_remote_callers_get_media_needs_task_id():
    stdio = {t.name: t for t in asyncio.run(srv.list_tools())}["get_media"]
    with _bound(OP_A):
        remote = {t.name: t for t in asyncio.run(srv.list_tools())}["get_media"]
    assert "task_id" in (remote.inputSchema.get("required") or []), remote.inputSchema
    assert "task_id" in remote.description and remote.description != stdio.description
    assert "task_id" not in (stdio.inputSchema.get("required") or []), "stdio schema must be unchanged"
    after = {t.name: t for t in asyncio.run(srv.list_tools())}["get_media"]
    assert after.description == stdio.description and after.inputSchema == stdio.inputSchema, \
        "the bound edit must not leak into a later stdio listing"


# ===========================================================================
# (6) toolvault read through the proxy (O20-mcp-server-15)
# ===========================================================================
async def _local_tools_execute(method, url, kw):
    """Exactly what POST /local/tools/execute runs (Orchestrator/routes/local_routes.py)."""
    from Orchestrator.tools.blackbox_tools import execute_tool
    assert method == "POST" and url.endswith("/local/tools/execute"), url
    body = kw["json"]
    operator = body.get("operator") or "system"
    params = dict(body.get("params") or {})
    params["operator"] = operator
    caller = (kw.get("headers") or {}).get("X-BlackBox-Caller")
    r = await execute_tool(body["tool"], params, operator, caller=caller or "local")
    return _FakeResponse(200, {"success": bool(r.success), "result": r.result})


def test_bound_toolvault_read_names_only_the_bound_operator():
    roster = [OP_A, OP_B, "carol-secret"]
    with _roster(roster), _bound(OP_A), _fake_backend(_local_tools_execute) as calls:
        res = asyncio.run(srv.call_tool("toolvault", {"action": "read", "tool_name": "search_snapshots"}))
    err, body = _payload(res)
    text = body if isinstance(body, str) else json.dumps(body)
    assert not err and "search_snapshots" in text, text[:300]
    assert calls and calls[0][2]["json"]["operator"] == OP_A
    leaked = [n for n in (OP_B, "carol-secret") if n in text]
    assert not leaked, f"toolvault read leaked the roster to a bound caller: {leaked}"
    assert f"enum: ['{OP_A}']" in text, text[:600]


# ===========================================================================
# (7) get_task_status (O20-mcp-server-03 follow-up: /tasks/{id} now names its owner)
# ===========================================================================
def _status_router(method, url, kw):
    if method == "POST" and url.endswith("/local/tools/execute"):
        tid = kw["json"]["params"]["task_id"]
        return _FakeResponse(200, {"success": True,
                                   "result": "Task completed!\n\n" + json.dumps(_TASKS[tid])})
    return _task_router(method, url, kw)


def _get_task_status(args, operator=None):
    with _fake_backend(_status_router) as calls:
        if operator is None:
            res = asyncio.run(srv.call_tool("get_task_status", args))
        else:
            with _bound(operator):
                res = asyncio.run(srv.call_tool("get_task_status", args))
    err, body = _payload(res)
    return err, body, calls


def test_bound_get_task_status_serves_own_task():
    err, body, calls = _get_task_status({"task_id": "T-ALICE"}, operator=OP_A)
    assert not err and "alice.png" in str(body), body
    assert [c[0] for c in calls] == ["GET", "POST"], calls


def test_bound_get_task_status_refuses_other_and_ownerless_tasks():
    for task_id in ("T-BOB", "T-NOOWNER", "T-MISSING"):
        err, body, calls = _get_task_status({"task_id": task_id}, operator=OP_A)
        assert err and body["error"]["code"] == "not_found", (task_id, body)
        assert OP_B not in json.dumps(body) and "bob.png" not in json.dumps(body), body
        assert not [c for c in calls if c[0] == "POST"], f"{task_id}: refused call was proxied: {calls}"


def test_bound_get_task_status_refuses_a_malformed_task_id_without_a_lookup():
    for args in ({"task_id": "../agent/apps"}, {}):
        err, body, calls = _get_task_status(args, operator=OP_A)
        assert err and body["error"]["code"] == "invalid_arguments", (args, body)
        assert calls == [], calls


def test_stdio_get_task_status_is_unchanged():
    err, body, calls = _get_task_status({"task_id": "T-BOB"})
    assert not err and "bob.png" in str(body), body
    assert not [c for c in calls if "/tasks/" in c[1]], f"stdio must not add an ownership lookup: {calls}"
    assert [c for c in calls if c[0] == "POST"], calls


# ===========================================================================
# (8) blackbox://volume/info (O20-mcp-server-07 sibling)
# ===========================================================================
def test_bound_volume_info_hides_server_paths():
    with _bound(OP_A):
        bound = json.loads(asyncio.run(srv.read_resource("blackbox://volume/info")).text)
    stdio = json.loads(asyncio.run(srv.read_resource("blackbox://volume/info")).text)
    assert "volume_path" not in bound and "index_path" not in bound, bound
    assert str(srv.BLACKBOX_ROOT) not in json.dumps(bound), bound
    assert "exists" in bound and "index_exists" in bound, bound
    assert stdio["volume_path"] == str(srv.VOLUME_FILE) and stdio["index_path"] == str(srv.SNAPSHOT_INDEX)


# ---------------------------------------------------------------------------
# Standalone runner (no pytest needed -- runs in the lean MCP venv).
# ---------------------------------------------------------------------------
def _run_standalone():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001 -- report every failure, keep going
            import traceback
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed} passed, {failed} failed, {len(tests)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
