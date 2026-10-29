#!/usr/bin/env python3
"""The MCP server never blocks its event loop on the index or the roster
(audit rank 46: O18-toolvault-modules-03, with its sibling O20-mcp-server-10).

One process serves every MCP session and the OAuth endpoints on one event
loop. The local index tools and resources re-parse the 16 MB snapshot index
(json.load) whenever it changed, i.e. after every mint, and /internal/reload
and the OAuth consent POST fetch the operator roster with a blocking
httpx.get (10 s timeout). All of that ran inline in async handlers; now it
runs in worker threads.

The runtime tests measure the longest gap between turns of a 20 ms ticker: a
call blocking the loop for 0.4 s shows up as a 0.4 s gap. A static check
covers every async function in the server. Hermetic: tmp files, stubs, no
backend.
"""
import ast
import asyncio
import importlib.util as _ilu
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent
os.environ.setdefault("BLACKBOX_ROOT", str(_HERE.parent))
sys.path.insert(0, str(_HERE))

_spec = _ilu.spec_from_file_location("bbmcp_off_loop_under_test", str(_HERE / "blackbox_mcp_server.py"))
srv = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(srv)

BLOCK = 0.4

INDEX = {
    "SNAP-1": {"operator": "alice", "timestamp": "2026-10-01T00:00:00Z",
               "type": "normal", "byte_start": 0, "byte_end": 5},
    "SNAP-2": {"operator": "bob", "timestamp": "2026-10-02T00:00:00Z",
               "type": "checkpoint", "byte_start": 5, "byte_end": 10},
}


async def _max_gap_during(coro):
    """Await ``coro`` while a 20 ms ticker runs; return (result, longest gap)."""
    gaps = []
    stop = asyncio.Event()
    ticking = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now
            ticking.set()

    t = asyncio.create_task(ticker())
    await ticking.wait()  # a call that blocks at once must land between ticks
    try:
        result = await coro
    finally:
        stop.set()
        await t
    return result, max(gaps or [0.0])


@pytest.fixture
def slow_index(tmp_path, monkeypatch):
    """A tiny index and volume whose index load blocks like a 16 MB parse."""
    volume = tmp_path / "SNAPSHOT_VOLUME.txt"
    volume.write_bytes(b"hellobyeee")
    monkeypatch.setattr(srv, "VOLUME_FILE", volume)

    def slow_load(force_refresh=False):
        time.sleep(BLOCK)
        return INDEX

    monkeypatch.setattr(srv, "load_snapshot_index", slow_load)

    async def resolved(provided):
        return provided or "alice"

    monkeypatch.setattr(srv, "resolve_operator", resolved)


@pytest.mark.parametrize("tool,args,expect", [
    ("seek_snapshot_direct", {"snap_id": "SNAP-1"}, "hello"),
    ("get_snapshot", {"snap_id": "SNAP-2"}, "byeee"),
    ("list_recent_snapshots", {"operator": "alice"}, "SNAP-1"),
    ("get_index_stats", {}, "total_snapshots"),
    ("browse_index", {}, "SNAP-2"),
    ("list_operators", {}, "alice"),
    ("refresh_index", {}, "2 snapshots"),
])
def test_index_tools_run_off_the_loop(slow_index, tool, args, expect):
    result, gap = asyncio.run(_max_gap_during(srv.call_tool(tool, args)))
    assert expect in result[0].text, result
    assert gap < 0.2, f"{tool} blocked the MCP event loop for {gap:.2f}s"


@pytest.mark.parametrize("uri,expect", [
    ("blackbox://index/stats", "total_snapshots"),
    ("blackbox://index/operators", "bob"),
    ("blackbox://index/recent", "SNAP-2"),
])
def test_index_resources_run_off_the_loop(slow_index, uri, expect):
    result, gap = asyncio.run(_max_gap_during(srv.read_resource(uri)))
    assert expect in result[0].content, result
    assert gap < 0.2, f"{uri} blocked the MCP event loop for {gap:.2f}s"


def test_internal_reload_fetches_the_roster_off_the_loop(tmp_path, monkeypatch):
    from starlette.datastructures import Headers
    from Orchestrator.utils import internal_secret

    secret_file = tmp_path / "credentials" / "mcp_reload.secret"
    secret = internal_secret.ensure_secret(secret_file)
    monkeypatch.setattr(srv, "_reload_secret_path", lambda: secret_file)
    monkeypatch.setattr(srv, "_load_runtime_public_url", lambda: "")
    monkeypatch.setattr(srv, "_load_token_map", lambda: {"tok-a": "alice", "tok-g": "ghost"})

    def slow_roster():
        time.sleep(BLOCK)
        return ["alice"]

    monkeypatch.setattr(srv, "_fetch_operators_sync", slow_roster)
    monkeypatch.setenv("BLACKBOX_MCP_ROSTER_ENFORCE", "1")
    middleware = SimpleNamespace(token_map={})
    monkeypatch.setitem(srv._AUTH_MIDDLEWARE, "instance", middleware)
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                              headers=Headers({internal_secret.SECRET_HEADER: secret}))
    response, gap = asyncio.run(_max_gap_during(srv._internal_reload_handler(request)))
    assert response.status_code == 200
    assert middleware.token_map == {"tok-a": "alice"}
    assert gap < 0.2, f"/internal/reload blocked the MCP event loop for {gap:.2f}s"


# Sync helpers that parse the index, read the volume or fetch the roster.
_BLOCKING = {"load_snapshot_index", "seek_snapshot_by_offset", "get_snapshot_metadata",
             "list_snapshots_by_operator", "_validate_token_operators",
             "_fetch_operators_sync", "_load_validated_token_map"}


def test_no_async_handler_calls_an_index_or_roster_helper_inline():
    tree = ast.parse((_HERE / "blackbox_mcp_server.py").read_text(encoding="utf-8"))
    hits = []

    def walk(node, in_async):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, True)
            elif isinstance(child, (ast.FunctionDef, ast.Lambda)):
                walk(child, False)
            else:
                if in_async and isinstance(child, ast.Call) \
                        and isinstance(child.func, ast.Name) and child.func.id in _BLOCKING:
                    hits.append(f"line {child.lineno}: {ast.unparse(child)[:80]}")
                walk(child, in_async)

    walk(tree, False)
    assert not hits, "blocking helpers called inline in async MCP code:\n" + "\n".join(hits)
