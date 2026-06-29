"""Reconnect-and-resume integration test for the ElevenLabs Scribe STT relay.

Drives `_elevenlabs_bridge` directly (no network, no real sleeps) with a scripted
fake client WebSocket and two fake upstream Scribe sockets. The first upstream
hits Scribe's `session_time_limit_exceeded` mid-utterance; the bridge MUST open a
FRESH upstream and resume WITHOUT surfacing an error or closing the client
socket, stitching the transcript across the rotation.

Mirrors test_stt_ws.py's monkeypatch style. Uses `asyncio.run()` to await the
bridge to completion — test_stt_ws.py drives async via TestClient and the repo's
pytest-asyncio is not in auto mode, so a plain sync test + asyncio.run is the
dependency-free idiom here.

Determinism (NO sleeps): the scripted client withholds its `stt_stop` frame on an
asyncio.Event that the fake `websockets` module fires only on its SECOND
`connect()` call. So the time-limit rotation is forced to happen BEFORE the stop
is delivered — otherwise `stop_evt` would suppress the rotate. The audio queue
inside the bridge decouples the single client pump from each per-epoch feeder, so
audio buffered before the rotation survives it.
"""
import asyncio
import json

from fastapi import WebSocketDisconnect

from Orchestrator.routes import stt_ws_routes


class _FakeUpstream:
    """Fake Scribe realtime socket: async-iterable over pre-scripted JSON frames.

    Records `.send()` payloads and `.close()` calls; raises StopAsyncIteration
    (i.e. the upstream closes) once its scripted frames are exhausted.
    """

    def __init__(self, frames):
        self._frames = [json.dumps(f) for f in frames]
        self.sent = []
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, data):
        self.sent.append(data)

    async def close(self, *a, **k):
        self.closed += 1


class _FakeWsModule:
    """Stands in for the `websockets` module reference inside stt_ws_routes.

    Records every `connect()` call and hands out the next pre-built upstream.
    Fires `second_connect` once the SECOND connect lands so the scripted client
    can withhold its stt_stop until the reconnect has actually happened.
    """

    def __init__(self, sockets, second_connect):
        self._sockets = list(sockets)
        self._second_connect = second_connect
        self.connect_calls = []

    async def connect(self, url, **kwargs):
        self.connect_calls.append(url)
        sock = self._sockets.pop(0)
        if len(self.connect_calls) >= 2:
            self._second_connect.set()
        return sock


class _FakeClientWS:
    """Scripted client side of /ws/stt.

    Yields audio frames then a *gated* stt_stop (held until the bridge opens its
    second upstream). Records everything the bridge sends back in `.sent`; counts
    `.close()` calls so the test can assert the client socket is never closed
    across the rotation. `accept()` is a no-op.
    """

    def __init__(self, frames, second_connect):
        self._frames = list(frames)
        self._second_connect = second_connect
        self._i = 0
        self.sent = []
        self.closed = 0

    async def accept(self):
        pass

    async def receive_json(self):
        if self._i >= len(self._frames):
            # Script exhausted without the relay ending: simulate a hangup rather
            # than blocking forever, so a buggy (non-reconnecting) bridge fails
            # fast instead of hanging the test.
            raise WebSocketDisconnect()
        frame = self._frames[self._i]
        if frame.get("type") == "stt_stop":
            # The time-limit rotation must happen first: hold the stop until the
            # bridge has opened its SECOND upstream session.
            await self._second_connect.wait()
        self._i += 1
        return frame

    async def send_json(self, obj):
        self.sent.append(obj)

    async def close(self, *a, **k):
        self.closed += 1


def test_elevenlabs_bridge_reconnects_and_resumes_on_session_time_limit(monkeypatch):
    async def scenario():
        second_connect = asyncio.Event()

        # Upstream #1: streams a partial, then hits Scribe's session cap and dies.
        upstream1 = _FakeUpstream([
            {"message_type": "partial_transcript", "text": "hello world"},
            {"message_type": "session_time_limit_exceeded",
             "error": "Maximum session duration exceeded"},
        ])
        # Upstream #2: the resumed session — a partial then the committed final.
        upstream2 = _FakeUpstream([
            {"message_type": "partial_transcript", "text": "second session"},
            {"message_type": "committed_transcript", "text": "second session done"},
        ])
        fake_ws_module = _FakeWsModule([upstream1, upstream2], second_connect)
        client = _FakeClientWS([
            {"type": "stt_audio", "pcm": "AAAA"},
            {"type": "stt_audio", "pcm": "AAAA"},
            {"type": "stt_stop"},
        ], second_connect)

        # Patch the symbols the bridge actually calls. websockets is referenced
        # as a module attr (`websockets.connect`), so replace the module ref.
        monkeypatch.setattr(stt_ws_routes, "websockets", fake_ws_module)
        monkeypatch.setattr(stt_ws_routes, "resolve_api_key", lambda: "test-key")

        # 10s backstop so a deadlock fails the test instead of hanging CI.
        await asyncio.wait_for(
            stt_ws_routes._elevenlabs_bridge(
                client, target="prompt", lang="en", sample_rate=24000
            ),
            timeout=10.0,
        )
        return fake_ws_module, client

    fake_ws_module, client = asyncio.run(scenario())

    # 1. A reconnect happened: exactly two upstream Scribe sessions were opened.
    assert len(fake_ws_module.connect_calls) == 2, (
        f"expected 2 upstream connects (reconnect-and-resume), "
        f"got {len(fake_ws_module.connect_calls)}"
    )

    # 2. The time-limit was invisible: the client NEVER saw an error frame.
    assert not any(m.get("type") == "stt_error" for m in client.sent), (
        f"session_time_limit_exceeded leaked an stt_error to the client: {client.sent}"
    )

    # 3. The transcript stitched continuity across the rotation: the final
    #    carries BOTH session 1's words and session 2's committed text.
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals, f"no stt_final delivered; sent={client.sent}"
    final_text = finals[-1]["text"]
    assert "hello world" in final_text, f"prefix lost across rotation: {final_text!r}"
    assert "second session done" in final_text, f"resumed session lost: {final_text!r}"

    # 4. Deltas streamed before the final, and the bridge never closed the client
    #    socket mid-session (the reconnect is upstream-only).
    types = [m.get("type") for m in client.sent]
    assert "stt_delta" in types, f"no interim deltas streamed: {client.sent}"
    first_final = types.index("stt_final")
    assert "stt_delta" in types[:first_final], f"no stt_delta before stt_final: {types}"
    assert client.closed == 0, "bridge must not close the client socket across the rotation"


# =============================================================================
# Failure-path hardening tests (client disconnect, no-progress rotate, cap).
# =============================================================================


class _BlockingUpstream:
    """Fake Scribe socket whose async iteration PARKS forever (cancellable).

    Proves the relay can only wake on the CLIENT disconnect (never on an upstream
    event), so a dead client is detected rather than parking the epoch forever.
    """

    def __init__(self):
        self._never = asyncio.Event()
        self.sent = []
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._never.wait()   # blocks until cancelled
        raise StopAsyncIteration   # unreachable

    async def send(self, data):
        self.sent.append(data)

    async def close(self, *a, **k):
        self.closed += 1


class _BlockingClient:
    """Client that connects then never speaks/stops: receive_json parks forever
    (cancellable). Records sends; counts closes."""

    def __init__(self):
        self._never = asyncio.Event()
        self.sent = []
        self.closed = 0

    async def accept(self):
        pass

    async def receive_json(self):
        await self._never.wait()
        raise WebSocketDisconnect()  # unreachable

    async def send_json(self, obj):
        self.sent.append(obj)

    async def close(self, *a, **k):
        self.closed += 1


class _AlwaysAudioClient:
    """Streams a fixed budget of audio frames (yielding cooperatively so it never
    starves the relay tasks), then PARKS — never stops, never disconnects. Lets the
    rotation loop run until it is bounded solely by the rotation cap."""

    def __init__(self, frame_budget):
        self._budget = frame_budget
        self._never = asyncio.Event()
        self.sent = []
        self.closed = 0

    async def accept(self):
        pass

    async def receive_json(self):
        if self._budget > 0:
            self._budget -= 1
            await asyncio.sleep(0)  # yield to the relay; not a real delay
            return {"type": "stt_audio", "pcm": "AAAA"}
        await self._never.wait()
        raise WebSocketDisconnect()  # unreachable

    async def send_json(self, obj):
        self.sent.append(obj)

    async def close(self, *a, **k):
        self.closed += 1


class _SlowFinalUpstream:
    """Upstream whose committed final lands only after several event-loop turns
    (simulating network latency), so the relay MUST drain on stt_stop to deliver it.
    A relay that returns the instant it sees the client_pump exit (which also fires
    on a clean stop) would cancel the reader before this final is read."""

    def __init__(self, partial_text, final_text, latency_turns=6):
        self._partial = {"message_type": "partial_transcript", "text": partial_text}
        self._final = {"message_type": "committed_transcript", "text": final_text}
        self._latency = latency_turns
        self._stage = 0
        self.sent = []
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._stage == 0:
            self._stage = 1
            return json.dumps(self._partial)
        if self._stage == 1:
            for _ in range(self._latency):
                await asyncio.sleep(0)   # committed final is "still in flight"
            self._stage = 2
            return json.dumps(self._final)
        raise StopAsyncIteration

    async def send(self, data):
        self.sent.append(data)

    async def close(self, *a, **k):
        self.closed += 1


def test_client_disconnect_mid_session_no_reconnect(monkeypatch):
    """CRITICAL defect: an abrupt client drop mid-session must be observed and end
    the session WITHOUT reconnecting against the (now dead) client."""

    async def scenario():
        # Upstream #1 never emits anything — the ONLY thing that can wake the epoch
        # is the client disconnect.
        upstream1 = _BlockingUpstream()
        fake_ws_module = _FakeWsModule([upstream1], asyncio.Event())
        # Client sends one audio frame, then receive_json raises WebSocketDisconnect
        # (the _FakeClientWS hangup-on-exhaustion path).
        client = _FakeClientWS([{"type": "stt_audio", "pcm": "AAAA"}], asyncio.Event())

        monkeypatch.setattr(stt_ws_routes, "websockets", fake_ws_module)
        monkeypatch.setattr(stt_ws_routes, "resolve_api_key", lambda: "test-key")

        # If the disconnect is NOT observed the epoch parks on the blocking upstream
        # forever; wait_for converts that hang into a test failure. The test simply
        # COMPLETING is the proof that the bridge returned promptly.
        await asyncio.wait_for(
            stt_ws_routes._elevenlabs_bridge(
                client, target="prompt", lang="en", sample_rate=24000
            ),
            timeout=10.0,
        )
        return fake_ws_module, client

    fake_ws_module, client = asyncio.run(scenario())

    # A dead client must NOT trigger a reconnect: connect happened exactly once.
    assert len(fake_ws_module.connect_calls) == 1, (
        f"a dead client must NOT trigger a reconnect; "
        f"connect called {len(fake_ws_module.connect_calls)}x"
    )
    # A clean client hangup need not surface an error.
    assert not any(m.get("type") == "stt_error" for m in client.sent), (
        f"client disconnect should not surface an stt_error: {client.sent}"
    )


def test_no_progress_rotate_does_not_reconnect(monkeypatch):
    """A session that claims the time limit having transcribed/consumed NOTHING is
    pathological — the relay must NOT spin reconnecting on it."""

    async def scenario():
        # Upstream #1: instant session_time_limit_exceeded, no partials, no audio
        # consumed -> a no-progress rotate.
        upstream1 = _FakeUpstream([
            {"message_type": "session_time_limit_exceeded",
             "error": "Maximum session duration exceeded"},
        ])
        # Spare socket so a (buggy) reconnect proceeds far enough to be asserted
        # against rather than raising on an empty socket list.
        upstream2 = _FakeUpstream([
            {"message_type": "session_time_limit_exceeded",
             "error": "Maximum session duration exceeded"},
        ])
        fake_ws_module = _FakeWsModule([upstream1, upstream2], asyncio.Event())
        # Client connects but never speaks and never stops: receive_json parks, so
        # the only way the loop ends is the no-progress guard returning "done".
        client = _BlockingClient()

        monkeypatch.setattr(stt_ws_routes, "websockets", fake_ws_module)
        monkeypatch.setattr(stt_ws_routes, "resolve_api_key", lambda: "test-key")

        await asyncio.wait_for(
            stt_ws_routes._elevenlabs_bridge(
                client, target="prompt", lang="en", sample_rate=24000
            ),
            timeout=10.0,
        )
        return fake_ws_module, client

    fake_ws_module, client = asyncio.run(scenario())

    assert len(fake_ws_module.connect_calls) == 1, (
        f"a no-progress rotate must NOT reconnect; "
        f"connect called {len(fake_ws_module.connect_calls)}x"
    )


def test_rotation_cap_bounds_reconnects(monkeypatch):
    """A provider that rotates on EVERY session (each making progress) must be
    bounded by _EL_MAX_ROTATIONS: initial connect + cap rotations, then an error."""

    cap = 2
    monkeypatch.setattr(stt_ws_routes, "_EL_MAX_ROTATIONS", cap)

    async def scenario():
        # cap + 1 upstreams, each making PROGRESS (a partial) then rotating.
        sockets = [
            _FakeUpstream([
                {"message_type": "partial_transcript", "text": f"chunk {i}"},
                {"message_type": "session_time_limit_exceeded",
                 "error": "Maximum session duration exceeded"},
            ])
            for i in range(cap + 1)
        ]
        fake_ws_module = _FakeWsModule(sockets, asyncio.Event())
        # Streams audio and never stops/disconnects, so only the cap bounds the loop.
        client = _AlwaysAudioClient(frame_budget=cap + 5)

        monkeypatch.setattr(stt_ws_routes, "websockets", fake_ws_module)
        monkeypatch.setattr(stt_ws_routes, "resolve_api_key", lambda: "test-key")

        await asyncio.wait_for(
            stt_ws_routes._elevenlabs_bridge(
                client, target="prompt", lang="en", sample_rate=24000
            ),
            timeout=10.0,
        )
        return fake_ws_module, client

    fake_ws_module, client = asyncio.run(scenario())

    # initial connect + exactly `cap` rotations.
    assert len(fake_ws_module.connect_calls) == cap + 1, (
        f"expected {cap + 1} connects (initial + {cap} rotations), "
        f"got {len(fake_ws_module.connect_calls)}"
    )
    errors = [m for m in client.sent if m.get("type") == "stt_error"]
    assert errors, f"cap exhaustion must surface an stt_error; sent={client.sent}"
    assert "could not be sustained" in errors[-1].get("message", ""), (
        f"unexpected cap error message: {errors[-1]}"
    )


def test_clean_stop_drains_final_despite_disconnect_evt(monkeypatch):
    """Regression for a bug in the hardened bridge: client_pump ALWAYS sets
    disconnect_evt on exit (incl. a clean stt_stop). If the epoch treated that as an
    abrupt drop it would return immediately and cancel the reader BEFORE the
    committed final (still in flight from the network) was delivered. The relay must
    instead DRAIN the single final on a clean stop. The upstream here withholds its
    committed final for several event-loop turns to model that network latency."""

    async def scenario():
        upstream1 = _SlowFinalUpstream("hello", "hello world", latency_turns=6)
        fake_ws_module = _FakeWsModule([upstream1], asyncio.Event())
        # Clean push-to-talk: one audio frame then stt_stop. _FakeClientWS gates its
        # stt_stop on this event, so PRE-SET it (single epoch, no rotation) — the stop
        # must actually be delivered for the clean-stop drain path to be exercised.
        stop_gate = asyncio.Event()
        stop_gate.set()
        client = _FakeClientWS([
            {"type": "stt_audio", "pcm": "AAAA"},
            {"type": "stt_stop"},
        ], stop_gate)

        monkeypatch.setattr(stt_ws_routes, "websockets", fake_ws_module)
        monkeypatch.setattr(stt_ws_routes, "resolve_api_key", lambda: "test-key")

        await asyncio.wait_for(
            stt_ws_routes._elevenlabs_bridge(
                client, target="prompt", lang="en", sample_rate=24000
            ),
            timeout=10.0,
        )
        return fake_ws_module, client

    fake_ws_module, client = asyncio.run(scenario())

    assert len(fake_ws_module.connect_calls) == 1, "clean stop should use one session"
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals, (
        f"clean-stop committed final was DROPPED (reader cancelled before drain); "
        f"sent={client.sent}"
    )
    assert "hello world" in finals[-1]["text"], f"wrong final delivered: {finals[-1]}"
