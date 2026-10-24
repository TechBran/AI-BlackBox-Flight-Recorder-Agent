"""MN.7 — agent/CLI session completion notify wiring tests.

When a Claude Code CLI turn exits, ``_finish_claude_turn`` (the transport's
on_exit hook, which runs independently of any WebSocket connection — so a
disconnected operator still learns the run finished) marks the session
"completed". That is the single completion choke-point, so it fires a
fire-and-forget notify(category="agent") via the sync→async bridge.

The bridge (``notify_in_background``) is mocked here so the test is offline and
asserts it is CALLED with the session operator + category="agent". A notify
failure must NOT prevent the reader from marking the session completed.
"""

import pytest

import Orchestrator.routes.agent_routes as agent_routes
from Orchestrator.models import AgentSession


def _session(operator="Brandon"):
    return AgentSession(
        session_id="sess-abc123",
        operator=operator,
        status="running",
    )


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def fake_bg(operator, title, body, category="general", **k):
        calls.append(
            {"operator": operator, "title": title, "body": body, "category": category}
        )

    monkeypatch.setattr(agent_routes, "notify_in_background", fake_bg)
    return calls


def test_session_completion_fires_agent_notify(captured):
    session = _session(operator="Brandon")

    agent_routes._finish_claude_turn(session, 0)

    assert session.status == "completed"
    assert len(captured) == 1
    assert captured[0]["operator"] == "Brandon"
    assert captured[0]["category"] == "agent"
    assert captured[0]["title"]  # short, non-empty


def test_system_or_empty_operator_suppressed(captured):
    """An unattributed agent session (no operator) does not spam."""
    session = _session(operator="")

    agent_routes._finish_claude_turn(session, 0)

    assert session.status == "completed"  # still marked completed
    assert captured == []


def test_notify_failure_does_not_break_completion(monkeypatch):
    """A notify-bridge exception must not stop the reader marking completion."""
    session = _session(operator="Brandon")

    def boom(*a, **k):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr(agent_routes, "notify_in_background", boom)

    agent_routes._finish_claude_turn(session, 0)

    assert session.status == "completed"  # completion still happened
