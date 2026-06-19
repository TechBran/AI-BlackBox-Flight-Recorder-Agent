"""Hermetic tests for the control_phone ToolVault executor (Tasks 3-4).

The executor module is loaded directly from its file (the same mechanism the
registry uses). mesh.resolve_origin + the two phone-HTTP seams
(_post_task / _get_status) are monkeypatched, so no tailscale, no sockets, and no
real phone are touched. The poll cadence/timeout come from the _poll_interval_secs
/ _total_timeout_secs helpers, which the timing tests monkeypatch to spin the loop
instantly.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

from Orchestrator.toolvault.context import ToolContext
from Orchestrator.local_provider.mesh import Node

_EXEC_PATH = (Path(__file__).resolve().parents[2]
              / "ToolVault" / "tools" / "control_phone" / "executor.py")
_spec = importlib.util.spec_from_file_location("control_phone_executor", _EXEC_PATH)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

NODE = Node(hostname="brandon-fold6", dns_name="brandon-fold6.tailnet-abc.ts.net",
            ip="100.88.0.7", online=True, os="android")
CTX = ToolContext(operator="Brandon", base_url="http://localhost:9091")


def _run(coro):
    return asyncio.run(coro)


def _aret(value):
    """An async stub that ignores its args and returns `value`."""
    async def f(*a, **k):
        return value
    return f


def test_requires_task():
    res = _run(cp.execute({}, CTX))
    assert res.success is False
    assert "task is required" in res.result.lower()


def test_no_reachable_device(monkeypatch):
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: None)
    res = _run(cp.execute({"task": "open maps"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "no_device"


def test_happy_path_waking_working_done(monkeypatch):
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: NODE)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)

    async def fake_post(base_url, payload):
        assert "brandon-fold6" in base_url           # addressed the resolved node
        assert payload == {"task": "open maps", "operator": "Brandon"}
        return {"task_id": "t1"}

    seq = iter([{"phase": "waking"}, {"phase": "working"},
                {"phase": "done", "result": "Opened Maps."}])

    async def fake_status(base_url, task_id):
        assert task_id == "t1"
        return next(seq)

    monkeypatch.setattr(cp, "_post_task", fake_post)
    monkeypatch.setattr(cp, "_get_status", fake_status)

    res = _run(cp.execute({"task": "open maps"}, CTX))
    assert res.success is True
    assert res.result == "Opened Maps."
    assert res.data["phase"] == "done"
    assert res.data["task_id"] == "t1"


def test_remote_error_surfaces_message(monkeypatch):
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: NODE)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)
    monkeypatch.setattr(cp, "_post_task", _aret({"task_id": "t1"}))
    monkeypatch.setattr(cp, "_get_status",
                        _aret({"phase": "error", "error": "tool refused for remote control"}))
    res = _run(cp.execute({"task": "send an sms"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "remote_error"
    assert "refused" in res.result.lower()


def test_wake_failed_when_post_raises(monkeypatch):
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: NODE)

    async def boom(base_url, payload):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(cp, "_post_task", boom)
    res = _run(cp.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "wake_failed"
    assert res.data["device"] == NODE.dns_name


def test_bad_response_when_no_task_id(monkeypatch):
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: NODE)
    monkeypatch.setattr(cp, "_post_task", _aret({"oops": 1}))
    res = _run(cp.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "bad_response"


def test_lost_contact_when_status_raises(monkeypatch):
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: NODE)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)
    monkeypatch.setattr(cp, "_post_task", _aret({"task_id": "t1"}))

    async def boom(base_url, task_id):
        raise ConnectionError("dropped")

    monkeypatch.setattr(cp, "_get_status", boom)
    res = _run(cp.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "lost_contact"


def test_phone_base_url_prefers_dns_name():
    assert cp._phone_base_url(NODE) == f"http://{NODE.dns_name}:{cp.REMOTE_CONTROL_PORT}"
    ip_only = Node(hostname="h", dns_name="", ip="100.88.0.7", online=True)
    assert cp._phone_base_url(ip_only) == f"http://100.88.0.7:{cp.REMOTE_CONTROL_PORT}"


# ── Task 4: poll/timeout semantics + cancellation-safety ──

def test_timeout_when_phone_stays_waking(monkeypatch):
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: NODE)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)
    monkeypatch.setattr(cp, "_total_timeout_secs", lambda: 0.0)  # deadline == now -> first check trips
    monkeypatch.setattr(cp, "_post_task", _aret({"task_id": "t1"}))
    monkeypatch.setattr(cp, "_get_status", _aret({"phase": "waking"}))  # never advances
    res = _run(cp.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "timeout"
    assert res.data["phase"] == "waking"


def test_done_within_budget_after_waking(monkeypatch):
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: NODE)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)
    monkeypatch.setattr(cp, "_total_timeout_secs", lambda: 5.0)
    monkeypatch.setattr(cp, "_post_task", _aret({"task_id": "t1"}))
    seq = iter([{"phase": "waking"}, {"phase": "waking"},
                {"phase": "working"}, {"phase": "done", "result": "ok"}])

    async def status(base_url, task_id):
        return next(seq)

    monkeypatch.setattr(cp, "_get_status", status)
    res = _run(cp.execute({"task": "x"}, CTX))
    assert res.success is True
    assert res.result == "ok"


def test_cancellation_propagates_not_swallowed(monkeypatch):
    # A cancelled turn must abort the poll cleanly, NOT be swallowed by the
    # broad `except Exception` and misreported as lost_contact.
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: NODE)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)
    monkeypatch.setattr(cp, "_total_timeout_secs", lambda: 60.0)
    monkeypatch.setattr(cp, "_post_task", _aret({"task_id": "t1"}))

    async def hang(base_url, task_id):
        await asyncio.sleep(5)  # long status call we cancel mid-flight
        return {"phase": "working"}

    monkeypatch.setattr(cp, "_get_status", hang)

    async def driver():
        task = asyncio.create_task(cp.execute({"task": "x"}, CTX))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(driver())
