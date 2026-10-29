#!/usr/bin/env python3
"""Hostile-input regression tests for the public MCP OAuth surface and the
server's own contracts (audit 2026-10-09 rank 80: O20-mcp-server-04/-05/-08/
-09/-11/-13/-14/-17/-18).

  (1) the OAuth edge -- /register, /authorize, /token and /revoke are
      rate-limited per client address (token buckets mirrored to a ledger that
      survives a restart) and their bodies are capped; /mcp is untouched.
  (2) dynamic client registration -- bounded metadata, a bounded store:
      unfinished registrations expire, one address may hold only a few, a full
      store drops the oldest unfinished one.
  (3) token comparison -- a non-ASCII bearer, consent token or PKCE verifier
      is a 4xx, never a 500.
  (4) refresh tokens -- issued with the code exchange, single-use and
      rotating; a replayed one revokes its family; they are client-bound,
      expire, die with the bearer that consented, and are stored hashed;
      /revoke revokes; the metadata advertises both.
  (5) the tool-call audit line names the CURRENT request's rid and token id.
  (6) every advertised resource reads; an unknown one is resource-not-found.
  (7) stdio: the SDK writes to the real stdout, every stray print to stderr.
  (8) startup refuses an empty or broken tool catalog.
  (9) one tool_registry, imported normally, and a lean import closure.
 (10) /internal/reload is not on the public listener; the control listener
      binds loopback only.

HERMETIC: no backend at :9091, no network beyond loopback test listeners, every
store in a temp dir. The HTTP transport runs in-process through
httpx.ASGITransport against the real Starlette app and SDK session manager.

RUN: <lean MCP venv>/bin/python -m pytest MCP/test_mcp_hardening.py -q
     (MCP/run_tests.sh runs it)
"""
import asyncio
import base64
import contextlib
import hashlib
import importlib.util as _ilu
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_TMP = Path(tempfile.mkdtemp(prefix="bbmcp_hardening_"))

TOKEN_A = "hardening-test-token-alice-" + "a" * 24
TOKEN_B = "hardening-test-token-bob-" + "b" * 24
OP_A, OP_B = "alice", "bob"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"

_ENV = {
    "BLACKBOX_ROOT": str(_REPO_ROOT),
    "BLACKBOX_MCP_TOKENS": "",
    "BLACKBOX_MCP_TOKENS_FILE": str(_TMP / "no_tokens.json"),
    "BLACKBOX_MCP_OAUTH_TOKENS_FILE": str(_TMP / "oauth_tokens.json"),
    "BLACKBOX_MCP_OAUTH_CLIENTS_FILE": str(_TMP / "oauth_clients.json"),
    "BLACKBOX_MCP_OAUTH_REFRESH_FILE": str(_TMP / "oauth_refresh_tokens.json"),
    "BLACKBOX_MCP_OAUTH_RATE_LEDGER_FILE": str(_TMP / "oauth_rate_ledger.json"),
    "BLACKBOX_MCP_RUNTIME_FILE": str(_TMP / "no_runtime.json"),
    "BLACKBOX_MCP_RELOAD_SECRET_FILE": str(_TMP / "credentials" / "mcp_reload.secret"),
    "BLACKBOX_MCP_PUBLIC_URL": "",
    "BLACKBOX_MCP_LOG_LEVEL": "INFO",
}
_saved_env = {k: os.environ.get(k) for k in _ENV}
os.environ.update(_ENV)
sys.path.insert(0, str(_HERE))
try:
    _spec = _ilu.spec_from_file_location("bbmcp_hardening", str(_HERE / "blackbox_mcp_server.py"))
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

from mcp import types as mcp_types  # noqa: E402

_ACCEPT = "application/json, text/event-stream"
_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "hardening-test", "version": "0"}}}
GENEROUS = {rule: (10_000, 1) for rule in getattr(srv, "_DEFAULT_OAUTH_RATE_LIMITS", {})}


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch):
    """Fresh stores, fresh in-memory token state, a two-operator token map."""
    for attr, name in (("BLACKBOX_MCP_OAUTH_TOKENS_FILE", "oauth_tokens.json"),
                       ("BLACKBOX_MCP_OAUTH_CLIENTS_FILE", "oauth_clients.json"),
                       ("BLACKBOX_MCP_OAUTH_REFRESH_FILE", "oauth_refresh_tokens.json"),
                       ("BLACKBOX_MCP_OAUTH_RATE_LEDGER_FILE", "oauth_rate_ledger.json")):
        monkeypatch.setattr(srv, attr, str(tmp_path / name), raising=False)
    monkeypatch.setattr(srv, "_reload_secret_path", lambda: tmp_path / "credentials" / "reload.secret")
    monkeypatch.setattr(srv, "_load_token_map", lambda: {TOKEN_A: OP_A, TOKEN_B: OP_B})
    monkeypatch.setattr(srv, "OAUTH_RATE_LIMITS", dict(GENEROUS), raising=False)
    monkeypatch.setattr(srv, "_ACCESS_TOKENS", {})
    monkeypatch.setattr(srv, "_AUTH_CODES", {})
    monkeypatch.setattr(srv, "_REVOKED_FAMILIES", set(), raising=False)
    monkeypatch.setitem(srv._AUTH_MIDDLEWARE, "instance", None)
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _App:
    """The real HTTP app, with one AsyncClient per client address."""

    def __init__(self, app):
        self.app = app
        self._clients = []

    def client(self, ip="203.0.113.10"):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=(ip, 40000)),
                              base_url="http://127.0.0.1")
        self._clients.append(c)
        return c

    async def close(self):
        for c in self._clients:
            await c.aclose()


def run_app(fn):
    """Build the app and run `await fn(app)` with its session manager live."""
    async def main():
        app, sm = srv.build_http_app()
        holder = _App(app)
        try:
            async with sm.run():
                return await asyncio.wait_for(fn(holder), 60)
        finally:
            await holder.close()
    return asyncio.run(main())


def _pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def register(c, redirect=CALLBACK, **extra):
    r = await c.post("/register", json={"redirect_uris": [redirect], "client_name": "Hardening",
                                        "token_endpoint_auth_method": "none", **extra})
    return r


async def authorize(c, client_id, token=TOKEN_A, redirect=CALLBACK, challenge=None):
    verifier, default_challenge = _pkce()
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
              "state": "st", "scope": "mcp", "code_challenge": challenge or default_challenge,
              "code_challenge_method": "S256"}
    r = await c.post("/authorize", data={**params, "blackbox_token": token})
    code = parse_qs(urlparse(r.headers.get("location", "")).query).get("code", [None])[0]
    return r, code, verifier


async def connect(c, token=TOKEN_A):
    """Register + consent + exchange: (client_id, token response dict)."""
    client_id = (await register(c)).json()["client_id"]
    _r, code, verifier = await authorize(c, client_id, token)
    t = await c.post("/token", data={"grant_type": "authorization_code", "code": code,
                                     "client_id": client_id, "redirect_uri": CALLBACK,
                                     "code_verifier": verifier})
    assert t.status_code == 200, t.text
    return client_id, t.json()


async def refresh(c, client_id, refresh_token):
    return await c.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                        "refresh_token": refresh_token})


async def mcp_status(c, bearer):
    r = await c.post("/mcp", json=_INIT, headers={"Authorization": f"Bearer {bearer}",
                                                   "Accept": _ACCEPT, "Content-Type": "application/json"})
    return r.status_code


def _store(path):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}


# ===========================================================================
# (1) The OAuth edge: rate limits and the body cap
# ===========================================================================
def test_register_is_rate_limited_per_address(monkeypatch):
    monkeypatch.setitem(srv.OAUTH_RATE_LIMITS, "register", (3, 3600))

    async def go(app):
        a, b = app.client("203.0.113.1"), app.client("198.51.100.2")
        codes = [(await register(a)).status_code for _ in range(4)]
        limited = await register(a)
        other = await register(b)
        return codes, limited, other.status_code

    codes, limited, other = run_app(go)
    assert codes[:3] == [201, 201, 201] and codes[3] == 429, codes
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    assert limited.json()["error"] == "temporarily_unavailable"
    assert other == 201, "another address has its own bucket"


def test_register_has_a_ceiling_across_every_address(monkeypatch):
    monkeypatch.setitem(srv.OAUTH_RATE_LIMITS, "register_all", (4, 3600))

    async def go(app):
        return [(await register(app.client(f"203.0.113.{i}"))).status_code for i in range(6)]

    assert run_app(go) == [201, 201, 201, 201, 429, 429]


def test_authorize_token_and_revoke_are_rate_limited(monkeypatch):
    for rule in ("authorize", "token", "revoke"):
        monkeypatch.setitem(srv.OAUTH_RATE_LIMITS, rule, (2, 600))

    async def go(app):
        c = app.client()
        out = {}
        out["authorize"] = [(await c.get("/authorize", params={"client_id": "x"})).status_code
                            for _ in range(3)]
        out["token"] = [(await c.post("/token", data={"grant_type": "x"})).status_code
                        for _ in range(3)]
        out["revoke"] = [(await c.post("/revoke", data={"token": "t", "client_id": "c"})).status_code
                         for _ in range(3)]
        return out

    out = run_app(go)
    assert out["authorize"][2] == 429 and 429 not in out["authorize"][:2], out
    assert out["token"][2] == 429 and 429 not in out["token"][:2], out
    assert out["revoke"][2] == 429 and 429 not in out["revoke"][:2], out


def test_a_restart_does_not_refill_the_buckets(monkeypatch, stores):
    monkeypatch.setitem(srv.OAUTH_RATE_LIMITS, "register", (2, 3600))
    ip = "203.0.113.77"

    async def burn(app):
        c = app.client(ip)
        codes = [(await register(c)).status_code for _ in range(3)]
        await asyncio.to_thread(app.app.state.oauth_limiter.flush, True)
        return codes

    assert run_app(burn) == [201, 201, 429]
    ledger = Path(srv.BLACKBOX_MCP_OAUTH_RATE_LEDGER_FILE)
    assert ledger.exists() and ip not in ledger.read_text(), "the ledger holds hashes, never addresses"

    async def again(app):          # a new app = a restarted server
        return (await register(app.client(ip))).status_code

    assert run_app(again) == 429


def test_the_limiter_keeps_a_bounded_number_of_buckets(tmp_path):
    limiter = srv._RateLimiter({"register": (1, 3600)}, ledger_path=tmp_path / "l.json", max_keys=5)
    for i in range(50):
        assert limiter.take([("register", f"client-{i}")]) == 0.0
    limiter.flush(force=True)
    assert len(limiter._buckets) == 5
    assert len(json.loads((tmp_path / "l.json").read_text())) == 5


def test_a_corrupt_ledger_starts_empty(tmp_path):
    path = tmp_path / "l.json"
    path.write_text('{"register|x": [0, 1], "bogus": "nope", "register|y": ["a", 2]')
    limiter = srv._RateLimiter({"register": (1, 3600)}, ledger_path=path)
    limiter.load()
    assert limiter._buckets == {}
    path.write_text(json.dumps({"register|x": [0, time.time()], "nope|y": [0, 1],
                                "register|z": ["a", 2]}))
    limiter.load()
    assert list(limiter._buckets) == ["register|x"]
    assert limiter.take([("register", "x")]) > 0


def test_register_refuses_an_oversized_body(stores):
    async def go(app):
        c = app.client()
        big = {"redirect_uris": [CALLBACK], "client_name": "x", "pad": "y" * (17 * 1024)}
        declared = await c.post("/register", json=big)

        async def chunks():               # no Content-Length: streamed
            for _ in range(40):
                yield b" " * 1024
        streamed = await c.post("/register", content=chunks(),
                                headers={"Content-Type": "application/json"})
        return declared, streamed

    declared, streamed = run_app(go)
    assert declared.status_code == 413 and declared.json()["error"] == "invalid_request"
    assert streamed.status_code == 413
    assert _store(srv.BLACKBOX_MCP_OAUTH_CLIENTS_FILE) == {}


def test_the_token_endpoint_body_is_capped_too():
    async def go(app):
        return (await app.client().post("/token", data={"grant_type": "authorization_code",
                                                        "code": "c" * (20 * 1024)})).status_code

    assert run_app(go) == 413


def test_mcp_is_neither_rate_limited_nor_buffered(monkeypatch):
    for rule in srv.OAUTH_RATE_LIMITS:
        monkeypatch.setitem(srv.OAUTH_RATE_LIMITS, rule, (1, 3600))

    async def go(app):
        c = app.client()
        return [await mcp_status(c, "not-a-token") for _ in range(5)]

    assert run_app(go) == [401] * 5


# ===========================================================================
# (2) Dynamic client registration: bounded metadata, bounded store
# ===========================================================================
@pytest.mark.parametrize("body,field", [
    ({"redirect_uris": [CALLBACK] * 11}, "redirect"),
    ({"redirect_uris": ["https://claude.ai/" + "a" * 2100]}, "redirect"),
    ({"redirect_uris": ["javascript:alert(1)"]}, "redirect"),
    ({"redirect_uris": ["data:text/html,hi"]}, "redirect"),
    ({"redirect_uris": ["https://claude.ai/cb#frag"]}, "redirect"),
    ({"redirect_uris": ["https:///no-host"]}, "redirect"),
    ({"redirect_uris": ["relative/path"]}, "redirect"),
    ({"redirect_uris": [CALLBACK], "client_name": "n" * 300}, "client_name"),
    ({"redirect_uris": [CALLBACK], "client_name": 12}, "client_name"),
    ({"redirect_uris": [CALLBACK], "logo_uri": {"x": 1}}, "logo_uri"),
    ({"redirect_uris": [CALLBACK], "contacts": "a@example.com"}, "contacts"),
    ({"redirect_uris": [CALLBACK], "contacts": ["a"] * 11}, "contacts"),
])
def test_register_bounds_every_stored_field(body, field):
    async def go(app):
        return await app.client().post("/register", json={**body, "token_endpoint_auth_method": "none"})

    r = run_app(go)
    assert r.status_code == 400, r.text
    assert field.split("_")[0] in r.json()["error_description"] or field == "redirect"
    assert _store(srv.BLACKBOX_MCP_OAUTH_CLIENTS_FILE) == {}


def test_register_accepts_the_real_clients_shapes():
    async def go(app):
        c = app.client()
        return [await c.post("/register", json={
            "redirect_uris": uris, "client_name": "Claude", "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "scope": "claudeai"}) for uris in (
                [CALLBACK], ["http://localhost:33418/callback"], ["http://127.0.0.1:6274/oauth/callback"],
                ["cursor://anysphere.cursor-retrieval/oauth/callback"])]

    for r in run_app(go):
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["grant_types"] == ["authorization_code", "refresh_token"]
        assert "registered_from" not in body and "pad" not in body


def test_unknown_metadata_is_not_stored():
    async def go(app):
        return await register(app.client(), pad="z" * 1000, jwks={"keys": []})

    r = run_app(go)
    stored = _store(srv.BLACKBOX_MCP_OAUTH_CLIENTS_FILE)[r.json()["client_id"]]
    assert "pad" not in stored and "jwks" not in stored


def test_unfinished_registrations_per_address_are_capped(monkeypatch):
    monkeypatch.setattr(srv, "MAX_PENDING_CLIENTS_PER_ADDRESS", 3)

    async def go(app):
        a, b = app.client("203.0.113.5"), app.client("203.0.113.6")
        out = [(await register(a)).status_code for _ in range(4)]
        out.append((await register(b)).status_code)
        # Completing a flow frees a slot: the client is no longer unfinished.
        client_id = (await register(b)).json()["client_id"]
        _r, code, verifier = await authorize(b, client_id)
        await b.post("/token", data={"grant_type": "authorization_code", "code": code,
                                     "client_id": client_id, "redirect_uri": CALLBACK,
                                     "code_verifier": verifier})
        out.append((await register(b)).status_code)
        out.append((await register(b)).status_code)
        return out

    assert run_app(go) == [201, 201, 201, 429, 201, 201, 201]


def test_unfinished_registrations_expire_after_a_day(stores):
    old = int(time.time()) - srv.PENDING_CLIENT_TTL - 60
    Path(srv.BLACKBOX_MCP_OAUTH_CLIENTS_FILE).write_text(json.dumps({
        "stale-pending": {"client_id": "stale-pending", "client_id_issued_at": old,
                          "redirect_uris": [CALLBACK], "registered_from": "x"},
        "old-but-used": {"client_id": "old-but-used", "client_id_issued_at": old,
                         "redirect_uris": [CALLBACK], "registered_from": "x",
                         "last_token_at": old},
        "legacy": {"client_id": "legacy", "client_id_issued_at": old,
                   "redirect_uris": [CALLBACK]},
    }))

    async def go(app):
        return (await register(app.client())).json()["client_id"]

    new_id = run_app(go)
    assert set(_store(srv.BLACKBOX_MCP_OAUTH_CLIENTS_FILE)) == {"old-but-used", "legacy", new_id}


def test_a_full_store_drops_the_oldest_unfinished_registration(monkeypatch, stores):
    monkeypatch.setattr(srv, "MAX_OAUTH_CLIENTS", 3)
    now = int(time.time())
    clients = {
        "used-1": {"client_id_issued_at": now - 50, "registered_from": "x", "last_token_at": now},
        "pending-old": {"client_id_issued_at": now - 40, "registered_from": "x"},
        "pending-new": {"client_id_issued_at": now - 30, "registered_from": "y"},
    }
    Path(srv.BLACKBOX_MCP_OAUTH_CLIENTS_FILE).write_text(json.dumps(clients))

    async def go(app):
        return (await register(app.client())).json()["client_id"]

    new_id = run_app(go)
    assert set(_store(srv.BLACKBOX_MCP_OAUTH_CLIENTS_FILE)) == {"used-1", "pending-new", new_id}


def test_a_store_full_of_used_clients_refuses_new_ones(monkeypatch, stores):
    monkeypatch.setattr(srv, "MAX_OAUTH_CLIENTS", 2)
    now = int(time.time())
    Path(srv.BLACKBOX_MCP_OAUTH_CLIENTS_FILE).write_text(json.dumps({
        "a": {"client_id_issued_at": now, "last_token_at": now},
        "b": {"client_id_issued_at": now, "last_token_at": now}}))

    async def go(app):
        return await register(app.client())

    r = run_app(go)
    assert r.status_code == 503 and r.json()["error"] == "temporarily_unavailable"
    assert set(_store(srv.BLACKBOX_MCP_OAUTH_CLIENTS_FILE)) == {"a", "b"}


# ===========================================================================
# (3) Token comparison: hostile bytes are a 4xx, never a 500
# ===========================================================================
def test_a_non_ascii_bearer_is_a_401_not_a_500():
    async def go(app):
        c = app.client()
        out = []
        for raw in (b"Bearer \xe9t\xe9", b"Bearer caf\xc3\xa9", b"Bearer a\x01b", b"Bearer " + b"x" * 5000):
            r = await c.post("/mcp", json=_INIT, headers=[(b"authorization", raw),
                                                         (b"accept", _ACCEPT.encode())])
            out.append(r.status_code)
        return out

    assert run_app(go) == [401, 401, 401, 401]


def test_match_token_compares_utf8_bytes():
    assert srv._match_token("tökén", {"tökén": "alice"})[0] == "alice"
    assert srv._match_token("tökén", {"token": "alice"}) == (None, None)
    assert srv._match_token("\udcff", {"?": "alice"}) == (None, None)
    assert srv._match_token("ab\udcff", {"ab�": "alice"}) == (None, None)


def test_a_non_ascii_consent_token_rerenders_the_form():
    async def go(app):
        c = app.client()
        client_id = (await register(c)).json()["client_id"]
        r, code, _v = await authorize(c, client_id, token="tökén")
        return r, code

    r, code = run_app(go)
    assert r.status_code == 401 and code is None
    assert "blackbox_token" in r.text


def test_a_malformed_pkce_verifier_is_refused_cleanly():
    async def go(app):
        c = app.client()
        client_id = (await register(c)).json()["client_id"]
        out = []
        for verifier in ("é" * 50, "short", "v" * 200):
            _r, code, _v = await authorize(c, client_id)
            t = await c.post("/token", data={"grant_type": "authorization_code", "code": code,
                                             "client_id": client_id, "redirect_uri": CALLBACK,
                                             "code_verifier": verifier})
            out.append((t.status_code, t.json().get("error")))
        # JSON values that are not strings never reach the PKCE check.
        t = await c.post("/token", json={"grant_type": "authorization_code", "code": 7,
                                         "client_id": client_id, "code_verifier": ["x"]})
        out.append((t.status_code, t.json().get("error")))
        return out

    for status, error in run_app(go):
        assert status == 400 and error in ("invalid_grant", "invalid_request"), (status, error)


def test_a_malformed_code_challenge_is_refused_at_authorize():
    async def go(app):
        c = app.client()
        client_id = (await register(c)).json()["client_id"]
        r, code, _v = await authorize(c, client_id, challenge="é" * 43)
        r2, code2, _v = await authorize(c, client_id, challenge="abc")
        return r, code, r2, code2

    r, code, r2, code2 = run_app(go)
    for resp, c in ((r, code), (r2, code2)):
        assert resp.status_code == 302 and c is None
        assert "error=invalid_request" in resp.headers["location"]


# ===========================================================================
# (4) Refresh tokens
# ===========================================================================
def test_the_code_exchange_issues_a_rotating_refresh_token():
    async def go(app):
        c = app.client()
        client_id, t1 = await connect(c)
        r = await refresh(c, client_id, t1["refresh_token"])
        t2 = r.json()
        return t1, r.status_code, t2, await mcp_status(c, t1["access_token"]), \
            await mcp_status(c, t2["access_token"])

    t1, status, t2, old_access, new_access = run_app(go)
    assert t1["refresh_token"].startswith("bbmcp_ort_") and t1["token_type"] == "Bearer"
    assert status == 200 and t2["refresh_token"] != t1["refresh_token"]
    assert t2["access_token"] != t1["access_token"]
    assert new_access == 200 and old_access == 200


def test_a_replayed_refresh_token_revokes_its_family():
    async def go(app):
        c = app.client()
        client_id, t1 = await connect(c)
        t2 = (await refresh(c, client_id, t1["refresh_token"])).json()
        replay = await refresh(c, client_id, t1["refresh_token"])
        after = await refresh(c, client_id, t2["refresh_token"])
        return (replay, after, await mcp_status(c, t1["access_token"]),
                await mcp_status(c, t2["access_token"]))

    replay, after, a1, a2 = run_app(go)
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert after.status_code == 400, "the rotated-to token dies with its family"
    assert a1 == 401 and a2 == 401, "every access token of the family is revoked"


def test_a_refresh_token_is_bound_to_its_client():
    async def go(app):
        c = app.client()
        client_id, t1 = await connect(c)
        other_id = (await register(c)).json()["client_id"]
        stolen = await refresh(c, other_id, t1["refresh_token"])
        own = await refresh(c, client_id, t1["refresh_token"])
        return stolen, own

    stolen, own = run_app(go)
    assert stolen.status_code == 400 and stolen.json()["error"] == "invalid_grant"
    assert own.status_code == 200, "a wrong-client attempt neither spends nor revokes it"


def test_an_expired_refresh_token_is_refused(monkeypatch):
    async def go(app):
        c = app.client()
        client_id, t1 = await connect(c)
        store = _store(srv.BLACKBOX_MCP_OAUTH_REFRESH_FILE)
        for rec in store.values():
            rec["expiry"] = time.time() - 1
        Path(srv.BLACKBOX_MCP_OAUTH_REFRESH_FILE).write_text(json.dumps(store))
        return await refresh(c, client_id, t1["refresh_token"])

    r = run_app(go)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_revoking_the_consenting_bearer_ends_the_oauth_connection():
    async def go(app):
        c = app.client()
        client_id, t1 = await connect(c, token=TOKEN_A)
        before = await mcp_status(c, t1["access_token"])
        mw = srv._AUTH_MIDDLEWARE["instance"]
        mw.token_map.pop(TOKEN_A)          # what /internal/reload does after a revoke
        after = await mcp_status(c, t1["access_token"])
        r = await refresh(c, client_id, t1["refresh_token"])
        return before, after, r

    before, after, r = run_app(go)
    assert before == 200 and after == 401
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_a_bearer_rebound_to_another_operator_does_not_keep_the_old_grant():
    async def go(app):
        c = app.client()
        client_id, t1 = await connect(c, token=TOKEN_A)
        srv._AUTH_MIDDLEWARE["instance"].token_map[TOKEN_A] = OP_B
        return await mcp_status(c, t1["access_token"]), await refresh(c, client_id, t1["refresh_token"])

    status, r = run_app(go)
    assert status == 401 and r.status_code == 400


def test_refresh_tokens_are_stored_hashed():
    async def go(app):
        return await connect(app.client())

    _cid, t1 = run_app(go)
    raw = Path(srv.BLACKBOX_MCP_OAUTH_REFRESH_FILE).read_text()
    assert t1["refresh_token"] not in raw
    assert hashlib.sha256(t1["refresh_token"].encode()).hexdigest() in json.loads(raw)
    assert Path(srv.BLACKBOX_MCP_OAUTH_REFRESH_FILE).stat().st_mode & 0o077 == 0


def test_revoke_ends_the_family_and_confirms_nothing():
    async def go(app):
        c = app.client()
        client_id, t1 = await connect(c)
        foreign = await c.post("/revoke", data={"token": t1["refresh_token"], "client_id": "someone-else"})
        still = await mcp_status(c, t1["access_token"])
        unknown = await c.post("/revoke", data={"token": "bbmcp_ort_nope", "client_id": client_id})
        missing = await c.post("/revoke", data={"client_id": client_id})
        revoked = await c.post("/revoke", data={"token": t1["refresh_token"], "client_id": client_id})
        return (foreign.status_code, still, unknown.status_code, missing.status_code,
                revoked.status_code, await refresh(c, client_id, t1["refresh_token"]),
                await mcp_status(c, t1["access_token"]))

    foreign, still, unknown, missing, revoked, after_refresh, after_access = run_app(go)
    assert foreign == 200 and still == 200, "another client's revoke is a silent no-op"
    assert unknown == 200 and missing == 400 and revoked == 200
    assert after_refresh.status_code == 400 and after_access == 401


def test_revoke_an_access_token_alone():
    async def go(app):
        c = app.client()
        client_id, t1 = await connect(c)
        await c.post("/revoke", data={"token": t1["access_token"], "client_id": client_id})
        return await mcp_status(c, t1["access_token"]), await refresh(c, client_id, t1["refresh_token"])

    status, r = run_app(go)
    assert status == 401 and r.status_code == 200


def test_metadata_advertises_refresh_and_revocation(monkeypatch):
    monkeypatch.setattr(srv, "BLACKBOX_MCP_PUBLIC_URL", "https://box.example:8443")

    async def go(app):
        return (await app.client().get("/.well-known/oauth-authorization-server")).json()

    m = run_app(go)
    assert m["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert m["revocation_endpoint"] == "https://box.example:8443/revoke"
    assert m["revocation_endpoint_auth_methods_supported"] == ["none"]


def test_the_code_exchange_marks_the_client_used():
    async def go(app):
        return await connect(app.client())

    client_id, _t = run_app(go)
    assert "last_token_at" in _store(srv.BLACKBOX_MCP_OAUTH_CLIENTS_FILE)[client_id]


# ===========================================================================
# (5) The tool-call audit line names the CURRENT request
# ===========================================================================
class _Lines(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_tool_call_audit_line_names_the_current_request():
    import re
    lines = _Lines()
    srv.logger.addHandler(lines)
    level = srv.logger.level         # the logger is shared by every imported copy
    srv.logger.setLevel(logging.INFO)

    async def go(app):
        c = app.client()
        hdrs = {"Authorization": f"Bearer {TOKEN_A}", "Accept": _ACCEPT,
                "Content-Type": "application/json"}
        r = await c.post("/mcp", json=_INIT, headers=hdrs)
        hdrs["mcp-session-id"] = r.headers["mcp-session-id"]
        await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                     headers=hdrs)
        for i in (2, 3):
            await c.post("/mcp", json={"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                       "params": {"name": "get_current_operator", "arguments": {}}},
                         headers=hdrs)

    async def fake_ops():
        return [OP_A], OP_A

    real = srv._fetch_operators
    srv._fetch_operators = fake_ops
    try:
        run_app(go)
    finally:
        srv._fetch_operators = real
        srv.logger.removeHandler(lines)
        srv.logger.setLevel(level)
    auth = [re.search(r"rid=(\w+) AUDIT auth-ok kind=static tid=(\S+)", ln) for ln in lines.lines]
    auth = [m.groups() for m in auth if m]
    calls = [re.search(r"rid=(\w+) tool=get_current_operator .*AUDIT tool-call tid=(\S+)", ln)
             for ln in lines.lines]
    calls = [m.groups() for m in calls if m]
    assert len(calls) == 2, lines.lines
    assert calls[0][0] != calls[1][0], "each call has its own rid"
    assert all(call in auth for call in calls), (calls, auth)
    assert auth.index(calls[0]) >= 2, "not the session-opening request's rid"


# ===========================================================================
# (6) Resources
# ===========================================================================
_INDEX = {
    "SNAP-1": {"operator": "alice", "timestamp": "2026-10-01T00:00:00Z", "type": "normal",
               "byte_start": 0, "byte_end": 5},
    "SNAP-2": {"operator": "bob", "timestamp": "2026-10-02T00:00:00Z", "type": "checkpoint",
               "byte_start": 5, "byte_end": 10},
}


def _read(uri):
    from pydantic import AnyUrl
    handler = srv.server.request_handlers[mcp_types.ReadResourceRequest]
    req = mcp_types.ReadResourceRequest(method="resources/read",
                                        params=mcp_types.ReadResourceRequestParams(uri=AnyUrl(uri)))
    return asyncio.run(handler(req))


@pytest.mark.parametrize("uri,key", [
    ("blackbox://index/stats", "total_snapshots"),
    ("blackbox://index/operators", "operators"),
    ("blackbox://index/recent", "recent_snapshots"),
    ("blackbox://volume/info", "exists"),
])
def test_every_advertised_resource_reads(monkeypatch, uri, key):
    monkeypatch.setattr(srv, "load_snapshot_index", lambda force_refresh=False: _INDEX)
    result = _read(uri)
    contents = result.root.contents
    assert len(contents) == 1 and contents[0].mimeType == "application/json"
    assert str(contents[0].uri) == uri
    assert key in json.loads(contents[0].text)


def test_the_advertised_list_and_the_readable_set_agree(monkeypatch):
    monkeypatch.setattr(srv, "load_snapshot_index", lambda force_refresh=False: _INDEX)
    for resource in asyncio.run(srv.list_resources()):
        assert _read(str(resource.uri)).root.contents


def test_an_unknown_resource_is_resource_not_found():
    from mcp.shared.exceptions import McpError
    with pytest.raises(McpError) as err:
        asyncio.run(srv.read_resource("blackbox://nope"))
    assert err.value.error.code == -32002


# ===========================================================================
# (7) stdio: only the SDK writes to stdout
# ===========================================================================
def test_stdio_frames_go_to_the_real_stdout_and_strays_to_stderr(monkeypatch):
    protocol = io.BytesIO()
    fake_stdout = io.TextIOWrapper(protocol, encoding="utf-8", write_through=True)
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    seen = {}

    @contextlib.asynccontextmanager
    async def fake_stdio_server(stdin=None, stdout=None):
        seen["stdout"] = stdout
        yield object(), object()

    async def fake_run(read_stream, write_stream, options):
        print("[WEB_FETCH] a stray print from a tool")
        await seen["stdout"].write('{"jsonrpc": "2.0", "id": 1, "result": {}}\n')
        await seen["stdout"].flush()
        seen["bytes"] = protocol.getvalue()

    monkeypatch.setattr(srv, "stdio_server", fake_stdio_server)
    monkeypatch.setattr(srv.server, "run", fake_run)
    asyncio.run(srv.main())
    frames = seen["bytes"].decode("utf-8").splitlines()
    assert frames == ['{"jsonrpc": "2.0", "id": 1, "result": {}}'], frames
    assert "a stray print" in fake_stderr.getvalue()


def test_stdio_server_answers_with_json_rpc_only():
    """End to end over a real stdio subprocess: every stdout line is a frame."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.update(_ENV)
    env["BLACKBOX_URL"] = "http://127.0.0.1:9"      # nothing listens: no backend
    msgs = [_INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read",
             "params": {"uri": "blackbox://volume/info"}}]
    proc = subprocess.Popen([sys.executable, str(_HERE / "blackbox_mcp_server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=env, cwd=str(_HERE))
    by_id = {}
    try:
        for m in msgs:
            proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()
        deadline = time.time() + 120
        while not {1, 2, 3} <= set(by_id) and time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if line.strip():
                frame = json.loads(line)              # every stdout line is a frame
                by_id[frame.get("id")] = frame
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    assert len(by_id[2]["result"]["tools"]) > 0
    assert "exists" in json.loads(by_id[3]["result"]["contents"][0]["text"])


# ===========================================================================
# (8) Startup refuses an empty or broken catalog
# ===========================================================================
def test_startup_refuses_an_empty_catalog(monkeypatch):
    monkeypatch.setattr(srv, "get_mcp_tools", lambda: [])
    with pytest.raises(SystemExit):
        srv._require_tool_catalog()


def test_startup_refuses_a_catalog_that_fails_to_build(monkeypatch):
    def broken():
        raise ModuleNotFoundError("No module named 'fastapi'")
    monkeypatch.setattr(srv, "get_mcp_tools", broken)
    with pytest.raises(SystemExit):
        srv._require_tool_catalog()


def test_stdio_main_refuses_before_serving(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    monkeypatch.setattr(srv, "get_mcp_tools", lambda: [])
    entered = []

    @contextlib.asynccontextmanager
    async def fake_stdio_server(stdin=None, stdout=None):
        entered.append(True)
        yield object(), object()

    monkeypatch.setattr(srv, "stdio_server", fake_stdio_server)
    with pytest.raises(SystemExit):
        asyncio.run(srv.main())
    assert entered == []


def test_the_real_catalog_builds():
    assert srv._require_tool_catalog() > 0


# ===========================================================================
# (9) One tool_registry, imported normally; a lean import closure
# ===========================================================================
def test_tool_registry_is_imported_once_under_its_own_name():
    assert srv.get_mcp_tools.__module__ == "Orchestrator.tools.tool_registry"
    assert "blackbox_tool_registry" not in sys.modules


def test_the_server_import_closure_stays_lean():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.update(_ENV)
    code = (
        "import importlib.util, json, sys\n"
        f"sys.path.insert(0, {str(_HERE)!r})\n"
        f"spec = importlib.util.spec_from_file_location('bbmcp_closure', {str(_HERE / 'blackbox_mcp_server.py')!r})\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "assert len(mod.get_mcp_tools()) > 0\n"
        "print(json.dumps(sorted(sys.modules)))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=120, env=env, cwd=str(_HERE))
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = set(json.loads(out.stdout.strip().splitlines()[-1]))
    heavy = {"Orchestrator.tools.blackbox_tools", "Orchestrator.contacts",
             "Orchestrator.tool_outcomes", "Orchestrator.toolvault.injector",
             "Orchestrator.toolvault.embeddings", "Orchestrator.toolvault.meta_tool",
             "Orchestrator.config", "fastapi", "aiohttp", "blackbox_tool_registry"}
    assert not loaded & heavy, sorted(loaded & heavy)


# ===========================================================================
# (10) /internal/reload: not public; the control listener is loopback only
# ===========================================================================
def test_the_public_app_does_not_serve_internal_reload(stores):
    from Orchestrator.utils import internal_secret
    secret = internal_secret.ensure_secret(srv._reload_secret_path())

    async def go(app):
        c = app.client("127.0.0.1")       # a Funnel request arrives from loopback
        return (await c.post("/internal/reload",
                             headers={internal_secret.SECRET_HEADER: secret})).status_code

    assert run_app(go) == 404


def test_the_control_listener_reloads_on_loopback_only(monkeypatch, stores):
    from Orchestrator.utils import internal_secret
    secret = internal_secret.ensure_secret(srv._reload_secret_path())
    monkeypatch.setattr(srv, "BLACKBOX_MCP_HTTP_HOST", "0.0.0.0")
    monkeypatch.setattr(srv, "_load_runtime_public_url", lambda: "")

    class _MW:
        token_map = {"old": OP_A}

    monkeypatch.setitem(srv._AUTH_MIDDLEWARE, "instance", _MW())

    async def go():
        listener = await srv._start_control_listener(port=0)
        assert listener is not None
        try:
            host, port = listener.address
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
                ok = await c.post("/internal/reload", headers={internal_secret.SECRET_HEADER: secret})
                bad = await c.post("/internal/reload", headers={internal_secret.SECRET_HEADER: "x"})
                other = await c.post("/mcp")
            return host, ok.status_code, bad.status_code, other.status_code
        finally:
            await listener.stop()

    host, ok, bad, other = asyncio.run(go())
    assert host == "127.0.0.1"
    assert (ok, bad, other) == (200, 403, 404)
    assert _MW.token_map == {TOKEN_A: OP_A, TOKEN_B: OP_B}


def test_a_taken_control_port_is_logged_not_fatal():
    import socket
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    try:
        assert asyncio.run(srv._start_control_listener(port=holder.getsockname()[1])) is None
    finally:
        holder.close()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_tmp():
    yield
    shutil.rmtree(_TMP, ignore_errors=True)
