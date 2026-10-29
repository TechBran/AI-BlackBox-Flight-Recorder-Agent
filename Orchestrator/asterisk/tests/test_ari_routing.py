"""ARI events are routed per channel, and hangups end the call flow at once.

Audit O11-telephony-asterisk-cellular-04 and -11. AsteriskIVR.run swapped the
client's single on_dtmf slot for its own handler and restored the saved one in
`finally`; _handle_outbound_call did the same with on_stasis_start. The TG200
has two GSM ports, so two calls at once is the designed case: when call A
ended first it restored the handler from before call B, so B's digits went
nowhere (and an outbound B's answer fell through to the inbound handler,
which ran the IVR on the person being called). Hangup events were dispatched
to an on_hangup slot nothing ever set, so a caller who hung up during the IVR
left it waiting out its timeouts.

These tests run the real AsteriskARIClient against a fake ARI server (aiohttp,
an ephemeral loopback port): events arrive over its WebSocket, and every
channel request is recorded. Nothing is dialled.
"""
import asyncio
import contextlib

import pytest
from aiohttp import web

from Orchestrator import config
from Orchestrator.asterisk import audio_ipc
from Orchestrator.asterisk import client as ari_mod
from Orchestrator.asterisk import ivr as ivr_mod
from Orchestrator.phone import pin_gate


class FakeARIServer:
    """Just enough ARI: /asterisk/info, the event WebSocket, and channel and
    playback requests (recorded; a play finishes after ``playback_s``)."""

    def __init__(self, playback_s=0.02):
        self.playback_s = playback_s
        self.requests = []
        self._sockets = set()
        self._serial = 0
        self.connected = asyncio.Event()
        self.port = None
        self._runner = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/ari/asterisk/info", self._info)
        app.router.add_get("/ari/events", self._events)
        app.router.add_route("*", "/ari/{tail:.*}", self._request)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self):
        for ws in list(self._sockets):
            await ws.close()
        await self._runner.cleanup()

    def client(self):
        return ari_mod.AsteriskARIClient(
            ari_url=f"http://127.0.0.1:{self.port}",
            ari_ws_url=f"ws://127.0.0.1:{self.port}/ari/events?app=blackbox",
            username="fake", password="fake", app_name="blackbox")

    async def _info(self, request):
        return web.json_response({"system": {"version": "fake-20"}})

    async def _events(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets.add(ws)
        self.connected.set()
        async for _ in ws:
            pass
        self._sockets.discard(ws)
        return ws

    async def emit(self, event):
        for ws in list(self._sockets):
            await ws.send_json(event)
        await asyncio.sleep(0.01)  # let the client dispatch it

    def _next(self, prefix):
        self._serial += 1
        return f"{prefix}-{self._serial}"

    async def _request(self, request):
        path, method = request.path, request.method
        self.requests.append((method, path, dict(request.query)))
        if method == "POST" and path.endswith("/play"):
            playback = self._next("pb")
            loop = asyncio.get_running_loop()
            loop.call_later(self.playback_s, lambda: asyncio.ensure_future(
                self.emit({"type": "PlaybackFinished", "playback": {"id": playback}})))
            return web.json_response({"id": playback})
        if method == "POST" and path == "/ari/channels":
            return web.json_response({"id": self._next("out")})
        return web.Response(status=204)

    def played(self, channel):
        return [q.get("media") for m, p, q in self.requests if p == f"/ari/channels/{channel}/play"]

    def did(self, method, path):
        return any(m == method and p == path for m, p, _ in self.requests)


def stasis_start(channel, args=(), caller="+15555550142"):
    return {"type": "StasisStart", "args": list(args),
            "channel": {"id": channel, "caller": {"number": caller},
                        "dialplan": {"exten": "s"}}}


def dtmf(channel, digit):
    return {"type": "ChannelDtmfReceived", "channel": {"id": channel}, "digit": digit}


def hangup(channel):
    return [{"type": "ChannelHangupRequest", "channel": {"id": channel}},
            {"type": "StasisEnd", "channel": {"id": channel}},
            {"type": "ChannelDestroyed", "channel": {"id": channel}}]


@contextlib.asynccontextmanager
async def ari_session(**kw):
    server = FakeARIServer(**kw)
    await server.start()
    client = server.client()
    try:
        assert await client.connect()
        await asyncio.wait_for(server.connected.wait(), 5)
        yield server, client
    finally:
        await client.disconnect()
        await server.stop()


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.fixture
def no_pin_one_operator(monkeypatch, tmp_path):
    """The IVR's PIN stage off and one operator, so a call goes straight to the
    backend menu (the PIN stage has its own tests)."""
    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", False)
    monkeypatch.setattr(config, "USERS_LIST", ["OperatorA"])
    monkeypatch.setattr(config, "IVR_DEFAULT_BACKEND", "openai_realtime")
    monkeypatch.setattr(ivr_mod, "resolve_prompt", lambda name, *fallbacks: name)
    monkeypatch.setattr(pin_gate, "LOCKOUT_PATH", tmp_path / "phone_pin_lockout.json")


# ── DTMF: two calls at once ────────────────────────────────────────────────

def test_two_concurrent_ivrs_each_get_their_own_digits_after_the_first_ends(no_pin_one_operator):
    async def scenario():
        async with ari_session() as (server, client):
            ivr_a = ivr_mod.AsteriskIVR(ari_client=client, channel_id="chan-a", caller_id="+15555550101")
            ivr_b = ivr_mod.AsteriskIVR(ari_client=client, channel_id="chan-b", caller_id="+15555550102")
            run_a = asyncio.ensure_future(ivr_a.run())
            run_b = asyncio.ensure_future(ivr_b.run())
            await _until(lambda: any("backend_menu" in (m or "") for m in server.played("chan-a"))
                         and any("backend_menu" in (m or "") for m in server.played("chan-b")))
            await server.emit(dtmf("chan-a", "2"))
            result_a = await asyncio.wait_for(run_a, 10)
            # A is done (and has cleaned up): B's digit must still reach B.
            await server.emit(dtmf("chan-b", "4"))
            result_b = await asyncio.wait_for(run_b, 10)
            return result_a, result_b

    result_a, result_b = asyncio.run(scenario())
    assert result_a["backend"] == "gemini_live"
    assert result_b["backend"] == "grok_live"


def test_digits_for_a_channel_nobody_handles_reach_no_other_call():
    async def scenario():
        async with ari_session() as (server, client):
            got = []

            async def handler(channel, digit):
                got.append((channel, digit))

            client.register_dtmf_handler("chan-a", handler)
            await server.emit(dtmf("chan-x", "9"))
            await server.emit(dtmf("chan-a", "1"))
            client.unregister_dtmf_handler("chan-a")
            await server.emit(dtmf("chan-a", "2"))
            return got

    assert asyncio.run(scenario()) == [("chan-a", "1")]


# ── StasisStart: an outbound answer goes to the call that dialled it ───────

def test_outbound_answers_are_matched_by_call_uuid_and_never_reach_the_inbound_handler():
    async def scenario():
        async with ari_session() as (server, client):
            inbound = []

            async def inbound_handler(channel, caller, callee, args):
                inbound.append(channel)

            client.on_stasis_start = inbound_handler
            first = client.expect_outbound_answer("uuid-1")
            second = client.expect_outbound_answer("uuid-2")
            await server.emit(stasis_start("out-2", ["outbound", "uuid-2"]))
            assert await asyncio.wait_for(second, 2) == "out-2"
            assert not first.done()
            # An answer nobody waits for (its call gave up) is hung up, not
            # handed to the inbound IVR.
            await server.emit(stasis_start("out-9", ["outbound", "uuid-gone"]))
            await _until(lambda: server.did("DELETE", "/ari/channels/out-9"))
            await server.emit(stasis_start("in-1", []))
            await _until(lambda: inbound == ["in-1"])
            client.cancel_outbound_answer("uuid-1")
            assert first.cancelled()

    asyncio.run(scenario())


class _RunningIPC:
    """An audio subprocess that is up but never sees an AudioSocket connect."""
    is_running = True

    def expect_channel(self, channel_id):
        return asyncio.Event()

    async def wait_for_channel(self, channel_id, timeout=15.0):
        return False

    def get_channel_rate(self, channel_id):
        return 8000


def test_two_outbound_calls_each_get_their_own_answer(monkeypatch):
    """Call 1 is answered and ends first; call 2's answer must still reach
    call 2 (it used to fall through to the inbound handler)."""
    from Orchestrator.phone.session import PhoneSession, CallDirection
    from Orchestrator.routes import asterisk_routes as ar

    # The call uuid travels in the originate body; record it on the client.
    originated = []
    real_originate = ari_mod.AsteriskARIClient.originate

    async def recording_originate(self, endpoint, callerid="", timeout=45, variables=None, app_args=""):
        channel = await real_originate(self, endpoint, callerid, timeout, variables, app_args)
        originated.append((channel, app_args.split(",")[1]))
        return channel

    monkeypatch.setattr(ari_mod.AsteriskARIClient, "originate", recording_originate)

    async def full():
        async with ari_session() as (server, client):
            inbound = []

            async def inbound_handler(channel, caller, callee, args):
                inbound.append(channel)

            client.on_stasis_start = inbound_handler
            monkeypatch.setattr(ari_mod, "get_ari_client", lambda: client)
            monkeypatch.setattr(audio_ipc, "get_ipc_client", lambda: _RunningIPC())
            sessions = [PhoneSession(session_id=f"ast-out-{n}", direction=CallDirection.OUTBOUND)
                        for n in (1, 2)]
            calls = [asyncio.ensure_future(ar._handle_outbound_call(s, "+15555550142", "tg-test"))
                     for s in sessions]
            await _until(lambda: len(originated) == 2)
            (chan1, uuid1), (chan2, uuid2) = originated
            await server.emit(stasis_start(chan1, ["outbound", uuid1]))
            await asyncio.wait_for(calls[0], 10)  # no AudioSocket: call 1 ends
            await server.emit(stasis_start(chan2, ["outbound", uuid2]))
            await asyncio.wait_for(calls[1], 10)
            continued = [p for m, p, _ in server.requests if p.endswith("/continue")]
            return inbound, continued, chan1, chan2

    inbound, continued, chan1, chan2 = asyncio.run(full())
    assert inbound == [], "an outbound answer must never run the inbound IVR"
    assert continued == [f"/ari/channels/{chan1}/continue", f"/ari/channels/{chan2}/continue"]


def test_an_outbound_call_the_callee_rejects_ends_at_once(monkeypatch):
    from Orchestrator.phone.session import PhoneSession, PhoneStatus, CallDirection
    from Orchestrator.routes import asterisk_routes as ar

    async def scenario():
        async with ari_session() as (server, client):
            monkeypatch.setattr(ari_mod, "get_ari_client", lambda: client)
            monkeypatch.setattr(audio_ipc, "get_ipc_client", lambda: _RunningIPC())
            session = PhoneSession(session_id="ast-out-r", direction=CallDirection.OUTBOUND)
            call = asyncio.ensure_future(ar._handle_outbound_call(session, "+15555550142", "tg-test"))
            await _until(lambda: session.asterisk_channel_id)
            for event in hangup(session.asterisk_channel_id):
                await server.emit(event)
            await asyncio.wait_for(call, 3)  # not the 50 s answer timeout
            return session

    session = asyncio.run(scenario())
    assert session.status in (PhoneStatus.FAILED, PhoneStatus.COMPLETED)


# ── hangups ─────────────────────────────────────────────────────────────────

def test_hangup_events_set_the_channel_watch_once_even_if_it_came_first():
    async def scenario():
        async with ari_session() as (server, client):
            watch = client.watch_hangup("chan-a")
            for event in hangup("chan-a"):
                await server.emit(event)
            assert watch.is_set()
            # A channel that is already gone when someone starts watching it.
            for event in hangup("chan-b"):
                await server.emit(event)
            assert client.watch_hangup("chan-b").is_set()
            client.unwatch_hangup("chan-a")
            client.unwatch_hangup("chan-b")

    asyncio.run(scenario())


def test_a_caller_who_hangs_up_during_the_ivr_ends_the_call_at_once(monkeypatch, tmp_path):
    """The PIN prompt is playing when the caller hangs up: the inbound handler
    ends within moments instead of waiting out 3 x 15 s PIN timeouts."""
    from Orchestrator.phone.session import PhoneStatus
    from Orchestrator.routes import asterisk_routes as ar

    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", True)
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "739104")
    monkeypatch.setattr(pin_gate, "LOCKOUT_PATH", tmp_path / "phone_pin_lockout.json")
    monkeypatch.setattr(ivr_mod, "resolve_prompt", lambda name, *fallbacks: name)
    monkeypatch.setattr(ivr_mod, "pin_prompt_name", lambda length: f"pin_prompt_{length}digit")
    monkeypatch.setattr(ar, "PHONE_SESSIONS", {})

    async def scenario():
        async with ari_session(playback_s=0.05) as (server, client):
            monkeypatch.setattr(ari_mod, "get_ari_client", lambda: client)
            monkeypatch.setattr(audio_ipc, "get_ipc_client", lambda: _RunningIPC())
            done = asyncio.Event()

            async def inbound(channel, caller, callee, args):
                try:
                    await ar.handle_inbound_call(channel, caller, callee, args)
                finally:
                    done.set()

            client.on_stasis_start = inbound
            await server.emit(stasis_start("chan-in", []))
            await _until(lambda: any("pin_prompt" in (m or "") for m in server.played("chan-in")))
            for event in hangup("chan-in"):
                await server.emit(event)
            loop = asyncio.get_running_loop()
            started = loop.time()
            await asyncio.wait_for(done.wait(), 3)
            return loop.time() - started, server

    elapsed, server = asyncio.run(scenario())
    assert elapsed < 3
    assert not any(p.endswith("/continue") for _, p, _ in server.requests), \
        "a hung-up call never reaches the AI"
    session = next(iter(ar.PHONE_SESSIONS.values()))
    assert session.status == PhoneStatus.COMPLETED and session.call_end
