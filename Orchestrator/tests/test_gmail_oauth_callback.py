"""Gmail OAuth authorize/callback hardening (audit X03-security-backend-05, O12-messaging-14,
O12-messaging-06).

The old callback reflected the ``error`` query parameter, the token-exchange
exception text and the operator name into HTML unescaped (reflected XSS on the
BlackBox origin), and its ``state`` was ``base64({"operator": ...})`` — predictable,
never required to match a pending flow, and identical for two concurrent
authorizations of the same operator (the second clobbered the first's PKCE
verifier). A missing operator silently defaulted to a hard-coded name.

Fixed contract pinned here:
  * authorize REQUIRES a known operator;
  * state is ``secrets.token_urlsafe(32)``, mapped server-side to
    {operator, code_verifier, redirect_uri, created_at}, single-use, 10-minute TTL;
  * the callback rejects unknown / forged / expired / replayed state with 400 and
    saves nothing;
  * every value interpolated into callback HTML is escaped, and the page carries a
    script-free CSP.

Hermetic: Google's Flow is replaced by a fake; the token store is a tmp dir.
"""

import base64
import json
import stat
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import google_auth_oauthlib.flow as oauth_flow
import pytest
from fastapi.testclient import TestClient

from Orchestrator import config
from Orchestrator.gmail import service
from Orchestrator.routes import gmail_routes


EVIL_OPERATOR = "<img src=x onerror=alert(3)>"


class _FakeCreds:
    token = "access-1"
    refresh_token = "refresh-1"
    token_uri = "https://oauth2.googleapis.com/token"
    client_id = "test-client-id"
    client_secret = "test-client-secret"
    scopes = list(service.SCOPES)
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)


class _FakeFlow:
    instances = []
    fail_with = None

    def __init__(self, redirect_uri):
        self.redirect_uri = redirect_uri
        self.code_verifier = f"verifier-{len(_FakeFlow.instances)}"
        self.credentials = _FakeCreds()
        self.fetched = None
        _FakeFlow.instances.append(self)

    @classmethod
    def from_client_config(cls, client_config, scopes, redirect_uri):
        return cls(redirect_uri)

    def authorization_url(self, **kwargs):
        state = kwargs["state"]
        return f"https://accounts.google.com/o/oauth2/auth?state={state}", state

    def fetch_token(self, code):
        if _FakeFlow.fail_with is not None:
            raise ValueError(_FakeFlow.fail_with)
        self.fetched = (code, self.code_verifier)


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Register every route module (and its middleware) on the shared app BEFORE
    # the TestClient starts it, like the other route tests do.
    import Orchestrator.app  # noqa: F401
    _FakeFlow.instances = []
    _FakeFlow.fail_with = None
    monkeypatch.setattr(oauth_flow, "Flow", _FakeFlow)
    monkeypatch.setattr(gmail_routes, "GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(gmail_routes, "GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(config, "USERS_LIST", ["Robin", "Bob", EVIL_OPERATOR])
    monkeypatch.setattr(service, "TOKEN_DIR", tmp_path / "gmail_tokens")
    monkeypatch.setattr(gmail_routes, "_pending_flows", {}, raising=False)
    return TestClient(gmail_routes.app, follow_redirects=False)


def _authorize(client, operator):
    resp = client.get("/auth/gmail/authorize", params={"operator": operator})
    assert resp.status_code in (302, 307), resp.text
    return parse_qs(urlparse(resp.headers["location"]).query)["state"][0]


def _token_files(tmp_path):
    d = tmp_path / "gmail_tokens"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


# ---------------------------------------------------------------------------
# authorize
# ---------------------------------------------------------------------------

def test_authorize_requires_an_operator(client):
    for params in ({}, {"operator": ""}, {"operator": "   "}):
        resp = client.get("/auth/gmail/authorize", params=params)
        assert resp.status_code == 400, (params, resp.status_code)
    assert _FakeFlow.instances == []


def test_authorize_rejects_an_unknown_operator(client):
    resp = client.get("/auth/gmail/authorize", params={"operator": "Mallory"})
    assert resp.status_code == 400
    assert _FakeFlow.instances == []


def test_state_is_random_and_held_server_side(client):
    s1 = _authorize(client, "Robin")
    s2 = _authorize(client, "Robin")

    assert s1 != s2, "two authorizations of one operator must not share a state"
    for s in (s1, s2):
        assert len(s) >= 43  # secrets.token_urlsafe(32)
        try:
            decoded = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        except Exception:
            decoded = b""
        assert b"Robin" not in decoded, "state must not encode the operator"
    pending = gmail_routes._pending_flows
    assert pending[s1]["operator"] == "Robin"
    assert pending[s1]["code_verifier"] == "verifier-0"
    assert pending[s2]["code_verifier"] == "verifier-1"
    assert "created_at" in pending[s1]


# ---------------------------------------------------------------------------
# callback — state validation
# ---------------------------------------------------------------------------

def test_callback_rejects_an_unknown_state(client, tmp_path):
    resp = client.get("/auth/gmail/callback", params={"code": "c", "state": "forged"})
    assert resp.status_code == 400
    assert _token_files(tmp_path) == []


def test_callback_rejects_the_old_predictable_state_format(client, tmp_path):
    legacy = base64.urlsafe_b64encode(json.dumps({"operator": "Robin"}).encode()).decode()
    resp = client.get("/auth/gmail/callback", params={"code": "c", "state": legacy})
    assert resp.status_code == 400
    assert _token_files(tmp_path) == []


def test_callback_state_is_single_use(client, tmp_path):
    state = _authorize(client, "Robin")
    first = client.get("/auth/gmail/callback", params={"code": "c", "state": state})
    assert first.status_code == 200, first.text
    assert _token_files(tmp_path) == ["Robin.json"]

    (tmp_path / "gmail_tokens" / "Robin.json").unlink()
    replay = client.get("/auth/gmail/callback", params={"code": "c2", "state": state})
    assert replay.status_code == 400
    assert _token_files(tmp_path) == []


def test_callback_state_expires_after_ten_minutes(client, tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(gmail_routes, "_now", lambda: clock["t"], raising=False)
    state = _authorize(client, "Robin")

    clock["t"] += 10 * 60 + 1
    resp = client.get("/auth/gmail/callback", params={"code": "c", "state": state})
    assert resp.status_code == 400
    assert _token_files(tmp_path) == []
    assert state not in gmail_routes._pending_flows


def test_unexpired_state_within_ttl_still_works(client, tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(gmail_routes, "_now", lambda: clock["t"], raising=False)
    state = _authorize(client, "Robin")

    clock["t"] += 9 * 60
    resp = client.get("/auth/gmail/callback", params={"code": "c", "state": state})
    assert resp.status_code == 200


def test_concurrent_flows_keep_their_own_pkce_verifier(client):
    s_first = _authorize(client, "Robin")
    _authorize(client, "Robin")

    resp = client.get("/auth/gmail/callback", params={"code": "code-A", "state": s_first})
    assert resp.status_code == 200
    exchanging = _FakeFlow.instances[-1]
    assert exchanging.fetched == ("code-A", "verifier-0")


def test_a_google_error_consumes_the_state(client, tmp_path):
    state = _authorize(client, "Robin")
    denied = client.get("/auth/gmail/callback", params={"error": "access_denied", "state": state})
    assert denied.status_code == 400

    later = client.get("/auth/gmail/callback", params={"code": "c", "state": state})
    assert later.status_code == 400
    assert _token_files(tmp_path) == []


# ---------------------------------------------------------------------------
# callback — escaping
# ---------------------------------------------------------------------------

def _assert_inert(resp, raw):
    assert raw not in resp.text, "a reflected value reached the page unescaped"
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "script-src" not in csp or "'none'" in csp


def test_callback_error_parameter_is_escaped(client):
    raw = "<script>alert(1)</script>"
    resp = client.get("/auth/gmail/callback", params={"error": raw})
    assert resp.status_code == 400
    _assert_inert(resp, raw)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text


def test_token_exchange_error_is_escaped(client, tmp_path):
    raw = "<script>alert(2)</script>"
    state = _authorize(client, "Robin")
    _FakeFlow.fail_with = raw

    resp = client.get("/auth/gmail/callback", params={"code": "c", "state": state})
    assert resp.status_code == 500
    _assert_inert(resp, raw)
    assert _token_files(tmp_path) == []


def test_success_page_escapes_the_operator(client):
    state = _authorize(client, EVIL_OPERATOR)
    resp = client.get("/auth/gmail/callback", params={"code": "c", "state": state})
    assert resp.status_code == 200
    _assert_inert(resp, EVIL_OPERATOR)
    assert "&lt;img src=x onerror=alert(3)&gt;" in resp.text


# ---------------------------------------------------------------------------
# callback — what gets saved
# ---------------------------------------------------------------------------

def test_success_saves_an_owner_only_token_with_expiry_and_no_client_secret(client, tmp_path):
    state = _authorize(client, "Robin")
    resp = client.get("/auth/gmail/callback", params={"code": "c", "state": state})
    assert resp.status_code == 200

    path = tmp_path / "gmail_tokens" / "Robin.json"
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "access-1"
    assert saved["refresh_token"] == "refresh-1"
    assert saved["expiry"].endswith("Z")
    assert "client_secret" not in saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
