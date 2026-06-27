#!/usr/bin/env python3
"""
Tests for the BlackBox MCP server's M1 hardening (uniform dispatcher, /chat/save
mint, structured error envelope, routing, full tool catalog).

WHY A STANDALONE RUNNER (the only clean option here):
    The server runs in the LEAN MCP/venv (mcp, httpx, requests, bs4 -- NO
    pytest), and that is the ONLY venv on this box with the `mcp` SDK importing
    the server requires (Orchestrator/venv has pytest but NO `mcp`, so it cannot
    import the module under test). Adding pytest to the lean venv would bloat the
    isolation the venv exists to preserve. So this file is a self-contained
    runner that executes under `MCP/venv/bin/python` using the stdlib only. It is
    ALSO written to be pytest-discoverable, so it runs under `python -m pytest`
    in any venv that happens to have BOTH `mcp` and `pytest`. Either way the
    backend HTTP layer is mocked, so no live BlackBox is required.

RUN (the supported path -- MCP/venv, no pytest needed):
    cd MCP && BLACKBOX_ROOT=<repo-root> venv/bin/python test_blackbox_mcp_server.py
"""

import asyncio
import importlib.util as _ilu
import json
import os
import sys
from pathlib import Path

# Import the server module by path (mirrors how Claude Code launches it). The
# module manipulates sys.path on import so Orchestrator.* resolves; we only need
# BLACKBOX_ROOT to point at the repo root (default = parent of MCP/).
_HERE = Path(__file__).resolve().parent
os.environ.setdefault("BLACKBOX_ROOT", str(_HERE.parent))
# Run from MCP/ so the same-dir `from operator_resolution import ...` works.
os.chdir(_HERE)
sys.path.insert(0, str(_HERE))

_spec = _ilu.spec_from_file_location("bbmcp_under_test", str(_HERE / "blackbox_mcp_server.py"))
srv = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(srv)


# ---------------------------------------------------------------------------
# Test doubles: a fake httpx.AsyncClient that records calls and returns canned
# JSON, so call_tool() exercises the real dispatch logic without a live backend.
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=None
            )


class _FakeClient:
    """Records POST/GET calls; returns responses from a routing function."""

    def __init__(self, router):
        self._router = router
        self.calls = []  # list of (method, url, kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._router("POST", url, kwargs)

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._router("GET", url, kwargs)


def _install_fake_client(router):
    """Patch srv.httpx.AsyncClient to yield a _FakeClient; return the recorder."""
    captured = {}

    def _factory(*a, **kw):
        c = _FakeClient(router)
        captured["client"] = c
        return c

    srv.httpx.AsyncClient = _factory
    return captured


_REAL_ASYNC_CLIENT = srv.httpx.AsyncClient


def _restore_client():
    srv.httpx.AsyncClient = _REAL_ASYNC_CLIENT


def _stub_operator(name="alice"):
    """Make resolve_operator deterministic without hitting GET /operators."""
    async def _fake_resolve(provided):
        return provided or name
    srv.resolve_operator = _fake_resolve
    # _proxy_tool calls the module-level resolve_operator, so patching the module
    # attribute is enough.


def _text(result):
    """Extract the first TextContent .text from a success list result."""
    return result[0].text


# ===========================================================================
# TESTS
# ===========================================================================

def test_list_tools_returns_full_catalog():
    tools = asyncio.run(srv.list_tools())
    assert len(tools) == 74, f"expected 74 tools, got {len(tools)}"
    names = {t.name for t in tools}
    # Spot-check representatives of each class.
    for n in ("search_snapshots", "mint_snapshot", "gmail_send",
              "perplexity_web_search", "gemini_image", "roll_dice"):
        assert n in names, f"{n} missing from catalog"


def test_valid_tool_names_memoized_and_complete():
    s1 = srv._valid_tool_names()
    s2 = srv._valid_tool_names()
    assert s1 is s2, "valid-tool-name set should be memoized (same object)"
    assert len(s1) == 74


def test_generic_tool_routes_to_local_tools_execute():
    """A registry tool with no dedicated branch proxies to /local/tools/execute."""
    _stub_operator("alice")

    def router(method, url, kwargs):
        return _FakeResponse(200, {"success": True, "result": "DICE=4"})

    cap = _install_fake_client(router)
    try:
        res = asyncio.run(srv.call_tool("roll_dice", {"sides": 6}))
    finally:
        _restore_client()
    client = cap["client"]
    assert len(client.calls) == 1
    method, url, kwargs = client.calls[0]
    assert method == "POST"
    assert url.endswith("/local/tools/execute"), url
    body = kwargs["json"]
    assert body["tool"] == "roll_dice"
    assert body["operator"] == "alice"
    assert body["params"] == {"sides": 6}  # operator stripped from params
    assert _text(res) == "DICE=4"


def test_web_search_routes_to_local_tools_execute():
    """Web-search tools fold into the uniform /local/tools/execute path."""
    _stub_operator("alice")

    def router(method, url, kwargs):
        return _FakeResponse(200, {"success": True, "result": "search results"})

    cap = _install_fake_client(router)
    try:
        res = asyncio.run(srv.call_tool("perplexity_web_search", {"query": "x"}))
    finally:
        _restore_client()
    method, url, kwargs = cap["client"].calls[0]
    assert url.endswith("/local/tools/execute"), url
    assert kwargs["json"]["tool"] == "perplexity_web_search"
    assert _text(res) == "search results"


def test_gmail_tool_routes_to_gmail_execute():
    """gmail_* tools route to the dedicated /gmail/execute whitelist."""
    _stub_operator("alice")

    def router(method, url, kwargs):
        return _FakeResponse(200, {"success": True, "result": "sent"})

    cap = _install_fake_client(router)
    try:
        res = asyncio.run(srv.call_tool("gmail_send", {"to": "a@b.c", "operator": "bob"}))
    finally:
        _restore_client()
    method, url, kwargs = cap["client"].calls[0]
    assert url.endswith("/gmail/execute"), url
    assert kwargs["json"]["tool"] == "gmail_send"
    assert kwargs["json"]["operator"] == "bob"  # explicit operator honored
    assert _text(res) == "sent"


def test_local_tool_does_not_proxy():
    """list_operators is a LOCAL index read -- it must NOT hit the backend."""
    # Point the index loader at an in-memory fake.
    srv._index_cache = {
        "SNAP-1": {"operator": "alice", "timestamp": "2026-01-01", "type": "normal",
                   "byte_start": 0, "byte_end": 10},
        "SNAP-2": {"operator": "bob", "timestamp": "2026-01-02", "type": "normal",
                   "byte_start": 10, "byte_end": 30},
    }
    srv._index_cache_mtime = 1e18  # never reload from disk

    def router(method, url, kwargs):
        raise AssertionError("local tool must not proxy to backend")

    _install_fake_client(router)
    try:
        res = asyncio.run(srv.call_tool("list_operators", {}))
    finally:
        _restore_client()
        srv._index_cache = None
        srv._index_cache_mtime = 0
    payload = json.loads(_text(res))
    ops = {o["name"]: o["snapshot_count"] for o in payload["operators"]}
    assert ops == {"alice": 1, "bob": 1}


def test_mint_snapshot_posts_to_chat_save_and_returns_snap_id():
    """mint_snapshot must POST /chat/save (not /chat) and return the real snap_id."""
    _stub_operator("alice")

    def router(method, url, kwargs):
        assert url.endswith("/chat/save"), f"mint must use /chat/save, got {url}"
        body = kwargs["json"]
        assert body["assistant_response"] == "remember this"
        assert "messages" not in body  # NOT the /chat shape
        return _FakeResponse(200, {"success": True, "minted": True,
                                   "snap_id": "SNAP-20260626-9999"})

    cap = _install_fake_client(router)
    try:
        res = asyncio.run(srv.call_tool("mint_snapshot", {"content": "remember this"}))
    finally:
        _restore_client()
    calls = cap["client"].calls
    assert len(calls) == 1 and calls[0][1].endswith("/chat/save")
    out = json.loads(_text(res))
    assert out["snap_id"] == "SNAP-20260626-9999"
    assert out["minted"] is True


def test_error_envelope_on_backend_500():
    """A backend 5xx yields a structured isError result with code=backend_error."""
    _stub_operator("alice")

    def router(method, url, kwargs):
        return _FakeResponse(500, {}, text="boom")

    _install_fake_client(router)
    try:
        res = asyncio.run(srv.call_tool("roll_dice", {"sides": 6}))
    finally:
        _restore_client()
    assert isinstance(res, srv.CallToolResult)
    assert res.isError is True
    body = json.loads(res.content[0].text)
    assert body["error"]["code"] == "backend_error"
    assert body["error"]["tool"] == "roll_dice"


def test_error_envelope_on_tool_failure():
    """An executor-level {success:False} yields code=tool_error."""
    _stub_operator("alice")

    def router(method, url, kwargs):
        return _FakeResponse(200, {"success": False, "error": "bad params"})

    _install_fake_client(router)
    try:
        res = asyncio.run(srv.call_tool("roll_dice", {"sides": 0}))
    finally:
        _restore_client()
    assert res.isError is True
    body = json.loads(res.content[0].text)
    assert body["error"]["code"] == "tool_error"
    assert "bad params" in body["error"]["message"]


def test_error_envelope_on_timeout():
    """A backend that never responds yields code=timeout (not a wedge)."""
    _stub_operator("alice")

    async def _hang(url, **kwargs):
        await asyncio.sleep(10)
        return _FakeResponse(200, {"success": True, "result": "late"})

    # Force a tiny timeout so the test is fast.
    old_timeout = srv.PROXY_TIMEOUT
    srv.PROXY_TIMEOUT = 0.05

    def router(method, url, kwargs):  # unused; we override post directly
        return _FakeResponse(200, {})

    cap = _install_fake_client(router)
    cap_client_holder = {}

    # Patch the factory's client.post to hang.
    real_factory = srv.httpx.AsyncClient

    def _factory(*a, **kw):
        c = _FakeClient(router)
        c.post = _hang
        cap_client_holder["c"] = c
        return c

    srv.httpx.AsyncClient = _factory
    try:
        res = asyncio.run(srv.call_tool("roll_dice", {"sides": 6}))
    finally:
        _restore_client()
        srv.PROXY_TIMEOUT = old_timeout
    assert res.isError is True
    body = json.loads(res.content[0].text)
    assert body["error"]["code"] == "timeout"


def test_unknown_tool_envelope():
    """An unknown tool name yields code=unknown_tool."""
    _stub_operator("alice")
    _install_fake_client(lambda *a: _FakeResponse(200, {}))
    try:
        res = asyncio.run(srv.call_tool("does_not_exist", {}))
    finally:
        _restore_client()
    assert res.isError is True
    body = json.loads(res.content[0].text)
    assert body["error"]["code"] == "unknown_tool"


# ---------------------------------------------------------------------------
# Standalone runner (no pytest needed -- runs in the lean MCP venv).
# ---------------------------------------------------------------------------
def _run_standalone():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
