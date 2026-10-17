"""Audit rank 45: /chat/stream builds its retrieval context OFF the event loop.

build_streaming_context (embed + hybrid search + rerank) is synchronous and
measured ~31 s warm on MS02 with the reranker on. Called inline from the async
routes it froze the whole event loop. These tests pin the fix: the build runs
in a worker thread inside the SSE generator, keepalive comment frames cover
the wait, the request-scoped origin device id still reaches it, and a failed
build becomes an SSE error instead of a hung stream.

Hermetic: the build and the provider stream are fakes; intervals are tiny.
"""
import asyncio
import json
import threading
import time

import pytest

import Orchestrator.routes.chat_routes as cr


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _GetReq:
    def __init__(self, params):
        self.query_params = params


@pytest.fixture
def fakes(monkeypatch):
    seen = {}

    def slow_build(messages, operator, provider="openai", window_guard_tokens=None):
        seen["thread"] = threading.current_thread().name
        seen["origin_device_id"] = cr._ORIGIN_DEVICE_ID.get()
        time.sleep(0.6)  # BLOCKING, like the real retrieval + rerank
        return list(messages), {"recent": []}, {}

    async def fake_stream(msgs, model, operator=None, **kw):
        seen["messages"] = msgs
        yield {"type": "content", "data": "ok"}

    monkeypatch.setattr(cr, "build_streaming_context", slow_build)
    monkeypatch.setattr(cr, "stream_openai_with_reasoning", fake_stream)
    monkeypatch.setattr(cr, "KEEPALIVE_INTERVAL_S", 0.1)
    return seen


async def _drain_with_ticker(response):
    """Drain the SSE body while a ticker counts event-loop turns."""
    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    stop.set()
    await t
    return "".join(chunks), ticks


def _post(body):
    async def go():
        resp = await cr.chat_stream_post(_Req(body))
        return await _drain_with_ticker(resp)
    return asyncio.run(go())


_BODY = {"messages": [{"role": "user", "content": "hi"}], "provider": "openai",
         "model": "test-model", "operator": "OffLoopTester", "origin_device_id": "100.64.0.9"}


def test_event_loop_keeps_running_while_context_builds(fakes):
    """The 0.6 s blocking build must not stall the loop: a 20 ms ticker keeps
    ticking. Inline (the old code) it would get ~0 ticks during the build."""
    text, ticks = _post(_BODY)
    assert "event: content" in text
    assert ticks >= 15, f"event loop stalled during the context build (ticks={ticks})"
    assert fakes["thread"] != threading.main_thread().name


def test_keepalive_frames_cover_the_build_before_stream_start(fakes):
    text, _ = _post(_BODY)
    head = text.split("event: stream_start", 1)[0]
    assert head.count(cr.SSE_KEEPALIVE_FRAME) >= 3, text[:400]
    # The frames are comments, so stream_start is still the first EVENT.
    assert "event:" not in head


def test_origin_device_id_reaches_the_offloaded_build(fakes):
    _post(_BODY)
    assert fakes["origin_device_id"] == "100.64.0.9"


def test_failed_build_becomes_an_sse_error_not_a_hang(monkeypatch, fakes):
    def boom(*a, **kw):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(cr, "build_streaming_context", boom)
    text, _ = _post(_BODY)
    assert "event: error" in text and "store unavailable" in text
    assert "event: stream_end" in text
    assert "messages" not in fakes  # the provider was never called


def test_get_route_also_builds_off_loop(fakes):
    params = {"messages": json.dumps(_BODY["messages"]), "provider": "openai",
              "model": "test-model", "operator": "OffLoopTester"}

    async def go():
        resp = await cr.chat_stream(_GetReq(params))
        return await _drain_with_ticker(resp)

    text, ticks = asyncio.run(go())
    assert "event: content" in text
    assert ticks >= 15
