# Orchestrator/tests/test_gmail_oauth_transport.py
"""No process-wide OAUTHLIB_INSECURE_TRANSPORT (audit O12-messaging-22, M3 U20 item 8).

gmail_routes set OAUTHLIB_INSECURE_TRANSPORT=1 for the whole Orchestrator at
import time ("allow HTTP for local dev"). That switches off oauthlib's https
check for EVERY OAuth endpoint the process ever talks to, so a token or
authorization endpoint on plain http would be accepted silently.

The Gmail flow never needed it: oauthlib checks the transport of the
authorization and token ENDPOINTS (Google's, always https), and only checks
the redirect URI when an authorization_response URL is parsed -- the callback
hands Flow.fetch_token the code directly. These tests drive the REAL
google_auth_oauthlib Flow (only the token POST is faked) over plain-http
localhost and over the tailnet's https, with the variable unset.

Hermetic: the token POST is answered in-process; tokens go to a tmp dir.
"""
import json
import os
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from fastapi.testclient import TestClient
from requests_oauthlib import OAuth2Session

from Orchestrator import config
from Orchestrator.gmail import service
from Orchestrator.routes import gmail_routes


def test_importing_gmail_routes_leaves_oauthlib_transport_checks_on():
    import inspect
    assert os.environ.get("OAUTHLIB_INSECURE_TRANSPORT") in (None, "")
    assert 'os.environ["OAUTHLIB_INSECURE_TRANSPORT"]' not in inspect.getsource(gmail_routes)


@pytest.fixture
def real_flow(tmp_path, monkeypatch):
    import Orchestrator.app  # noqa: F401 — register routes like the other route tests
    monkeypatch.setattr(gmail_routes, "GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(gmail_routes, "GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(config, "USERS_LIST", ["Robin"])
    monkeypatch.setattr(service, "TOKEN_DIR", tmp_path / "gmail_tokens")
    monkeypatch.setattr(gmail_routes, "_pending_flows", {}, raising=False)
    posted = []

    def fake_request(self, method, url, *a, **kw):
        posted.append((method, url, kw.get("data")))
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp.request = requests.Request(method, url).prepare()
        resp.headers["Content-Type"] = "application/json"
        resp._content = json.dumps({
            "access_token": "access-1", "refresh_token": "refresh-1", "token_type": "Bearer",
            "expires_in": 3600, "scope": " ".join(service.SCOPES),
        }).encode()
        return resp

    monkeypatch.setattr(OAuth2Session, "request", fake_request)
    return posted


@pytest.mark.parametrize("base_url", ["http://localhost:9091", "https://box.tail1234.ts.net"])
def test_connect_flow_works_without_insecure_transport(real_flow, tmp_path, monkeypatch, base_url):
    monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
    client = TestClient(gmail_routes.app, base_url=base_url, follow_redirects=False)
    r = client.get("/auth/gmail/authorize", params={"operator": "Robin"})
    assert r.status_code in (302, 307), r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["redirect_uri"] == [f"{base_url}/auth/gmail/callback"]
    assert q["code_challenge_method"] == ["S256"]

    r = client.get("/auth/gmail/callback", params={"code": "code-1", "state": q["state"][0]})
    assert r.status_code == 200, r.text
    assert "Gmail connected for Robin" in r.text
    method, url, data = real_flow[0]
    assert (method, url) == ("POST", "https://oauth2.googleapis.com/token")
    assert data["code"] == "code-1" and data["code_verifier"]           # PKCE verifier restored
    assert data["redirect_uri"] == f"{base_url}/auth/gmail/callback"
    assert (tmp_path / "gmail_tokens").exists() and any((tmp_path / "gmail_tokens").iterdir())


def test_a_plain_http_token_endpoint_is_refused_again(real_flow):
    """With the process-wide switch gone (the process env as gmail_routes left
    it, deliberately not cleaned here), oauthlib refuses an http endpoint."""
    from google_auth_oauthlib.flow import Flow
    from oauthlib.oauth2.rfc6749.errors import InsecureTransportError

    cfg = gmail_routes._client_config()
    cfg["web"]["token_uri"] = "http://token.example/token"
    flow = Flow.from_client_config(cfg, scopes=service.SCOPES,
                                   redirect_uri="http://localhost:9091/auth/gmail/callback")
    flow.code_verifier = "v" * 64
    with pytest.raises(InsecureTransportError):
        flow.fetch_token(code="code-1")
    assert real_flow == []
