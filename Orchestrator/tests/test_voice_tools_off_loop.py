"""Audit rank 46 (O08-stt-realtime-01, X05-architecture-07): the live voice
bridges never run retrieval or ledger reads on the event loop.

Every realtime, Gemini Live and Grok Live session, every phone bridge and every
/ws/stt socket shares the server's one event loop, and the voice system prompt
tells the model to search memory first and often. The search tool ran the
embed + rerank retrieval inline (plus a 58 MB read of the ledger for an
argument hybrid_retrieve ignores), list_recent_snapshots parsed the snapshot
index and read the whole volume inline, session configure built its context
inline, and /ws/stt probed the local stack's health inline before streaming.
All of that now runs in worker threads.

Each case stubs the blocking helper with a 0.4 s sleep and measures the
longest gap between turns of a 20 ms ticker on the loop.
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import Orchestrator.routes.gemini_live_routes as gm
import Orchestrator.routes.grok_live_routes as gk
import Orchestrator.routes.realtime_routes as rt
from Orchestrator.models import GeminiLiveSession, GrokLiveSession, RealtimeSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS

BLOCK = 0.4



async def _settled(coro, session):
    """Run a voice handler, then wait for the tool tasks it dispatched (M4 U26
    runs each voice tool as a session task; the reply lands when it finishes)."""
    await coro
    await asyncio.gather(*session.tool_tasks)

async def _max_gap_during(coro):
    gaps = []
    stop = asyncio.Event()
    ticking = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now
            ticking.set()

    t = asyncio.create_task(ticker())
    await ticking.wait()
    try:
        result = await coro
    finally:
        stop.set()
        await t
    return result, max(gaps or [0.0])


def _slow(ret):
    def stub(*a, **k):
        time.sleep(BLOCK)
        return ret
    return stub


SNAP = "=== SNAPSHOT SNAP-20260101-0001 ===\nhello from the ledger\n"


# ---------------------------------------------------------------------------
# search_snapshots
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod,fn,session_cls", [
    (rt, "execute_search_snapshots", RealtimeSession),
    (gm, "execute_search_snapshots", GeminiLiveSession),
    (gk, "execute_grok_search_snapshots", GrokLiveSession),
], ids=["realtime", "gemini_live", "grok_live"])
def test_voice_search_runs_off_the_loop(monkeypatch, mod, fn, session_cls):
    seen = {}

    def slow_retrieve(vol_txt, query, k=3, operator=""):
        seen["vol_txt"] = vol_txt
        time.sleep(BLOCK)
        return [SNAP]

    monkeypatch.setattr(mod, "hybrid_retrieve", slow_retrieve)
    monkeypatch.setattr(mod, "read_text_safe", _slow("x" * 64))  # the old dead read
    session = session_cls(session_id="t-search", operator="system")
    out, gap = asyncio.run(_max_gap_during(getattr(mod, fn)(session, {"query": "ledger"})))
    assert "hello from the ledger" in out
    assert seen["vol_txt"] == "", "the ledger is no longer read just to be ignored"
    assert gap < 0.2, f"{mod.__name__} search blocked the event loop for {gap:.2f}s"


# ---------------------------------------------------------------------------
# list_recent_snapshots
# ---------------------------------------------------------------------------

def _patch_recent(monkeypatch):
    import Orchestrator.fossils as fossils
    data = SNAP.encode()
    index = {"SNAP-20260101-0001": {"operator": "system", "byte_start": 0, "byte_end": len(data)}}
    monkeypatch.setattr(fossils, "load_snapshot_index", _slow(index))
    monkeypatch.setattr(fossils, "read_volume_bytes", _slow(data))


def test_realtime_recent_snapshots_run_off_the_loop(monkeypatch):
    _patch_recent(monkeypatch)
    session = RealtimeSession(session_id="t-recent", operator="system")
    session.openai_ws = FakeUpstreamWS()
    event = {"type": "response.function_call_arguments.done", "call_id": "c1",
             "name": "list_recent_snapshots", "arguments": json.dumps({"count": 1})}
    _, gap = asyncio.run(_max_gap_during(_settled(rt.handle_openai_message(session, event), session)))
    assert "hello from the ledger" in session.openai_ws.sent[0]["item"]["output"]
    assert gap < 0.2, f"recent snapshots blocked the event loop for {gap:.2f}s"


def test_grok_recent_snapshots_run_off_the_loop(monkeypatch):
    _patch_recent(monkeypatch)
    session = GrokLiveSession(session_id="t-recent-gk", operator="system")
    session.grok_ws = FakeUpstreamWS()
    event = {"type": "response.function_call_arguments.done", "call_id": "c2",
             "name": "list_recent_snapshots", "arguments": json.dumps({"count": 1})}
    _, gap = asyncio.run(_max_gap_during(_settled(gk.handle_grok_message(session, event), session)))
    assert "hello from the ledger" in session.grok_ws.sent[0]["item"]["output"]
    assert gap < 0.2, f"recent snapshots blocked the event loop for {gap:.2f}s"


def test_gemini_recent_snapshots_run_off_the_loop(monkeypatch):
    _patch_recent(monkeypatch)
    session = GeminiLiveSession(session_id="t-recent-gm", operator="system")
    session.gemini_ws = FakeUpstreamWS()
    event = {"toolCall": {"functionCalls": [
        {"id": "fc-r", "name": "list_recent_snapshots", "args": {"count": 1}}]}}
    _, gap = asyncio.run(_max_gap_during(_settled(gm.handle_gemini_message(session, event), session)))
    responses = session.gemini_ws.sent[0]["toolResponse"]["functionResponses"]
    assert "hello from the ledger" in responses[0]["response"]["result"]
    assert gap < 0.2, f"recent snapshots blocked the event loop for {gap:.2f}s"


# ---------------------------------------------------------------------------
# session configure (context build)
# ---------------------------------------------------------------------------

def _session(ws_attr):
    s = MagicMock()
    setattr(s, ws_attr, MagicMock())
    getattr(s, ws_attr).send = AsyncMock()
    s.provenance = {}
    s.context_injected = False
    s.resumption_handle = None
    s.voice = ""
    return s


@pytest.mark.parametrize("mod,fn,ws_attr,voice", [
    (rt, "configure_openai_session", "openai_ws", "ash"),
    (gm, "configure_gemini_session", "gemini_ws", "Charon"),
    (gk, "configure_grok_session", "grok_ws", "Ara"),
], ids=["realtime", "gemini_live", "grok_live"])
def test_voice_configure_builds_context_off_the_loop(monkeypatch, mod, fn, ws_attr, voice):
    monkeypatch.setattr(mod, "build_fossil_context", _slow(
        ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})))
    session = _session(ws_attr)
    _, gap = asyncio.run(_max_gap_during(
        getattr(mod, fn)(session=session, operator="op", voice=voice)))
    assert getattr(session, ws_attr).send.await_count >= 1
    assert gap < 0.2, f"{fn} blocked the event loop for {gap:.2f}s"


# ---------------------------------------------------------------------------
# /ws/stt provider resolution
# ---------------------------------------------------------------------------

class _ClientWS:
    def __init__(self):
        self.sent = []

    async def accept(self):
        pass

    async def receive_json(self):
        return {"type": "stt_start", "provider": None}

    async def send_json(self, msg):
        self.sent.append(msg)


def test_ws_stt_resolves_its_provider_off_the_loop(monkeypatch):
    from Orchestrator.routes import stt_ws_routes as stt_ws
    # A wedged llama-swap makes the health probe wait out its 2 s + 2 s.
    monkeypatch.setattr(stt_ws, "onbox_stt_available", _slow(False))
    monkeypatch.setattr(stt_ws, "local_streaming_stt_available", lambda: False)
    monkeypatch.setattr(stt_ws, "resolve_stt_provider", lambda *a, **k: None)
    ws = _ClientWS()
    _, gap = asyncio.run(_max_gap_during(stt_ws.ws_stt(ws)))
    assert ws.sent[0]["type"] == "stt_error"
    assert ws.sent[-1] == {"type": "stt_done"}
    assert gap < 0.2, f"/ws/stt start blocked the event loop for {gap:.2f}s"
