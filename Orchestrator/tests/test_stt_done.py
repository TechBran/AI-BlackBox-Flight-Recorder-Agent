"""Terminal-marker (stt_done) + stop-path finalization tests for /ws/stt.

Covers the 2026-07-09 fix for the intermittent "spinner forever / transcript
never inserted" STT stop bug (journal-proven: the trailing stt_final arrives
whenever the provider flushes, but clients used to hang up after a blind grace):

  1. ENDPOINT: {"type":"stt_done"} is ALWAYS the last frame — after the trailing
     final, after an stt_error, and even when the bridge produced NOTHING.
  2. OPENAI bridge: a hallucination-filtered final on the stop path sends an
     authoritative EMPTY stt_final and exits promptly (no 5s drain park).
  3. ELEVENLABS bridge: a filtered stop-final still delivers the carried
     rotation prefix (real speech from earlier epochs is not lost); a
     stop-commit that dies on Scribe's cap seam is surfaced (not swallowed) and
     the bridge returns promptly so stt_done reaches the client.
  4. GOOGLE bridge: the trailing final that Google flushes only AFTER the
     half-close is delivered BEFORE the bridge returns (so the endpoint's
     stt_done can never overtake it), and a filtered post-stop final emits the
     authoritative empty final. The gRPC SDK is faked via sys.modules (the lazy
     imports inside _google_bridge hit the sys.modules cache), which makes the
     worker-thread bridge unit-testable without credentials or network.

Fakes mirror test_stt_ws_reconnect.py's monkeypatch style; asyncio.run drives
the bridges directly (pytest-asyncio is not in auto mode here).
"""
import asyncio
import json
import sys
import time
import types

from fastapi import WebSocketDisconnect

from Orchestrator.routes import stt_ws_routes


# =============================================================================
# Shared fakes
# =============================================================================


class _ScriptClient:
    """Scripted client side of /ws/stt: yields its frames, then simulates a
    hangup (so a buggy bridge fails fast instead of hanging the test)."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []
        self.closed = 0

    async def accept(self):
        pass

    async def receive_json(self):
        if not self._frames:
            raise WebSocketDisconnect()
        return self._frames.pop(0)

    async def send_json(self, obj):
        self.sent.append(obj)

    async def close(self, *a, **k):
        self.closed += 1


class _GatedStopClient(_ScriptClient):
    """Like _ScriptClient but withholds its stt_stop until `gate` fires (used to
    force an ElevenLabs rotation BEFORE the stop is delivered)."""

    def __init__(self, frames, gate):
        super().__init__(frames)
        self._gate = gate

    async def receive_json(self):
        if self._frames and self._frames[0].get("type") == "stt_stop":
            await self._gate.wait()
        return await super().receive_json()


class _FakeWsModule:
    """Stands in for the `websockets` module ref inside stt_ws_routes. Hands out
    pre-built upstreams; optionally fires `second_connect` on the 2nd connect."""

    def __init__(self, sockets, second_connect=None):
        self._sockets = list(sockets)
        self._second_connect = second_connect
        self.connect_calls = []

    async def connect(self, url, **kwargs):
        self.connect_calls.append(url)
        sock = self._sockets.pop(0)
        if self._second_connect is not None and len(self.connect_calls) >= 2:
            self._second_connect.set()
        return sock


def _finals(client):
    return [m for m in client.sent if m.get("type") == "stt_final"]


# =============================================================================
# 1. Endpoint: stt_done is ALWAYS the last frame
# =============================================================================


def _ws_client(monkeypatch, fake_bridge=None, provider="openai"):
    import Orchestrator.app  # noqa: F401 — registers routes onto the shared app
    if fake_bridge is not None:
        monkeypatch.setattr(stt_ws_routes, "run_stt_bridge", fake_bridge)
    monkeypatch.setattr(stt_ws_routes, "resolve_stt_provider", lambda p=None: provider)
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app)


def test_stt_done_follows_the_trailing_final(monkeypatch):
    async def fake_bridge(ws, provider, start):
        await ws.send_json({"type": "stt_delta", "text": "Hel", "target": "prompt"})
        await ws.send_json({"type": "stt_final", "text": "Hello", "target": "prompt"})

    client = _ws_client(monkeypatch, fake_bridge)
    with client.websocket_connect("/ws/stt") as ws:
        ws.send_json({"type": "stt_start", "target": "prompt"})
        assert ws.receive_json()["type"] == "stt_delta"
        assert ws.receive_json()["type"] == "stt_final"
        assert ws.receive_json() == {"type": "stt_done"}, (
            "the terminal marker must be the frame AFTER the trailing final"
        )


def test_stt_done_sent_even_when_bridge_produces_nothing(monkeypatch):
    async def fake_bridge(ws, provider, start):
        return  # finalization produced NOTHING (empty/filtered/failed)

    client = _ws_client(monkeypatch, fake_bridge)
    with client.websocket_connect("/ws/stt") as ws:
        ws.send_json({"type": "stt_start", "target": "prompt"})
        assert ws.receive_json() == {"type": "stt_done"}, (
            "a session that finalizes to nothing must STILL tell the client it is over"
        )


def test_bridge_exception_sends_stt_error_then_stt_done(monkeypatch):
    async def fake_bridge(ws, provider, start):
        raise RuntimeError("provider exploded")

    client = _ws_client(monkeypatch, fake_bridge)
    with client.websocket_connect("/ws/stt") as ws:
        ws.send_json({"type": "stt_start", "target": "prompt"})
        err = ws.receive_json()
        assert err["type"] == "stt_error" and "provider exploded" in err["message"]
        assert ws.receive_json() == {"type": "stt_done"}


def test_no_provider_sends_stt_error_then_stt_done(monkeypatch):
    client = _ws_client(monkeypatch, provider=None)
    with client.websocket_connect("/ws/stt") as ws:
        ws.send_json({"type": "stt_start", "target": "prompt"})
        assert ws.receive_json()["type"] == "stt_error"
        assert ws.receive_json() == {"type": "stt_done"}


# =============================================================================
# 2. OpenAI bridge: filtered stop-final -> authoritative empty final, no park
# =============================================================================


class _FakeOpenAIUpstream:
    """Fake OpenAI realtime socket: emits its scripted transcription events only
    AFTER the bridge sends input_audio_buffer.commit (the manual-stop flush),
    then PARKS forever — so a bridge that fails to exit on a filtered final sits
    in its 5s drain and blows the test's 2s deadline."""

    def __init__(self, frames_after_commit):
        self._frames = [json.dumps(f) for f in frames_after_commit]
        self._commit = asyncio.Event()
        self._never = asyncio.Event()
        self.sent = []
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._commit.wait()
        if self._frames:
            return self._frames.pop(0)
        await self._never.wait()
        raise StopAsyncIteration  # unreachable

    async def send(self, data):
        self.sent.append(data)
        try:
            if json.loads(data).get("type") == "input_audio_buffer.commit":
                self._commit.set()
        except Exception:
            pass

    async def close(self, *a, **k):
        self.closed += 1


def _run_openai_bridge(monkeypatch, upstream_frames, timeout=2.0):
    async def scenario():
        upstream = _FakeOpenAIUpstream(upstream_frames)
        fake_ws_module = _FakeWsModule([upstream])
        client = _ScriptClient([
            {"type": "stt_audio", "pcm": "AAAA"},
            {"type": "stt_stop"},
        ])
        monkeypatch.setattr(stt_ws_routes, "websockets", fake_ws_module)
        monkeypatch.setattr(stt_ws_routes.config, "OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(stt_ws_routes.config, "OPENAI_REALTIME_URL",
                            "wss://fake.test/v1/realtime")
        # The 2s deadline IS the assertion that the bridge exits promptly on the
        # stop path instead of parking on its 5s relay-drain backstop.
        await asyncio.wait_for(
            stt_ws_routes._openai_bridge(
                client, target="prompt", lang="en", sample_rate=24000
            ),
            timeout=timeout,
        )
        return client

    return asyncio.run(scenario())


def test_openai_filtered_stop_final_sends_empty_final_promptly(monkeypatch):
    client = _run_openai_bridge(monkeypatch, [
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "Thank you."},   # canonical whisper hallucination
    ])
    finals = _finals(client)
    assert finals == [{"type": "stt_final", "text": "", "target": "prompt"}], (
        f"a filtered stop-final must become an authoritative EMPTY final "
        f"(client discards, not resurrects, the interim); sent={client.sent}"
    )


def test_openai_stop_final_delivered(monkeypatch):
    client = _run_openai_bridge(monkeypatch, [
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "hello world from openai"},
    ])
    finals = _finals(client)
    assert len(finals) == 1 and finals[0]["text"] == "hello world from openai", (
        f"trailing final lost: {client.sent}"
    )


# =============================================================================
# 3. ElevenLabs bridge: cap-seam stop paths
# =============================================================================


class _RotatingScribe:
    """Epoch-1 Scribe fake: streams a partial then hits the session cap."""

    def __init__(self, partial_text):
        self._frames = [
            json.dumps({"message_type": "partial_transcript", "text": partial_text}),
            json.dumps({"message_type": "session_time_limit_exceeded",
                        "error": "Maximum session duration exceeded"}),
        ]
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


class _CommitGatedScribe:
    """Epoch-2 Scribe fake: emits its committed (final) frame only after the
    feeder sends the commit=True stop flush, then parks (cancellable)."""

    def __init__(self, committed_text):
        self._committed = json.dumps(
            {"message_type": "committed_transcript", "text": committed_text})
        self._commit = asyncio.Event()
        self._never = asyncio.Event()
        self._emitted = False
        self.sent = []
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._commit.wait()
        if not self._emitted:
            self._emitted = True
            return self._committed
        await self._never.wait()
        raise StopAsyncIteration  # unreachable

    async def send(self, data):
        self.sent.append(data)
        try:
            if json.loads(data).get("commit"):
                self._commit.set()
        except Exception:
            pass

    async def close(self, *a, **k):
        self.closed += 1


class _DeadOnCommitScribe:
    """Accepts audio but RAISES on the commit (stop) frame — Scribe's cap-close
    landing exactly at the stop seam. Emits nothing (parks, cancellable)."""

    def __init__(self):
        self._never = asyncio.Event()
        self.sent = []
        self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._never.wait()
        raise StopAsyncIteration  # unreachable

    async def send(self, data):
        if json.loads(data).get("commit"):
            raise RuntimeError("socket closed (scribe session cap)")
        self.sent.append(data)

    async def close(self, *a, **k):
        self.closed += 1


def test_elevenlabs_filtered_stop_final_still_delivers_rotation_prefix(monkeypatch):
    """Cap seam: epoch 1 transcribed real speech (carried as the rotation
    prefix), then the post-rotation stop commit returns an empty/hallucinated
    final. The REAL speech must still be delivered as the final."""

    async def scenario():
        gate = asyncio.Event()
        fake_ws_module = _FakeWsModule(
            [_RotatingScribe("hello world we are testing"),
             _CommitGatedScribe("")],   # empty committed final -> hallucination-filtered
            second_connect=gate,
        )
        client = _GatedStopClient([
            {"type": "stt_audio", "pcm": "AAAA"},
            {"type": "stt_audio", "pcm": "AAAA"},
            {"type": "stt_stop"},
        ], gate)
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

    assert len(fake_ws_module.connect_calls) == 2, "rotation did not happen"
    finals = _finals(client)
    assert finals, f"stop-pending final was LOST at the cap seam; sent={client.sent}"
    assert finals[-1]["text"] == "hello world we are testing", (
        f"the carried prefix (real speech) must survive a filtered stop-final; "
        f"got {finals[-1]!r}"
    )


def test_elevenlabs_dead_commit_surfaces_and_returns_promptly(monkeypatch):
    """A stop-commit that dies on Scribe's socket means NO final is coming: the
    bridge must skip its 5s drain and return promptly (so the endpoint's
    stt_done reaches the client and IT can commit the newest partial)."""

    async def scenario():
        fake_ws_module = _FakeWsModule([_DeadOnCommitScribe()])
        client = _ScriptClient([
            {"type": "stt_audio", "pcm": "AAAA"},
            {"type": "stt_stop"},
        ])
        monkeypatch.setattr(stt_ws_routes, "websockets", fake_ws_module)
        monkeypatch.setattr(stt_ws_routes, "resolve_api_key", lambda: "test-key")
        # 2s deadline: the old code silently swallowed the send failure and then
        # burned a REAL 5s in the final-drain backstop.
        await asyncio.wait_for(
            stt_ws_routes._elevenlabs_bridge(
                client, target="prompt", lang="en", sample_rate=24000
            ),
            timeout=2.0,
        )
        return fake_ws_module, client

    fake_ws_module, client = asyncio.run(scenario())

    assert len(fake_ws_module.connect_calls) == 1, "dead commit must not reconnect"
    assert not _finals(client), (
        f"no final can exist after a dead commit (the CLIENT synthesizes the "
        f"fallback from its newest partial); sent={client.sent}"
    )
    assert not any(m.get("type") == "stt_error" for m in client.sent), (
        "the cap seam is an expected provider behavior, not a client-facing error"
    )


# =============================================================================
# 4. Google bridge: late-flushed final delivered BEFORE the bridge returns
# =============================================================================


def _fake_google_sdk(monkeypatch, final_text, stall_s=0.0):
    """Install sys.modules fakes for the lazy imports inside _google_bridge.

    The fake SpeechClient consumes the request generator to EXHAUSTION (i.e. it
    waits for the half-close that stt_stop triggers) and only then yields the
    trailing final — modeling the journal-proven incident where Google flushes
    the final after the client used to hang up.

    `stall_s` sleeps in the worker thread AFTER the half-close before ending
    the stream (models a stalled gRPC flush); `final_text=None` produces NO
    trailing final at all. The stall must stay short: asyncio.run's loop
    shutdown waits for lingering executor threads.
    """

    class _AudioEncoding:
        LINEAR16 = 1

    class _ExplicitDecodingConfig:
        AudioEncoding = _AudioEncoding

        def __init__(self, **kw):
            pass

    def _kwargs_cls(name):
        return type(name, (), {"__init__": lambda self, **kw: None})

    def _response(text, is_final):
        alt = types.SimpleNamespace(transcript=text)
        result = types.SimpleNamespace(alternatives=[alt], is_final=is_final)
        return types.SimpleNamespace(results=[result])

    class _FakeSpeechClient:
        def __init__(self, *a, **k):
            pass

        def streaming_recognize(self, requests):
            def _gen():
                for _ in requests:
                    pass  # block until the bridge half-closes (None sentinel)
                if stall_s:
                    time.sleep(stall_s)  # stalled provider flush (worker thread)
                if final_text is not None:
                    yield _response(final_text, True)
            return _gen()

    co_mod = types.ModuleType("google.api_core.client_options")
    co_mod.ClientOptions = _kwargs_cls("ClientOptions")
    v2_mod = types.ModuleType("google.cloud.speech_v2")
    v2_mod.SpeechClient = _FakeSpeechClient
    types_mod = types.ModuleType("google.cloud.speech_v2.types")
    types_mod.ExplicitDecodingConfig = _ExplicitDecodingConfig
    types_mod.RecognitionConfig = _kwargs_cls("RecognitionConfig")
    types_mod.StreamingRecognitionConfig = _kwargs_cls("StreamingRecognitionConfig")
    types_mod.StreamingRecognitionFeatures = _kwargs_cls("StreamingRecognitionFeatures")
    types_mod.StreamingRecognizeRequest = _kwargs_cls("StreamingRecognizeRequest")

    monkeypatch.setitem(sys.modules, "google.api_core.client_options", co_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.speech_v2", v2_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.speech_v2.types", types_mod)


def _run_google_bridge(monkeypatch, tmp_path, final_text, stall_s=0.0):
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"project_id": "test-proj"}))
    monkeypatch.setattr(stt_ws_routes.config, "GOOGLE_APPLICATION_CREDENTIALS",
                        str(creds))
    _fake_google_sdk(monkeypatch, final_text, stall_s=stall_s)

    async def scenario():
        client = _ScriptClient([
            {"type": "stt_audio", "pcm": "AAAA"},
            {"type": "stt_stop"},
        ])
        await asyncio.wait_for(
            stt_ws_routes._google_bridge(
                client, target="prompt", lang="en", sample_rate=24000
            ),
            timeout=10.0,
        )
        # KEY assertion point: everything asserted on `client.sent` below was
        # delivered BEFORE the bridge returned — i.e. the endpoint's stt_done
        # (sent right after the bridge returns) can never overtake the final.
        return client

    return asyncio.run(scenario())


def test_google_post_stop_final_delivered_before_bridge_returns(monkeypatch, tmp_path):
    client = _run_google_bridge(monkeypatch, tmp_path, "hello from google")
    finals = _finals(client)
    assert len(finals) == 1 and finals[0]["text"] == "hello from google", (
        f"the final Google flushes AFTER the half-close must be delivered before "
        f"the bridge returns; sent={client.sent}"
    )
    assert not any(m.get("type") == "stt_error" for m in client.sent), client.sent


def test_google_filtered_post_stop_final_emits_empty_final(monkeypatch, tmp_path):
    client = _run_google_bridge(monkeypatch, tmp_path, "Thank you.")
    finals = _finals(client)
    assert finals == [{"type": "stt_final", "text": "", "target": "prompt"}], (
        f"a hallucination-filtered post-stop final must become an authoritative "
        f"EMPTY final; sent={client.sent}"
    )


def test_google_flush_expiry_returns_promptly_without_final(monkeypatch, tmp_path):
    """A Google worker stalled past _GOOGLE_STOP_FLUSH_TIMEOUT_S must NOT hang
    the bridge: it returns (without a trailing final) so the endpoint's stt_done
    still reaches the client, which then commits its newest partial. The flush
    timeout is patched to 0.2s and the worker stalls for 1.2s — well past it —
    while the overall wait_for proves the bridge did not ride out the stall."""

    monkeypatch.setattr(stt_ws_routes, "_GOOGLE_STOP_FLUSH_TIMEOUT_S", 0.2)
    start_ts = time.monotonic()
    client = _run_google_bridge(monkeypatch, tmp_path, final_text=None, stall_s=1.2)
    elapsed = time.monotonic() - start_ts

    assert not _finals(client), (
        f"no final exists on the flush-expiry path; sent={client.sent}"
    )
    assert not any(m.get("type") == "stt_error" for m in client.sent), (
        f"a stalled flush is a bounded-drain case, not a client-facing error; "
        f"sent={client.sent}"
    )
    # The bridge itself returned at ~0.2s; the remaining elapsed time is
    # asyncio.run's loop shutdown waiting out the lingering 1.2s worker thread.
    # Anything near/over the stall duration + margin means the bridge hung.
    assert elapsed < 3.0, f"bridge rode out the stalled flush ({elapsed:.2f}s)"
