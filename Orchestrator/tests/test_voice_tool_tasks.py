"""Voice tool calls run as tracked per-session tasks (audit rank 51:
O08-stt-realtime-02, O09-live-voice-providers-05).

A tool call used to run INLINE in the upstream listener: while it ran nothing
was read from the provider, and the 60 s stale timer then reconnected and
cancelled the tool mid-call, so the function call never got an answer.
control_phone alone has a 300 s budget. Pins, for all three routes:

  * the listener keeps draining frames while a tool is in flight;
  * a tool past its deadline is answered with an error (no dangling call id);
  * teardown cancels in-flight tools;
  * a result for a previous upstream socket is not sent on the new one;
  * a 70 s tool next to a running keepalive causes no reconnect.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import websockets.exceptions  # noqa: F401 — production loads it with websockets.connect

import Orchestrator.routes.gemini_live_routes as glr
import Orchestrator.routes.grok_live_routes as gk
import Orchestrator.routes.realtime_routes as rt
import Orchestrator.routes.voice_ws_shared as shared
import Orchestrator.tools as tools_pkg
from Orchestrator.models import GeminiLiveSession, GrokLiveSession, RealtimeSession
from Orchestrator.tests.gemini_live_fakes import FakeGeminiWS
from Orchestrator.tests.voice_ws_fakes import FakePortalWS, FakeStreamWS, FakeUpstreamWS

_REAL_SLEEP = asyncio.sleep


class _GatedExecutor:
    """Stands in for BlackBoxToolExecutor: execute() waits for `gate`."""

    gate: asyncio.Event = None
    calls: list = []
    cancelled: list = []
    work = None   # optional coroutine factory run instead of the gate

    def __init__(self, operator="system"):
        self.operator = operator

    async def execute(self, name, args):
        type(self).calls.append(name)
        try:
            if type(self).work is not None:
                await type(self).work()
            else:
                await type(self).gate.wait()
        except asyncio.CancelledError:
            type(self).cancelled.append(name)
            raise
        return SimpleNamespace(result=f"{name} finished", rich_result=lambda: f"{name} finished")


@pytest.fixture
def gated(monkeypatch):
    _GatedExecutor.gate = asyncio.Event()
    _GatedExecutor.calls = []
    _GatedExecutor.cancelled = []
    _GatedExecutor.work = None
    monkeypatch.setattr(tools_pkg, "BlackBoxToolExecutor", _GatedExecutor)
    return _GatedExecutor


def _openai_call(call_id="c1", name="slow_tool"):
    return {"type": "response.function_call_arguments.done", "call_id": call_id,
            "name": name, "arguments": "{}"}


def _gemini_call(*ids, name="slow_tool"):
    return {"toolCall": {"functionCalls": [{"id": i, "name": name, "args": {}} for i in ids]}}


SPEC = {
    "openai": SimpleNamespace(module=rt, cls=RealtimeSession, ws_attr="openai_ws",
                              listener="openai_listener", handler="handle_openai_message",
                              call=_openai_call,
                              later={"type": "response.output_audio_transcript.delta",
                                     "delta": "still here"}),
    "grok": SimpleNamespace(module=gk, cls=GrokLiveSession, ws_attr="grok_ws",
                            listener="grok_listener", handler="handle_grok_message",
                            call=_openai_call,
                            later={"type": "response.output_audio_transcript.delta",
                                   "delta": "still here"}),
    "gemini": SimpleNamespace(module=glr, cls=GeminiLiveSession, ws_attr="gemini_ws",
                              listener="gemini_listener", handler="handle_gemini_message",
                              call=lambda call_id="c1", name="slow_tool": _gemini_call(call_id, name=name),
                              later={"serverContent": {"outputTranscription": {"text": "still here"}}}),
}
NAMES = ["openai", "grok", "gemini"]


def _session(name):
    spec = SPEC[name]
    session = spec.cls(session_id=f"tools-{name}", operator="system")
    session.portal_ws = FakePortalWS()
    return session


def _answers(name, upstream):
    """call ids answered on `upstream`, in order."""
    if name == "gemini":
        return [r["id"] for f in upstream.sent if "toolResponse" in f
                for r in f["toolResponse"]["functionResponses"]]
    return [f["item"]["call_id"] for f in upstream.sent
            if f.get("type") == "conversation.item.create"
            and f.get("item", {}).get("type") == "function_call_output"]


def _answer_texts(name, upstream):
    if name == "gemini":
        return [r["response"]["result"] for f in upstream.sent if "toolResponse" in f
                for r in f["toolResponse"]["functionResponses"]]
    return [f["item"]["output"] for f in upstream.sent
            if f.get("item", {}).get("type") == "function_call_output"]


class _StreamUpstream(FakeStreamWS):
    """FakeStreamWS whose sends are recorded like FakeUpstreamWS."""


def _stream(name, frames):
    if name == "gemini":
        ws = FakeGeminiWS([json.dumps(f) for f in frames])
        ws.sent = []

        async def send(payload):
            ws.sent.append(json.loads(payload))
        ws.send = send
        return ws
    return _StreamUpstream([json.dumps(f) for f in frames])


async def _drain_tools(session):
    for task in list(session.tool_tasks):
        try:
            await asyncio.wait_for(task, timeout=2)
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_listener_keeps_draining_while_a_tool_runs(gated, name):
    spec = SPEC[name]

    async def run():
        session = _session(name)
        upstream = _stream(name, [spec.call(), spec.later])
        setattr(session, spec.ws_attr, upstream)

        # The listener must reach the end of the stream with the tool still
        # running (inline, it would sit on the tool until the gate opened).
        await asyncio.wait_for(getattr(spec.module, spec.listener)(session), timeout=2)
        deltas = [f["data"] for f in session.portal_ws.sent if f.get("type") == "transcript_delta"]
        assert deltas == ["still here"], "frames after the tool call were not drained"
        assert _answers(name, upstream) == [], "answered before the tool finished"
        assert len(session.tool_tasks) == 1

        gated.gate.set()
        await _drain_tools(session)
        assert _answers(name, upstream) == ["c1"]
        assert _answer_texts(name, upstream) == ["slow_tool finished"]
        if name != "gemini":
            assert upstream.sent[-1] == {"type": "response.create"}
        assert not session.tool_tasks

    asyncio.run(run())


@pytest.mark.parametrize("name", NAMES)
def test_tool_past_its_deadline_is_answered_with_an_error(gated, monkeypatch, name):
    spec = SPEC[name]
    monkeypatch.setattr(shared, "VOICE_TOOL_DEADLINE_S", 0.05)

    async def run():
        session = _session(name)
        upstream = FakeUpstreamWS()
        setattr(session, spec.ws_attr, upstream)
        await getattr(spec.module, spec.handler)(session, spec.call())
        await _drain_tools(session)
        return upstream, session

    upstream, session = asyncio.run(run())
    assert _answers(name, upstream) == ["c1"], "the overrun call id was left dangling"
    text = _answer_texts(name, upstream)[0]
    assert "failed" in text and "TimeoutError" in text
    assert gated.cancelled == ["slow_tool"], "the overrun tool must be cancelled"
    errors = [f for f in session.portal_ws.sent
              if f.get("type") == "tool_result" and f["data"].get("error")]
    assert errors, "the client must learn the tool failed"


@pytest.mark.parametrize("name", NAMES)
def test_a_tool_that_raises_is_answered_with_an_error(gated, monkeypatch, name):
    """The listener-level responder can no longer see a tool's exception (the
    tool runs in its own task), so the task itself must answer the call id."""
    spec = SPEC[name]

    async def explode():
        raise RuntimeError("executor exploded")

    gated.work = explode

    async def run():
        session = _session(name)
        upstream = FakeUpstreamWS()
        setattr(session, spec.ws_attr, upstream)
        await getattr(spec.module, spec.handler)(session, spec.call())
        await _drain_tools(session)
        return upstream

    upstream = asyncio.run(run())
    assert _answers(name, upstream) == ["c1"]
    assert "executor exploded" in _answer_texts(name, upstream)[0]
    if name != "gemini":
        assert upstream.sent[-1] == {"type": "response.create"}


@pytest.mark.parametrize("name", NAMES)
def test_teardown_cancels_in_flight_tools(gated, monkeypatch, name):
    spec = SPEC[name]
    save_name = "save_grok_session_to_blackbox" if name == "grok" else "save_session_to_blackbox"
    monkeypatch.setattr(spec.module, save_name, AsyncMock())

    async def run():
        session = _session(name)
        generation = shared.claim_session(session)
        upstream = FakeUpstreamWS()
        setattr(session, spec.ws_attr, upstream)
        await getattr(spec.module, spec.handler)(session, spec.call())
        await _REAL_SLEEP(0)
        assert gated.calls == ["slow_tool"]
        await spec.module._end_portal_session(session, session.portal_ws, generation, None)
        return upstream, session

    upstream, session = asyncio.run(run())
    assert gated.cancelled == ["slow_tool"]
    assert not session.tool_tasks
    assert _answers(name, upstream) == []


@pytest.mark.parametrize("name", NAMES)
def test_result_for_a_previous_upstream_is_not_sent_on_the_new_one(gated, name):
    """After a reconnect the new upstream session does not know the old call id."""
    spec = SPEC[name]

    async def run():
        session = _session(name)
        old, new = FakeUpstreamWS(), FakeUpstreamWS()
        setattr(session, spec.ws_attr, old)
        await getattr(spec.module, spec.handler)(session, spec.call())
        await _REAL_SLEEP(0)
        setattr(session, spec.ws_attr, new)     # a reconnect swapped the socket
        gated.gate.set()
        await _drain_tools(session)
        return old, new

    old, new = asyncio.run(run())
    assert _answers(name, new) == []
    assert _answers(name, old) == []


@pytest.mark.parametrize("name", NAMES)
def test_calls_of_one_session_run_in_arrival_order(gated, name):
    spec = SPEC[name]
    order = []

    async def work():
        order.append(len(order))
        await _REAL_SLEEP(0)

    gated.work = work

    async def run():
        session = _session(name)
        upstream = FakeUpstreamWS()
        setattr(session, spec.ws_attr, upstream)
        for cid in ("c1", "c2", "c3"):
            await getattr(spec.module, spec.handler)(session, spec.call(cid))
        await _drain_tools(session)
        return upstream

    upstream = asyncio.run(run())
    assert _answers(name, upstream) == ["c1", "c2", "c3"]


def test_gemini_tool_call_cancellation_cancels_the_task(gated):
    async def run():
        session = _session("gemini")
        upstream = FakeUpstreamWS()
        session.gemini_ws = upstream
        await glr.handle_gemini_message(session, _gemini_call("a1", "a2"))
        await _REAL_SLEEP(0)
        await glr.handle_gemini_message(session, {"toolCallCancellation": {"ids": ["a1"]}})
        gated.gate.set()
        await _drain_tools(session)
        return upstream

    upstream = asyncio.run(run())
    assert gated.cancelled == ["slow_tool"]
    assert _answers("gemini", upstream) == ["a2"], "a cancelled call is not answered"


# ---------------------------------------------------------------------------
class _Clock:
    def __init__(self, monkeypatch):
        self.t = 2_000_000.0
        monkeypatch.setattr(time, "time", lambda: self.t)
        monkeypatch.setattr(asyncio, "sleep", self.sleep)

    async def sleep(self, delay, result=None):
        self.t += delay
        await _REAL_SLEEP(0)
        return result


LOOPS = {"openai": ("openai_keepalive_loop", "openai_reconnect"),
         "grok": ("grok_keepalive_loop", "grok_reconnect"),
         "gemini": ("gemini_keepalive_loop", "gemini_reconnect")}


@pytest.mark.parametrize("name", NAMES)
def test_seventy_second_tool_beside_the_keepalive_causes_no_reconnect(gated, monkeypatch, name):
    spec = SPEC[name]
    loop_name, reconnect_name = LOOPS[name]
    reconnects = []

    async def fake_reconnect(session):
        reconnects.append(session)

    monkeypatch.setattr(spec.module, reconnect_name, fake_reconnect)

    async def run():
        clock = _Clock(monkeypatch)
        start = clock.t

        async def seventy_seconds():
            await asyncio.sleep(70)

        gated.work = seventy_seconds
        session = _session(name)
        upstream = FakeUpstreamWS()
        setattr(session, spec.ws_attr, upstream)
        keepalive = asyncio.create_task(getattr(spec.module, loop_name)(session))
        await getattr(spec.module, spec.handler)(session, spec.call())
        await _drain_tools(session)
        while clock.t - start < 120:
            await _REAL_SLEEP(0)
        session.intentional_disconnect = True
        await asyncio.wait_for(keepalive, timeout=2)
        return upstream

    upstream = asyncio.run(run())
    assert reconnects == [], "a long tool must not look like a dead upstream"
    assert _answers(name, upstream) == ["c1"]


def test_phone_bridge_stop_cancels_tools_of_its_session(gated, monkeypatch):
    """The phone Gemini leg runs the route's handler, so its tool calls are
    session tasks too; hanging up must cancel them."""
    from Orchestrator.phone.bridge import PhoneAIBridge
    from Orchestrator.phone.session import AIBackend, PhoneSession

    async def run():
        bridge = PhoneAIBridge(PhoneSession(session_id="tools-call",
                                            ai_backend=AIBackend.GEMINI_LIVE))
        session = _session("gemini")
        session.gemini_ws = FakeUpstreamWS()
        bridge._ai_session = session
        bridge._save_session = AsyncMock()
        await glr.handle_gemini_message(session, _gemini_call("p1"))
        await _REAL_SLEEP(0)
        await bridge.stop()
        return session

    session = asyncio.run(run())
    assert gated.cancelled == ["slow_tool"]
    assert not session.tool_tasks


# ---------------------------------------------------------------------------
# A cancelled caller stays cancelled (review of rank 51)
# ---------------------------------------------------------------------------
# cancel_task swallowed every CancelledError, its CALLER's included. In the
# Gemini listener's toolCallCancellation branch that meant: a teardown or a
# reconnect cancelling the listener while it waited for a tool to unwind left
# the listener running, and retire_upstream (which closes the socket only after
# the listener is gone) waited on it until Google closed the idle socket.
class _IdleGeminiUpstream:
    """Yields the scripted frames (the second once `gate` is set), then idles."""

    def __init__(self, first, second, gate):
        self._frames = [first, second]
        self._gate = gate
        self.idle = asyncio.Event()
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed = True
        self.idle.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if len(self._frames) == 2:
            return self._frames.pop(0)
        if self._frames:
            await self._gate.wait()
            return self._frames.pop(0)
        await self.idle.wait()            # Gemini says nothing while the user is quiet
        raise StopAsyncIteration


def test_retiring_the_listener_while_a_tool_unwinds_does_not_hang(monkeypatch):
    async def run():
        started, unwinding = asyncio.Event(), asyncio.Event()

        async def slow_unwinding_tool(session, name, args):
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                unwinding.set()
                await _REAL_SLEEP(0.3)   # stopping a CU or phone task takes a moment
                raise

        monkeypatch.setattr(glr, "_execute_gemini_tool", slow_unwinding_tool)
        session = _session("gemini")
        session.gemini_ws = _IdleGeminiUpstream(
            json.dumps(_gemini_call("c1", name="use_computer")),
            json.dumps({"toolCallCancellation": {"ids": ["c1"]}}),
            started)
        listener = asyncio.create_task(glr.gemini_listener(session))
        session.listener_task = listener
        await unwinding.wait()
        await _REAL_SLEEP(0)             # the listener is back reading (or, before: blocked)
        retire = asyncio.create_task(shared.retire_upstream(session, "gemini_ws"))
        done, _ = await asyncio.wait({retire}, timeout=3)
        state = {"retired": bool(done), "listener_done": listener.done()}
        if session.gemini_ws is not None:
            session.gemini_ws.idle.set()
        retire.cancel()
        await _REAL_SLEEP(0.35)
        return state

    assert asyncio.run(run()) == {"retired": True, "listener_done": True}


def test_cancel_task_lets_the_callers_own_cancellation_through():
    async def run():
        async def child():
            try:
                await asyncio.sleep(3600)
            finally:
                await _REAL_SLEEP(0.05)  # an unwind that takes a moment

        after = []

        async def caller(teardown):
            task = asyncio.create_task(child())
            await _REAL_SLEEP(0)
            await shared.cancel_task(task, teardown=teardown)
            after.append(teardown)       # reached only when the cancel was absorbed

        plain = asyncio.create_task(caller(False))
        absorbing = asyncio.create_task(caller(True))
        await _REAL_SLEEP(0.01)          # both callers are inside cancel_task
        plain.cancel()
        absorbing.cancel()
        await asyncio.wait({plain, absorbing})
        return plain, absorbing, after

    plain, absorbing, after = asyncio.run(run())
    assert plain.cancelled(), "a cancelled caller must not run on"
    assert not absorbing.cancelled() and after == [True], \
        "a teardown runs to its end (the transcript save comes after)"


def test_cancel_task_never_raises_the_tasks_own_cancellation():
    async def run():
        task = asyncio.create_task(asyncio.sleep(3600))
        await _REAL_SLEEP(0)
        await shared.cancel_task(task)
        return task

    assert asyncio.run(run()).cancelled()
