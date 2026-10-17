"""The Vertex reranker refuses a GOOGLE_APPLICATION_CREDENTIALS file that is
not a Google service-account key (U3 review).

The credentials upload now only accepts service-account keys, but a file that
an older build accepted may already sit at GOOGLE_APPLICATION_CREDENTIALS.
google.auth.default() honours external_account configs there, which read a
local file (e.g. .env) and POST it to a token_url chosen by whoever wrote the
config. rerank._vertex_token_and_project is the only google.auth.default()
caller, so it re-checks the file before handing it to google-auth.

Hermetic: google.auth.default is replaced by a recorder; files live in tmp_path.
"""
from __future__ import annotations

import json

import pytest

from Orchestrator import rerank

SA = {"type": "service_account", "client_email": "svc@example.iam.gserviceaccount.com",
      "project_id": "example-project", "private_key": "test-placeholder-not-a-key",
      "token_uri": "https://oauth2.googleapis.com/token"}


class _Creds:
    token = "tok"

    def refresh(self, request):
        pass


@pytest.fixture
def google_default(monkeypatch):
    import google.auth
    calls = []

    def _default(*a, **k):
        calls.append(k)
        return _Creds(), "example-project"
    monkeypatch.setattr(google.auth, "default", _default)
    return calls


def _gac(monkeypatch, tmp_path, payload) -> None:
    path = tmp_path / "creds.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))


@pytest.mark.parametrize("payload", [
    {"type": "external_account", "token_url": "https://collector.example/token",
     "credential_source": {"file": "/srv/blackbox/.env"}},
    {"type": "authorized_user", "client_id": "x", "client_secret": "y", "refresh_token": "z"},
    {**SA, "token_uri": "http://127.0.0.1:9091/x"},
    [1, 2],
    "not json at all",
])
def test_vertex_refuses_a_non_service_account_gac_file(monkeypatch, tmp_path, google_default, payload):
    _gac(monkeypatch, tmp_path, payload)
    assert rerank._vertex_token_and_project({}) == (None, None)
    assert google_default == [], "google-auth must never see a refused credentials file"


def test_vertex_refuses_a_missing_gac_file(monkeypatch, tmp_path, google_default):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "gone.json"))
    assert rerank._vertex_token_and_project({}) == (None, None)
    assert google_default == []


def test_vertex_still_uses_a_real_service_account(monkeypatch, tmp_path, google_default):
    monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
    _gac(monkeypatch, tmp_path, SA)
    assert rerank._vertex_token_and_project({}) == ("tok", "example-project")
    assert len(google_default) == 1


def test_vertex_without_gac_still_defers_to_google_auth(monkeypatch, google_default):
    """No GOOGLE_APPLICATION_CREDENTIALS: google-auth's own lookup is unchanged."""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
    assert rerank._vertex_token_and_project({}) == ("tok", "example-project")
