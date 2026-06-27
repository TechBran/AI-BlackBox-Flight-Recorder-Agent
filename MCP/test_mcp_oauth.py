#!/usr/bin/env python3
"""
CHUNK 6a gate: prove the BlackBox MCP server's HTTP transport exposes the PUBLIC
OAuth bootstrap surface (discovery metadata + dynamic client registration) and
that the bearer-auth middleware EXEMPTS it -- WITHOUT opening the /mcp gate.

This connects to a REAL server SUBPROCESS over real HTTP (same faithful pattern
as test_mcp_auth.py). It exercises:

  (a) GET /.well-known/oauth-authorization-server  -> 200 + valid RFC 8414
      metadata (issuer + authorization/token/registration endpoints correct,
      S256 advertised, public-client auth method "none"), NO bearer.
  (a) GET /.well-known/oauth-protected-resource    -> 200 + valid RFC 9728
      metadata (resource = the MCP URL, authorization_servers = [issuer]),
      NO bearer.
  (b) POST /register {redirect_uris:[...]}          -> 201 + a client_id and
      the echoed redirect_uris, NO bearer.
  (c) the .well-known endpoints + /register all work WITHOUT any Authorization
      header (the exemption is real).
  (d) POST /mcp WITHOUT a token is STILL 401 (the exemption did NOT open the
      /mcp gate).

Plus negative DCR checks: POST /register with NO redirect_uris -> 400; and the
registered client is persisted to the GITIGNORED 0600 store.

The HTTP server is launched with a TEST token map via BLACKBOX_MCP_TOKENS so the
test never touches the real Manifest/mcp_tokens.json. The OAuth client store is
pointed at a TEMP file via BLACKBOX_MCP_OAUTH_CLIENTS_FILE so the test never
writes the real Manifest/mcp_oauth_clients.json. A custom BLACKBOX_MCP_PUBLIC_URL
is injected so the metadata assertions are deterministic.

NO backend (:9091) required -- every assertion here is pure HTTP against the MCP
server's own routes.

RUN (lean MCP/venv -- no pytest needed):
    MCP/venv/bin/python MCP/test_mcp_oauth.py
"""

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
os.environ.setdefault("BLACKBOX_ROOT", str(_REPO_ROOT))

# A TEST token map -- never the real store (only used to prove /mcp still 401s).
VALID_TOKEN = "test-token-oauth-aaaaaaaaaaaaaaaaaaaaaaaa"
VALID_OPERATOR = "alice"
TEST_TOKEN_MAP = {VALID_TOKEN: VALID_OPERATOR}

# A deterministic public origin so metadata assertions don't depend on the box's
# real Funnel hostname.
TEST_PUBLIC_URL = "https://oauth-test.example.com:8443"
EXPECTED_MCP_PATH = "/mcp"
EXPECTED_RESOURCE = TEST_PUBLIC_URL + EXPECTED_MCP_PATH


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


# CONSENT FLOW: /authorize no longer auto-approves. The operator must submit a
# VALID BlackBox token at the consent form, and the issued code/access token
# binds to THAT token's operator -- NOT a fixed env operator. So the default
# server's OAuth bindings resolve to the token map's operator (VALID_OPERATOR =
# "alice"), which the (6b-store)/(k) assertions key on. (The legacy env knob
# BLACKBOX_MCP_OAUTH_OPERATOR no longer drives the binding on the consent path.)
TEST_OAUTH_OPERATOR = VALID_OPERATOR


def _start_http_server(port: int, clients_file: Path,
                       tokens_file: Path = None) -> subprocess.Popen:
    env = dict(os.environ)
    env["BLACKBOX_ROOT"] = str(_REPO_ROOT)
    env["BLACKBOX_MCP_HTTP_PORT"] = str(port)
    env["BLACKBOX_MCP_HTTP_HOST"] = "127.0.0.1"
    env["BLACKBOX_MCP_TOKENS"] = json.dumps(TEST_TOKEN_MAP)
    # Never read the real token store.
    env["BLACKBOX_MCP_TOKENS_FILE"] = str(_HERE / "__no_such_token_file__.json")
    # Never write the real OAuth client store; deterministic public URL.
    env["BLACKBOX_MCP_OAUTH_CLIENTS_FILE"] = str(clients_file)
    env["BLACKBOX_MCP_PUBLIC_URL"] = TEST_PUBLIC_URL
    # CHUNK 6b: pin the OAuth operator + point the access-token store at a TEMP
    # file so the test never writes the real Manifest/mcp_oauth_tokens.json.
    env["BLACKBOX_MCP_OAUTH_OPERATOR"] = TEST_OAUTH_OPERATOR
    if tokens_file is not None:
        env["BLACKBOX_MCP_OAUTH_TOKENS_FILE"] = str(tokens_file)
    env.setdefault("BLACKBOX_MCP_LOG_LEVEL", "WARNING")
    return subprocess.Popen(
        [sys.executable, str(_HERE / "blackbox_mcp_server.py"), "--transport", "http"],
        cwd=str(_HERE), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def _raw_mcp_init_post(base_url: str, headers: dict) -> httpx.Response:
    """POST a minimal JSON-RPC initialize to /mcp; used to probe the auth gate."""
    init_body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26",
                   "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    h.update(headers)
    return httpx.post(base_url, json=init_body, headers=h, timeout=10.0)


# =============================================================================
# CHUNK 6c: validate OAuth access tokens on /mcp through the SAME M3 operator-
# isolation path as a static bearer. The tests below (k-n) drive the full OAuth
# flow to mint a real access token, then use it AS the /mcp bearer and assert:
#   (k) end-to-end: initialize + list_tools -> 74 tools (gate opens for OAuth).
#   (l) operator-isolation: an OAuth token bound to operator X cannot read
#       ANOTHER operator's snap (get_snapshot/seek_snapshot_direct -> not_found)
#       and list_operators/get_current_operator return ONLY X (mirrors M3 #2A,
#       but via an OAuth token -- proving OAuth is NOT a bypass).
#   (m) an EXPIRED or bogus OAuth access token on /mcp -> 401.
#   (n) the static-bearer path STILL works on /mcp (regression).
# =============================================================================
_ACCEPT = "application/json, text/event-stream"
BACKEND_URL = os.getenv("BLACKBOX_URL", "http://localhost:9091")

# For the isolation test (l): a seeded two-operator index where the OAuth
# operator equals one of the seeded operators ("bob"), so a bob-bound OAuth
# token is denied alice's data and sees only bob in the roster.
ISO_OAUTH_OPERATOR = "bob"     # the OAuth token binds to this operator
ISO_OTHER_OPERATOR = "alice"   # the OTHER operator whose data must stay hidden
# A BlackBox token bound to "bob" -- the operator submits THIS at the consent
# form, so the issued OAuth code/token binds to bob (the AUTHENTICATED operator).
ISO_BOB_TOKEN = "test-token-iso-bob-bbbbbbbbbbbbbbbbbbbbbbbb"
ISO_TOKEN_MAP = {ISO_BOB_TOKEN: ISO_OAUTH_OPERATOR}


def _backend_up() -> bool:
    try:
        return httpx.get(f"{BACKEND_URL}/operators", timeout=3.0).status_code == 200
    except Exception:
        return False


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
    """Open an MCP session on /mcp under `token`; return the mcp-session-id."""
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


def _raw_list_tools(client: httpx.Client, base_url: str, token: str, sid: str):
    """tools/list on `sid` under `token`. Returns the list of tool dicts (or [])."""
    r = client.post(base_url, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": _ACCEPT, "mcp-session-id": sid})
    obj = _sse_or_json(r)
    if obj and "result" in obj:
        return obj["result"].get("tools", [])
    return []


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


def _authorize_get_post(origin: str, params: dict, token: str,
                        follow_redirects: bool = False):
    """Drive the two-step consent /authorize flow with a given token.

    Step 1: GET /authorize (the OAuth params) -> the consent HTML form (no code).
    Step 2: POST /authorize (the same OAuth params + blackbox_token) -> a 302
            with ?code on a VALID token, or a 401 form re-render on an invalid
            one. Returns the POST httpx.Response (callers inspect status + code).

    `params` is the OAuth query dict (response_type/client_id/redirect_uri/state/
    scope/code_challenge/code_challenge_method). The GET is issued first (so the
    test still exercises the form-render path), then the POST submits the same
    params as form fields plus the operator's `blackbox_token`.
    """
    httpx.get(f"{origin}/authorize", params=params,
              follow_redirects=False, timeout=10.0)
    data = dict(params)
    data["blackbox_token"] = token
    return httpx.post(f"{origin}/authorize", data=data,
                      follow_redirects=follow_redirects, timeout=10.0)


def _mint_access_token(origin: str, registered_id: str, redirect_uri: str,
                       token: str):
    """Run the full OAuth flow (GET form -> POST consent -> code -> token) and
    return the minted access_token (or None on any failure). The code/token bind
    to the AUTHENTICATED `token`'s operator. Reused by tests (k) + (l)."""
    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    r = _authorize_get_post(origin, {
        "response_type": "code", "client_id": registered_id,
        "redirect_uri": redirect_uri, "state": "iso-state", "scope": "mcp",
        "code_challenge": challenge, "code_challenge_method": "S256",
    }, token)
    if r.status_code != 302:
        return None
    code = parse_qs(urlparse(r.headers.get("location", "")).query).get("code", [None])[0]
    if not code:
        return None
    tr = httpx.post(f"{origin}/token", data={
        "grant_type": "authorization_code", "code": code,
        "client_id": registered_id, "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }, timeout=10.0)
    if tr.status_code != 200:
        return None
    return tr.json().get("access_token")


def _register_client(origin: str, redirect_uri: str):
    """DCR a public client on `origin`; return its client_id (or None)."""
    r = httpx.post(f"{origin}/register", json={
        "redirect_uris": [redirect_uri],
        "client_name": "Iso Test Client",
        "token_endpoint_auth_method": "none",
    }, timeout=10.0)
    if r.status_code in (200, 201):
        return r.json().get("client_id")
    return None


def _run_oauth_isolation(failures: list) -> None:
    """Test (l): stand up a SEEDED two-operator server whose OAuth operator is
    `bob`, mint a bob-bound OAuth access token, and prove it is subject to the
    SAME M3 #2A isolation as a static bearer -- cannot read alice's data, CAN
    read its own, and the roster is scoped to bob only. This proves an OAuth
    token is routed to request.state.bound_operator on the IDENTICAL path and
    is NEVER a bypass of per-operator scoping. (The init+74-tools e2e is asserted
    separately as (k) on the default-index server.)

    Needs the backend (get_current_operator/list_operators do a real /operators
    round-trip); skipped with a recorded failure if it is down (mirrors
    test_mcp_auth.py's (g-n))."""
    if not _backend_up():
        failures.append("(l) SKIPPED: backend down (get_current_operator/list_operators "
                        "need a real /operators round-trip)")
        return

    # A temp BLACKBOX_ROOT overlay with a real two-operator index + volume (same
    # technique as test_mcp_auth.py's (e)/(g-n) seeded server).
    tmp = Path(tempfile.mkdtemp(prefix="bbmcp_oauth_iso_"))
    (tmp / "Manifest").mkdir()
    (tmp / "Volumes").mkdir()
    (tmp / "Orchestrator").symlink_to(_REPO_ROOT / "Orchestrator")
    seed_index = {
        "SNAP-ALICE-1": {"operator": ISO_OTHER_OPERATOR, "timestamp": "2026-01-02",
                         "type": "normal", "byte_start": 0, "byte_end": 10},
        "SNAP-BOB-1": {"operator": ISO_OAUTH_OPERATOR, "timestamp": "2026-01-03",
                       "type": "normal", "byte_start": 10, "byte_end": 30},
    }
    (tmp / "Manifest" / "snapshot_index.json").write_text(json.dumps(seed_index))
    # bytes [0:10)='ALICEDATA!' (alice), [10:30)='BOBDATAxxxxxxxxxxxx' (bob).
    (tmp / "Volumes" / "SNAPSHOT_VOLUME.txt").write_text("ALICEDATA!BOBDATAxxxxxxxxxxxxxx")

    iso_tmpdir = Path(tempfile.mkdtemp(prefix="bbmcp_oauth_iso_store_"))
    iso_clients = iso_tmpdir / "mcp_oauth_clients.json"
    iso_tokens = iso_tmpdir / "mcp_oauth_tokens.json"

    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    mcp_url = f"{origin}/mcp"
    # Start the seeded server with the OAuth operator pinned to "bob" and its OWN
    # public URL (origin) so /authorize's exact redirect_uri match works.
    proc = _start_http_server(port, iso_clients, iso_tokens)
    # _start_http_server pins BLACKBOX_MCP_OAUTH_OPERATOR=oauth-op + a fixed
    # public URL; override BOTH plus BLACKBOX_ROOT for this seeded/isolated run.
    # (We relaunch with a tailored env rather than thread params through.)
    proc.terminate()
    try:
        proc.communicate(timeout=5)
    except Exception:
        proc.kill()

    env = dict(os.environ)
    env["BLACKBOX_ROOT"] = str(tmp)
    env["BLACKBOX_MCP_HTTP_PORT"] = str(port)
    env["BLACKBOX_MCP_HTTP_HOST"] = "127.0.0.1"
    # Seed a bob-bound BlackBox token: the consent form authenticates THIS token,
    # so the OAuth code/access token binds to bob (the authenticated operator).
    env["BLACKBOX_MCP_TOKENS"] = json.dumps(ISO_TOKEN_MAP)
    env["BLACKBOX_MCP_TOKENS_FILE"] = str(_HERE / "__no_such_token_file__.json")
    env["BLACKBOX_MCP_OAUTH_CLIENTS_FILE"] = str(iso_clients)
    env["BLACKBOX_MCP_OAUTH_TOKENS_FILE"] = str(iso_tokens)
    env["BLACKBOX_MCP_PUBLIC_URL"] = origin   # redirect_uri match needs real origin
    env.setdefault("BLACKBOX_MCP_LOG_LEVEL", "WARNING")
    proc = subprocess.Popen(
        [sys.executable, str(_HERE / "blackbox_mcp_server.py"), "--transport", "http"],
        cwd=str(_HERE), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    tail = ""
    try:
        if not _wait_for_http(mcp_url):
            failures.append("(l) seeded OAuth server did not come up")
            return

        cb = "https://claude.ai/api/mcp/auth_callback"
        client_id = _register_client(origin, cb)
        if not client_id:
            failures.append("(l) could not register OAuth client on seeded server")
            return
        access_token = _mint_access_token(origin, client_id, cb, ISO_BOB_TOKEN)
        if not access_token or not access_token.startswith("bbmcp_oat_"):
            failures.append(f"(l) could not mint OAuth access token: {access_token!r}")
            return

        c = httpx.Client(timeout=15)
        # Open an /mcp session under the bob-bound OAuth token (the gate must
        # accept it; (k) asserts the full init+74-tools count on the default-index
        # server where ToolVault/ is present -- this seeded overlay lacks the
        # root ToolVault/ dir, so its tool count is intentionally not asserted).
        sid = _raw_init(c, mcp_url, access_token)
        if not sid:
            failures.append("(l) OAuth token did not open an /mcp session on the seeded server")
            c.close()
            return

        # ---- (l) operator-isolation via the OAuth token (mirror M3 #2A) ----
        # bob's OAuth token must NOT read alice's snap (get_snapshot) ...
        err, body = _raw_call(c, mcp_url, access_token, sid, "get_snapshot", {"snap_id": "SNAP-ALICE-1"})
        txt = json.dumps(body)
        if err is True and "not_found" in txt and "ALICEDATA" not in txt and "content" not in body:
            print("[oauth] PASS (l-1) OAuth(bob) get_snapshot(alice) -> not_found, no content "
                  "(OAuth token subject to M3 #2A isolation)")
        else:
            failures.append(f"(l-1) OAuth cross-op get_snapshot NOT denied: err={err} body={txt[:200]}")
        # ... nor via seek_snapshot_direct ...
        err, body = _raw_call(c, mcp_url, access_token, sid, "seek_snapshot_direct", {"snap_id": "SNAP-ALICE-1"})
        txt = json.dumps(body)
        if err is True and "not_found" in txt and "ALICEDATA" not in txt:
            print("[oauth] PASS (l-2) OAuth(bob) seek_snapshot_direct(alice) -> not_found, no content")
        else:
            failures.append(f"(l-2) OAuth cross-op seek_snapshot_direct NOT denied: err={err} body={txt[:200]}")
        # ... but CAN read its OWN (bob's) snap (gate is per-operator, not deny-all) ...
        err, body = _raw_call(c, mcp_url, access_token, sid, "get_snapshot", {"snap_id": "SNAP-BOB-1"})
        if err is not True and isinstance(body, dict) and body.get("metadata", {}).get("operator") == ISO_OAUTH_OPERATOR:
            print("[oauth] PASS (l-3) OAuth(bob) CAN read its OWN snap (per-operator gate, not deny-all)")
        else:
            failures.append(f"(l-3) OAuth token denied its OWN snap (over-blocking): err={err} body={json.dumps(body)[:200]}")
        # ... list_operators -> only bob (roster not leaked over the OAuth token) ...
        err, body = _raw_call(c, mcp_url, access_token, sid, "list_operators", {})
        names = {o["name"] for o in body.get("operators", [])} if isinstance(body, dict) else set()
        if names == {ISO_OAUTH_OPERATOR}:
            print("[oauth] PASS (l-4) OAuth(bob) list_operators -> only bob (roster not leaked)")
        else:
            failures.append(f"(l-4) OAuth list_operators leaked roster: {names}")
        # ... get_current_operator -> resolved=bob, roster=['bob'], alice absent.
        err, body = _raw_call(c, mcp_url, access_token, sid, "get_current_operator", {})
        if isinstance(body, dict):
            ops = body.get("operators")
            if (ops == [ISO_OAUTH_OPERATOR] and body.get("count") == 1
                    and body.get("resolved") == ISO_OAUTH_OPERATOR
                    and ISO_OTHER_OPERATOR not in (ops or [])):
                print("[oauth] PASS (l-5) OAuth(bob) get_current_operator -> resolved=bob, "
                      "roster=['bob'] count=1 (binding via OAuth == binding via static bearer)")
            else:
                failures.append(f"(l-5) OAuth get_current_operator leaked/wrong: operators={ops!r} "
                                f"count={body.get('count')!r} resolved={body.get('resolved')!r}")
        else:
            failures.append(f"(l-5) OAuth get_current_operator unexpected body: {json.dumps(body)[:200]}")
        c.close()
    finally:
        proc.terminate()
        try:
            tail = proc.communicate(timeout=5)[0]
        except Exception:
            proc.kill(); tail = ""
        if any(f.startswith(("(k)", "(l")) for f in failures) and tail:
            print("\n[oauth] ---- seeded OAuth server output (tail) ----")
            print("\n".join(tail.splitlines()[-25:]))


def main() -> int:
    failures = []
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    mcp_url = f"{origin}/mcp"
    _tmpdir = Path(tempfile.mkdtemp(prefix="bbmcp_oauth_"))
    clients_file = _tmpdir / "mcp_oauth_clients.json"
    # CHUNK 6b: a TEMP access-token store so the test never writes the real
    # Manifest/mcp_oauth_tokens.json. Chunk 6c will read this binding shape.
    tokens_file = _tmpdir / "mcp_oauth_tokens.json"

    proc = _start_http_server(port, clients_file, tokens_file)
    try:
        if not _wait_for_http(mcp_url):
            try:
                proc.terminate(); out = proc.communicate(timeout=5)[0]
            except Exception:
                out = ""
            print("[oauth] FAIL: HTTP server did not come up")
            print(out)
            return 1

        # ---- (a) AS metadata, NO bearer -> 200 + RFC 8414 shape ----
        r = httpx.get(f"{origin}/.well-known/oauth-authorization-server", timeout=10.0)
        if r.status_code == 200:
            try:
                m = r.json()
                checks = {
                    "issuer": m.get("issuer") == TEST_PUBLIC_URL,
                    "authorization_endpoint": m.get("authorization_endpoint") == TEST_PUBLIC_URL + "/authorize",
                    "token_endpoint": m.get("token_endpoint") == TEST_PUBLIC_URL + "/token",
                    "registration_endpoint": m.get("registration_endpoint") == TEST_PUBLIC_URL + "/register",
                    "S256": m.get("code_challenge_methods_supported") == ["S256"],
                    "grant_types": m.get("grant_types_supported") == ["authorization_code"],
                    "response_types": m.get("response_types_supported") == ["code"],
                    "auth_methods_none": m.get("token_endpoint_auth_methods_supported") == ["none"],
                }
                bad = [k for k, ok in checks.items() if not ok]
                if not bad:
                    print("[oauth] PASS (a) /.well-known/oauth-authorization-server -> "
                          "200 + valid RFC 8414 metadata (issuer/endpoints/S256/none)")
                else:
                    failures.append(f"(a) AS metadata wrong fields: {bad} | body={json.dumps(m)[:300]}")
            except Exception as e:
                failures.append(f"(a) AS metadata not JSON ({e}): {r.text[:200]}")
        else:
            failures.append(f"(a) AS metadata expected 200, got {r.status_code}: {r.text[:200]}")

        # ---- (a) Protected Resource metadata, NO bearer -> 200 + RFC 9728 ----
        r = httpx.get(f"{origin}/.well-known/oauth-protected-resource", timeout=10.0)
        if r.status_code == 200:
            try:
                m = r.json()
                if (m.get("resource") == EXPECTED_RESOURCE
                        and m.get("authorization_servers") == [TEST_PUBLIC_URL]):
                    print("[oauth] PASS (a) /.well-known/oauth-protected-resource -> "
                          "200 + valid RFC 9728 metadata (resource=MCP url, AS=[issuer])")
                else:
                    failures.append(f"(a) PR metadata wrong: resource={m.get('resource')!r} "
                                    f"authorization_servers={m.get('authorization_servers')!r}")
            except Exception as e:
                failures.append(f"(a) PR metadata not JSON ({e}): {r.text[:200]}")
        else:
            failures.append(f"(a) PR metadata expected 200, got {r.status_code}: {r.text[:200]}")

        # ---- (b) DCR: POST /register {redirect_uris} -> 201 + client_id, NO bearer ----
        reg_body = {
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "client_name": "Test OAuth Client",
            "token_endpoint_auth_method": "none",
        }
        r = httpx.post(f"{origin}/register", json=reg_body, timeout=10.0)
        registered_id = None
        if r.status_code in (200, 201):
            try:
                body = r.json()
                registered_id = body.get("client_id")
                if (registered_id
                        and body.get("redirect_uris") == reg_body["redirect_uris"]
                        and body.get("token_endpoint_auth_method") == "none"
                        and "client_secret" not in body):
                    print(f"[oauth] PASS (b) POST /register -> {r.status_code} + client_id="
                          f"{registered_id!r} (public client, no secret)")
                else:
                    failures.append(f"(b) /register body wrong: {json.dumps(body)[:300]}")
            except Exception as e:
                failures.append(f"(b) /register not JSON ({e}): {r.text[:200]}")
        else:
            failures.append(f"(b) /register expected 200/201, got {r.status_code}: {r.text[:200]}")

        # ---- (c) the exemption is real: all three worked with NO Authorization
        #          header above. Confirm explicitly that even an EMPTY/garbage
        #          Authorization header doesn't break the public endpoints. ----
        r = httpx.get(f"{origin}/.well-known/oauth-authorization-server",
                      headers={"Authorization": "Bearer garbage"}, timeout=10.0)
        if r.status_code == 200:
            print("[oauth] PASS (c) public endpoints reachable WITHOUT a valid bearer "
                  "(no-header above + bad-header here both 200)")
        else:
            failures.append(f"(c) public endpoint rejected a (irrelevant) bad bearer: {r.status_code}")

        # ---- (d) /mcp WITHOUT a token is STILL 401 (exemption didn't open gate) ----
        r = _raw_mcp_init_post(mcp_url, headers={})
        if r.status_code == 401 and "bearer" in r.headers.get("www-authenticate", "").lower():
            print("[oauth] PASS (d) POST /mcp WITHOUT token -> STILL 401 + "
                  "WWW-Authenticate: Bearer (exemption did NOT open the /mcp gate)")
        else:
            failures.append(f"(d) /mcp without token should 401, got {r.status_code} / "
                            f"{r.headers.get('www-authenticate')!r}")

        # ---- (d') positive control: /mcp WITH the valid token is NOT 401 ----
        r = _raw_mcp_init_post(mcp_url, headers={"Authorization": f"Bearer {VALID_TOKEN}"})
        if r.status_code != 401:
            print(f"[oauth] PASS (d') POST /mcp WITH valid token -> {r.status_code} "
                  "(not 401; gate opens for a real credential)")
        else:
            failures.append(f"(d') /mcp WITH valid token unexpectedly 401")

        # ---- negative DCR: missing redirect_uris -> 400 ----
        r = httpx.post(f"{origin}/register", json={"client_name": "no-redirects"}, timeout=10.0)
        if r.status_code == 400:
            print("[oauth] PASS (e) POST /register with NO redirect_uris -> 400 (validated)")
        else:
            failures.append(f"(e) /register w/o redirect_uris should 400, got {r.status_code}")

        # ---- store: the registered client is persisted, 0600, to the temp store ----
        if registered_id:
            if clients_file.exists():
                mode = oct(clients_file.stat().st_mode & 0o777)
                try:
                    persisted = json.loads(clients_file.read_text())
                except Exception as ex:
                    persisted = {}
                    failures.append(f"(f) client store not readable JSON: {ex}")
                if registered_id in persisted and mode == "0o600":
                    print(f"[oauth] PASS (f) client persisted to gitignored store, perms={mode}")
                else:
                    failures.append(f"(f) store wrong: id_present={registered_id in persisted} "
                                    f"perms={mode}")
            else:
                failures.append(f"(f) client store file not created: {clients_file}")

        # =====================================================================
        # CHUNK 6b: the Authorization Code + PKCE flow (/authorize + /token).
        # These reuse the client registered in (b) above (registered_id +
        # reg_body["redirect_uris"][0]).
        # =====================================================================
        cb_redirect = reg_body["redirect_uris"][0]

        def _pkce_pair():
            """Return (verifier, S256 challenge) -- a fresh PKCE pair."""
            verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
            digest = hashlib.sha256(verifier.encode("ascii")).digest()
            challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            return verifier, challenge

        def _authorize_params(client_id, redirect_uri, challenge, method="S256",
                              state="xyz-state-123", with_challenge=True):
            """Build the OAuth /authorize param dict."""
            q = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": "mcp",
            }
            if with_challenge:
                q["code_challenge"] = challenge
                q["code_challenge_method"] = method
            return q

        def _authorize_get(client_id, redirect_uri, challenge, method="S256",
                           state="xyz-state-123", with_challenge=True):
            """GET /authorize (no redirect-follow) -> the httpx.Response. With
            VALID params this is now the 200 consent FORM (no code); with bad
            params it is the pre-form validation error (302/400, no code)."""
            return httpx.get(
                f"{origin}/authorize",
                params=_authorize_params(client_id, redirect_uri, challenge,
                                         method, state, with_challenge),
                follow_redirects=False, timeout=10.0)

        def _authorize(client_id, redirect_uri, challenge, method="S256",
                       state="xyz-state-123", with_challenge=True,
                       token=VALID_TOKEN):
            """Full consent flow: GET the form, then POST the OAuth params +
            `token` -> the POST httpx.Response. With a VALID token + valid params
            this is a 302 with ?code (bound to the token's operator)."""
            return _authorize_get_post(
                origin,
                _authorize_params(client_id, redirect_uri, challenge,
                                  method, state, with_challenge),
                token)

        def _code_from_redirect(resp):
            """Pull ?code & ?state out of a 302 Location, or (None, None)."""
            loc = resp.headers.get("location", "")
            qs = parse_qs(urlparse(loc).query)
            return (qs.get("code", [None])[0], qs.get("state", [None])[0])

        # =====================================================================
        # CONSENT-AUTH regression (closes the public auto-approve bypass): the
        # /authorize endpoint no longer mints a code on GET. It renders a consent
        # form; a code is issued ONLY when the operator POSTs a VALID BlackBox
        # token, and binds to THAT token's operator. These prove:
        #   (consent-1) GET /authorize (valid params) -> 200 HTML form, NO code.
        #   (consent-2) POST /authorize (valid token) -> 302 + code.
        #   (consent-3) POST /authorize (WRONG/MISSING token) -> 401, NO code,
        #               form re-rendered (no bypass).
        #   (consent-5) PKCE/redirect/state validation STILL rejects on the POST
        #               path too (not only on GET).
        # (consent-4, "issued token acts as the token's operator", is asserted
        #  end-to-end by the seeded isolation test (l) via ISO_BOB_TOKEN.)
        # =====================================================================
        if registered_id:
            _v, _c = _pkce_pair()
            _q = _authorize_params(registered_id, cb_redirect, _c, state="consent-1")

            # (consent-1) GET with VALID params -> 200 consent form, NO code,
            # NO redirect. The form must carry a password input named
            # 'blackbox_token' + the OAuth params as hidden fields.
            g = httpx.get(f"{origin}/authorize", params=_q,
                          follow_redirects=False, timeout=10.0)
            body_l = g.text.lower()
            if (g.status_code == 200
                    and g.headers.get("location") is None
                    and "code=" not in g.headers.get("location", "")
                    and 'name="blackbox_token"' in body_l
                    and 'type="password"' in body_l
                    and "<form" in body_l):
                print("[oauth] PASS (consent-1) GET /authorize (valid params) -> "
                      "200 HTML consent form (password blackbox_token), NO code issued")
            else:
                failures.append(f"(consent-1) GET should render the consent form w/o a code: "
                                f"status={g.status_code} loc={g.headers.get('location')!r} "
                                f"has_token_field={'name=\"blackbox_token\"' in body_l} "
                                f"body={g.text[:160]!r}")

            # (consent-2) POST with a VALID token -> 302 + code (bound to alice).
            p = httpx.post(f"{origin}/authorize",
                           data={**_q, "blackbox_token": VALID_TOKEN},
                           follow_redirects=False, timeout=10.0)
            pc, ps = _code_from_redirect(p)
            if p.status_code == 302 and pc and ps == "consent-1":
                print("[oauth] PASS (consent-2) POST /authorize (VALID token) -> 302 + code "
                      "(consent authenticated; code minted)")
            else:
                failures.append(f"(consent-2) POST w/ valid token should 302+code: "
                                f"status={p.status_code} code={pc!r} state={ps!r}")

            # (consent-3a) POST with a WRONG token -> 401, NO code, form re-rendered.
            pw = httpx.post(f"{origin}/authorize",
                            data={**_q, "blackbox_token": "totally-wrong-token-zzzz"},
                            follow_redirects=False, timeout=10.0)
            pwc, _ = _code_from_redirect(pw)
            pw_l = pw.text.lower()
            if (pw.status_code == 401 and pwc is None
                    and pw.headers.get("location") is None
                    and 'name="blackbox_token"' in pw_l):
                print("[oauth] PASS (consent-3a) POST /authorize (WRONG token) -> 401, NO code, "
                      "consent form re-rendered (auto-approve bypass closed)")
            else:
                failures.append(f"(consent-3a) wrong token should 401+form, no code: "
                                f"status={pw.status_code} code={pwc!r} "
                                f"has_form={'name=\"blackbox_token\"' in pw_l}")

            # (consent-3b) POST with a MISSING token -> 401, NO code (same bypass-close).
            pm = httpx.post(f"{origin}/authorize", data=_q,
                            follow_redirects=False, timeout=10.0)
            pmc, _ = _code_from_redirect(pm)
            if pm.status_code == 401 and pmc is None:
                print("[oauth] PASS (consent-3b) POST /authorize (MISSING token) -> 401, NO code")
            else:
                failures.append(f"(consent-3b) missing token should 401, no code: "
                                f"status={pm.status_code} code={pmc!r}")

            # (consent-5) the param validation STILL fires on the POST path, even
            # with a VALID token -- a valid token does NOT let a malformed request
            # through. PKCE-missing -> no code; bad state -> no code; unregistered
            # redirect_uri -> direct 400 (no open redirect) + no code.
            # PKCE missing on POST:
            _qn = _authorize_params(registered_id, cb_redirect, _c,
                                    state="consent-5", with_challenge=False)
            r5a = httpx.post(f"{origin}/authorize",
                             data={**_qn, "blackbox_token": VALID_TOKEN},
                             follow_redirects=False, timeout=10.0)
            c5a, _ = _code_from_redirect(r5a)
            # missing state on POST (delete state from a valid param set):
            _qs = _authorize_params(registered_id, cb_redirect, _c, state="consent-5")
            _qs.pop("state")
            r5b = httpx.post(f"{origin}/authorize",
                             data={**_qs, "blackbox_token": VALID_TOKEN},
                             follow_redirects=False, timeout=10.0)
            c5b, _ = _code_from_redirect(r5b)
            # unregistered redirect_uri on POST -> direct 400, no open redirect:
            _qe = _authorize_params(registered_id, "https://evil.example.com/steal",
                                    _c, state="consent-5")
            r5c = httpx.post(f"{origin}/authorize",
                             data={**_qe, "blackbox_token": VALID_TOKEN},
                             follow_redirects=False, timeout=10.0)
            c5c, _ = _code_from_redirect(r5c)
            if (c5a is None and c5b is None and c5c is None
                    and r5c.status_code == 400 and r5c.headers.get("location") is None):
                print("[oauth] PASS (consent-5) POST path STILL enforces PKCE + state + "
                      "exact redirect_uri (valid token does NOT bypass validation; "
                      "bad redirect_uri -> direct 400, no open redirect)")
            else:
                failures.append(f"(consent-5) POST-path validation leaked: pkce_code={c5a!r} "
                                f"state_code={c5b!r} evil_code={c5c!r} "
                                f"evil_status={r5c.status_code} evil_loc={r5c.headers.get('location')!r}")
        else:
            failures.append("(consent-*) no registered client to run consent-auth checks")

        # ---- (6b-e) FULL code flow: authorize -> code -> token -> access_token ----
        access_token = None
        if registered_id:
            verifier, challenge = _pkce_pair()
            r = _authorize(registered_id, cb_redirect, challenge, state="state-e")
            code, echoed = _code_from_redirect(r)
            if r.status_code == 302 and code:
                tr = httpx.post(f"{origin}/token", data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": registered_id,
                    "redirect_uri": cb_redirect,
                    "code_verifier": verifier,
                }, timeout=10.0)
                if tr.status_code == 200:
                    tb = tr.json()
                    access_token = tb.get("access_token")
                    if (access_token
                            and access_token.startswith("bbmcp_oat_")
                            and tb.get("token_type") == "Bearer"
                            and isinstance(tb.get("expires_in"), int)
                            and tb.get("expires_in") > 0):
                        print("[oauth] PASS (6b-e) FULL code flow: /authorize -> code -> "
                              f"/token -> access_token (type=Bearer, expires_in={tb.get('expires_in')})")
                    else:
                        failures.append(f"(6b-e) token response wrong: {json.dumps(tb)[:300]}")
                else:
                    failures.append(f"(6b-e) /token expected 200, got {tr.status_code}: {tr.text[:200]}")
            else:
                failures.append(f"(6b-e) /authorize expected 302 + code, got {r.status_code} "
                                f"loc={r.headers.get('location')!r}")
        else:
            failures.append("(6b-e) no registered client to run the code flow")

        # ---- (6b-j) state is echoed back on the authorize redirect ----
        if registered_id:
            _v, _c = _pkce_pair()
            r = _authorize(registered_id, cb_redirect, _c, state="unique-state-J-42")
            _code, echoed = _code_from_redirect(r)
            if r.status_code == 302 and echoed == "unique-state-J-42":
                print("[oauth] PASS (6b-j) state echoed back unchanged on the authorize redirect")
            else:
                failures.append(f"(6b-j) state not echoed: status={r.status_code} echoed={echoed!r}")

        # ---- (6b-f) PKCE missing -> rejected; method != S256 -> rejected ----
        if registered_id:
            _v, _c = _pkce_pair()
            # missing code_challenge entirely
            r1 = _authorize(registered_id, cb_redirect, _c, with_challenge=False)
            c1, _ = _code_from_redirect(r1)
            # present but wrong method
            r2 = _authorize(registered_id, cb_redirect, _c, method="plain")
            c2, _ = _code_from_redirect(r2)
            # Rejected = NO code issued. (Either a redirected error or a 400.)
            if c1 is None and c2 is None:
                print("[oauth] PASS (6b-f) /authorize rejects missing PKCE challenge AND "
                      "method!=S256 (no code issued)")
            else:
                failures.append(f"(6b-f) PKCE not enforced: missing->code={c1!r} plain->code={c2!r}")

        # ---- (6b-h) redirect_uri NOT matching the registered client -> rejected ----
        if registered_id:
            _v, _c = _pkce_pair()
            r = _authorize(registered_id, "https://evil.example.com/steal", _c)
            # Must be a DIRECT error (NOT a 302 to the unregistered URI -> no open redirect).
            if r.status_code == 400 and r.headers.get("location") is None:
                print("[oauth] PASS (6b-h) /authorize rejects an unregistered redirect_uri "
                      "with a direct 400 (no open redirect)")
            else:
                failures.append(f"(6b-h) bad redirect_uri not rejected safely: status={r.status_code} "
                                f"loc={r.headers.get('location')!r}")

        # ---- (6b-g) /token with a WRONG code_verifier -> rejected ----
        if registered_id:
            verifier, challenge = _pkce_pair()
            r = _authorize(registered_id, cb_redirect, challenge, state="state-g")
            code, _ = _code_from_redirect(r)
            if r.status_code == 302 and code:
                tr = httpx.post(f"{origin}/token", data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": registered_id,
                    "redirect_uri": cb_redirect,
                    "code_verifier": verifier + "TAMPERED",  # wrong verifier
                }, timeout=10.0)
                if tr.status_code == 400 and tr.json().get("error") == "invalid_grant":
                    print("[oauth] PASS (6b-g) /token with a WRONG code_verifier -> "
                          "400 invalid_grant (PKCE verified)")
                else:
                    failures.append(f"(6b-g) wrong verifier not rejected: status={tr.status_code} "
                                    f"body={tr.text[:200]}")
            else:
                failures.append(f"(6b-g) setup authorize failed: status={r.status_code}")

        # ---- (6b-i) code is SINGLE-USE: a 2nd /token with the same code -> rejected ----
        if registered_id:
            verifier, challenge = _pkce_pair()
            r = _authorize(registered_id, cb_redirect, challenge, state="state-i")
            code, _ = _code_from_redirect(r)
            if r.status_code == 302 and code:
                common = {
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": registered_id,
                    "redirect_uri": cb_redirect,
                    "code_verifier": verifier,
                }
                t1 = httpx.post(f"{origin}/token", data=common, timeout=10.0)
                t2 = httpx.post(f"{origin}/token", data=common, timeout=10.0)
                if (t1.status_code == 200
                        and t2.status_code == 400
                        and t2.json().get("error") == "invalid_grant"):
                    print("[oauth] PASS (6b-i) authorization code is SINGLE-USE "
                          "(1st /token 200, 2nd same code -> 400 invalid_grant)")
                else:
                    failures.append(f"(6b-i) code not single-use: t1={t1.status_code} t2={t2.status_code} "
                                    f"t2body={t2.text[:150]}")
            else:
                failures.append(f"(6b-i) setup authorize failed: status={r.status_code}")

        # ---- (6b-store) access token persisted to the gitignored 0600 store,
        #      bound to the pinned OAuth operator (chunk 6c reads this shape) ----
        if access_token:
            if tokens_file.exists():
                tmode = oct(tokens_file.stat().st_mode & 0o777)
                try:
                    tstore = json.loads(tokens_file.read_text())
                except Exception as ex:
                    tstore = {}
                    failures.append(f"(6b-store) token store not readable JSON: {ex}")
                binding = tstore.get(access_token) or {}
                if (access_token in tstore
                        and binding.get("operator") == TEST_OAUTH_OPERATOR
                        and isinstance(binding.get("expiry"), (int, float))
                        and tmode == "0o600"):
                    print(f"[oauth] PASS (6b-store) access token persisted to gitignored "
                          f"store perms={tmode}, bound operator={TEST_OAUTH_OPERATOR!r} "
                          "(chunk 6c can validate)")
                else:
                    failures.append(f"(6b-store) token store wrong: present={access_token in tstore} "
                                    f"operator={binding.get('operator')!r} perms={tmode}")
            else:
                failures.append(f"(6b-store) access-token store file not created: {tokens_file}")

        # =====================================================================
        # CHUNK 6c (against this default server -- full repo root, so ToolVault/
        # is present and the canonical 74-tool count holds; no backend needed):
        #   (k) end-to-end: the OAuth access token minted in 6b-e opens /mcp ->
        #       initialize + list_tools = 74 (gate accepts a REAL OAuth credential
        #       and routes it through the SAME path as a static bearer).
        #   (m) an EXPIRED or BOGUS OAuth access token on /mcp -> 401.
        #   (n) the STATIC-bearer path STILL works on /mcp (regression).
        # =====================================================================
        # ---- (k) OAuth token (from 6b-e) opens /mcp -> initialize + 74 tools ----
        if access_token:
            ck = httpx.Client(timeout=15)
            sid_oauth = _raw_init(ck, mcp_url, access_token)
            tools_oauth = _raw_list_tools(ck, mcp_url, access_token, sid_oauth)
            if sid_oauth and len(tools_oauth) == 74:
                print(f"[oauth] PASS (k) OAuth access token on /mcp -> initialize + "
                      f"list_tools = {len(tools_oauth)} tools (gate opens for a REAL OAuth "
                      "credential, same path as a static bearer)")
            else:
                failures.append(f"(k) OAuth e2e: sid={sid_oauth!r} tools={len(tools_oauth)} "
                                "(expected a session + 74 tools)")
            ck.close()
        else:
            failures.append("(k) no OAuth access token minted in 6b-e to run the e2e check")

        # ---- (m1) a BOGUS bbmcp_oat_ token (never issued) -> 401 ----
        r = _raw_mcp_init_post(mcp_url, headers={
            "Authorization": "Bearer bbmcp_oat_never_issued_bogus_token_zzzzzzzzzzzz"})
        if r.status_code == 401 and "bearer" in r.headers.get("www-authenticate", "").lower():
            print("[oauth] PASS (m1) BOGUS OAuth access token on /mcp -> 401 + "
                  "WWW-Authenticate: Bearer (never-issued token rejected)")
        else:
            failures.append(f"(m1) bogus OAuth token should 401, got {r.status_code} / "
                            f"{r.headers.get('www-authenticate')!r}")

        # ---- (m2) an EXPIRED OAuth access token on /mcp -> 401 (REAL probe). We
        #      stand up a DEDICATED server whose token store is PRE-SEEDED with an
        #      already-expired token. The server boots with an EMPTY in-process
        #      mirror (it only fills on in-process /token issuance), so validating
        #      this token forces the file-store fallback, whose STRICT `expiry <=
        #      now` check must reject it -> the middleware 401s. This probes the
        #      real /mcp gate, not just the store shape. (No backend needed: a 401
        #      short-circuits before any tool work.) ----
        exp_tmpdir = Path(tempfile.mkdtemp(prefix="bbmcp_oauth_exp_"))
        exp_clients = exp_tmpdir / "mcp_oauth_clients.json"
        exp_tokens = exp_tmpdir / "mcp_oauth_tokens.json"
        expired_token = "bbmcp_oat_expired_seeded_token_yyyyyyyyyyyyyyyyyyyy"
        exp_tokens.write_text(json.dumps({
            expired_token: {"operator": TEST_OAUTH_OPERATOR, "expiry": time.time() - 3600},
        }))
        os.chmod(str(exp_tokens), 0o600)
        exp_port = _free_port()
        exp_origin = f"http://127.0.0.1:{exp_port}"
        exp_mcp = f"{exp_origin}/mcp"
        exp_proc = _start_http_server(exp_port, exp_clients, exp_tokens)
        try:
            if not _wait_for_http(exp_mcp):
                failures.append("(m2) expired-token probe server did not come up")
            else:
                r = _raw_mcp_init_post(exp_mcp, headers={"Authorization": f"Bearer {expired_token}"})
                if r.status_code == 401 and "bearer" in r.headers.get("www-authenticate", "").lower():
                    print("[oauth] PASS (m2) EXPIRED OAuth access token on /mcp -> 401 + "
                          "WWW-Authenticate: Bearer (strict expiry enforced; expired == invalid)")
                else:
                    failures.append(f"(m2) expired OAuth token should 401, got {r.status_code} / "
                                    f"{r.headers.get('www-authenticate')!r}")
                # positive control on the SAME server: a FRESHLY minted token (not
                # expired) is accepted -> proves it's the expiry, not the store.
                ecid = _register_client(exp_origin, "https://claude.ai/api/mcp/auth_callback")
                fresh = _mint_access_token(exp_origin, ecid,
                                           "https://claude.ai/api/mcp/auth_callback",
                                           VALID_TOKEN) if ecid else None
                if fresh:
                    r2 = _raw_mcp_init_post(exp_mcp, headers={"Authorization": f"Bearer {fresh}"})
                    if r2.status_code != 401:
                        print(f"[oauth] PASS (m2') control: a FRESH (unexpired) OAuth token on the "
                              f"SAME server -> {r2.status_code} (not 401; expiry is the discriminator)")
                    else:
                        failures.append("(m2') fresh unexpired OAuth token unexpectedly 401")
                else:
                    failures.append("(m2') could not mint a fresh control token")
        finally:
            exp_proc.terminate()
            try:
                exp_proc.communicate(timeout=5)
            except Exception:
                exp_proc.kill()

        # ---- (n) the STATIC-bearer path STILL works on /mcp (regression) ----
        c = httpx.Client(timeout=15)
        sid_static = _raw_init(c, mcp_url, VALID_TOKEN)
        tools_static = _raw_list_tools(c, mcp_url, VALID_TOKEN, sid_static)
        if sid_static and len(tools_static) == 74:
            print(f"[oauth] PASS (n) STATIC bearer on /mcp STILL works -> initialize + "
                  f"list_tools = {len(tools_static)} tools (no regression from the OAuth branch)")
        else:
            failures.append(f"(n) static-bearer regression: sid={sid_static!r} "
                            f"tools={len(tools_static)} (expected 74)")
        c.close()

    finally:
        proc.terminate()
        try:
            tail = proc.communicate(timeout=5)[0]
        except Exception:
            proc.kill(); tail = ""

    # ---- (l): OAuth operator-isolation (mirror M3 #2A via an OAuth token)
    #      on a SEEDED two-operator server (OAuth operator = bob). ----
    _run_oauth_isolation(failures)

    if failures:
        print("\n[oauth] ---- HTTP server output (tail) ----")
        print("\n".join((tail or "").splitlines()[-25:]))
        print("\n[oauth] FAILURES:")
        for f in failures:
            print(f"  - {f}")
        print(f"\n[oauth] {len(failures)} assertion(s) failed.")
        return 1

    print("\n[oauth] ALL ASSERTIONS PASSED "
          "(a: both .well-known 200 + valid metadata; b: /register 201 + client_id; "
          "c: public WITHOUT bearer; d: /mcp without token STILL 401, with token not 401; "
          "e: missing redirect_uris 400; f: client persisted 0600 to gitignored store; "
          "consent-1: GET -> 200 form no code; consent-2: POST valid token -> 302+code; "
          "consent-3: POST wrong/missing token -> 401 no code (bypass closed); "
          "consent-5: POST path still enforces PKCE/state/exact-redirect; "
          "6b-e: full code flow -> access_token; 6b-f: PKCE missing/!=S256 rejected; "
          "6b-g: wrong verifier rejected; 6b-h: bad redirect_uri direct-400 no-open-redirect; "
          "6b-i: code single-use; 6b-j: state echoed; "
          "6b-store: access token persisted 0600 + operator-bound; "
          "6c-k: OAuth token opens /mcp -> init + 74 tools; "
          "6c-l: OAuth token subject to M3 #2A isolation (cross-op not_found, "
          "own-read ok, roster scoped); 6c-m: bogus + EXPIRED OAuth token -> 401 "
          "(strict expiry); 6c-n: static-bearer path STILL works).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
