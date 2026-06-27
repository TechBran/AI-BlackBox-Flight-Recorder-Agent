#!/usr/bin/env python3
"""
M2 smoke test: prove the BlackBox MCP server's Streamable HTTP transport works
end-to-end by connecting to it as a REAL MCP CLIENT over Streamable HTTP.

This is the M2 milestone gate. It:
  1. Starts the HTTP server as a SUBPROCESS on a free test port (127.0.0.1).
  2. Connects with the SDK's streamablehttp_client + ClientSession.
  3. Asserts:
       (a) list_tools() returns the full 74-tool catalog,
       (b) a LOCAL read tool (get_index_stats) returns a valid result WITHOUT
           touching the backend,
       (c) a PROXIED tool (get_current_operator) round-trips through the backend
           at :9091 and returns a valid result.
  This proves list + local + proxied all work over Streamable HTTP.

WHY A SUBPROCESS (and not in-process):
    uvicorn owns its own event loop; running the server in-process and a client
    in the same loop is fragile. A subprocess is exactly how a real deployment
    runs it, so it is the faithful smoke. The server's stdout/stderr is captured;
    on failure it is dumped to aid debugging.

REQUIRES: the BlackBox backend running at :9091 (the proxied-tool assertion does
a real backend round-trip). list + local tools work without it; the proxied
assertion will report a clear skip-style failure if the backend is down.

RUN (lean MCP/venv -- no pytest needed):
    cd MCP && BLACKBOX_ROOT=<repo-root> venv/bin/python test_mcp_http_transport.py
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
os.environ.setdefault("BLACKBOX_ROOT", str(_REPO_ROOT))

EXPECTED_TOOL_COUNT = 74
BACKEND_URL = os.getenv("BLACKBOX_URL", "http://localhost:9091")

# M3: the HTTP transport now REQUIRES bearer auth. This smoke runs the server with
# a TEST token map (env BLACKBOX_MCP_TOKENS, never the real store) and connects
# with the matching bearer header, so it exercises the SAME list/local/proxied
# path it always did -- now through the auth gate. (Auth-rejection is covered
# exhaustively by test_mcp_auth.py.)
SMOKE_TOKEN = "smoke-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SMOKE_OPERATOR = "system"
SMOKE_TOKEN_MAP = {SMOKE_TOKEN: SMOKE_OPERATOR}


def _free_port() -> int:
    """Grab an ephemeral free port on localhost, then release it for the server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_http(url: str, timeout: float = 25.0) -> bool:
    """Poll the /mcp endpoint until the server answers (any HTTP status = up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # A bare GET without a session id will be rejected by the MCP
            # transport, but ANY HTTP response means the listener is up.
            r = httpx.get(url, timeout=2.0)
            return True
        except Exception:
            # Some MCP GETs require headers; a 4xx still means "up". Catch only
            # connection errors here.
            try:
                httpx.post(url, timeout=2.0, json={})
                return True
            except httpx.HTTPError:
                time.sleep(0.25)
        time.sleep(0.25)
    return False


def _backend_up() -> bool:
    try:
        r = httpx.get(f"{BACKEND_URL}/operators", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


async def _drive_client(base_url: str) -> dict:
    """Connect as a real MCP client and exercise list + local + proxied tools."""
    results = {}
    headers = {"Authorization": f"Bearer {SMOKE_TOKEN}"}  # M3: auth now required
    async with streamablehttp_client(base_url, headers=headers) as (read, write, _get_sid):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # (a) list tools -- full catalog over HTTP
            listed = await session.list_tools()
            names = {t.name for t in listed.tools}
            results["tool_count"] = len(listed.tools)
            results["tool_names"] = names

            # (b) LOCAL read tool: get_index_stats (no backend hop)
            local = await session.call_tool("get_index_stats", {})
            results["local_isError"] = bool(getattr(local, "isError", False))
            local_text = local.content[0].text if local.content else ""
            results["local_text"] = local_text

            # (c) PROXIED tool: get_current_operator (real backend round-trip)
            proxied = await session.call_tool("get_current_operator", {})
            results["proxied_isError"] = bool(getattr(proxied, "isError", False))
            results["proxied_text"] = proxied.content[0].text if proxied.content else ""
    return results


def main() -> int:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}/mcp"

    env = dict(os.environ)
    env["BLACKBOX_ROOT"] = str(_REPO_ROOT)
    env["BLACKBOX_MCP_HTTP_PORT"] = str(port)
    env["BLACKBOX_MCP_HTTP_HOST"] = "127.0.0.1"
    # M3: inject a TEST token map (env source only -- never the real store) so the
    # auth gate accepts SMOKE_TOKEN. Point the file source at a non-existent path.
    env["BLACKBOX_MCP_TOKENS"] = json.dumps(SMOKE_TOKEN_MAP)
    env["BLACKBOX_MCP_TOKENS_FILE"] = str(_HERE / "__no_such_token_file__.json")
    # Quiet the server logs a touch; stderr is still captured for failures.
    env.setdefault("BLACKBOX_MCP_LOG_LEVEL", "WARNING")

    server_py = str(_HERE / "blackbox_mcp_server.py")
    print(f"[smoke] starting HTTP server: {sys.executable} {server_py} --transport http  (port {port})")
    proc = subprocess.Popen(
        [sys.executable, server_py, "--transport", "http"],
        cwd=str(_HERE),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    failures = []
    try:
        if not _wait_for_http(base_url):
            out = ""
            try:
                proc.terminate()
                out = proc.communicate(timeout=5)[0]
            except Exception:
                pass
            print("[smoke] FAIL: server did not come up in time")
            print(out)
            return 1

        backend_ok = _backend_up()
        if not backend_ok:
            print(f"[smoke] WARNING: backend {BACKEND_URL} appears DOWN -- "
                  "the proxied-tool assertion (c) will fail.")

        res = asyncio.run(_drive_client(base_url))

        # --- Assertion (a): full catalog ---
        if res["tool_count"] == EXPECTED_TOOL_COUNT:
            print(f"[smoke] PASS (a) list_tools over HTTP -> {res['tool_count']} tools")
        else:
            failures.append(f"(a) expected {EXPECTED_TOOL_COUNT} tools, got {res['tool_count']}")
        for must in ("get_index_stats", "get_current_operator", "search_snapshots"):
            if must not in res["tool_names"]:
                failures.append(f"(a) tool {must} missing from HTTP catalog")

        # --- Assertion (b): local read tool ---
        if res["local_isError"]:
            failures.append(f"(b) get_index_stats returned isError: {res['local_text'][:200]}")
        else:
            try:
                stats = json.loads(res["local_text"])
                if "total_snapshots" in stats and "operators" in stats:
                    print(f"[smoke] PASS (b) local get_index_stats over HTTP -> "
                          f"{stats['total_snapshots']} snapshots")
                else:
                    failures.append(f"(b) get_index_stats missing expected keys: {res['local_text'][:200]}")
            except Exception as e:
                failures.append(f"(b) get_index_stats result not valid JSON ({e}): {res['local_text'][:200]}")

        # --- Assertion (c): proxied tool (real backend round-trip) ---
        if res["proxied_isError"]:
            failures.append(f"(c) get_current_operator returned isError: {res['proxied_text'][:200]}")
        else:
            try:
                op = json.loads(res["proxied_text"])
                if "resolved" in op and "operators" in op:
                    print(f"[smoke] PASS (c) proxied get_current_operator over HTTP -> "
                          f"resolved={op.get('resolved')!r}, {op.get('count')} operator(s)")
                else:
                    failures.append(f"(c) get_current_operator missing expected keys: {res['proxied_text'][:200]}")
            except Exception as e:
                failures.append(f"(c) get_current_operator result not valid JSON ({e}): {res['proxied_text'][:200]}")

    finally:
        proc.terminate()
        try:
            tail = proc.communicate(timeout=5)[0]
        except Exception:
            proc.kill()
            tail = ""
        if failures:
            print("\n[smoke] ---- server output (tail) ----")
            print("\n".join((tail or "").splitlines()[-30:]))

    if failures:
        print("\n[smoke] FAILURES:")
        for f in failures:
            print(f"  - {f}")
        print(f"\n[smoke] {len(failures)} assertion(s) failed.")
        return 1

    print("\n[smoke] ALL 3 ASSERTIONS PASSED (list + local + proxied over Streamable HTTP).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
