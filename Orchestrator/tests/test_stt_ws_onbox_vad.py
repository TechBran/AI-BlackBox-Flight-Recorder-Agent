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
- missing VAD deps -> honest stt_error naming the missing piece;
- W3 rolling partials: every ONBOX_STT_PARTIAL_MS while speech is active the
  OPEN utterance buffer is transcribed and emitted as a cumulative stt_delta
  (the exact shape the Android live-partials chip / Portal interim consume);
  DROP-FRAME (one in flight max, due ticks while busy are skipped, never
  queued); partials stop at SPEECH_END and the final supersedes a stale
  in-flight result; ONBOX_STT_PARTIALS=0 disables.
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

    def __init__(self, feeds=(), flush=None, active=b""):
        self._feeds = [list(f) for f in feeds]
        self._flush = flush
        self.active = active            # W3: the "open utterance buffer"
        self.fed = []
        self.flushed = 0

    def feed(self, pcm):
        self.fed.append(pcm)
        return self._feeds.pop(0) if self._feeds else []

    def flush(self):
        self.flushed += 1
        ev, self._flush = self._flush, None
        return ev

    def active_pcm(self):
        return self.active


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


# ── W3: rolling partials while speech is active ─────────────────────────────

def _audio():
    return {"type": "stt_audio", "pcm": _PCM}


class _ClockedClientWS(_FakeClientWS):
    """Frames are (dt_seconds, frame) pairs: receiving advances the fake
    partial clock by dt and yields to the event loop (a real socket receive
    awaits too), so in-flight partial tasks get scheduled between frames.
    A frame may be a CALLABLE (runs side effects at that point in the
    timeline — e.g. releasing a blocked transcriber) returning the frame."""

    def __init__(self, timed_frames, clock):
        super().__init__([])
        self._timed = list(timed_frames)
        self._clock = clock

    async def receive_json(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if not self._timed:
            raise WebSocketDisconnect()
        dt, frame = self._timed.pop(0)
        self._clock["t"] += dt
        if callable(frame):
            frame = frame()
        return frame


def _patch_partial_clock(monkeypatch):
    """Fake clock for the W3 cadence + clean partial-env defaults."""
    clock = {"t": 0.0}
    monkeypatch.delenv("ONBOX_STT_PARTIALS", raising=False)
    monkeypatch.delenv("ONBOX_STT_PARTIAL_MS", raising=False)
    monkeypatch.setattr(stt_ws_routes, "_monotonic", lambda: clock["t"])
    return clock


def test_partial_cadence_honored_with_fake_clock(monkeypatch):
    async def scenario():
        clock = _patch_partial_clock(monkeypatch)
        gate = _FakeGate(
            feeds=[[Event(EventKind.SPEECH_START)], [], [], [], [], [], [],
                   [Event(EventKind.SPEECH_END, b"utt")]],
            active=b"cur-buf")
        _patch_ready(monkeypatch, gate=gate, transcripts=["the final"])
        partial_calls = []
        partial_texts = ["hello there", "hello there friend"]

        async def _fake_partial(pcm):
            partial_calls.append((clock["t"], pcm))
            return partial_texts.pop(0)
        monkeypatch.setattr(stt_ws_routes, "_transcribe_partial", _fake_partial)

        frames = [
            (0.1, _audio()),   # SPEECH_START -> first partial due at 1.6
            (0.5, _audio()),   # t=0.6  not due
            (1.1, _audio()),   # t=1.7  due -> partial #1; next due 3.2
            (0.1, _audio()),   # t=1.8  (lets the emit land)
            (0.7, _audio()),   # t=2.5  not due
            (0.8, _audio()),   # t=3.3  due -> partial #2; next due 4.8
            (0.1, _audio()),   # t=3.4  (lets the emit land)
            (0.1, _audio()),   # t=3.5  SPEECH_END -> final
            (0.1, {"type": "stt_stop"}),
        ]
        client = _ClockedClientWS(frames, clock)
        await asyncio.wait_for(
            stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                          sample_rate=24000),
            timeout=10.0)
        return client, partial_calls

    client, partial_calls = asyncio.run(scenario())
    # cadence honored: exactly the two DUE ticks fired, on the open buffer
    assert [round(t, 2) for t, _ in partial_calls] == [1.7, 3.3]
    assert all(pcm == b"cur-buf" for _, pcm in partial_calls)
    # partials go out as CUMULATIVE stt_delta in the exact cloud-bridge shape
    # (Android live-partials chip + Portal interim consume {type,text,target})
    deltas = [m for m in client.sent if m.get("type") == "stt_delta"]
    assert deltas == [
        {"type": "stt_delta", "text": "hello there", "target": "prompt"},
        {"type": "stt_delta", "text": "hello there friend", "target": "prompt"},
    ]
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals == [{"type": "stt_final", "text": "the final", "target": "prompt"}]
    assert max(client.sent.index(d) for d in deltas) < client.sent.index(finals[0])


def test_inflight_partial_tick_skipped_not_queued(monkeypatch):
    async def scenario():
        clock = _patch_partial_clock(monkeypatch)
        gate = _FakeGate(
            feeds=[[Event(EventKind.SPEECH_START)], [], [], [],
                   [Event(EventKind.SPEECH_END, b"utt")]],
            active=b"cur-buf")
        _patch_ready(monkeypatch, gate=gate, transcripts=["the final"])
        release = asyncio.Event()
        calls = []

        async def _blocked_partial(pcm):
            calls.append(clock["t"])
            await release.wait()
            return "late partial"
        monkeypatch.setattr(stt_ws_routes, "_transcribe_partial", _blocked_partial)

        def _release_then_audio():
            release.set()          # free the stuck partial only at the end
            return _audio()

        frames = [
            (0.1, _audio()),           # SPEECH_START; due 1.6
            (1.6, _audio()),           # t=1.7 due -> partial launched (BLOCKED)
            (1.6, _audio()),           # t=3.3 due but IN FLIGHT -> tick skipped
            (1.6, _audio()),           # t=4.9 due but IN FLIGHT -> tick skipped
            (0.1, _release_then_audio),  # t=5.0 release; SPEECH_END -> final
            (0.1, {"type": "stt_stop"}),
        ]
        client = _ClockedClientWS(frames, clock)
        await asyncio.wait_for(
            stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                          sample_rate=24000),
            timeout=10.0)
        return client, calls

    client, calls = asyncio.run(scenario())
    # DROP-FRAME: one transcription in flight max — the due ticks at 3.3/4.9
    # were skipped, not queued
    assert [round(t, 2) for t in calls] == [1.7]
    # the released stale result arrives AFTER SPEECH_END -> discarded
    assert not any(m.get("type") == "stt_delta" for m in client.sent)
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals == [{"type": "stt_final", "text": "the final", "target": "prompt"}]


def test_no_partial_after_speech_end_final_supersedes(monkeypatch):
    async def scenario():
        clock = _patch_partial_clock(monkeypatch)
        gate = _FakeGate(
            feeds=[[Event(EventKind.SPEECH_START)], [],
                   [Event(EventKind.SPEECH_END, b"utt")], [], []],
            active=b"cur-buf")
        _patch_ready(monkeypatch, gate=gate, transcripts=["the final"])
        release = asyncio.Event()
        calls = []

        async def _blocked_partial(pcm):
            calls.append(clock["t"])
            await release.wait()
            return "stale partial"
        monkeypatch.setattr(stt_ws_routes, "_transcribe_partial", _blocked_partial)

        def _release_then_audio():
            release.set()
            return _audio()

        frames = [
            (0.1, _audio()),           # SPEECH_START; due 1.6
            (1.6, _audio()),           # t=1.7 -> partial launched (BLOCKED)
            (0.3, _audio()),           # t=2.0 SPEECH_END -> final (partial in flight)
            (0.1, _release_then_audio),  # t=2.1 stale partial completes now...
            (0.1, _audio()),           # t=2.2 ...and MUST be dropped
            (0.1, {"type": "stt_stop"}),
        ]
        client = _ClockedClientWS(frames, clock)
        await asyncio.wait_for(
            stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                          sample_rate=24000),
            timeout=10.0)
        return client, calls

    client, calls = asyncio.run(scenario())
    assert calls, "the partial WAS in flight (suppression, not never-ran)"
    # partials STOP at SPEECH_END: the stale result never reaches the client
    assert not any(m.get("type") == "stt_delta" for m in client.sent)
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals == [{"type": "stt_final", "text": "the final", "target": "prompt"}]


def test_partials_disabled_by_env_flag(monkeypatch):
    async def scenario():
        clock = _patch_partial_clock(monkeypatch)
        monkeypatch.setenv("ONBOX_STT_PARTIALS", "0")
        gate = _FakeGate(
            feeds=[[Event(EventKind.SPEECH_START)], [], [],
                   [Event(EventKind.SPEECH_END, b"utt")]],
            active=b"cur-buf")
        _patch_ready(monkeypatch, gate=gate, transcripts=["the final"])
        calls = []

        async def _fake_partial(pcm):
            calls.append(clock["t"])
            return "must never emit"
        monkeypatch.setattr(stt_ws_routes, "_transcribe_partial", _fake_partial)

        frames = [
            (0.1, _audio()),   # SPEECH_START
            (1.6, _audio()),   # t=1.7 would be due — but partials are OFF
            (1.6, _audio()),   # t=3.3 would be due — still OFF
            (0.1, _audio()),   # SPEECH_END -> final
            (0.1, {"type": "stt_stop"}),
        ]
        client = _ClockedClientWS(frames, clock)
        await asyncio.wait_for(
            stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                          sample_rate=24000),
            timeout=10.0)
        return client, calls

    client, calls = asyncio.run(scenario())
    assert calls == []                                     # never transcribed
    assert not any(m.get("type") == "stt_delta" for m in client.sent)
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals == [{"type": "stt_final", "text": "the final", "target": "prompt"}]


def test_hallucination_filtered_partial_suppressed(monkeypatch):
    async def scenario():
        clock = _patch_partial_clock(monkeypatch)
        gate = _FakeGate(
            feeds=[[Event(EventKind.SPEECH_START)], [], [],
                   [Event(EventKind.SPEECH_END, b"utt")]],
            active=b"cur-buf")
        _patch_ready(monkeypatch, gate=gate, transcripts=["real words"])
        monkeypatch.setattr(stt_ws_routes, "is_whisper_hallucination",
                            lambda t: t == "Thank you.")

        async def _fake_partial(pcm):
            return "Thank you."          # classic whisper silence hallucination
        monkeypatch.setattr(stt_ws_routes, "_transcribe_partial", _fake_partial)

        frames = [
            (0.1, _audio()),   # SPEECH_START; due 1.6
            (1.6, _audio()),   # t=1.7 due -> partial (hallucinated)
            (0.1, _audio()),   # lets the (suppressed) emit land
            (0.1, _audio()),   # SPEECH_END -> final
            (0.1, {"type": "stt_stop"}),
        ]
        client = _ClockedClientWS(frames, clock)
        await asyncio.wait_for(
            stt_ws_routes._onbox_vad_loop(client, target="prompt", lang="en",
                                          sample_rate=24000),
            timeout=10.0)
        return client

    client = asyncio.run(scenario())
    assert not any(m.get("type") == "stt_delta" for m in client.sent)
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals == [{"type": "stt_final", "text": "real words", "target": "prompt"}]


def test_partial_env_knobs(monkeypatch):
    monkeypatch.delenv("ONBOX_STT_PARTIALS", raising=False)
    monkeypatch.delenv("ONBOX_STT_PARTIAL_MS", raising=False)
    assert stt_ws_routes._partials_enabled() is True       # default ON
    assert stt_ws_routes._partial_interval_s() == pytest.approx(1.5)
    monkeypatch.setenv("ONBOX_STT_PARTIALS", "0")
    assert stt_ws_routes._partials_enabled() is False
    monkeypatch.setenv("ONBOX_STT_PARTIAL_MS", "500")
    assert stt_ws_routes._partial_interval_s() == pytest.approx(0.5)
    monkeypatch.setenv("ONBOX_STT_PARTIAL_MS", "junk")     # garbage -> default
    assert stt_ws_routes._partial_interval_s() == pytest.approx(1.5)


def test_transcribe_partial_default_wraps_transcribe_utterance(monkeypatch):
    seen = []

    def _fake(pcm):
        seen.append(pcm)
        return "words"
    monkeypatch.setattr(stt_ws_routes, "_transcribe_utterance", _fake)
    out = asyncio.run(stt_ws_routes._transcribe_partial(b"pcm-bytes"))
    assert out == "words"
    assert seen == [b"pcm-bytes"]


def test_gate_active_pcm_exposes_open_utterance():
    from Orchestrator.stt.vad import UtteranceGate
    gate = UtteranceGate(sample_rate=16000, min_speech_ms=1, pre_roll_ms=0,
                         scorer=lambda f: 1.0)
    assert gate.active_pcm() == b""                        # idle
    frame = b"\x01\x00" * 512
    events = gate.feed(frame)
    assert any(e.kind is EventKind.SPEECH_START for e in events)
    assert gate.active_pcm() == frame                      # open buffer so far
    gate.feed(frame)
    assert gate.active_pcm() == frame * 2                  # grows with speech
    gate.flush()
    assert gate.active_pcm() == b""                        # reset after flush


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
