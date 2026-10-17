"""The gmail_* executors report the service layer's errors as failures (audit W15).

The Gmail service helpers never raise for an API problem: they return an
``{"error": ...}`` dict (or a one-element list of one). The five executors used
to wrap whatever came back in ``ToolResult(success=True, ...)``, so a dead token
looked like a successful tool call to the model, to the MCP gateway, and to the
scheduler's tool-failure verdict. An empty result ("no messages matched") is
still a success.

Runs the real on-disk executor modules with the service layer mocked.
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext, ToolResult


@pytest.fixture(autouse=True)
def fresh_registry():
    registry.invalidate_cache()
    yield
    registry.invalidate_cache()


def _run(tool, params, operator="Robin"):
    ex = registry.get_executor(tool)
    return asyncio.run(ex(params, ToolContext(operator=operator)))


DEAD = "Gmail API error: ('invalid_grant: Bad Request', {'error': 'invalid_grant'})"


def test_gmail_search_error_is_a_failure():
    with patch("Orchestrator.gmail.service.list_messages", return_value=[{"error": DEAD}]):
        result = _run("gmail_search", {"query": "is:unread"})
    assert result.success is False
    assert "invalid_grant" in result.result


def test_gmail_search_with_no_matches_is_a_success():
    with patch("Orchestrator.gmail.service.list_messages", return_value=[]):
        result = _run("gmail_search", {"query": "from:nobody"})
    assert result.success is True
    assert json.loads(result.result) == []


def test_gmail_read_error_is_a_failure():
    with patch("Orchestrator.gmail.service.get_message", return_value={"error": DEAD}):
        result = _run("gmail_read", {"message_id": "m1"})
    assert result.success is False


def test_gmail_send_error_is_a_failure():
    with patch("Orchestrator.gmail.service.send_email", return_value={"error": "Send failed: " + DEAD}):
        result = _run("gmail_send", {"to": "a@example.com", "subject": "s", "body": "b"})
    assert result.success is False


def test_gmail_reply_stops_when_the_original_cannot_be_read():
    with patch("Orchestrator.gmail.service.get_message", return_value={"error": DEAD}), \
            patch("Orchestrator.gmail.service.send_email") as send:
        result = _run("gmail_reply", {"message_id": "m1", "thread_id": "t1", "body": "hi"})
    assert result.success is False
    send.assert_not_called()


def test_gmail_reply_send_error_is_a_failure():
    original = {"from": "sender@example.com", "subject": "Question"}
    with patch("Orchestrator.gmail.service.get_message", return_value=original), \
            patch("Orchestrator.gmail.service.send_email", return_value={"error": "Send failed"}):
        result = _run("gmail_reply", {"message_id": "m1", "thread_id": "t1", "body": "hi"})
    assert result.success is False


def test_gmail_labels_errors_are_failures():
    with patch("Orchestrator.gmail.service.get_labels", return_value=[{"error": DEAD}]):
        assert _run("gmail_labels", {"action": "list"}).success is False
    with patch("Orchestrator.gmail.service.modify_message", return_value={"error": DEAD}):
        assert _run("gmail_labels", {"action": "archive", "message_id": "m1"}).success is False


def test_a_revoked_grant_surfaces_as_a_failed_call_with_reconnect_guidance(monkeypatch):
    """Through the real executor choke point: the reauth exception from the
    credential layer becomes a failed ToolResult that tells the operator what to do."""
    from Orchestrator.gmail import service
    from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor

    def dead(operator):
        raise service.GoogleReauthRequired(operator, "invalid_grant")

    monkeypatch.setattr(service, "_get_credentials", dead)
    monkeypatch.setattr(service, "workspace_connected", lambda operator: True)
    for tool, params in (("gmail_search", {"query": "x"}),
                         ("list_events", {"time_min": "2026-10-01T00:00:00Z",
                                          "time_max": "2026-10-31T00:00:00Z"})):
        result = asyncio.run(BlackBoxToolExecutor(operator="Robin").execute(tool, params))
        assert isinstance(result, ToolResult)
        assert result.success is False, tool
        assert "Reconnect" in result.result and "invalid_grant" in result.result, result.result
