#!/usr/bin/env python3
"""
M3 security gate: prove the BlackBox MCP server's HTTP transport enforces bearer
auth, binds the operator to the token (anti-spoof + anti-leak), and leaves the
stdio path untouched.

This connects to a REAL server SUBPROCESS over real HTTP (same faithful pattern
as the M2 smoke). It exercises:

  (a) no Authorization header                -> 401 + WWW-Authenticate: Bearer
  (b) wrong/unknown token                    -> 401 + WWW-Authenticate: Bearer
  (c) valid token                            -> a tool call succeeds over HTTP
  (d) operator binding (anti-spoof): a call whose tool-args assert
      operator="SomeoneElse" still runs as the token's BOUND operator
      (asserted via get_current_operator, which echoes the resolved operator)
  (e) read-tool scoping (anti-leak): a read tool over HTTP (browse_index) is
      scoped to the BOUND operator, never operator='' (spans-all). Proven by
      seeding the index with two operators and asserting only the bound
      operator's snapshots come back.
  (f) stdio path still works WITHOUT auth (unchanged) -- list + a local read
      tool over the stdio transport, no token anywhere.

The HTTP server is launched with a TEST token map via BLACKBOX_MCP_TOKENS (env
JSON), so the test never reads or writes the real Manifest/mcp_tokens.json and
no real token is involved.

REQUIRES: the BlackBox backend at :9091 for (c)/(d) (get_current_operator does a
real /operators round-trip). (a),(b),(e),(f) do not need the backend; (e) uses an
injected in-memory index, (f) is fully local.

RUN (lean MCP/venv -- no pytest needed):
    cd MCP && BLACKBOX_ROOT=<repo-root> venv/bin/python test_mcp_auth.py
"""

import asyncio
import importlib.util as _ilu
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
from mcp.client.stdio import stdio_client, StdioServerParameters

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
os.environ.setdefault("BLACKBOX_ROOT", str(_REPO_ROOT))

# A TEST token map -- never the real store. Two tokens bound to two operators so
# we can prove per-token operator binding + read scoping.
VALID_TOKEN = "test-token-brandon-aaaaaaaaaaaaaaaaaaaaaaaa"
VALID_OPERATOR = "alice"
OTHER_TOKEN = "test-token-other-bbbbbbbbbbbbbbbbbbbbbbbb"
OTHER_OPERATOR = "bob"
WRONG_TOKEN = "totally-wrong-token-zzzzzzzzzzzzzzzzzzzzzzzz"
TEST_TOKEN_MAP = {VALID_TOKEN: VALID_OPERATOR, OTHER_TOKEN: OTHER_OPERATOR}

BACKEND_URL = os.getenv("BLACKBOX_URL", "http://localhost:9091")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_http(url: str, timeout: float = 25.0) -> bool:
    """Poll until the listener answers (ANY HTTP status = up, incl. 401)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=2.0)
            return True
        except httpx.HTTPError:
            try:
                httpx.post(url, timeout=2.0, json={})
                return True
            except httpx.HTTPError:
                time.sleep(0.25)
        time.sleep(0.25)
    return False


def _backend_up() -> bool:
    try:
        return httpx.get(f"{BACKEND_URL}/operators", timeout=3.0).status_code == 200
    except Exception:
        return False


def _start_http_server(port: int, extra_env: dict) -> subprocess.Popen:
    env = dict(os.environ)
    env["BLACKBOX_ROOT"] = str(_REPO_ROOT)
    env["BLACKBOX_MCP_HTTP_PORT"] = str(port)
    env["BLACKBOX_MCP_HTTP_HOST"] = "127.0.0.1"
    env["BLACKBOX_MCP_TOKENS"] = json.dumps(TEST_TOKEN_MAP)
    # Point the file source at a path that does NOT exist so the real store is
    # never read by the test (env is the only source).
    env["BLACKBOX_MCP_TOKENS_FILE"] = str(_HERE / "__no_such_token_file__.json")
    env.setdefault("BLACKBOX_MCP_LOG_LEVEL", "WARNING")
    env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, str(_HERE / "blackbox_mcp_server.py"), "--transport", "http"],
        cwd=str(_HERE), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


# ---------------------------------------------------------------------------
# (a) + (b): raw HTTP -- a missing or wrong bearer must 401 BEFORE any MCP work.
# We POST a minimal JSON-RPC initialize body; the auth gate must reject first.
# ---------------------------------------------------------------------------
def _raw_post(base_url: str, headers: dict) -> httpx.Response:
    init_body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26",
                   "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    h.update(headers)
    return httpx.post(base_url, json=init_body, headers=h, timeout=10.0)


# ---------------------------------------------------------------------------
# (c) + (d): a real MCP client WITH the valid bearer header.
# ---------------------------------------------------------------------------
async def _drive_authed_client(base_url: str) -> dict:
    out = {}
    headers = {"Authorization": f"Bearer {VALID_TOKEN}"}
    async with streamablehttp_client(base_url, headers=headers) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # (c) a tool call succeeds over HTTP with a valid token.
            stats = await session.call_tool("get_index_stats", {})
            out["c_isError"] = bool(getattr(stats, "isError", False))
            out["c_text"] = stats.content[0].text if stats.content else ""
            # (d) operator binding: assert a DIFFERENT operator in the args; the
            # resolved operator must still be the token's bound operator.
            cur = await session.call_tool(
                "get_current_operator", {"operator": "SomeoneElse"})
            out["d_isError"] = bool(getattr(cur, "isError", False))
            out["d_text"] = cur.content[0].text if cur.content else ""
    return out


# ---------------------------------------------------------------------------
# (e): read scoping. browse_index over HTTP must return ONLY the bound operator's
# snapshots even with operator unset (no operator='' spans-all remotely).
# ---------------------------------------------------------------------------
async def _drive_browse(base_url: str) -> dict:
    out = {}
    headers = {"Authorization": f"Bearer {VALID_TOKEN}"}
    async with streamablehttp_client(base_url, headers=headers) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("browse_index", {})  # operator unset
            out["e_isError"] = bool(getattr(res, "isError", False))
            out["e_text"] = res.content[0].text if res.content else ""
    return out


# ---------------------------------------------------------------------------
# (f): stdio path -- no auth, unchanged.
# ---------------------------------------------------------------------------
async def _drive_stdio() -> dict:
    out = {}
    env = dict(os.environ)
    env["BLACKBOX_ROOT"] = str(_REPO_ROOT)
    # Deliberately set a token map in env: it must be IGNORED on stdio (no auth).
    env["BLACKBOX_MCP_TOKENS"] = json.dumps(TEST_TOKEN_MAP)
    env.setdefault("BLACKBOX_MCP_LOG_LEVEL", "WARNING")
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(_HERE / "blackbox_mcp_server.py")],  # no flags -> stdio default
        env=env, cwd=str(_HERE),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            out["f_tool_count"] = len(listed.tools)
            stats = await session.call_tool("get_index_stats", {})
            out["f_isError"] = bool(getattr(stats, "isError", False))
            out["f_text"] = stats.content[0].text if stats.content else ""
    return out


def main() -> int:
    failures = []
    backend_ok = _backend_up()
    if not backend_ok:
        print(f"[auth] WARNING: backend {BACKEND_URL} appears DOWN -- "
              "(c)/(d) need a real /operators round-trip and will fail.")

    # ----- HTTP server #1: default index (for a,b,c,d) -----
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}/mcp"
    proc = _start_http_server(port, extra_env={})
    try:
        if not _wait_for_http(base_url):
            try:
                proc.terminate(); out = proc.communicate(timeout=5)[0]
            except Exception:
                out = ""
            print("[auth] FAIL: HTTP server did not come up")
            print(out)
            return 1

        # (a) no Authorization header -> 401
        r = _raw_post(base_url, headers={})
        if r.status_code == 401 and "bearer" in r.headers.get("www-authenticate", "").lower():
            print("[auth] PASS (a) no Authorization header -> 401 + WWW-Authenticate: Bearer")
        else:
            failures.append(f"(a) expected 401+WWW-Authenticate:Bearer, got "
                            f"{r.status_code} / {r.headers.get('www-authenticate')!r}")

        # (b) wrong token -> 401
        r = _raw_post(base_url, headers={"Authorization": f"Bearer {WRONG_TOKEN}"})
        if r.status_code == 401 and "bearer" in r.headers.get("www-authenticate", "").lower():
            print("[auth] PASS (b) wrong token -> 401 + WWW-Authenticate: Bearer")
        else:
            failures.append(f"(b) expected 401 for wrong token, got {r.status_code}")

        # also: a malformed (non-Bearer) scheme is rejected
        r = _raw_post(base_url, headers={"Authorization": "Basic abc"})
        if r.status_code != 401:
            failures.append(f"(b') malformed non-Bearer scheme should 401, got {r.status_code}")

        # (c) + (d) need the backend (get_current_operator round-trip)
        if backend_ok:
            res = asyncio.run(_drive_authed_client(base_url))
            if res["c_isError"]:
                failures.append(f"(c) valid-token tool call returned isError: {res['c_text'][:200]}")
            else:
                try:
                    stats = json.loads(res["c_text"])
                    if "total_snapshots" in stats:
                        print(f"[auth] PASS (c) valid token -> tool call ok "
                              f"({stats['total_snapshots']} snapshots)")
                    else:
                        failures.append(f"(c) unexpected stats body: {res['c_text'][:200]}")
                except Exception as e:
                    failures.append(f"(c) stats not JSON ({e}): {res['c_text'][:200]}")

            if res["d_isError"]:
                failures.append(f"(d) get_current_operator returned isError: {res['d_text'][:200]}")
            else:
                try:
                    op = json.loads(res["d_text"])
                    if op.get("resolved") == VALID_OPERATOR and op.get("needs_selection") is False:
                        print(f"[auth] PASS (d) operator binding -> resolved={op['resolved']!r} "
                              f"(ignored caller-asserted 'SomeoneElse')")
                    else:
                        failures.append(f"(d) expected resolved={VALID_OPERATOR!r} bound, "
                                        f"got resolved={op.get('resolved')!r} "
                                        f"needs_selection={op.get('needs_selection')!r}")
                except Exception as e:
                    failures.append(f"(d) op body not JSON ({e}): {res['d_text'][:200]}")
        else:
            failures.append("(c)/(d) SKIPPED: backend down (cannot verify auth-success path)")
    finally:
        proc.terminate()
        try:
            tail1 = proc.communicate(timeout=5)[0]
        except Exception:
            proc.kill(); tail1 = ""

    # ----- HTTP server #2: seeded two-operator index (for e) -----
    # We pre-seed an index JSON and point BLACKBOX_ROOT-resolved paths via env.
    # Give the server a tiny TWO-operator index it will actually load, by pointing
    # BLACKBOX_ROOT at a temp-dir OVERLAY: a real Manifest/Volumes we seed, plus a
    # symlink to the real Orchestrator/ so the server's import-time
    # `from web_tools import ...` + `Orchestrator.toolvault` resolve. cwd stays the
    # real MCP/ (set in _start_http_server), so `operator_resolution` still imports.
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="bbmcp_e_"))
    (tmp / "Manifest").mkdir()
    (tmp / "Volumes").mkdir()
    # Symlink the bits the server imports at startup (sys.path uses BLACKBOX_ROOT
    # and BLACKBOX_ROOT/Orchestrator). MCP/ is reached via cwd, not BLACKBOX_ROOT.
    (tmp / "Orchestrator").symlink_to(_REPO_ROOT / "Orchestrator")
    seed_index = {
        "SNAP-ALICE-1": {"operator": VALID_OPERATOR, "timestamp": "2026-01-02",
                         "type": "normal", "byte_start": 0, "byte_end": 10},
        "SNAP-BOB-1": {"operator": OTHER_OPERATOR, "timestamp": "2026-01-03",
                       "type": "normal", "byte_start": 10, "byte_end": 30},
    }
    (tmp / "Manifest" / "snapshot_index.json").write_text(json.dumps(seed_index))
    (tmp / "Volumes" / "SNAPSHOT_VOLUME.txt").write_text("x" * 30)

    port2 = _free_port()
    base_url2 = f"http://127.0.0.1:{port2}/mcp"
    env_e = {"BLACKBOX_ROOT": str(tmp)}
    proc2 = _start_http_server(port2, extra_env=env_e)
    try:
        if not _wait_for_http(base_url2):
            try:
                proc2.terminate(); out = proc2.communicate(timeout=5)[0]
            except Exception:
                out = ""
            failures.append("(e) seeded HTTP server did not come up")
            print(out)
        else:
            res = asyncio.run(_drive_browse(base_url2))
            if res["e_isError"]:
                failures.append(f"(e) browse_index returned isError: {res['e_text'][:200]}")
            else:
                try:
                    body = json.loads(res["e_text"])
                    ops = {s["operator"] for s in body.get("snapshots", [])}
                    if ops == {VALID_OPERATOR}:
                        print(f"[auth] PASS (e) read scoping -> browse_index returned ONLY "
                              f"operator={VALID_OPERATOR!r} ({body['returned']} snap), NOT bob")
                    else:
                        failures.append(f"(e) read leaked across operators: returned operators={ops} "
                                        f"(expected only {{{VALID_OPERATOR!r}}})")
                except Exception as e:
                    failures.append(f"(e) browse body not JSON ({e}): {res['e_text'][:200]}")
    finally:
        proc2.terminate()
        try:
            tail2 = proc2.communicate(timeout=5)[0]
        except Exception:
            proc2.kill(); tail2 = ""

    # ----- (f) stdio path, no auth -----
    try:
        res = asyncio.run(_drive_stdio())
        if res["f_tool_count"] == 74 and not res["f_isError"]:
            try:
                stats = json.loads(res["f_text"])
                if "total_snapshots" in stats:
                    print(f"[auth] PASS (f) stdio path WITHOUT auth -> list({res['f_tool_count']}) "
                          f"+ local read ok (token map ignored on stdio)")
                else:
                    failures.append(f"(f) stdio stats missing keys: {res['f_text'][:200]}")
            except Exception as e:
                failures.append(f"(f) stdio stats not JSON ({e}): {res['f_text'][:200]}")
        else:
            failures.append(f"(f) stdio expected 74 tools + no error, got "
                            f"count={res['f_tool_count']} isError={res['f_isError']}")
    except Exception as e:
        failures.append(f"(f) stdio path raised: {e}")

    if failures:
        print("\n[auth] ---- HTTP server #1 output (tail) ----")
        print("\n".join((tail1 or "").splitlines()[-20:]))
        print("\n[auth] FAILURES:")
        for f in failures:
            print(f"  - {f}")
        print(f"\n[auth] {len(failures)} assertion(s) failed.")
        return 1

    print("\n[auth] ALL 6 ASSERTIONS PASSED "
          "(401 no-header, 401 wrong-token, valid-token ok, operator-binding, "
          "read-scoping, stdio-unchanged).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
