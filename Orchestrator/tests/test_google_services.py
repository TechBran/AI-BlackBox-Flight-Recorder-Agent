"""Tests for the shared Google OAuth core + per-API service builders.

Task 1 of the Google Workspace integration: the same Gmail sign-in now also
grants Docs/Sheets/Slides/Drive/Calendar, and per-API service builders share a
single `_get_credentials` helper.
"""

import pytest

from Orchestrator.gmail import service


# --- SCOPES ----------------------------------------------------------------

def test_gmail_scopes_retained():
    """The two original Gmail scopes must still be present."""
    assert "https://www.googleapis.com/auth/gmail.readonly" in service.SCOPES
    assert "https://www.googleapis.com/auth/gmail.send" in service.SCOPES


def test_workspace_scopes_present():
    """The five new Workspace scopes are granted by the same sign-in."""
    for scope in (
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/presentations",
        "https://www.googleapis.com/auth/calendar",
    ):
        assert scope in service.SCOPES, f"missing scope: {scope}"


# --- _get_credentials ------------------------------------------------------

def test_get_credentials_none_when_no_token(monkeypatch):
    """Returns None when no token file exists for the operator."""
    monkeypatch.setattr(service, "load_tokens", lambda op: None)
    assert service._get_credentials("nobody") is None


def test_get_credentials_none_when_no_refresh_token(monkeypatch):
    """Returns None when the stored token lacks a refresh_token."""
    monkeypatch.setattr(service, "load_tokens", lambda op: {"access_token": "a"})
    assert service._get_credentials("partial") is None


# --- per-API service builders ----------------------------------------------

@pytest.fixture
def build_recorder(monkeypatch):
    """Stub _get_credentials with a sentinel and record build() calls."""
    sentinel = object()
    monkeypatch.setattr(service, "_get_credentials", lambda op: sentinel)

    calls = []

    def fake_build(api, version, credentials=None, cache_discovery=None):
        calls.append({
            "api": api,
            "version": version,
            "credentials": credentials,
            "cache_discovery": cache_discovery,
        })
        return ("BUILT", api, version)

    monkeypatch.setattr(service, "build", fake_build)
    return calls, sentinel


@pytest.mark.parametrize("builder,api,version", [
    ("get_gmail_service", "gmail", "v1"),
    ("get_docs_service", "docs", "v1"),
    ("get_sheets_service", "sheets", "v4"),
    ("get_slides_service", "slides", "v1"),
    ("get_drive_service", "drive", "v3"),
    ("get_calendar_service", "calendar", "v3"),
])
def test_service_builders(build_recorder, builder, api, version):
    """Each builder calls build() with the right api/version and shared creds."""
    calls, sentinel = build_recorder
    result = getattr(service, builder)("op")

    assert result == ("BUILT", api, version)
    assert len(calls) == 1
    call = calls[0]
    assert call["api"] == api
    assert call["version"] == version
    assert call["credentials"] is sentinel
    assert call["cache_discovery"] is False


@pytest.mark.parametrize("builder", [
    "get_gmail_service",
    "get_docs_service",
    "get_sheets_service",
    "get_slides_service",
    "get_drive_service",
    "get_calendar_service",
])
def test_service_builders_none_without_creds(monkeypatch, builder):
    """Each builder returns None when credentials are unavailable."""
    monkeypatch.setattr(service, "_get_credentials", lambda op: None)
    # build() should never be called when creds are None.
    monkeypatch.setattr(service, "build", lambda *a, **k: pytest.fail("build called"))
    assert getattr(service, builder)("op") is None


# --- workspace_connected ---------------------------------------------------

def test_workspace_connected_true(monkeypatch):
    monkeypatch.setattr(
        service, "load_tokens",
        lambda op: {"access_token": "a", "refresh_token": "r"},
    )
    assert service.workspace_connected("op") is True


def test_workspace_connected_false_no_token(monkeypatch):
    monkeypatch.setattr(service, "load_tokens", lambda op: None)
    assert service.workspace_connected("op") is False


def test_workspace_connected_false_no_refresh(monkeypatch):
    monkeypatch.setattr(service, "load_tokens", lambda op: {"access_token": "a"})
    assert service.workspace_connected("op") is False
