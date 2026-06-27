"""
Executor-level coverage for two ToolVault tools un-masked by the M1 MCP
dispatcher collapse (commit 9468f845).

Before M1, the MCP server had bespoke branches for get_task_status (hit the
correct /tasks/ route) and get_music_status (hit /music/status). The collapse
routes both through /local/tools/execute -> the ToolVault executor, which
exposed two pre-existing bugs:

  FIX 1: get_task_status/executor.py used /task/{id} (SINGULAR) -- there is NO
         such backend handler, so it 404'd. The real route is /tasks/{id}
         (PLURAL, task_routes.py:85). These tests assert the GET URL is /tasks/.
  FIX 2: get_music_status had NO executor.py (schema-only) -> get_executor
         returned None -> "Unknown tool". These tests assert it now resolves and
         targets /music/status.

We mock aiohttp.ClientSession (the executors' HTTP client) so no live backend is
needed -- the same pattern as test_cron_auto_resolution.py.
"""

import asyncio
import json

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext

import ToolVault.tools.get_task_status.executor as gts_mod
import ToolVault.tools.get_music_status.executor as gms_mod


# ---------------------------------------------------------------------------
# Fake aiohttp session that records the GET URL and returns canned JSON.
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._body


class _FakeSession:
    captured = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, timeout=None):
        _FakeSession.captured["url"] = url
        return _FakeResp(_FakeSession.status, _FakeSession.body)


def _install(monkeypatch, module, status, body):
    _FakeSession.captured = {}
    _FakeSession.status = status
    _FakeSession.body = body
    monkeypatch.setattr(module.aiohttp, "ClientSession", _FakeSession)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


CTX = ToolContext(operator="alice", base_url="http://localhost:9091")


# ===========================================================================
# FIX 1 -- get_task_status hits /tasks/ (plural), not /task/ (singular).
# ===========================================================================
def test_get_task_status_uses_plural_tasks_route(monkeypatch):
    _install(monkeypatch, gts_mod, 200, {
        "task_id": "T1", "status": "completed", "result_url": "/ui/uploads/x.png",
        "result_data": {"artifact": "a"},
    })
    res = _run(gts_mod.execute({"task_id": "T1"}, CTX))
    url = _FakeSession.captured["url"]
    assert url == "http://localhost:9091/tasks/T1", url
    assert "/task/T1" != url  # explicitly NOT the singular (404) route
    assert res.success is True


def test_get_task_status_surfaces_result_url_and_data(monkeypatch):
    """The completed-task path returns status + result_url + full task JSON so the
    generate->poll->retrieve media loop gets the real fields (result_url, not url)."""
    _install(monkeypatch, gts_mod, 200, {
        "task_id": "T2", "status": "completed",
        "result_url": "/ui/uploads/song.wav",
        "result_data": {"artifact": "song"},
    })
    res = _run(gts_mod.execute({"task_id": "T2"}, CTX))
    assert res.success is True
    assert "/ui/uploads/song.wav" in res.result          # surfaced in the summary
    assert res.data["result_url"] == "/ui/uploads/song.wav"
    assert res.data["result_data"]["artifact"] == "song"  # full task JSON in data


def test_get_task_status_failed_uses_error_message(monkeypatch):
    _install(monkeypatch, gts_mod, 200, {
        "task_id": "T3", "status": "failed", "error_message": "quota exhausted",
    })
    res = _run(gts_mod.execute({"task_id": "T3"}, CTX))
    assert res.success is False
    assert "quota exhausted" in res.result


def test_get_task_status_404_is_not_found(monkeypatch):
    _install(monkeypatch, gts_mod, 404, {})
    res = _run(gts_mod.execute({"task_id": "missing"}, CTX))
    assert res.success is False
    assert "not found" in res.result.lower()


def test_get_task_status_requires_task_id():
    res = _run(gts_mod.execute({}, CTX))
    assert res.success is False
    assert "required" in res.result.lower()


# ===========================================================================
# FIX 2 -- get_music_status has an executor that targets /music/status.
# ===========================================================================
def test_get_music_status_executor_is_registered():
    """Registry resolves an executor (was None -> 'Unknown tool' before FIX 2)."""
    assert registry.get_executor("get_music_status") is not None


def test_get_music_status_targets_music_status_route(monkeypatch):
    _install(monkeypatch, gms_mod, 200, {"lyria_available": True, "model": "lyria"})
    res = _run(gms_mod.execute({}, CTX))
    url = _FakeSession.captured["url"]
    assert url == "http://localhost:9091/music/status", url
    assert res.success is True
    body = json.loads(res.result)
    assert body["lyria_available"] is True
    assert res.data["model"] == "lyria"


def test_get_music_status_non_200_fails(monkeypatch):
    _install(monkeypatch, gms_mod, 503, {})
    res = _run(gms_mod.execute({}, CTX))
    assert res.success is False
    assert "503" in res.result
