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


# CHUNK 6b: the operator every OAuth-minted access token must bind to (the
# server reads BLACKBOX_MCP_OAUTH_OPERATOR; we pin it so the binding is
# deterministic and the access-token store assertion is exact).
TEST_OAUTH_OPERATOR = "oauth-op"


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

        def _authorize(client_id, redirect_uri, challenge, method="S256",
                       state="xyz-state-123", with_challenge=True):
            """GET /authorize (no redirect-follow) -> the httpx.Response."""
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
            return httpx.get(f"{origin}/authorize", params=q,
                             follow_redirects=False, timeout=10.0)

        def _code_from_redirect(resp):
            """Pull ?code & ?state out of a 302 Location, or (None, None)."""
            loc = resp.headers.get("location", "")
            qs = parse_qs(urlparse(loc).query)
            return (qs.get("code", [None])[0], qs.get("state", [None])[0])

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

    finally:
        proc.terminate()
        try:
            tail = proc.communicate(timeout=5)[0]
        except Exception:
            proc.kill(); tail = ""

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
          "6b-e: full code flow -> access_token; 6b-f: PKCE missing/!=S256 rejected; "
          "6b-g: wrong verifier rejected; 6b-h: bad redirect_uri direct-400 no-open-redirect; "
          "6b-i: code single-use; 6b-j: state echoed; "
          "6b-store: access token persisted 0600 + operator-bound).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
