"""Unit tests for the on-box VAD-gated /ws/stt loop (W2, plan 2026-07-22).

Drives ws_stt / _onbox_vad_loop directly with a scripted fake client WebSocket,
a fake UtteranceGate and a fake transcriber (house pattern: mirrors
test_onbox_stt_bridge.py's asyncio.run idiom — no network, no real sleeps).

Contract under test:
- session start warms AND PRIMES (2026-07-22 bridge audit: health 200 proves
  HTTP liveness, not whisper residency) before stt_status listening;
- event sequence loading_models -> listening -> speech -> processing -> final
  -> stt_done, finals in the exact cloud-bridge shape;
- D10: prime/warm ceiling -> honest stt_error, NEVER a silent cloud fallback;
- hallucination-filtered mid-stream finals are suppressed;
- client stop / disconnect -> gate.flush() tail is transcribed and delivered;
- 429 on the transcription POST is retried; the STREAM model is used;
- ONBOX_STT_REALTIME=1 routes to the parked realtime bridge (default = VAD);
- missing VAD deps -> honest stt_error naming the missing piece.
"""
import asyncio
import base64

import pytest
from fastapi import WebSocketDisconnect

from Orchestrator.routes import stt_ws_routes
from Orchestrator.stt.vad import Event, EventKind


_PCM = base64.b64encode(b"\x00\x00" * 240).decode()


class _FakeClientWS:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def accept(self):
        pass

    async def receive_json(self):
        if not self._frames:
            raise WebSocketDisconnect()
        return self._frames.pop(0)

    async def send_json(self, obj):
        self.sent.append(obj)


class _FakeGate:
    """Scripted UtteranceGate: each feed() pops the next event list."""

    def __init__(self, feeds=(), flush=None):
        self._feeds = [list(f) for f in feeds]
        self._flush = flush
        self.fed = []
        self.flushed = 0

    def feed(self, pcm):
        self.fed.append(pcm)
        return self._feeds.pop(0) if self._feeds else []

    def flush(self):
        self.flushed += 1
        ev, self._flush = self._flush, None
        return ev


def _patch_ready(monkeypatch, *, gate, transcripts):
    """Standard 'stack is warm and healthy' patch set for the VAD loop."""
    monkeypatch.setattr(stt_ws_routes, "_vad_missing_dep", lambda: None)

    async def _warm_ok(url):
        return True
    monkeypatch.setattr(stt_ws_routes, "_probe_speaches_health", _warm_ok)

    async def _prime_ok(timeout_s):
        return None
    monkeypatch.setattr(stt_ws_routes, "_prime_stt_model", _prime_ok)
    monkeypatch.setattr(stt_ws_routes, "_make_utterance_gate", lambda: gate)

    calls = []

    def _fake_transcribe(pcm):
        calls.append(pcm)
        return transcripts.pop(0)
    monkeypatch.setattr(stt_ws_routes, "_transcribe_utterance", _fake_transcribe)
    return calls


# ── event sequence through the full endpoint (incl. terminal stt_done) ──────

def test_vad_loop_event_sequence_through_endpoint(monkeypatch):
    async def scenario():
        gate = _FakeGate(feeds=[
            [Event(EventKind.SPEECH_START)],
            [Event(EventKind.SPEECH_END, b"utt-pcm")],
        ])
        seen_voice = {"active": False}

        def _transcribe(pcm):
            from Orchestrator import local_stack
            if local_stack.is_voice_active():
                seen_voice["active"] = True   # D12 held during transcription
            return "hello vad"

        monkeypatch.setattr(stt_ws_routes, "_vad_missing_dep", lambda: None)

        async def _warm_ok(url):
            return True
        monkeypatch.setattr(stt_ws_routes, "_probe_speaches_health", _warm_ok)

        async def _prime_ok(timeout_s):
            return None
        monkeypatch.setattr(stt_ws_routes, "_prime_stt_model", _prime_ok)
        monkeypatch.setattr(stt_ws_routes, "_make_utterance_gate", lambda: gate)
        monkeypatch.setattr(stt_ws_routes, "_transcribe_utterance", _transcribe)
        monkeypatch.setattr(stt_ws_routes, "resolve_stt_provider",
                            lambda p=None, **kw: "onbox")
        monkeypatch.setattr(stt_ws_routes, "local_streaming_stt_available", lambda: False)
        monkeypatch.setattr(stt_ws_routes, "onbox_stt_available", lambda: True)
        monkeypatch.delenv("ONBOX_STT_REALTIME", raising=False)

        client = _FakeClientWS([
            {"type": "stt_start", "target": "prompt", "provider": "onbox",
             "sample_rate": 24000},
            {"type": "stt_audio", "pcm": _PCM},
            {"type": "stt_audio", "pcm": _PCM},
            {"type": "stt_stop"},
        ])
        await asyncio.wait_for(stt_ws_routes.ws_stt(client), timeout=10.0)
        return client, gate, seen_voice

    client, gate, seen_voice = asyncio.run(scenario())
    states = [(m.get("type"), m.get("state")) for m in client.sent]
    assert states[0] == ("stt_status", "loading_models")
    assert states[1] == ("stt_status", "listening")
    assert states[2] == ("stt_status", "speech")
    assert states[3] == ("stt_status", "processing")
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals == [{"type": "stt_final", "text": "hello vad", "target": "prompt"}]
    assert client.sent[-1] == {"type": "stt_done"}          # terminal marker last
    assert client.sent.index(finals[0]) < len(client.sent) - 1
    assert gate.flushed == 1                                 # stop flushed the gate
    assert seen_voice["active"] is True                      # D12 voice_session held


# ── D10: warm/prime ceiling -> honest stt_error, never silent cloud ─────────

def test_priming_failure_yields_honest_error_no_fallback(monkeypatch):
    async def scenario():
        gate = _FakeGate()
        _patch_ready(monkeypatch, gate=gate, transcripts=[])

        async def _prime_boom(timeout_s):
            raise RuntimeError("model load failed")
        monkeypatch.setattr(stt_ws_routes, "_prime_stt_model", _prime_boom)

        class _Boom:
            async def connect(self, *a, **k):
                raise AssertionError("must NOT open any upstream after a failed prime")
        monkeypatch.setattr(stt_ws_routes, "websockets", _Boom())

        client = _FakeClientWS([{"type": "stt_audio", "pcm": _PCM}])
        await asyncio.wait_for(
            stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                          sample_rate=24000),
            timeout=10.0)
        return client

    client = asyncio.run(scenario())
    types = [m.get("type") for m in client.sent]
    assert types[0] == "stt_status"                        # affordance shown
    assert client.sent[0]["state"] == "loading_models"
    assert types[-1] == "stt_error"                        # honest error
    assert not any(m.get("type") == "stt_final" for m in client.sent)


def test_warm_ceiling_yields_honest_error_prime_never_runs(monkeypatch):
    async def scenario():
        gate = _FakeGate()
        _patch_ready(monkeypatch, gate=gate, transcripts=[])
        monkeypatch.setattr(stt_ws_routes, "_ONBOX_WARM_CEILING_S", 0.1)
        monkeypatch.setattr(stt_ws_routes, "_ONBOX_WARM_POLL_S", 0.02)

        async def _never(url):
            return False
        monkeypatch.setattr(stt_ws_routes, "_probe_speaches_health", _never)

        async def _prime_forbidden(timeout_s):
            raise AssertionError("prime must not run before health is 200")
        monkeypatch.setattr(stt_ws_routes, "_prime_stt_model", _prime_forbidden)

        client = _FakeClientWS([{"type": "stt_audio", "pcm": _PCM}])
        await asyncio.wait_for(
            stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                          sample_rate=24000),
            timeout=10.0)
        return client

    client = asyncio.run(scenario())
    assert client.sent[0] == {"type": "stt_status", "state": "loading_models"}
    assert client.sent[-1]["type"] == "stt_error"
    assert not any(m.get("type") == "stt_final" for m in client.sent)


# ── hallucination filter ────────────────────────────────────────────────────

def test_hallucination_filtered_final_suppressed(monkeypatch):
    async def scenario():
        gate = _FakeGate(feeds=[
            [Event(EventKind.SPEECH_START)],
            [Event(EventKind.SPEECH_END, b"utt")],
        ])
        _patch_ready(monkeypatch, gate=gate, transcripts=["Thank you."])
        monkeypatch.setattr(stt_ws_routes, "is_whisper_hallucination",
                            lambda t: t == "Thank you.")
        client = _FakeClientWS([
            {"type": "stt_audio", "pcm": _PCM},
            {"type": "stt_audio", "pcm": _PCM},
            {"type": "stt_stop"},
        ])
        await asyncio.wait_for(
            stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                          sample_rate=24000),
            timeout=10.0)
        return client

    client = asyncio.run(scenario())
    # the filtered mid-stream final is SUPPRESSED (no stt_final at all) and the
    # loop resumes listening afterwards
    assert not any(m.get("type") == "stt_final" for m in client.sent)
    states = [(m.get("type"), m.get("state")) for m in client.sent]
    proc = states.index(("stt_status", "processing"))
    assert ("stt_status", "listening") in states[proc + 1:]


# ── flush on stop / disconnect ──────────────────────────────────────────────

def test_stop_flush_emits_tail_final(monkeypatch):
    async def scenario():
        gate = _FakeGate(feeds=[[Event(EventKind.SPEECH_START)]],
                         flush=Event(EventKind.SPEECH_END, b"tail-pcm"))
        _patch_ready(monkeypatch, gate=gate, transcripts=["tail words"])
        client = _FakeClientWS([
            {"type": "stt_audio", "pcm": _PCM},
            {"type": "stt_stop"},
        ])
        await asyncio.wait_for(
            stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                          sample_rate=24000),
            timeout=10.0)
        return client, gate

    client, gate = asyncio.run(scenario())
    assert gate.flushed == 1
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals == [{"type": "stt_final", "text": "tail words", "target": "prompt"}]


def test_flush_on_disconnect_emits_tail_final(monkeypatch):
    async def scenario():
        gate = _FakeGate(feeds=[[Event(EventKind.SPEECH_START)]],
                         flush=Event(EventKind.SPEECH_END, b"tail-pcm"))
        _patch_ready(monkeypatch, gate=gate, transcripts=["dropped mid word"])
        # frames run out -> receive_json raises WebSocketDisconnect (abrupt drop)
        client = _FakeClientWS([{"type": "stt_audio", "pcm": _PCM}])
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(
                stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                              sample_rate=24000),
                timeout=10.0)
        return client, gate

    client, gate = asyncio.run(scenario())
    assert gate.flushed == 1
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals == [{"type": "stt_final", "text": "dropped mid word",
                       "target": "prompt"}]


# ── transcription POST: 429 retried, STREAM model used ──────────────────────

def test_transcribe_utterance_retries_429_and_uses_stream_model(monkeypatch):
    from Orchestrator.stt import file_transcribe as ft
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "stt_stream_model", lambda: "turbo-model")
    monkeypatch.setattr(local_stack, "base_url_root", lambda: "http://127.0.0.1:9098")
    monkeypatch.setattr(ft, "_ONBOX_429_BACKOFF_BASE", 0)
    monkeypatch.setattr(ft, "_ONBOX_429_BACKOFF_MAX", 0)

    class _Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

    responses = [_Resp(429, {}), _Resp(200, {"text": " ok "})]
    calls = []

    def _post(url, data=None, files=None, timeout=None):
        calls.append({"url": url, "data": data, "timeout": timeout})
        return responses.pop(0)
    monkeypatch.setattr(ft.requests, "post", _post)

    out = stt_ws_routes._transcribe_utterance(b"\x00\x00" * 1600)
    assert out == "ok"
    assert len(calls) == 2                                  # 429 retried
    assert all(c["data"]["model"] == "turbo-model" for c in calls)
    assert calls[0]["url"].endswith("/upstream/speaches/v1/audio/transcriptions")


# ── parked realtime bridge behind ONBOX_STT_REALTIME=1 ──────────────────────

def test_realtime_env_routes_to_parked_bridge(monkeypatch):
    called = {}

    async def _parked(ws, **kw):
        called["parked"] = True

    async def _vad(ws, **kw):
        called["vad"] = True
    monkeypatch.setattr(stt_ws_routes, "_onbox_bridge", _parked)
    monkeypatch.setattr(stt_ws_routes, "_onbox_vad_loop", _vad)
    monkeypatch.setenv("ONBOX_STT_REALTIME", "1")
    asyncio.run(stt_ws_routes.run_stt_bridge(None, "onbox", {"target": "prompt"}))
    assert called == {"parked": True}


def test_default_routes_to_vad_loop(monkeypatch):
    called = {}

    async def _parked(ws, **kw):
        called["parked"] = True

    async def _vad(ws, **kw):
        called["vad"] = True
    monkeypatch.setattr(stt_ws_routes, "_onbox_bridge", _parked)
    monkeypatch.setattr(stt_ws_routes, "_onbox_vad_loop", _vad)
    monkeypatch.delenv("ONBOX_STT_REALTIME", raising=False)
    asyncio.run(stt_ws_routes.run_stt_bridge(None, "onbox", {"target": "prompt"}))
    assert called == {"vad": True}


# ── missing VAD deps -> honest stt_error naming the missing piece ───────────

def test_missing_vad_deps_honest_error(monkeypatch):
    async def scenario():
        monkeypatch.setattr(stt_ws_routes, "_vad_missing_dep",
                            lambda: "onnxruntime is not available")

        async def _warm_forbidden(ws):
            raise AssertionError("must not warm when VAD deps are missing")
        monkeypatch.setattr(stt_ws_routes, "_warm_and_prime", _warm_forbidden)
        client = _FakeClientWS([{"type": "stt_audio", "pcm": _PCM}])
        await asyncio.wait_for(
            stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                          sample_rate=24000),
            timeout=10.0)
        return client

    client = asyncio.run(scenario())
    assert len(client.sent) == 1
    assert client.sent[0]["type"] == "stt_error"
    assert "onnxruntime" in client.sent[0]["message"]      # names the missing piece
    assert not any(m.get("type") == "stt_final" for m in client.sent)


def test_vad_missing_dep_reports_model_absent(monkeypatch, tmp_path):
    pytest.importorskip("onnxruntime")
    from Orchestrator.stt import vad
    monkeypatch.setattr(vad, "default_vad_model_path",
                        lambda: tmp_path / "nope.onnx")
    msg = stt_ws_routes._vad_missing_dep()
    assert msg is not None and "silero" in msg.lower()


# ── priming WAV helper is a real 16k mono WAV ───────────────────────────────

def test_silence_wav_is_valid_16k_mono():
    import io
    import wave
    data = stt_ws_routes._silence_wav()
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        assert w.getnframes() == int(16000 * 0.2)
