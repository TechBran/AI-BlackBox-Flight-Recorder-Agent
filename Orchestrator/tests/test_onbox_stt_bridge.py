"""Unit tests for the on-box (Design-B) streaming STT bridge.

Drives _onbox_bridge directly with a scripted fake client WebSocket and a fake
Speaches upstream. No network, no real sleeps: the warm probe and websockets
module are monkeypatched. Mirrors test_stt_ws_reconnect.py's asyncio.run idiom.
"""
import asyncio
import json

from fastapi import WebSocketDisconnect

from Orchestrator.routes import stt_ws_routes


class _FakeUpstream:
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
    def __init__(self, sock):
        self._sock = sock
        self.connect_calls = []
    async def connect(self, url, **kwargs):
        self.connect_calls.append(url)
        return self._sock


class _FakeClientWS:
    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0
        self.sent = []
        self.closed = 0
    async def accept(self):
        pass
    async def receive_json(self):
        if self._i >= len(self._frames):
            raise WebSocketDisconnect()
        f = self._frames[self._i]; self._i += 1
        return f
    async def send_json(self, obj):
        self.sent.append(obj)
    async def close(self, *a, **k):
        self.closed += 1


def _patch_localstack(monkeypatch):
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "stt_stream_model", lambda: "turbo")
    monkeypatch.setattr(local_stack, "speaches_realtime_ws_url",
                        lambda m, **k: "ws://127.0.0.1:9099/v1/realtime?model=turbo")
    monkeypatch.setattr(local_stack, "speaches_warm_url",
                        lambda: "http://127.0.0.1:9098/upstream/speaches/health")


def test_onbox_bridge_emits_loading_affordance_then_relays_final(monkeypatch):
    async def scenario():
        _patch_localstack(monkeypatch)
        # Warm succeeds immediately.
        async def _warm_ok(url):
            return True
        monkeypatch.setattr(stt_ws_routes, "_probe_speaches_health", _warm_ok)
        upstream = _FakeUpstream([
            {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": "hello on box"},
        ])
        monkeypatch.setattr(stt_ws_routes, "websockets", _FakeWsModule(upstream))
        client = _FakeClientWS([
            {"type": "stt_audio", "pcm": "AAAA"},
            {"type": "stt_stop"},
        ])
        await asyncio.wait_for(
            stt_ws_routes._onbox_bridge(client, target="prompt", lang="en", sample_rate=24000),
            timeout=10.0)
        return client, upstream
    client, upstream = asyncio.run(scenario())
    types = [m.get("type") for m in client.sent]
    assert types[0] == "stt_status" and client.sent[0]["state"] == "loading_models"
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals and finals[-1]["text"] == "hello on box"
    assert finals[-1]["target"] == "prompt"


def test_onbox_bridge_ceiling_yields_honest_error_no_switch(monkeypatch):
    async def scenario():
        _patch_localstack(monkeypatch)
        monkeypatch.setattr(stt_ws_routes, "_ONBOX_WARM_CEILING_S", 0.1)
        monkeypatch.setattr(stt_ws_routes, "_ONBOX_WARM_POLL_S", 0.02)
        async def _never(url):
            return False
        monkeypatch.setattr(stt_ws_routes, "_probe_speaches_health", _never)
        # If the bridge tried to connect anyway, this would blow up the test.
        class _Boom:
            async def connect(self, *a, **k):
                raise AssertionError("must NOT connect after warm ceiling")
        monkeypatch.setattr(stt_ws_routes, "websockets", _Boom())
        client = _FakeClientWS([{"type": "stt_audio", "pcm": "AAAA"}])
        await asyncio.wait_for(
            stt_ws_routes._onbox_bridge(client, target="prompt", lang="en", sample_rate=24000),
            timeout=10.0)
        return client
    client = asyncio.run(scenario())
    types = [m.get("type") for m in client.sent]
    assert "stt_status" in types                 # affordance was shown
    assert types[-1] == "stt_error"              # honest error, no cloud switch
    # never emitted a provider-switch / non-onbox final
    assert not any(m.get("type") == "stt_final" for m in client.sent)


def test_onbox_bridge_holds_voice_session_during_relay(monkeypatch):
    async def scenario():
        _patch_localstack(monkeypatch)
        from Orchestrator import local_stack
        seen = {"active": False}
        async def _warm_ok(url):
            return True
        monkeypatch.setattr(stt_ws_routes, "_probe_speaches_health", _warm_ok)

        class _Probe(_FakeUpstream):
            async def send(self, data):
                # while the bridge is streaming to Speaches, the voice session
                # must be held (retrieval_gate would block).
                if local_stack.is_voice_active():
                    seen["active"] = True
                await super().send(data)
        upstream = _Probe([
            {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": "x"},
        ])
        monkeypatch.setattr(stt_ws_routes, "websockets", _FakeWsModule(upstream))
        client = _FakeClientWS([{"type": "stt_audio", "pcm": "AAAA"}, {"type": "stt_stop"}])
        await asyncio.wait_for(
            stt_ws_routes._onbox_bridge(client, target="prompt", lang="en", sample_rate=24000),
            timeout=10.0)
        return seen
    seen = asyncio.run(scenario())
    assert seen["active"] is True
