"""Tests for Batch E module executors (Task 6.2 — final batch).

Batch E migrates the 5 Gmail executors OUT of the monolithic
``blackbox_tools._execute_<name>`` methods INTO per-tool
``ToolVault/tools/<name>/executor.py`` modules:

    gmail_search, gmail_read, gmail_send, gmail_reply, gmail_labels

These tests run against the REAL on-disk modules (no tmp_path) — they assert the
5 executors load cleanly and route correctly with the Gmail service layer mocked
(or short-circuit on missing required params, no network).
"""

import asyncio
import inspect
import json
from unittest.mock import patch

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor


BATCH_E = [
    "gmail_search",
    "gmail_read",
    "gmail_send",
    "gmail_reply",
    "gmail_labels",
]


@pytest.fixture(autouse=True)
def fresh_registry():
    """Invalidate the executor cache around each test so on-disk edits register."""
    registry.invalidate_cache()
    yield
    registry.invalidate_cache()


# ---------------------------------------------------------------------------
# 1. Every Batch E executor loads: callable, no load_errors, valid signature.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", BATCH_E)
def test_executor_is_callable(name):
    ex = registry.get_executor(name)
    assert ex is not None, f"get_executor({name!r}) returned None"
    assert callable(ex)
    assert inspect.iscoroutinefunction(ex), f"{name} executor is not async"
    positional = [
        p
        for p in inspect.signature(ex).parameters.values()
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert len(positional) == 2, f"{name} executor must take (params, ctx)"


@pytest.mark.parametrize("name", BATCH_E)
def test_no_load_error_for_executor(name):
    registry.get_executor(name)
    errors = registry.load_errors()
    assert name not in errors, f"{name} has load errors: {errors.get(name)}"


def test_all_batch_e_loaded():
    """All 5 resolve to a callable."""
    assert all(registry.get_executor(n) is not None for n in BATCH_E)


# ---------------------------------------------------------------------------
# 2. Short-circuit routing smokes (no network needed — missing required params).
# ---------------------------------------------------------------------------

def test_gmail_read_requires_message_id():
    ex = registry.get_executor("gmail_read")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "message_id is required" in result.result


def test_gmail_send_requires_fields():
    ex = registry.get_executor("gmail_send")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "required" in result.result


def test_gmail_reply_requires_fields():
    ex = registry.get_executor("gmail_reply")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "required" in result.result


def test_gmail_labels_modify_requires_message_id():
    ex = registry.get_executor("gmail_labels")
    result = asyncio.run(ex({"action": "archive"}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "message_id required" in result.result


# ---------------------------------------------------------------------------
# 3. Routing smokes with the Gmail service layer mocked.
# ---------------------------------------------------------------------------

def test_gmail_search_routes_to_list_messages():
    ex = registry.get_executor("gmail_search")
    fake = [{"id": "abc", "subject": "Hi"}]
    with patch("Orchestrator.gmail.service.list_messages", return_value=fake) as m:
        result = asyncio.run(
            ex({"query": "is:unread", "max_results": 5}, ToolContext(operator="Brandon"))
        )
    assert isinstance(result, ToolResult)
    assert result.success is True
    m.assert_called_once_with("Brandon", "is:unread", 5)
    assert json.loads(result.result) == fake


def test_gmail_search_max_results_capped_at_20():
    ex = registry.get_executor("gmail_search")
    with patch("Orchestrator.gmail.service.list_messages", return_value=[]) as m:
        asyncio.run(ex({"max_results": 100}, ToolContext(operator="system")))
    # third positional arg (max_results) must be clamped to 20
    assert m.call_args[0][2] == 20


def test_gmail_read_routes_to_get_message():
    ex = registry.get_executor("gmail_read")
    fake = {"id": "m1", "subject": "Subj", "from": "a@b.com"}
    with patch("Orchestrator.gmail.service.get_message", return_value=fake) as m:
        result = asyncio.run(
            ex({"message_id": "m1"}, ToolContext(operator="Brandon"))
        )
    assert result.success is True
    m.assert_called_once_with("Brandon", "m1")
    assert json.loads(result.result) == fake


def test_gmail_send_routes_to_send_email():
    ex = registry.get_executor("gmail_send")
    fake = {"status": "sent", "id": "x1"}
    with patch("Orchestrator.gmail.service.send_email", return_value=fake) as m:
        result = asyncio.run(
            ex(
                {"to": "a@b.com", "subject": "Hello", "body": "Body", "cc": "c@d.com"},
                ToolContext(operator="Brandon"),
            )
        )
    assert result.success is True
    m.assert_called_once_with("Brandon", "a@b.com", "Hello", "Body", "c@d.com")
    assert json.loads(result.result) == fake


def test_gmail_reply_prefixes_re_and_routes():
    ex = registry.get_executor("gmail_reply")
    original = {"from": "sender@x.com", "subject": "Question"}
    sent = {"status": "sent", "id": "r1"}
    with patch("Orchestrator.gmail.service.get_message", return_value=original), patch(
        "Orchestrator.gmail.service.send_email", return_value=sent
    ) as send_mock:
        result = asyncio.run(
            ex(
                {"message_id": "m1", "thread_id": "t1", "body": "My reply"},
                ToolContext(operator="Brandon"),
            )
        )
    assert result.success is True
    # subject should be prefixed with "Re: "
    args, kwargs = send_mock.call_args
    assert args == ("Brandon", "sender@x.com", "Re: Question", "My reply")
    assert kwargs["reply_to_message_id"] == "m1"
    assert kwargs["thread_id"] == "t1"


def test_gmail_labels_list_routes_to_get_labels():
    ex = registry.get_executor("gmail_labels")
    fake = [{"id": "INBOX", "name": "INBOX"}]
    with patch("Orchestrator.gmail.service.get_labels", return_value=fake) as m:
        result = asyncio.run(
            ex({"action": "list"}, ToolContext(operator="Brandon"))
        )
    assert result.success is True
    m.assert_called_once_with("Brandon")
    assert json.loads(result.result) == fake


def test_gmail_labels_mark_read_routes_to_modify_message():
    ex = registry.get_executor("gmail_labels")
    fake = {"status": "ok"}
    with patch("Orchestrator.gmail.service.modify_message", return_value=fake) as m:
        result = asyncio.run(
            ex(
                {"action": "mark_read", "message_id": "m1"},
                ToolContext(operator="Brandon"),
            )
        )
    assert result.success is True
    # mark_read => add=[], remove=["UNREAD"]
    m.assert_called_once_with("Brandon", "m1", [], ["UNREAD"])


# ---------------------------------------------------------------------------
# 4. Dispatch-path smoke through the executor façade.
# ---------------------------------------------------------------------------

def test_gmail_read_requires_message_id_via_dispatch():
    ex = BlackBoxToolExecutor(operator="Brandon")
    result = asyncio.run(ex.execute("gmail_read", {}))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "message_id is required" in result.result
