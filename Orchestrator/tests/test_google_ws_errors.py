"""Tests for the shared Google Workspace HttpError -> message mapper.

The key behavior: a 403 whose Google reason is SERVICE_DISABLED (the API is not
enabled in the Cloud project) must NOT tell the user to reconnect -- reconnecting
does nothing; they must enable the API. A 403 from an insufficient scope still
maps to the reconnect message. This split is what the live smoke surfaced.
"""

import json

from googleapiclient.errors import HttpError

from Orchestrator.google_workspace._errors import (
    http_error_to_dict,
    _is_service_disabled,
)


class FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "Forbidden"


def _service_disabled_content():
    return json.dumps({
        "error": {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "message": "Google Docs API has not been used in project 1 ... or it is disabled.",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                 "reason": "SERVICE_DISABLED",
                 "domain": "googleapis.com"},
            ],
        }
    }).encode()


def test_service_disabled_403_says_enable_not_reconnect():
    e = HttpError(FakeResp(403), _service_disabled_content())
    out = http_error_to_dict(e, "Docs")
    assert "not enabled" in out["error"].lower()
    assert "reconnect" not in out["error"].lower()
    assert "Docs" in out["error"]


def test_scope_403_says_reconnect():
    content = json.dumps({
        "error": {"code": 403, "message": "insufficient authentication scopes",
                  "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}
    }).encode()
    e = HttpError(FakeResp(403), content)
    out = http_error_to_dict(e, "Calendar")
    assert "reconnect" in out["error"].lower()
    assert "Calendar" in out["error"]


def test_403_unparseable_content_defaults_to_reconnect():
    # Non-JSON body -> _is_service_disabled returns False -> reconnect (safe default).
    e = HttpError(FakeResp(403), b"forbidden")
    out = http_error_to_dict(e, "Drive")
    assert "reconnect" in out["error"].lower()
    assert "Drive" in out["error"]


def test_non_403_returns_generic_with_status():
    e = HttpError(FakeResp(500), b'{"error": {"message": "boom"}}')
    out = http_error_to_dict(e, "Sheets")
    assert "Sheets API error" in out["error"]
    assert "500" in out["error"]


def test_is_service_disabled_detection():
    assert _is_service_disabled(HttpError(FakeResp(403), _service_disabled_content())) is True
    assert _is_service_disabled(HttpError(FakeResp(403), b"not json")) is False
    assert _is_service_disabled(HttpError(FakeResp(403),
        b'{"error": {"details": [{"reason": "OTHER"}]}}')) is False
