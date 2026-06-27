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
    # alice/bob are synthetic test operators absent from the real box roster, so
    # disable the startup roster-validation guard for these auth-invariant tests
    # (the guard itself is covered by test_mcp_operator_scoping.py).
    env["BLACKBOX_MCP_ROSTER_ENFORCE"] = "0"
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
# RAW JSON-RPC helpers (for #2A direct-tool gating + #2B session-hijack). The
# MCP ClientSession won't let us reuse a session-id under a DIFFERENT token, so
# we drive the wire directly (mirrors the reviewer's probe_direct/probe_session).
# ---------------------------------------------------------------------------
_ACCEPT = "application/json, text/event-stream"


def _sse_or_json(resp: httpx.Response):
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except Exception:
                    pass
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _raw_init(client: httpx.Client, base_url: str, token: str) -> str:
    """Open a session under `token`; return the mcp-session-id."""
    r = client.post(base_url, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "p", "version": "0"}}},
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json", "Accept": _ACCEPT})
    sid = r.headers.get("mcp-session-id")
    client.post(base_url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                         "Accept": _ACCEPT, "mcp-session-id": sid})
    return sid


def _raw_call(client: httpx.Client, base_url: str, token: str, sid: str, name: str, args: dict):
    """tools/call on `sid` under `token`. Returns (isError, parsed_body_or_text)."""
    r = client.post(base_url, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": args}},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": _ACCEPT, "mcp-session-id": sid})
    obj = _sse_or_json(r)
    if obj and "result" in obj:
        cont = obj["result"].get("content", [])
        err = bool(obj["result"].get("isError"))
        if cont:
            try:
                return err, json.loads(cont[0]["text"])
            except Exception:
                return err, cont[0].get("text")
    return None, obj


def _run_2a_2b(base_url: str) -> list:
    """Run #2A (direct-tool gating) + #2B (session hijack) against the seeded
    two-operator server. Returns a list of failure strings ([] = all pass).
    Seeded volume content: 'ALICEDATA!' (alice's snap) + 'BOBDATA...' (bob's)."""
    fails = []
    c = httpx.Client(timeout=15)

    # --- #2A: bob, correctly bound, tries to read ALICE's data across tools ---
    sid_b = _raw_init(c, base_url, OTHER_TOKEN)  # bob
    # get_snapshot for alice's snap -> not_found (NOT the content)
    err, body = _raw_call(c, base_url, OTHER_TOKEN, sid_b, "get_snapshot", {"snap_id": "SNAP-ALICE-1"})
    txt = json.dumps(body)
    if err is True and "not_found" in txt and "ALICEDATA" not in txt and "content" not in body:
        print("[auth] PASS (g) #2A get_snapshot(alice) by bob -> not_found, no content")
    else:
        fails.append(f"(g) get_snapshot cross-op NOT denied: err={err} body={txt[:200]}")
    # seek_snapshot_direct for alice's snap -> not_found
    err, body = _raw_call(c, base_url, OTHER_TOKEN, sid_b, "seek_snapshot_direct", {"snap_id": "SNAP-ALICE-1"})
    txt = json.dumps(body)
    if err is True and "not_found" in txt and "ALICEDATA" not in txt:
        print("[auth] PASS (h) #2A seek_snapshot_direct(alice) by bob -> not_found, no content")
    else:
        fails.append(f"(h) seek_snapshot_direct cross-op NOT denied: err={err} body={txt[:200]}")
    # bob CAN read his OWN snap (positive control)
    err, body = _raw_call(c, base_url, OTHER_TOKEN, sid_b, "get_snapshot", {"snap_id": "SNAP-BOB-1"})
    if err is not True and isinstance(body, dict) and body.get("metadata", {}).get("operator") == OTHER_OPERATOR:
        print("[auth] PASS (i) #2A bob CAN read his own snap (gate is per-operator, not deny-all)")
    else:
        fails.append(f"(i) bob denied his OWN snap (over-blocking): err={err} body={json.dumps(body)[:200]}")
    # list_operators -> only bob; get_index_stats -> only bob + no FS paths
    err, body = _raw_call(c, base_url, OTHER_TOKEN, sid_b, "list_operators", {})
    names = {o["name"] for o in body.get("operators", [])} if isinstance(body, dict) else set()
    if names == {OTHER_OPERATOR}:
        print("[auth] PASS (j) #2A list_operators by bob -> only bob (roster not leaked)")
    else:
        fails.append(f"(j) list_operators leaked roster: {names}")
    # get_current_operator -> roster scoped to ONLY bob (mirrors the list_operators
    # fix; a bound HTTP caller must never see the full who-else-is-on-this-box
    # roster). resolved=bob, operators==['bob'], count==1, alice absent.
    err, body = _raw_call(c, base_url, OTHER_TOKEN, sid_b, "get_current_operator", {})
    if isinstance(body, dict):
        ops = body.get("operators")
        if (ops == [OTHER_OPERATOR]
                and body.get("count") == 1
                and body.get("resolved") == OTHER_OPERATOR
                and VALID_OPERATOR not in (ops or [])):
            print("[auth] PASS (o) #2A get_current_operator by bob -> roster=['bob'] "
                  "count=1 (roster not leaked)")
        else:
            fails.append(f"(o) get_current_operator leaked roster: operators={ops!r} "
                         f"count={body.get('count')!r} resolved={body.get('resolved')!r}")
    else:
        fails.append(f"(o) get_current_operator unexpected body: {json.dumps(body)[:200]}")
    err, body = _raw_call(c, base_url, OTHER_TOKEN, sid_b, "get_index_stats", {})
    ops = set((body.get("operators") or {}).keys()) if isinstance(body, dict) else set()
    leaks_paths = isinstance(body, dict) and ("index_file" in body or "volume_file" in body)
    if ops == {OTHER_OPERATOR} and not leaks_paths:
        print("[auth] PASS (k) #2A get_index_stats by bob -> only bob, no FS paths")
    else:
        fails.append(f"(k) get_index_stats leaked: ops={ops} fs_paths={leaks_paths}")
    # get_media with a `..` traversal url -> rejected
    err, body = _raw_call(c, base_url, OTHER_TOKEN, sid_b, "get_media", {"url": "/ui/../../etc/passwd"})
    txt = json.dumps(body)
    if err is True and ("Illegal path" in txt or "escapes" in txt or "invalid_arguments" in txt):
        print("[auth] PASS (l) #2A get_media `..` traversal -> rejected")
    else:
        fails.append(f"(l) get_media traversal NOT rejected: err={err} body={txt[:200]}")

    # --- #2B: token-A request riding token-B's session must run as A, not B ---
    sid_bob = _raw_init(c, base_url, OTHER_TOKEN)  # bob opens a session
    # alice's token reuses bob's session-id
    err, body = _raw_call(c, base_url, VALID_TOKEN, sid_bob, "get_current_operator", {})
    resolved = body.get("resolved") if isinstance(body, dict) else None
    if resolved == VALID_OPERATOR:
        print("[auth] PASS (m) #2B tokenA on tokenB's session -> runs as alice (NOT bob; hijack closed)")
    elif resolved == OTHER_OPERATOR:
        fails.append("(m) #2B SESSION HIJACK: tokenA on bob's session executed as bob (STALE BINDING)")
    else:
        fails.append(f"(m) #2B unexpected resolved={resolved!r}")
    # and the data it sees is alice's, never bob's
    err, body = _raw_call(c, base_url, VALID_TOKEN, sid_bob, "browse_index", {})
    seen = {s["operator"] for s in body.get("snapshots", [])} if isinstance(body, dict) else set()
    if seen == {VALID_OPERATOR}:
        print("[auth] PASS (n) #2B tokenA on bob's session browse_index -> only alice's data")
    elif seen == {OTHER_OPERATOR}:
        fails.append("(n) #2B CROSS-OP READ: tokenA on bob's session read BOB's snapshots")
    else:
        fails.append(f"(n) #2B browse leaked/odd: operators={seen}")
    c.close()
    return fails


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
    # Real, distinguishable content so a cross-operator read is observable:
    # bytes [0:10)='ALICEDATA!' (alice), [10:30)='BOBDATAxxxxxxxxxxxx' (bob).
    (tmp / "Volumes" / "SNAPSHOT_VOLUME.txt").write_text("ALICEDATA!BOBDATAxxxxxxxxxxxxxx")

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
            # #2A + #2B regression suite against the SAME seeded two-operator
            # server (alice+bob snapshots, real volume content).
            if backend_ok:
                failures.extend(_run_2a_2b(base_url2))
            else:
                failures.append("(g-n) SKIPPED: backend down (get_current_operator needs it)")
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

    print("\n[auth] ALL 15 ASSERTIONS PASSED "
          "(401 no-header, 401 wrong-token, valid-token ok, operator-binding, "
          "read-scoping, stdio-unchanged; #2A get_snapshot/seek/own-read/"
          "list_operators/get_current_operator/index_stats/media-traversal gated; "
          "#2B session-hijack closed for get_current_operator + browse_index).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
