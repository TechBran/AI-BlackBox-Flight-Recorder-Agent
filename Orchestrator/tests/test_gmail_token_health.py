"""Dead Google tokens are reported honestly (audit W15, O12-messaging-15, O12-messaging-05).

Before this, a revoked/expired grant looked healthy everywhere:

  * ``_get_credentials`` built ``Credentials`` WITHOUT ``expiry``. google-auth
    treats a missing expiry as "never expires", so the refresh-and-save branch
    never ran — every call after the first hour rode the transport's silent
    401-refresh, and a dead grant was only discovered deep inside an API call.
  * ``GET /gmail/status/{operator}`` derived ``connected`` from token-file
    presence and swallowed the failing getProfile probe, so an operator whose
    token had been ``invalid_grant`` for weeks still read "Connected".
  * Token files were written 0644 with a plain write_text (not atomic).

These tests pin the fixed contract. Everything is hermetic: the token store is a
tmp dir, Google's token endpoint is replaced by a monkeypatched
``Credentials.refresh``, and the Gmail client by a fake ``build``.
"""

import asyncio
import json
import stat
from datetime import datetime, timedelta, timezone

import httplib2
import pytest
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError

from Orchestrator.gmail import service


def _naive_utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(dt):
    return _naive_utc(dt).isoformat() + "Z"


NOW = datetime.now(timezone.utc)


@pytest.fixture
def token_dir(tmp_path, monkeypatch):
    d = tmp_path / "gmail_tokens"
    monkeypatch.setattr(service, "TOKEN_DIR", d)
    monkeypatch.setattr(service, "GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(service, "GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    return d


def _write_token(d, operator, **fields):
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "access_token": "old-access",
        "refresh_token": "refresh-1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    data.update(fields)
    (d / f"{operator}.json").write_text(json.dumps(data))
    return data


def _read_token(d, operator):
    return json.loads((d / f"{operator}.json").read_text())


@pytest.fixture
def refresh_ok(monkeypatch):
    """Google's token endpoint, answering with a fresh one-hour access token."""
    calls = []

    def fake_refresh(self, request):
        calls.append(self.refresh_token)
        self.token = "new-access"
        self.expiry = _naive_utc(datetime.now(timezone.utc) + timedelta(hours=1))

    monkeypatch.setattr(service.Credentials, "refresh", fake_refresh)
    return calls


def _refresh_raising(monkeypatch, exc):
    def fake_refresh(self, request):
        raise exc

    monkeypatch.setattr(service.Credentials, "refresh", fake_refresh)


def _invalid_grant():
    # The exact shape google-auth raised on the live box (journal, 2026-10-12).
    return RefreshError(
        "invalid_grant: Bad Request",
        {"error": "invalid_grant", "error_description": "Bad Request"},
    )


# ---------------------------------------------------------------------------
# Credentials know their expiry, refresh when due, and persist the refresh
# ---------------------------------------------------------------------------

def test_credentials_carry_the_stored_expiry(token_dir, refresh_ok):
    expiry = NOW + timedelta(minutes=30)
    _write_token(token_dir, "Robin", expiry=_iso(expiry))

    creds = service._get_credentials("Robin")

    assert creds.expiry == _naive_utc(expiry)
    assert creds.expired is False
    assert refresh_ok == [], "a token that is still valid must not be refreshed"


def test_expired_token_is_refreshed_and_the_refresh_is_persisted(token_dir, refresh_ok):
    _write_token(token_dir, "Robin", expiry=_iso(NOW - timedelta(hours=1)))

    creds = service._get_credentials("Robin")

    assert refresh_ok == ["refresh-1"]
    assert creds.token == "new-access"
    saved = _read_token(token_dir, "Robin")
    assert saved["access_token"] == "new-access"
    assert saved["refresh_token"] == "refresh-1"
    # The new expiry is on disk, so the NEXT call knows when to refresh again.
    stored = datetime.fromisoformat(saved["expiry"].replace("Z", "+00:00"))
    assert stored > NOW + timedelta(minutes=50)


def test_legacy_token_without_expiry_refreshes_once_and_records_it(token_dir, refresh_ok):
    # Token files written before expiry was persisted (every file on a box
    # upgraded from the old code) — including the old client_secret copy.
    _write_token(token_dir, "Robin", client_id="old-id", client_secret="old-secret")

    service._get_credentials("Robin")
    assert refresh_ok == ["refresh-1"]
    saved = _read_token(token_dir, "Robin")
    assert saved.get("expiry"), "expiry must be recorded after the first refresh"
    # Client credentials come from config at load time; the token file must not
    # keep a copy of the OAuth client secret.
    assert "client_secret" not in saved
    assert "client_id" not in saved

    service._get_credentials("Robin")
    assert refresh_ok == ["refresh-1"], "the recorded expiry stops a second refresh"


def test_revoked_grant_raises_reauth_required(token_dir, monkeypatch):
    original = _write_token(token_dir, "Robin", expiry=_iso(NOW - timedelta(minutes=5)))
    _refresh_raising(monkeypatch, _invalid_grant())

    with pytest.raises(service.GoogleReauthRequired) as info:
        service._get_credentials("Robin")

    err = info.value
    assert isinstance(err, RefreshError), "existing RefreshError handlers must still catch it"
    assert err.code == "invalid_grant"
    assert err.operator == "Robin"
    assert "Reconnect" in str(err) and "Robin" in str(err)
    # The dead token is left in place so status can say "reconnect needed".
    assert _read_token(token_dir, "Robin") == original


def test_transient_refresh_failure_is_not_reported_as_revoked(token_dir, monkeypatch):
    _write_token(token_dir, "Robin", expiry=_iso(NOW - timedelta(minutes=5)))
    _refresh_raising(monkeypatch, RefreshError("server busy", retryable=True))

    with pytest.raises(RefreshError) as info:
        service._get_credentials("Robin")
    assert not isinstance(info.value, service.GoogleReauthRequired)


# ---------------------------------------------------------------------------
# The token store is owner-only and written atomically
# ---------------------------------------------------------------------------

def test_save_tokens_is_atomic_and_owner_only(token_dir):
    service.save_tokens("Robin", {"access_token": "a", "refresh_token": "r"})
    service.save_tokens("Robin", {"access_token": "b", "refresh_token": "r"})

    path = token_dir / "Robin.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_dir.stat().st_mode) == 0o700
    assert _read_token(token_dir, "Robin")["access_token"] == "b"
    assert sorted(p.name for p in token_dir.iterdir()) == ["Robin.json"], "no temp files left behind"


def test_save_tokens_tightens_a_world_readable_legacy_file(token_dir):
    _write_token(token_dir, "Robin")
    (token_dir / "Robin.json").chmod(0o644)
    token_dir.chmod(0o777)

    service.save_tokens("Robin", {"access_token": "a", "refresh_token": "r"})

    assert stat.S_IMODE((token_dir / "Robin.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(token_dir.stat().st_mode) == 0o700


# ---------------------------------------------------------------------------
# GET /gmail/status/{operator}
# ---------------------------------------------------------------------------

class _FakeGmail:
    """users().getProfile(userId="me").execute() -> profile, or raise."""

    def __init__(self, outcome):
        self._outcome = outcome

    def users(self):
        return self

    def getProfile(self, userId):
        assert userId == "me"
        return self

    def execute(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


@pytest.fixture
def fake_gmail(monkeypatch):
    def install(outcome):
        monkeypatch.setattr(
            service, "build",
            lambda api, version, credentials=None, cache_discovery=None: _FakeGmail(outcome),
        )
    return install


def _status(operator):
    from Orchestrator.routes import gmail_routes
    resp = asyncio.run(gmail_routes.gmail_status(operator))
    return json.loads(resp.body)


def _http_error(status):
    return HttpError(httplib2.Response({"status": str(status)}), b'{"error": {"message": "nope"}}')


def test_status_reports_invalid_grant_as_needs_reauth(token_dir, fake_gmail, refresh_ok):
    # The live-box shape: the access token still LOOKS valid by its expiry, the
    # API call 401s, the transport's refresh hits invalid_grant.
    _write_token(token_dir, "Robin", expiry=_iso(NOW + timedelta(minutes=30)))
    fake_gmail(_invalid_grant())

    body = _status("Robin")

    assert body["connected"] is False
    assert body["needs_reauth"] is True
    assert body["state"] == "needs_reauth"
    assert body["error"] == "invalid_grant"
    assert body["reauth_url"] == "/auth/gmail/authorize?operator=Robin"
    assert body["operator"] == "Robin"
    assert body["email"] == ""


def test_status_reports_a_grant_that_fails_its_scheduled_refresh(token_dir, fake_gmail, monkeypatch):
    _write_token(token_dir, "Robin", expiry=_iso(NOW - timedelta(hours=2)))
    _refresh_raising(monkeypatch, _invalid_grant())
    fake_gmail(AssertionError("the API must not be called with a dead grant"))

    body = _status("Robin")

    assert body["connected"] is False
    assert body["needs_reauth"] is True
    assert body["error"] == "invalid_grant"
    assert body["reauth_url"] == "/auth/gmail/authorize?operator=Robin"


def test_status_treats_http_401_as_needs_reauth(token_dir, fake_gmail, refresh_ok):
    _write_token(token_dir, "Robin", expiry=_iso(NOW + timedelta(minutes=30)))
    fake_gmail(_http_error(401))

    body = _status("Robin")
    assert body["connected"] is False
    assert body["needs_reauth"] is True
    assert body["error"] == "unauthorized"


def test_status_for_a_healthy_token_is_unchanged(token_dir, fake_gmail, refresh_ok):
    _write_token(token_dir, "Robin", expiry=_iso(NOW + timedelta(minutes=30)))
    fake_gmail({"emailAddress": "robin@example.com"})

    body = _status("Robin")

    assert body["connected"] is True
    assert body["email"] == "robin@example.com"
    assert body["operator"] == "Robin"
    assert body["state"] == "connected"
    assert body["needs_reauth"] is False
    assert body["reauth_url"] is None
    assert body["error"] is None


def test_status_network_failure_is_unverified_not_dead(token_dir, fake_gmail, refresh_ok):
    # Can't reach Google is NOT proof the grant is dead: don't push a re-sign-in.
    _write_token(token_dir, "Robin", expiry=_iso(NOW + timedelta(minutes=30)))
    fake_gmail(TransportError("connection reset"))

    body = _status("Robin")
    assert body["connected"] is True
    assert body["state"] == "unverified"
    assert body["needs_reauth"] is False
    assert body["reauth_url"] is None
    assert body["error"] == "probe_failed"


def test_status_without_a_token_is_not_connected(token_dir, fake_gmail):
    fake_gmail(AssertionError("no token -> no API call"))

    body = _status("Nobody")
    assert body["connected"] is False
    assert body["state"] == "not_connected"
    assert body["needs_reauth"] is False
    assert body["reauth_url"] is None


def test_status_reauth_url_encodes_the_operator(token_dir, fake_gmail, refresh_ok):
    _write_token(token_dir, "Flight Recorder", expiry=_iso(NOW + timedelta(minutes=30)))
    fake_gmail(_invalid_grant())

    body = _status("Flight Recorder")
    assert body["reauth_url"] == "/auth/gmail/authorize?operator=Flight%20Recorder"


# ---------------------------------------------------------------------------
# A 403 is a dead grant only when Google says the token lacks a scope
# ---------------------------------------------------------------------------

def _http_error_body(status, body):
    return HttpError(httplib2.Response({"status": str(status)}), json.dumps(body).encode())


RATE_LIMIT_403 = {"error": {
    "code": 403, "message": "User Rate Limit Exceeded", "status": "PERMISSION_DENIED",
    "errors": [{"domain": "usageLimits", "reason": "userRateLimitExceeded",
                "message": "User Rate Limit Exceeded"}],
}}
GLOBAL_RATE_LIMIT_403 = {"error": {
    "code": 403, "message": "Rate Limit Exceeded",
    "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}],
}}
DAILY_LIMIT_403 = {"error": {
    "code": 403, "message": "Daily Limit Exceeded",
    "errors": [{"domain": "usageLimits", "reason": "dailyLimitExceeded"}],
}}
QUOTA_403 = {"error": {
    "code": 403, "message": "Quota exceeded for quota metric 'Queries'.", "status": "PERMISSION_DENIED",
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                 "reason": "RATE_LIMIT_EXCEEDED", "domain": "googleapis.com"}],
}}
SCOPE_403 = {"error": {
    "code": 403, "message": "Request had insufficient authentication scopes.",
    "status": "PERMISSION_DENIED",
    "errors": [{"message": "Insufficient Permission", "domain": "global",
                "reason": "insufficientPermissions"}],
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                 "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "domain": "googleapis.com"}],
}}


@pytest.mark.parametrize("body", [RATE_LIMIT_403, GLOBAL_RATE_LIMIT_403, DAILY_LIMIT_403,
                                  QUOTA_403, {"error": {"code": 403, "message": "nope"}}])
def test_a_403_that_is_not_about_scopes_is_not_a_dead_grant(body):
    assert service.reauth_error_code(_http_error_body(403, body)) is None


@pytest.mark.parametrize("body", [
    SCOPE_403,
    {"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}},
    {"error": {"code": 403, "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}},
])
def test_a_403_for_missing_scopes_is_a_dead_grant(body):
    assert service.reauth_error_code(_http_error_body(403, body)) == "insufficient_scope"


def test_status_for_a_rate_limited_probe_is_unverified_not_dead(token_dir, fake_gmail, refresh_ok):
    # A quota blip must not push the operator to sign in again.
    _write_token(token_dir, "Robin", expiry=_iso(NOW + timedelta(minutes=30)))
    fake_gmail(_http_error_body(403, RATE_LIMIT_403))

    body = _status("Robin")
    assert body["connected"] is True
    assert body["state"] == "unverified"
    assert body["needs_reauth"] is False
    assert body["reauth_url"] is None


def test_status_for_a_scope_403_needs_reauth(token_dir, fake_gmail, refresh_ok):
    _write_token(token_dir, "Robin", expiry=_iso(NOW + timedelta(minutes=30)))
    fake_gmail(_http_error_body(403, SCOPE_403))

    body = _status("Robin")
    assert body["connected"] is False
    assert body["needs_reauth"] is True
    assert body["error"] == "insufficient_scope"


def test_a_gmail_tool_rate_limit_error_carries_the_real_error_not_reconnect():
    text = service._api_error("Search failed", "Robin", _http_error_body(403, RATE_LIMIT_403))
    assert "Reconnect" not in text
    assert "Rate Limit" in text


# ---------------------------------------------------------------------------
# Disconnect works for the token the status calls unreadable
# ---------------------------------------------------------------------------

def test_disconnect_removes_an_unreadable_token_file(token_dir):
    from Orchestrator.routes import gmail_routes

    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "Robin.json").write_text("{not json")
    assert _status("Robin")["error"] == "token_unreadable"

    resp = asyncio.run(gmail_routes.gmail_disconnect("Robin"))

    assert resp.status_code == 200
    assert json.loads(resp.body)["disconnected"] is True
    assert not (token_dir / "Robin.json").exists()
    assert _status("Robin")["state"] == "not_connected"


# ---------------------------------------------------------------------------
# The token store lives under the project root, whatever the process CWD
# ---------------------------------------------------------------------------

def test_token_store_resolves_under_the_project_root_not_the_cwd(tmp_path, monkeypatch):
    from Orchestrator.utils import paths

    root = tmp_path / "box"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(service, "TOKEN_DIR", None)  # the default: resolved on use
    monkeypatch.setenv("BLACKBOX_ROOT", str(root))
    monkeypatch.chdir(elsewhere)
    paths.blackbox_root.cache_clear()
    try:
        assert service._token_path("Robin") == root / "Manifest" / "gmail_tokens" / "Robin.json"
        service.save_tokens("Robin", {"refresh_token": "r"})
        assert (root / "Manifest" / "gmail_tokens" / "Robin.json").exists()
        assert not (elsewhere / "Manifest").exists()
    finally:
        paths.blackbox_root.cache_clear()
