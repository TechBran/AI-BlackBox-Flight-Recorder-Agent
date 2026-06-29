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
