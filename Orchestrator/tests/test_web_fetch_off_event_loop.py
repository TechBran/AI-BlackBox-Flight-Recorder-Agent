"""web_fetch never runs on the event loop (U3 review follow-up to audit
X03-security-backend-04 / O02-chat-routes-A-06).

perform_web_fetch is synchronous and may use its whole time budget (25 s) on a
slow or hostile page. The live-voice handlers (OpenAI realtime, Grok live,
Gemini live) and the ToolVault web_fetch executor are `async def`s that used to
call it inline, so one URL a model was talked into fetching froze the whole
Orchestrator for every other session. Each must hand the fetch to a worker
thread.

Hermetic: perform_web_fetch is replaced by a recorder (no network) and the
upstream sockets are the shared fake WebSockets.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

import Orchestrator.routes.gemini_live_routes as gm
import Orchestrator.routes.grok_live_routes as gk
import Orchestrator.routes.realtime_routes as rt
import Orchestrator.web_tools as web_tools
from Orchestrator.models import GeminiLiveSession, GrokLiveSession, RealtimeSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS
from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext


class _Recorder:
    def __init__(self):
        self.threads = []

    def __call__(self, url, max_chars=80000, *a, **k):
        self.threads.append(threading.get_ident())
        return f"fetched {url}"


def _run_and_get_loop_thread(coro_factory):
    loop_thread = {}

    async def main():
        loop_thread["id"] = threading.get_ident()
        await coro_factory()

    asyncio.run(main())
    return loop_thread["id"]


def _openai_style_event():
    return {"type": "response.function_call_arguments.done", "call_id": "call-wf",
            "name": "web_fetch", "arguments": "{\"url\": \"https://example.com/\"}"}


def test_realtime_web_fetch_runs_off_the_event_loop(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(rt, "perform_web_fetch", rec)
    session = RealtimeSession(session_id="t-wf-rt", operator="system")
    session.openai_ws = FakeUpstreamWS()
    loop = _run_and_get_loop_thread(lambda: rt.handle_openai_message(session, _openai_style_event()))
    assert rec.threads and rec.threads[0] != loop
    assert "fetched https://example.com/" in session.openai_ws.sent[0]["item"]["output"]


def test_grok_live_web_fetch_runs_off_the_event_loop(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(gk, "perform_web_fetch", rec)
    session = GrokLiveSession(session_id="t-wf-gk", operator="system")
    session.grok_ws = FakeUpstreamWS()
    loop = _run_and_get_loop_thread(lambda: gk.handle_grok_message(session, _openai_style_event()))
    assert rec.threads and rec.threads[0] != loop
    assert "fetched https://example.com/" in session.grok_ws.sent[0]["item"]["output"]


def test_gemini_live_web_fetch_runs_off_the_event_loop(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(gm, "perform_web_fetch", rec)
    session = GeminiLiveSession(session_id="t-wf-gm", operator="system")
    session.gemini_ws = FakeUpstreamWS()
    event = {"toolCall": {"functionCalls": [
        {"id": "fc-wf", "name": "web_fetch", "args": {"url": "https://example.com/"}}]}}
    loop = _run_and_get_loop_thread(lambda: gm.handle_gemini_message(session, event))
    assert rec.threads and rec.threads[0] != loop
    responses = session.gemini_ws.sent[0]["toolResponse"]["functionResponses"]
    assert "fetched https://example.com/" in str(responses[0]["response"])


@pytest.fixture
def fresh_registry():
    registry.invalidate_cache()
    yield
    registry.invalidate_cache()


def test_toolvault_web_fetch_executor_runs_off_the_event_loop(monkeypatch, fresh_registry):
    rec = _Recorder()
    monkeypatch.setattr(web_tools, "perform_web_fetch", rec)
    ex = registry.get_executor("web_fetch")
    out = {}

    async def call():
        out["res"] = await ex({"url": "https://example.com/"}, ToolContext(operator="system"))

    loop = _run_and_get_loop_thread(call)
    assert rec.threads and rec.threads[0] != loop
    assert out["res"].success is True
