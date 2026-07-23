#!/usr/bin/env python3
"""
stt_ws_routes.py - Multi-provider STREAMING speech-to-text WebSocket endpoint.

Exposes /ws/stt, the single uniform live-transcription channel for web + Android
clients. The client contract is provider-agnostic:

    UP:   {"type":"stt_start","target":"prompt","provider":"openai"|"google"|"",
           "lang":"en","sample_rate":16000}
          then repeated {"type":"stt_audio","pcm":"<base64 PCM16>"}
          then {"type":"stt_stop"}
    DOWN: {"type":"stt_delta","text":...,"target":...}
          {"type":"stt_final","text":...,"target":...}
          {"type":"stt_error","message":...}
          {"type":"stt_done"}                  (terminal — ALWAYS the last frame)
          {"type":"stt_status","state":"loading_models"}   (onbox warm affordance, additive)
          {"type":"stt_status","state":"listening"|"speech"|"processing"}
              (onbox VAD loop lifecycle, additive — cloud bridges never send them)

stt_delta.text is the CUMULATIVE interim transcript so far (client replaces the
interim region); stt_final.text is the full final (client commits).

stt_done (added 2026-07-09, additive — old clients ignore unknown types) is the
TERMINAL marker: sent best-effort as the last frame of every session — after the
trailing stt_final on a normal stop, after an stt_error on failures, and even
when finalization produced NOTHING (empty/filtered/failed). Clients key their
stop-teardown on it instead of a blind grace timer. When the hallucination
filter swallows a stop-path final, the bridge first sends an authoritative
EMPTY stt_final ({"text":""} — or the carried rotation prefix on ElevenLabs) so
the client discards, rather than resurrects, the filtered interim; a session
that ends with NO stt_final at all means "the server never finalized" and the
client may commit its newest partial as a fallback. One narrow exception to
"always the last frame": a Google worker stalled past the bounded post-stop
flush (_GOOGLE_STOP_FLUSH_TIMEOUT_S) can emit a trailing final AFTER stt_done —
the client has torn down by then, so that send fails and is logged, never
double-applied.

Providers stream interim text with opposite semantics (OpenAI incremental,
Google cumulative), so InterimAccumulator normalizes both to this uniform
contract.

The endpoint resolves a provider via resolve_stt_provider() and bridges to either
OpenAI's realtime transcription WS (gRPC-free, websockets lib) or Google Cloud
Speech-to-Text v2 streaming (gRPC, run in a thread executor so the asyncio event
loop is never blocked). Pure provider-event translation lives in
Orchestrator/stt/streaming.py; whisper hallucination filtering in whisper_filter.

SAMPLE-RATE (resolved 2026-06-05 via live test): OpenAI realtime transcription
REQUIRES sample_rate >= 24000 (it rejects 16000 with "format.rate integer below
minimum value"). Google Cloud Speech v2 accepts 24000, and the native Android
capture is already 24kHz. So the client standardizes on 24000 and we pass it
straight through to both providers. Default fallback is 24000.

`resolve_stt_provider` and `run_stt_bridge` are referenced as MODULE GLOBALS so
the test suite can monkeypatch them.
"""

import asyncio
import base64
import json
import os
import time

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from Orchestrator.checkpoint import app
from Orchestrator import config
from Orchestrator.elevenlabs.client import (
    resolve_api_key, WS_BASE_URL, auth_headers, map_error, classify_realtime_frame,
)
from Orchestrator.stt.resolve import resolve_stt_provider, local_streaming_stt_available, onbox_stt_available
from Orchestrator.stt.streaming import (
    map_openai_event, map_google_result, InterimAccumulator, join_transcript_segments,
)
from Orchestrator.whisper_filter import is_whisper_hallucination


# Google Cloud Speech v2 (chirp_2) requires full BCP-47 codes (e.g. "en-US"),
# not bare ISO-639-1 ("en") — it rejects "en" with "language not supported by
# the model in the location". The uniform client contract sends short codes,
# so normalize here. OpenAI, by contrast, accepts "en" directly.
_GOOGLE_LANG = {
    "en": "en-US", "es": "es-US", "fr": "fr-FR", "de": "de-DE", "it": "it-IT",
    "pt": "pt-BR", "ja": "ja-JP", "ko": "ko-KR", "zh": "cmn-Hans-CN",
}


def _normalize_google_lang(lang):
    code = (lang or "").strip()
    if not code:
        return "en-US"
    return _GOOGLE_LANG.get(code.lower(), code)


def _stop_latency_ms(stop_ts: dict):
    """stop→now latency in whole ms, or None if the client hasn't stopped yet.
    `stop_ts` is a {"v": monotonic-or-None} holder shared with the bridge."""
    if stop_ts.get("v") is None:
        return None
    return int((time.monotonic() - stop_ts["v"]) * 1000)


async def _send_final(websocket: WebSocket, provider: str, m: dict, stop_ts: dict):
    """Deliver an stt_final with delivery telemetry (2026-07-09 stop-bug audit):
    EVERY delivery attempt logs the stop→final latency, and a failed delivery
    logs loudly before re-raising instead of vanishing silently."""
    lat = _stop_latency_ms(stop_ts)
    lat_s = f"{lat}" if lat is not None else "pre-stop"
    try:
        await websocket.send_json(m)
        print(f"[STT/WS] {provider} stt_final delivered "
              f"text_len={len(m.get('text') or '')} stop_to_final_ms={lat_s}")
    except Exception as e:
        print(f"[STT/WS] {provider} stt_final delivery FAILED "
              f"(text_len={len(m.get('text') or '')}, stop_to_final_ms={lat_s}): {e!r}")
        raise


@app.websocket("/ws/stt")
async def ws_stt(websocket: WebSocket):
    await websocket.accept()
    try:
        start = await websocket.receive_json()
        if start.get("type") != "stt_start":
            await websocket.send_json({"type": "stt_error", "message": "expected stt_start"})
            return
        # Local streaming resolves ONLY when a registered server advertises the
        # realtime (/v1/realtime) capability; otherwise local stays file-only and
        # must not win STREAMING resolution.
        provider = resolve_stt_provider(start.get("provider"),
                                        local_ok=local_streaming_stt_available(),
                                        onbox_ok=onbox_stt_available())
        if not provider:
            await websocket.send_json({"type": "stt_error", "message": "no STT provider configured"})
            return
        print(f"[STT/WS] start provider={provider} sample_rate={start.get('sample_rate')} "
              f"lang={start.get('lang')} target={start.get('target')}")
        try:
            await run_stt_bridge(websocket, provider, start)
        except WebSocketDisconnect:
            raise
        except Exception as e:
            # Surface the failure BEFORE the terminal marker so clients observe
            # stt_error -> stt_done in order.
            try:
                await websocket.send_json({"type": "stt_error", "message": str(e)})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        # Terminal marker (2026-07-09): {"type":"stt_done"} is ALWAYS the last
        # frame of a session — after the trailing stt_final, after an stt_error,
        # and even when finalization produced NOTHING (empty/filtered/failed) —
        # so clients key their stop-teardown on it instead of a blind grace
        # timer. Best-effort: the client may already be gone, and that's fine.
        try:
            await websocket.send_json({"type": "stt_done"})
        except Exception:
            pass


async def run_stt_bridge(websocket: WebSocket, provider: str, start: dict):
    """Dispatch to the per-provider streaming bridge."""
    target = start.get("target")
    lang = start.get("lang")
    sample_rate = start.get("sample_rate", 24000)
    if provider == "google":
        await _google_bridge(websocket, target=target, lang=lang, sample_rate=sample_rate)
    elif provider == "elevenlabs":
        await _elevenlabs_bridge(websocket, target=target, lang=lang, sample_rate=sample_rate)
    elif provider == "openai":
        await _openai_bridge(websocket, target=target, lang=lang, sample_rate=sample_rate)
    elif provider == "onbox":
        if _onbox_realtime_enabled():
            # PARKED Design-B realtime-WS bridge (pre-1.0 /v1/realtime event
            # schema, never verified live) — opt-in via ONBOX_STT_REALTIME=1
            # pending the protocol audit. The VAD-gated loop is the default.
            await _onbox_bridge(websocket, target=target, lang=lang, sample_rate=sample_rate)
        else:
            await _onbox_vad_loop(websocket, target=target, lang=lang, sample_rate=sample_rate)
    elif provider == "local":
        await _local_bridge(websocket, target=target, lang=lang, sample_rate=sample_rate)
    else:
        # No silent OpenAI fallback: an unknown/local provider must fail loudly,
        # not get mis-routed to the OpenAI realtime bridge.
        raise RuntimeError(f"live streaming is not supported for STT provider '{provider}'")


# =============================================================================
# OpenAI realtime transcription bridge (websockets lib, no gRPC)
# =============================================================================

async def _openai_bridge(websocket: WebSocket, *, target, lang, sample_rate):
    """Bridge client PCM -> OpenAI realtime transcription -> client deltas/finals."""
    api_key = (config.OPENAI_API_KEY or "").strip()
    if not api_key:
        await websocket.send_json({"type": "stt_error", "message": "OPENAI_API_KEY not configured"})
        return

    url = f"{config.OPENAI_REALTIME_URL}?intent=transcription"
    openai_ws = await websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        open_timeout=10, ping_interval=20, ping_timeout=30, close_timeout=10,
    )
    try:
        # Configure transcription-only session. turn_detection omitted (null) so
        # we drive commits manually on stt_stop. We pass the client-declared
        # sample_rate through (see module SAMPLE-RATE WATCH-ITEM).
        session_update = {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": sample_rate},
                        "transcription": {
                            "model": config.STT_OPENAI_STREAM,
                            "language": lang or "en",
                            "delay": config.STT_OPENAI_DELAY,
                        },
                        "turn_detection": None,
                    },
                },
            },
        }
        await openai_ws.send(json.dumps(session_update))
        print(f"[STT/WS] openai connected model={config.STT_OPENAI_STREAM} rate={sample_rate} "
              f"url={url}; session.update sent")

        # Set when the client requests a manual stop (commit sent). The relay
        # keeps draining until it delivers the final for the committed audio,
        # then exits — so a normal push-to-talk stop never drops the last
        # utterance's stt_final.
        stop_evt = asyncio.Event()
        stop_ts = {"v": None}  # monotonic time of the stop-commit (telemetry)

        # Normalize OpenAI's incremental deltas into a cumulative interim
        # transcript so stt_delta.text is uniform across providers.
        acc = InterimAccumulator()

        async def client_to_openai():
            """Pump client audio into OpenAI; commit + signal stop on stt_stop."""
            while True:
                msg = await websocket.receive_json()
                mtype = msg.get("type")
                if mtype == "stt_audio":
                    pcm = msg.get("pcm", "")
                    if pcm:
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": pcm,
                        }))
                elif mtype == "stt_stop":
                    await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    stop_ts["v"] = time.monotonic()
                    stop_evt.set()
                    return

        async def openai_to_client():
            """Relay OpenAI transcription events back to the client."""
            async for raw in openai_ws:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                etype = event.get("type", "")
                # Surface OpenAI error events (e.g. rejected session config / bad
                # model) instead of silently swallowing them as "unmapped".
                if "error" in etype:
                    detail = (event.get("error") or {}).get("message") or json.dumps(event)[:300]
                    print(f"[STT/WS] openai ERROR event: {json.dumps(event)[:500]}")
                    try:
                        await websocket.send_json({"type": "stt_error", "message": f"OpenAI: {detail}"})
                    except Exception:
                        pass
                    continue
                m = acc.openai(event)
                if not m:
                    continue
                if m["type"] == "stt_final" and is_whisper_hallucination(m.get("text", "")):
                    print(f"[STT/WS] openai stt_final FILTERED (hallucination) "
                          f"text_len={len(m.get('text', ''))}")
                    if stop_evt.is_set():
                        # This WAS the trailing final for the manual stop — nothing
                        # more is coming. Send an authoritative EMPTY final (so the
                        # client discards, not resurrects, the filtered interim)
                        # and exit; the endpoint's stt_done then follows at once
                        # instead of the client parking on the 5s drain backstop.
                        await _send_final(websocket, "openai",
                                          {"type": "stt_final", "text": "", "target": target},
                                          stop_ts)
                        return
                    continue
                m["target"] = target
                if m["type"] == "stt_final":
                    await _send_final(websocket, "openai", m, stop_ts)
                else:
                    await websocket.send_json(m)
                # On a manual stop, the final for the committed audio is the last
                # thing we need — stop draining once it's delivered.
                if m["type"] == "stt_final" and stop_evt.is_set():
                    return

        pump = asyncio.ensure_future(client_to_openai())
        relay = asyncio.ensure_future(openai_to_client())
        try:
            done, pending = await asyncio.wait(
                {pump, relay}, return_when=asyncio.FIRST_COMPLETED
            )
            if pump in done and relay not in done:
                # Client stopped: the OpenAI session stays open and delivers the
                # final transcript AFTER our commit. Drain for it instead of
                # cancelling synchronously (which would drop the last stt_final).
                # 5s backstop so we never hang if no final ever arrives.
                try:
                    await asyncio.wait_for(relay, timeout=5.0)
                except asyncio.TimeoutError:
                    # Stop-path telemetry (parity with the elevenlabs/google
                    # bridges): the drain expired, so NO final was delivered for
                    # this stop. stt_done still goes out and the client commits
                    # its newest partial as the fallback final.
                    print(f"[STT/WS] openai stop ended WITHOUT a final "
                          f"(relay drain expired, "
                          f"stop_to_end_ms={_stop_latency_ms(stop_ts)})")
                    relay.cancel()
                    try:
                        await relay
                    except (asyncio.CancelledError, WebSocketDisconnect):
                        pass
                    except Exception:
                        pass
            elif relay in done:
                # OpenAI closed / error / disconnect first: cancel the pump.
                pump.cancel()
                try:
                    await pump
                except (asyncio.CancelledError, WebSocketDisconnect):
                    pass
                except Exception:
                    pass

            # Surface real errors from whichever task(s) finished.
            for t in (pump, relay):
                if t.done() and not t.cancelled():
                    exc = t.exception()
                    if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                        raise exc
        finally:
            for t in (pump, relay):
                if not t.done():
                    t.cancel()
    finally:
        # Always close the OpenAI WS — billing + cleanup.
        try:
            await openai_ws.close()
        except Exception:
            pass


# =============================================================================
# ElevenLabs Scribe realtime bridge (websockets lib, no gRPC)
# =============================================================================

def _el_audio_msg(pcm_b64: str, sample_rate: int, commit: bool = False) -> str:
    """Build a Scribe realtime input_audio_chunk frame (returns the JSON string).

    `pcm_b64` is base64-encoded raw PCM16 — the client already sends it that way,
    so we pass it straight through as `audio_base_64`. `commit=True` (with an
    empty chunk) flushes the tail and triggers the committed (final) transcript.
    Pure so it can be unit-tested without a socket.
    """
    return json.dumps({
        "message_type": "input_audio_chunk",
        "audio_base_64": pcm_b64,
        "sample_rate": sample_rate,
        "commit": commit,
    })


# Absolute backstop on consecutive session rotations (storm guard). Only
# PROGRESSING sessions count toward it (a no-progress instant rotate bails early),
# so the real ceiling on continuous dictation is ~30 x Scribe's per-session limit
# (minutes each) — comfortably beyond any realistic single utterance. Tunable.
_EL_MAX_ROTATIONS = 30


def _resample_pcm16(pcm_bytes: bytes, src_rate: int, dst_rate: int = 24000) -> bytes:
    """Resample raw PCM16 mono to dst_rate. Speaches /v1/realtime is fixed at
    24 kHz; pass through when the rates already match. Lazy numpy import."""
    if src_rate == dst_rate or not pcm_bytes:
        return pcm_bytes
    import numpy as np
    pcm_bytes = pcm_bytes[: len(pcm_bytes) & ~1]  # drop a dangling odd byte: frombuffer needs a multiple of 2
    if not pcm_bytes:
        return b""
    a = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)
    n = max(1, round(len(a) * dst_rate / src_rate))
    out = np.interp(np.linspace(0, 1, n, endpoint=False),
                    np.linspace(0, 1, len(a), endpoint=False), a)
    return np.clip(out, -32768, 32767).astype("<i2").tobytes()


async def _local_bridge(websocket: WebSocket, *, target, lang, sample_rate):
    """Bridge client PCM -> a registered local Speaches /v1/realtime transcription
    session -> client stt_final events.

    OpenAI realtime protocol, but Speaches-specific: NO session.update (input format
    is fixed to pcm16@24k and setting it errors), server-VAD auto-segments (so we
    only commit on a push-to-talk stt_stop), and it emits PER-UTTERANCE finals only
    (no interim deltas). Client audio is resampled to 24 kHz."""
    from Orchestrator.onboarding.custom_servers import resolve_audio
    resolved = resolve_audio("streaming")
    if not resolved:
        await websocket.send_json({"type": "stt_error", "message": "no local streaming STT server available"})
        return
    srv, model = resolved
    from urllib.parse import quote
    ws_url = (srv.get("base_url", "").replace("https://", "wss://").replace("http://", "ws://")
              + f"/realtime?model={quote(model)}&intent=transcription")
    headers = {}
    if srv.get("api_key"):
        headers["Authorization"] = f"Bearer {srv['api_key']}"
    local_ws = await websockets.connect(
        ws_url, additional_headers=headers,
        open_timeout=10, ping_interval=20, ping_timeout=30, close_timeout=10, max_size=None,
    )
    try:
        print(f"[STT/WS] local connected model={model} rate={sample_rate}->24000 url={ws_url}")
        stop_evt = asyncio.Event()
        stop_ts = {"v": None}

        async def client_to_local():
            while True:
                msg = await websocket.receive_json()
                mtype = msg.get("type")
                if mtype == "stt_audio":
                    pcm_b64 = msg.get("pcm", "")
                    if pcm_b64:
                        raw = _resample_pcm16(base64.b64decode(pcm_b64), sample_rate, 24000)
                        await local_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(raw).decode(),
                        }))
                elif mtype == "stt_stop":
                    # Speaches server-VAD auto-commits on a pause; an explicit
                    # input_audio_buffer.commit races it into an abrupt socket
                    # close ("no close frame"). Feed ~0.7 s of trailing silence to
                    # trigger the VAD cut for the final utterance instead.
                    silence = base64.b64encode(b"\x00\x00" * int(24000 * 0.7)).decode()
                    await local_ws.send(json.dumps({
                        "type": "input_audio_buffer.append", "audio": silence}))
                    stop_ts["v"] = time.monotonic()
                    stop_evt.set()
                    return

        async def local_to_client():
            try:
                async for raw in local_ws:
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    etype = event.get("type", "")
                    if "error" in etype:
                        detail = (event.get("error") or {}).get("message") or json.dumps(event)[:300]
                        print(f"[STT/WS] local ERROR event: {json.dumps(event)[:500]}")
                        try:
                            await websocket.send_json({"type": "stt_error", "message": f"local: {detail}"})
                        except Exception:
                            pass
                        continue
                    if etype != "conversation.item.input_audio_transcription.completed":
                        continue  # lifecycle events -- ignore (server emits finals only)
                    text = (event.get("transcript") or "").strip()
                    if is_whisper_hallucination(text):
                        if stop_evt.is_set():
                            await _send_final(websocket, "local",
                                              {"type": "stt_final", "text": "", "target": target}, stop_ts)
                            return
                        continue
                    await _send_final(websocket, "local",
                                      {"type": "stt_final", "text": text, "target": target}, stop_ts)
                    if stop_evt.is_set():
                        return
            except websockets.ConnectionClosed:
                # Speaches closes the socket right after the last final on stop --
                # a normal end-of-session, not an error to surface.
                return

        pump = asyncio.ensure_future(client_to_local())
        relay = asyncio.ensure_future(local_to_client())
        try:
            done, _pending = await asyncio.wait({pump, relay}, return_when=asyncio.FIRST_COMPLETED)
            if pump in done and relay not in done:
                try:
                    await asyncio.wait_for(relay, timeout=5.0)
                except asyncio.TimeoutError:
                    relay.cancel()
                    try:
                        await relay
                    except (asyncio.CancelledError, WebSocketDisconnect):
                        pass
                    except Exception:
                        pass
            elif relay in done:
                pump.cancel()
                try:
                    await pump
                except (asyncio.CancelledError, WebSocketDisconnect):
                    pass
                except Exception:
                    pass
            for t in (pump, relay):
                if t.done() and not t.cancelled():
                    exc = t.exception()
                    if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                        raise exc
        finally:
            for t in (pump, relay):
                if not t.done():
                    t.cancel()
    finally:
        try:
            await local_ws.close()
        except Exception:
            pass


# =============================================================================
# On-box (Design B) streaming STT — direct-to-:9099 Speaches with a D10 warm
# affordance and D12 voice-session serialization.
# =============================================================================

# D10: generous ceiling on warming the audio group before an honest error;
# NEVER a silent provider switch (§5.3 / D10).
_ONBOX_WARM_CEILING_S = 30.0
_ONBOX_WARM_POLL_S = 1.0


def _probe_speaches_health_sync(url: str) -> bool:
    """Blocking one-shot health GET (run in an executor). 200 = ready; a 429
    (concurrency), 5xx, or connection error = not ready yet. requests imported
    lazily so this module stays websockets-only at import time."""
    try:
        import requests
        r = requests.get(url, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


async def _probe_speaches_health(url: str) -> bool:
    """Async wrapper so tests can monkeypatch a single awaitable; runs the
    blocking GET off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _probe_speaches_health_sync, url)


async def _warm_audio_group(websocket: WebSocket) -> bool:
    """D10 affordance: emit stt_status {state:'loading_models'} then warm the
    on-box audio group by GETting llama-swap's /upstream/speaches/health (:9098)
    until healthy. Returns True once warm; False if the ~30s ceiling passes (the
    caller then sends an honest stt_error — NEVER a silent provider switch). A
    busy (429) or not-yet-ready probe just keeps the affordance up until the
    ceiling (realtime 429 contract, correction [28])."""
    from Orchestrator import local_stack
    await websocket.send_json({"type": "stt_status", "state": "loading_models"})
    url = local_stack.speaches_warm_url()
    deadline = time.monotonic() + _ONBOX_WARM_CEILING_S
    while time.monotonic() < deadline:
        if await _probe_speaches_health(url):
            return True
        await asyncio.sleep(_ONBOX_WARM_POLL_S)
    print(f"[STT/WS] onbox warm ceiling ({_ONBOX_WARM_CEILING_S}s) exceeded — honest stt_error")
    return False


async def _relay_realtime(websocket: WebSocket, upstream_ws, *, target, sample_rate, label):
    """Shared Speaches /v1/realtime relay body (cloned from _local_bridge): client
    PCM -> resample-to-24k -> upstream; per-utterance finals -> client; ~0.7s
    trailing-silence stop; hallucination filter; drain-for-final with a 5s
    backstop. `label` tags the log/error lines ('onbox')."""
    stop_evt = asyncio.Event()
    stop_ts = {"v": None}

    async def client_to_upstream():
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "stt_audio":
                pcm_b64 = msg.get("pcm", "")
                if pcm_b64:
                    raw = _resample_pcm16(base64.b64decode(pcm_b64), sample_rate, 24000)
                    await upstream_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(raw).decode(),
                    }))
            elif mtype == "stt_stop":
                # Server-VAD auto-commits on a pause; feed ~0.7s trailing silence
                # to trigger the final-utterance cut (an explicit commit races the
                # socket close, per _local_bridge).
                silence = base64.b64encode(b"\x00\x00" * int(24000 * 0.7)).decode()
                await upstream_ws.send(json.dumps({
                    "type": "input_audio_buffer.append", "audio": silence}))
                stop_ts["v"] = time.monotonic()
                stop_evt.set()
                return

    async def upstream_to_client():
        try:
            async for raw in upstream_ws:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                etype = event.get("type", "")
                if "error" in etype:
                    detail = (event.get("error") or {}).get("message") or json.dumps(event)[:300]
                    print(f"[STT/WS] {label} ERROR event: {json.dumps(event)[:500]}")
                    try:
                        await websocket.send_json({"type": "stt_error", "message": f"{label}: {detail}"})
                    except Exception:
                        pass
                    continue
                if etype != "conversation.item.input_audio_transcription.completed":
                    continue  # lifecycle events -- server emits finals only
                text = (event.get("transcript") or "").strip()
                if is_whisper_hallucination(text):
                    if stop_evt.is_set():
                        await _send_final(websocket, label,
                                          {"type": "stt_final", "text": "", "target": target}, stop_ts)
                        return
                    continue
                await _send_final(websocket, label,
                                  {"type": "stt_final", "text": text, "target": target}, stop_ts)
                if stop_evt.is_set():
                    return
        except websockets.ConnectionClosed:
            return

    pump = asyncio.ensure_future(client_to_upstream())
    relay = asyncio.ensure_future(upstream_to_client())
    try:
        done, _pending = await asyncio.wait({pump, relay}, return_when=asyncio.FIRST_COMPLETED)
        if pump in done and relay not in done:
            try:
                await asyncio.wait_for(relay, timeout=5.0)
            except asyncio.TimeoutError:
                relay.cancel()
                try:
                    await relay
                except (asyncio.CancelledError, WebSocketDisconnect):
                    pass
                except Exception:
                    pass
        elif relay in done:
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
            except Exception:
                pass
        for t in (pump, relay):
            if t.done() and not t.cancelled():
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                    raise exc
    finally:
        for t in (pump, relay):
            if not t.done():
                t.cancel()


async def _onbox_bridge(websocket: WebSocket, *, target, lang, sample_rate):
    """PARKED (2026-07-23, W2): on-box Design-B realtime-WS streaming bridge.
    It assumes the pre-1.0 Speaches /v1/realtime event schema, which has never
    been verified live — reachable ONLY via ONBOX_STT_REALTIME=1 pending the
    protocol audit; the VAD-gated loop (_onbox_vad_loop) is the default onbox
    path. Warms the audio group through the llama-swap :9098 /upstream
    passthrough (with the D10 loading affordance), then connects DIRECT to the
    pinned Speaches :9099/v1/realtime for near-real-time latency. The whole
    stream runs inside local_stack.voice_session() so retrieval-group dispatch
    serializes behind it (D12). NEVER falls back to a cloud provider — an unmet
    warm ceiling is an honest stt_error."""
    from Orchestrator import local_stack
    async with local_stack.voice_session():
        if not await _warm_audio_group(websocket):
            await websocket.send_json({"type": "stt_error",
                                       "message": "on-box STT models still loading — please retry"})
            return
        model = local_stack.stt_stream_model()
        ws_url = local_stack.speaches_realtime_ws_url(model)
        upstream_ws = await websockets.connect(
            ws_url, open_timeout=10, ping_interval=20, ping_timeout=30,
            close_timeout=10, max_size=None,
        )
        try:
            print(f"[STT/WS] onbox connected model={model} rate={sample_rate}->24000 url={ws_url}")
            await _relay_realtime(websocket, upstream_ws,
                                  target=target, sample_rate=sample_rate, label="onbox")
        finally:
            try:
                await upstream_ws.close()
            except Exception:
                pass


# =============================================================================
# On-box VAD-gated streaming STT loop (W2, plan 2026-07-22) — the DEFAULT onbox
# path. Server-side silero VAD segments the inbound stream into utterances;
# each time the gate closes the utterance is transcribed through the
# G4-validated Speaches upstream BATCH path (no dependency on the unverified
# pre-1.0 /v1/realtime schema).
# =============================================================================

_ONBOX_REALTIME_ENV = "ONBOX_STT_REALTIME"
_ONBOX_PRIME_WAV_S = 0.2   # ~0.2s of silence is enough to force whisper residency

# W3 rolling partials (plan 2026-07-22): while speech is active the OPEN
# utterance buffer is transcribed every ONBOX_STT_PARTIAL_MS and emitted as a
# cumulative stt_delta. ONBOX_STT_PARTIALS=0 disables (default ON).
_ONBOX_PARTIALS_ENV = "ONBOX_STT_PARTIALS"
_ONBOX_PARTIAL_MS_ENV = "ONBOX_STT_PARTIAL_MS"
_ONBOX_PARTIAL_MS_DEFAULT = 1500

# W3 seam: the rolling-partials scheduler reads time through this module
# global ONLY, so tests drive the cadence with a fake clock without touching
# the stdlib time module (stop_ts telemetry stays on time.monotonic).
_monotonic = time.monotonic


def _partials_enabled() -> bool:
    """Rolling partials are ON unless ONBOX_STT_PARTIALS=0."""
    return (os.environ.get(_ONBOX_PARTIALS_ENV, "") or "").strip() != "0"


def _partial_interval_s() -> float:
    """Partial cadence in seconds (ONBOX_STT_PARTIAL_MS, default 1500ms;
    garbage falls back to the default, never crashes the stream)."""
    raw = (os.environ.get(_ONBOX_PARTIAL_MS_ENV, "") or "").strip()
    try:
        ms = int(raw) if raw else _ONBOX_PARTIAL_MS_DEFAULT
    except ValueError:
        ms = _ONBOX_PARTIAL_MS_DEFAULT
    return max(1, ms) / 1000.0


async def _transcribe_partial(pcm16: bytes) -> str:
    """Async partial-transcription seam (module-level so tests monkeypatch a
    single awaitable): the same G4-validated upstream batch path + STREAM
    (turbo) model the finals use, run off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _transcribe_utterance, pcm16)


def _onbox_realtime_enabled() -> bool:
    """True only when the operator explicitly opts into the parked realtime
    bridge (ONBOX_STT_REALTIME=1). Default OFF -> the VAD-gated loop."""
    return (os.environ.get(_ONBOX_REALTIME_ENV, "") or "").strip() == "1"


def _vad_missing_dep() -> "str | None":
    """Preflight the VAD dependencies at stream start. Returns a human-readable
    message NAMING the missing piece (onnxruntime / silero model file), or None
    when the gate can run. A missing dep is an honest stt_error — NEVER a
    silent fall to a cloud provider (D10)."""
    try:
        import onnxruntime  # noqa: F401 — pinned in requirements.txt
    except Exception as e:
        return (f"onnxruntime is not available ({e.__class__.__name__}: {e}) — "
                f"install the pinned onnxruntime for on-box VAD")
    try:
        from Orchestrator.stt import vad as _vad
        path = _vad.default_vad_model_path()
    except Exception as e:
        return f"silero VAD model path could not be resolved: {e}"
    try:
        present = path.exists() and path.stat().st_size > 0
    except OSError as e:
        return f"silero VAD model unreadable at {path}: {e}"
    if not present:
        return (f"silero VAD model missing at {path} — download it via the "
                f"wizard (local-models key 'silero-vad')")
    return None


def _make_utterance_gate():
    """Gate factory (module-level so tests inject a scripted fake). The real
    gate scores with silero v5 ONNX on CPU; inbound audio is resampled to
    16 kHz mono before feeding (the v5 frame contract)."""
    from Orchestrator.stt.vad import UtteranceGate
    return UtteranceGate(sample_rate=16000)


def _pcm16_to_wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container (Speaches wants a real
    audio file on the batch path, not bare PCM)."""
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _silence_wav(duration_s: float = _ONBOX_PRIME_WAV_S, sample_rate: int = 16000) -> bytes:
    """A ~0.2s silent 16k mono WAV — the D10 priming payload."""
    return _pcm16_to_wav(b"\x00\x00" * int(sample_rate * duration_s), sample_rate)


def _transcribe_utterance(pcm16: bytes) -> str:
    """Blocking per-utterance transcription (run in an executor): 16k mono PCM
    -> WAV -> the G4-validated upstream Speaches batch path with the STREAM
    (turbo) model — utterance latency matters, NOT the large-v3 batch model.
    The llama-swap 429 concurrency retry lives in onbox_transcribe_upstream."""
    from Orchestrator import local_stack
    from Orchestrator.stt.file_transcribe import onbox_transcribe_upstream
    return onbox_transcribe_upstream(
        _pcm16_to_wav(pcm16), "audio/wav", "utterance.wav",
        model=local_stack.stt_stream_model())


def _prime_stt_model_sync(timeout_s: float) -> None:
    """Blocking prime: transcribe ~0.2s of silence through the upstream batch
    path with the STREAM model, forcing whisper residency (raises on failure)."""
    from Orchestrator import local_stack
    from Orchestrator.stt.file_transcribe import onbox_transcribe_upstream
    onbox_transcribe_upstream(
        _silence_wav(), "audio/wav", "prime.wav",
        model=local_stack.stt_stream_model(), timeout=timeout_s)


async def _prime_stt_model(timeout_s: float) -> None:
    """Async wrapper (module-level so tests monkeypatch one awaitable); runs
    the blocking prime POST off the event loop."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _prime_stt_model_sync, timeout_s)


async def _warm_and_prime(websocket: WebSocket) -> bool:
    """D10 warm affordance, residency-proven (2026-07-22 bridge audit): the old
    gate stopped at /upstream/speaches/health 200, which proves HTTP liveness,
    NOT whisper residency — the first utterance after any eviction was lost. So
    after health goes 200 we PRIME by transcribing ~0.2s of silence with the
    STREAM model (measured ~20s cold — fits inside the 30s D10 ceiling) and
    only then does the caller report listening. Returns False when the ceiling
    passes or the prime fails; the caller then emits an honest stt_error —
    NEVER a silent cloud fallback."""
    from Orchestrator import local_stack
    await websocket.send_json({"type": "stt_status", "state": "loading_models"})
    url = local_stack.speaches_warm_url()
    deadline = time.monotonic() + _ONBOX_WARM_CEILING_S
    while True:
        if await _probe_speaches_health(url):
            break
        if time.monotonic() >= deadline:
            print(f"[STT/WS] onbox warm ceiling ({_ONBOX_WARM_CEILING_S}s) exceeded "
                  f"before health 200 — honest stt_error")
            return False
        await asyncio.sleep(_ONBOX_WARM_POLL_S)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        print("[STT/WS] onbox warm consumed the full D10 ceiling — no time to prime")
        return False
    try:
        t0 = time.monotonic()
        await asyncio.wait_for(_prime_stt_model(remaining), timeout=remaining)
        print(f"[STT/WS] onbox whisper primed in {time.monotonic() - t0:.1f}s "
              f"(residency proven, not just health)")
    except Exception as e:
        print(f"[STT/WS] onbox prime failed within the D10 ceiling: {e!r}")
        return False
    return True


async def _onbox_vad_loop(websocket: WebSocket, *, target, lang, sample_rate):
    """On-box VAD-gated streaming STT — the default onbox path (W2).

    Session start: warm + PRIME the audio group (D10 affordance, 30s ceiling ->
    honest stt_error), then listen. Per inbound chunk: decode/resample to 16k
    mono (same inbound pipeline the cloud bridges use) and feed the silero
    UtteranceGate; SPEECH_START -> stt_status{speech}; SPEECH_END(utterance) ->
    stt_status{processing} -> transcribe via the G4-validated upstream batch
    path with the STREAM model -> hallucination filter -> stt_final in the
    EXACT cloud-bridge event shape. While speech is active, rolling partials
    (W3, default ON, ONBOX_STT_PARTIALS=0 disables): every ONBOX_STT_PARTIAL_MS
    the OPEN utterance buffer is transcribed and emitted as a cumulative
    stt_delta — DROP-FRAME (one in flight max; a due tick while busy is
    skipped), partials stop at SPEECH_END, and the final always supersedes a
    stale in-flight result. Client stop (or disconnect) -> gate.flush()
    -> tail final; the endpoint then emits the terminal stt_done. The whole
    stream holds local_stack.voice_session() (D12). NEVER falls back to a
    cloud provider — missing VAD deps or an unmet ceiling is an honest
    stt_error."""
    from Orchestrator import local_stack
    from Orchestrator.stt import vad as _vad
    missing = _vad_missing_dep()
    if missing and "model missing" in missing:
        # Fresh-box path (live gap 2026-07-23): the ~2.2MB silero model isn't a
        # wizard row, so the FIRST mic use on a new box lands here. Fetch it
        # under the D10 loading affordance (ensure_vad_model is lock-guarded,
        # atomic, bounded-timeout) instead of erroring; only a failed fetch is
        # an honest error.
        await websocket.send_json({"type": "stt_status", "state": "loading_models"})
        try:
            await asyncio.get_running_loop().run_in_executor(None, _vad.ensure_vad_model)
            missing = _vad_missing_dep()
        except Exception as e:  # noqa: BLE001 — surfaced as the honest error below
            missing = f"silero VAD model fetch failed: {e}"
    if missing:
        print(f"[STT/WS] onbox VAD loop unavailable: {missing}")
        await websocket.send_json({
            "type": "stt_error",
            "message": f"on-box STT unavailable: {missing}"})
        return
    async with local_stack.voice_session():
        if not await _warm_and_prime(websocket):
            await websocket.send_json({
                "type": "stt_error",
                "message": "on-box STT models still loading — please retry"})
            return
        await websocket.send_json({"type": "stt_status", "state": "listening"})
        gate = _make_utterance_gate()
        loop = asyncio.get_running_loop()
        stop_ts = {"v": None}
        print(f"[STT/WS] onbox VAD loop listening rate={sample_rate}->16000 "
              f"target={target}")

        # ── W3 rolling partials ─────────────────────────────────────────────
        partials_on = _partials_enabled()
        partial_interval = _partial_interval_s()
        speech_active = False
        speech_gen = 0        # bumped when the utterance closes — stale partials die
        partial_task = None   # the single in-flight partial task (drop-frame)
        next_partial_t = None  # fake-clock time the next partial is due

        async def _emit_partial(pcm: bytes, gen: int) -> None:
            """Transcribe a snapshot of the open utterance buffer and emit an
            interim stt_delta (cumulative for the utterance — the exact shape
            the Android live-partials chip and Portal interim text consume).
            A stale result (the utterance closed while in flight) is dropped —
            the final always supersedes. Best-effort: failures are logged,
            never fatal to the stream."""
            try:
                text = await _transcribe_partial(pcm)
            except Exception as e:
                print(f"[STT/WS] onbox partial transcription failed (dropped): {e!r}")
                return
            if gen != speech_gen or not speech_active:
                return   # utterance closed mid-flight — the final supersedes
            text = (text or "").strip()
            if not text or is_whisper_hallucination(text):
                return   # same whisper output class as finals -> same filter
            try:
                await websocket.send_json(
                    {"type": "stt_delta", "text": text, "target": target})
            except Exception as e:
                print(f"[STT/WS] onbox stt_delta delivery failed: {e!r}")

        def _partial_tick() -> None:
            """Cadence check, run after each inbound audio chunk. DROP-FRAME
            POLICY: never more than one partial transcription in flight — a
            due tick while busy is SKIPPED, not queued (bounded GPU load;
            finals always win)."""
            nonlocal partial_task, next_partial_t
            if not partials_on or not speech_active or next_partial_t is None:
                return
            if partial_task is not None and not partial_task.done():
                return   # busy — skip this tick
            now = _monotonic()
            if now < next_partial_t:
                return
            pcm = gate.active_pcm()
            if not pcm:
                return
            next_partial_t = now + partial_interval
            partial_task = asyncio.ensure_future(_emit_partial(pcm, speech_gen))

        def _close_partials() -> None:
            """SPEECH_END / stop / disconnect: stop the cadence and invalidate
            any in-flight partial (its late result is discarded)."""
            nonlocal speech_active, speech_gen, next_partial_t
            speech_active = False
            speech_gen += 1
            next_partial_t = None

        async def _finish_utterance(pcm: bytes, *, stopping: bool) -> None:
            """Transcribe one closed utterance and deliver its final. On the
            stop path a filtered/empty transcript still yields an authoritative
            EMPTY stt_final (cloud-bridge contract: the client discards, not
            resurrects, its interim); mid-stream it is simply suppressed and
            the loop resumes listening."""
            if not pcm:
                return
            await websocket.send_json({"type": "stt_status", "state": "processing"})
            try:
                text = await loop.run_in_executor(None, _transcribe_utterance, pcm)
            except Exception as e:
                # Honest per-utterance failure — surfaced, never swallowed into
                # a silent cloud fallback. Mid-stream the session survives.
                print(f"[STT/WS] onbox utterance transcription FAILED: {e!r}")
                try:
                    await websocket.send_json({
                        "type": "stt_error",
                        "message": f"on-box transcription failed: {e}"})
                except Exception:
                    pass
                if not stopping:
                    await websocket.send_json({"type": "stt_status", "state": "listening"})
                return
            text = (text or "").strip()
            if text and is_whisper_hallucination(text):
                print(f"[STT/WS] onbox stt_final FILTERED (hallucination) "
                      f"text_len={len(text)}")
                text = ""
            if text:
                await _send_final(websocket, "onbox",
                                  {"type": "stt_final", "text": text, "target": target},
                                  stop_ts)
            elif stopping:
                await _send_final(websocket, "onbox",
                                  {"type": "stt_final", "text": "", "target": target},
                                  stop_ts)
            if not stopping:
                await websocket.send_json({"type": "stt_status", "state": "listening"})

        try:
            while True:
                msg = await websocket.receive_json()
                mtype = msg.get("type")
                if mtype == "stt_audio":
                    pcm_b64 = msg.get("pcm", "")
                    if not pcm_b64:
                        continue
                    pcm16k = _resample_pcm16(base64.b64decode(pcm_b64), sample_rate, 16000)
                    for ev in gate.feed(pcm16k):
                        if ev.kind is _vad.SPEECH_START:
                            speech_active = True
                            next_partial_t = _monotonic() + partial_interval
                            await websocket.send_json({"type": "stt_status", "state": "speech"})
                        elif ev.kind is _vad.SPEECH_END:
                            _close_partials()   # partials STOP at SPEECH_END
                            await _finish_utterance(ev.pcm, stopping=False)
                    _partial_tick()
                elif mtype == "stt_stop":
                    stop_ts["v"] = time.monotonic()
                    _close_partials()
                    tail = gate.flush()
                    if tail is not None:
                        await _finish_utterance(tail.pcm or b"", stopping=True)
                    return
        except WebSocketDisconnect:
            # Abrupt client drop without stt_stop: commit the open tail
            # best-effort (the socket is usually gone; failed sends are fine),
            # then re-raise so the endpoint's normal teardown runs.
            if stop_ts["v"] is None:
                stop_ts["v"] = time.monotonic()
            _close_partials()
            tail = gate.flush()
            if tail is not None and tail.pcm:
                try:
                    await _finish_utterance(tail.pcm, stopping=True)
                except Exception:
                    pass
            raise
        finally:
            # Never leave a stray partial running past the session — a late
            # stt_delta must not chase the terminal stt_done.
            if partial_task is not None and not partial_task.done():
                partial_task.cancel()


async def _elevenlabs_bridge(websocket: WebSocket, *, target, lang, sample_rate):
    """Bridge client PCM -> ElevenLabs Scribe realtime -> client deltas/finals,
    with TRANSPARENT reconnect-and-resume on Scribe's session_time_limit_exceeded.

    A single client_pump drains the client socket into an asyncio.Queue (audio
    survives a reconnect) and ALWAYS sets disconnect_evt on exit so an abrupt
    client drop is observed. An upstream-manager loop opens a Scribe session per
    epoch; on the session cap it opens a fresh session and resumes WITHOUT telling
    the client (no stt_final, client socket stays open), stitching a carried
    `prefix` for transcript continuity. Reconnect is bounded (progress-guard +
    _EL_MAX_ROTATIONS + small backoff) so a flapping provider or a dead client
    can't storm. A normal stt_stop commits + drains the single stt_final.
    """
    key = resolve_api_key()
    if not key:
        await websocket.send_json({"type": "stt_error", "message": "ELEVENLABS_API_KEY not configured"})
        return

    url = (
        f"{WS_BASE_URL}/v1/speech-to-text/realtime"
        f"?model_id={config.ELEVENLABS_STT_STREAM_MODEL}"
        f"&sample_rate={sample_rate}&commit_strategy=manual"
    )
    if lang and lang != "auto":
        url += f"&language_code={lang}"

    audio_q: asyncio.Queue = asyncio.Queue()
    stop_evt = asyncio.Event()
    disconnect_evt = asyncio.Event()  # set when the CLIENT socket drops/ends
    prefix = ""
    stop_ts = {"v": None}          # monotonic time of the client's stt_stop (telemetry)
    commit_failed = {"v": False}   # the stop-commit never reached Scribe (socket died)
    final_sent = {"v": False}      # an stt_final (incl. an authoritative empty) was delivered

    async def client_pump():
        """Runs ONCE for the whole logical session: client -> queue. ALWAYS sets
        disconnect_evt on exit so the manager wakes even on an abrupt client drop."""
        try:
            while True:
                msg = await websocket.receive_json()
                mtype = msg.get("type")
                if mtype == "stt_audio":
                    pcm = msg.get("pcm", "")
                    if pcm:
                        await audio_q.put(("audio", pcm))
                elif mtype == "stt_stop":
                    stop_ts["v"] = time.monotonic()
                    await audio_q.put(("stop", None))
                    stop_evt.set()
                    return
        except WebSocketDisconnect:
            pass
        finally:
            disconnect_evt.set()

    async def run_epoch(el_ws) -> str:
        """Relay ONE Scribe connection. Returns 'rotate' (cap hit AND the epoch made
        progress -> reconnect) or 'done' (stop final delivered / client gone / fatal
        error / a no-progress rotate that must not be retried)."""
        nonlocal prefix
        acc = InterimAccumulator()
        last_interim = {"text": ""}
        rotate = {"v": False}
        progressed = {"v": False}  # consumed >=1 audio chunk OR delivered >=1 transcript

        async def feeder():
            while True:
                kind, pcm = await audio_q.get()
                progressed["v"] = True
                try:
                    if kind == "audio":
                        await el_ws.send(_el_audio_msg(pcm, sample_rate, commit=False))
                    else:  # stop
                        await el_ws.send(_el_audio_msg("", sample_rate, commit=True))
                        return
                except Exception as e:
                    # Scribe closed the socket under us (its ~30s cap closes with a
                    # clean 1000 mid-stream). Stop feeding and return cleanly — the
                    # reader's rotate fingerprint drives the reconnect; raising here
                    # would instead crash the epoch and tear down the client WS.
                    if kind == "stop":
                        # The stop-commit never reached Scribe → NO final is coming
                        # on this socket (cap seam at exactly the stop). Do NOT
                        # swallow that silently: flag it so the epoch skips the
                        # pointless 5s final-drain and the endpoint's stt_done
                        # reaches the client promptly (the client then commits its
                        # newest partial as the fallback final).
                        commit_failed["v"] = True
                        print(f"[STT/WS] elevenlabs stop-commit send FAILED "
                              f"(scribe socket dead): {e!r}")
                    return

        async def reader():
            async for raw in el_ws:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                err_code = classify_realtime_frame(event)
                if err_code:
                    print(f"[STT/WS] elevenlabs ERROR message: {json.dumps(event)[:500]}")
                    if err_code == "session_time_limit_exceeded":
                        rotate["v"] = True
                        return
                    await websocket.send_json({"type": "stt_error", "message": map_error(0, event)})
                    return
                m = acc.elevenlabs(event)
                if not m:
                    continue
                if m["type"] == "stt_final" and is_whisper_hallucination(m.get("text", "")):
                    print(f"[STT/WS] elevenlabs stt_final FILTERED (hallucination) "
                          f"text_len={len(m.get('text', ''))}")
                    if stop_evt.is_set():
                        # The committed (stop) final was hallucination-filtered —
                        # nothing more is coming. Deliver what is REAL: the carried
                        # prefix from earlier session rotations (speech the user
                        # actually produced), or an authoritative EMPTY final when
                        # there is none, so the client discards rather than
                        # resurrects the filtered interim. stt_done follows.
                        fm = {"type": "stt_final", "text": prefix, "target": target}
                        await _send_final(websocket, "elevenlabs", fm, stop_ts)
                        final_sent["v"] = True
                        return
                    continue
                progressed["v"] = True
                if m["type"] == "stt_delta":
                    last_interim["text"] = m["text"]
                    m = {"type": "stt_delta", "text": join_transcript_segments(prefix, m["text"])}
                else:
                    m = {"type": "stt_final", "text": join_transcript_segments(prefix, m["text"])}
                m["target"] = target
                if m["type"] == "stt_final":
                    await _send_final(websocket, "elevenlabs", m, stop_ts)
                    final_sent["v"] = True
                else:
                    await websocket.send_json(m)
                if m["type"] == "stt_final" and stop_evt.is_set():
                    return
            # Fell out of `async for` = Scribe ended the frame stream ITSELF
            # (provider-initiated close), NOT via our error/stop/rotate returns.
            # MEASURED 2026-07-05: at its ~30s session cap Scribe closes with a
            # CLEAN WebSocket 1000 and NO session_time_limit_exceeded frame — so the
            # error-frame path above never fires. Fingerprint of a cap-close vs. a
            # real end: the stream ended, the client never sent stt_stop, and the
            # session had transcribed (progressed). That trio can only mean "Scribe
            # timed out mid-utterance" → rotate (reconnect + resume, prefix carries
            # continuity), bounded by _EL_MAX_ROTATIONS in the caller.
            print(
                f"[STT/WS] scribe frame-stream ended by provider "
                f"close_code={getattr(el_ws, 'close_code', None)} "
                f"reason={getattr(el_ws, 'close_reason', None)!r} "
                f"progressed={progressed['v']} stop={stop_evt.is_set()}"
            )
            if progressed["v"] and not stop_evt.is_set() and not disconnect_evt.is_set():
                rotate["v"] = True

        feeder_task = asyncio.ensure_future(feeder())
        reader_task = asyncio.ensure_future(reader())
        disc_wait = asyncio.ensure_future(disconnect_evt.wait())
        tasks = (feeder_task, reader_task, disc_wait)
        try:
            await asyncio.wait(set(tasks), return_when=asyncio.FIRST_COMPLETED)
            # Abrupt client drop (NO stt_stop) -> terminal, never reconnect against a
            # dead client. NOTE: client_pump ALWAYS sets disconnect_evt on exit, incl.
            # a clean stt_stop, so gate on stop_evt — a normal stop must fall through
            # to drain the single committed final below (else the last utterance's
            # stt_final is dropped while the reader still awaits it from the network).
            if disconnect_evt.is_set() and not stop_evt.is_set():
                return "done"
            if (feeder_task.done() and not reader_task.done() and not rotate["v"]
                    and not commit_failed["v"]):
                # client committed (stop): drain for the single final, 5s backstop.
                # (Skipped when the commit never reached Scribe — no final can
                # come, so waiting would only delay the terminal stt_done.)
                try:
                    await asyncio.wait_for(asyncio.shield(reader_task), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
            for t in (feeder_task, reader_task):
                if t.done() and not t.cancelled():
                    exc = t.exception()
                    if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                        raise exc
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, WebSocketDisconnect):
                    pass
                except Exception:
                    pass

        if rotate["v"] and not stop_evt.is_set() and not disconnect_evt.is_set():
            # A session that connected and instantly claimed the time limit WITHOUT
            # transcribing or consuming anything is pathological -> do not spin on it.
            if not progressed["v"]:
                return "done"
            # resume-not-replay: audio already sent to the dying session but not yet
            # returned as a partial is not re-fed; continuity is carried by the last
            # delivered partial (prefix). A small boundary sliver may be dropped.
            prefix = join_transcript_segments(prefix, last_interim["text"])
            return "rotate"
        return "done"

    pump_task = asyncio.ensure_future(client_pump())
    rotations = 0
    try:
        while not stop_evt.is_set() and not disconnect_evt.is_set():
            try:
                el_ws = await websockets.connect(
                    url, additional_headers=auth_headers(key),
                    open_timeout=10, ping_interval=20, ping_timeout=30, close_timeout=10,
                )
            except Exception as e:
                print(f"[STT/WS] elevenlabs connect failed: {e}")
                try:
                    await websocket.send_json({"type": "stt_error", "message": f"STT connection failed: {e}"})
                except Exception:
                    pass
                break
            print(f"[STT/WS] elevenlabs connected model={config.ELEVENLABS_STT_STREAM_MODEL} "
                  f"rate={sample_rate} commit_strategy=manual")
            try:
                result = await run_epoch(el_ws)
            finally:
                try:
                    await el_ws.close()
                except Exception:
                    pass
            if result == "rotate" and not stop_evt.is_set() and not disconnect_evt.is_set():
                rotations += 1
                if rotations > _EL_MAX_ROTATIONS:
                    print(f"[STT/WS] elevenlabs rotation cap ({_EL_MAX_ROTATIONS}) exceeded — ending session")
                    try:
                        await websocket.send_json({"type": "stt_error", "message": "STT session could not be sustained"})
                    except Exception:
                        pass
                    break
                print(f"[STT/WS] elevenlabs session_time_limit_exceeded — reconnecting & resuming (attempt {rotations})")
                await asyncio.sleep(min(0.2 * rotations, 1.0))  # small backoff against a flapping provider
                continue
            break
        if stop_evt.is_set() and not final_sent["v"]:
            # Stop-path telemetry (2026-07-09): the stop window closed with NO
            # stt_final delivered (cap seam ate it / commit never landed). The
            # endpoint's stt_done still goes out, and the client commits its
            # newest partial as the fallback final.
            print(f"[STT/WS] elevenlabs stop ended WITHOUT a final "
                  f"(commit_failed={commit_failed['v']}, "
                  f"stop_to_end_ms={_stop_latency_ms(stop_ts)})")
    finally:
        if not pump_task.done():
            pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
        except Exception:
            pass


# =============================================================================
# Google Cloud Speech-to-Text v2 streaming bridge (gRPC, off-loop)
# =============================================================================

# Bounded post-stop flush: after the client's stt_stop we half-close the gRPC
# stream and Google flushes the trailing final — usually <2s, but the API has
# NO deadline (journal-proven multi-second stalls, 2026-07-08). The Android
# client waits up to 10s for stt_done, so the layered backstops must be
# STRICTLY ordered: 7s flush + 1s emit-drain = 8s worst case server-side,
# leaving a 2s margin under the client's 10s. On expiry the terminal marker
# still goes out and the client commits its newest partial as the fallback.
_GOOGLE_STOP_FLUSH_TIMEOUT_S = 7.0
# Cap on draining already-scheduled cross-thread emits after the flush (part of
# the 8s worst case above; normally instantaneous).
_GOOGLE_EMIT_DRAIN_TIMEOUT_S = 1.0


async def _google_bridge(websocket: WebSocket, *, target, lang, sample_rate):
    """Bridge client PCM -> Google STT v2 streaming -> client deltas/finals.

    Google's streaming client is synchronous/gRPC, so we run it in a worker
    thread and shuttle audio in via a thread-safe queue and results out via
    loop.call_soon_threadsafe. This keeps the asyncio event loop unblocked.
    """
    creds_path = config.GOOGLE_APPLICATION_CREDENTIALS
    if not creds_path:
        await websocket.send_json({"type": "stt_error", "message": "GOOGLE_APPLICATION_CREDENTIALS not configured"})
        return
    try:
        with open(creds_path, "r") as f:
            project_id = json.load(f).get("project_id")
    except (OSError, ValueError) as e:
        await websocket.send_json({"type": "stt_error", "message": f"invalid Google credentials file: {e}"})
        return
    if not project_id:
        await websocket.send_json({"type": "stt_error", "message": "project_id missing from Google credentials file"})
        return

    # Lazy SDK import (kept lazy for symmetry with file_transcribe + optionality).
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient
    from google.cloud.speech_v2.types import (
        ExplicitDecodingConfig,
        RecognitionConfig,
        StreamingRecognitionConfig,
        StreamingRecognitionFeatures,
        StreamingRecognizeRequest,
    )

    loop = asyncio.get_running_loop()
    region = config.STT_GOOGLE_REGION
    recognizer = f"projects/{project_id}/locations/{region}/recognizers/_"

    # PCM16 LINEAR16 explicit decoding — client sends raw PCM at sample_rate.
    rec_config = RecognitionConfig(
        explicit_decoding_config=ExplicitDecodingConfig(
            encoding=ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=sample_rate,
            audio_channel_count=1,
        ),
        language_codes=[_normalize_google_lang(lang)],
        model=config.STT_GOOGLE_MODEL,
    )
    streaming_config = StreamingRecognitionConfig(
        config=rec_config,
        streaming_features=StreamingRecognitionFeatures(interim_results=True),
    )

    # Thread-safe audio handoff. The worker thread pulls PCM bytes from this
    # queue; None is the sentinel to end the stream (stt_stop / disconnect).
    import queue as _queue
    audio_q: "_queue.Queue" = _queue.Queue()

    # Normalize interim semantics into the uniform cumulative contract. Google's
    # interim results are already cumulative, but routing through the accumulator
    # keeps the buffer-reset-on-final behavior consistent across providers. Only
    # the worker thread touches `acc`, so no locking is needed.
    acc = InterimAccumulator()

    def _request_gen():
        # First request carries the streaming config, then audio chunks.
        yield StreamingRecognizeRequest(
            recognizer=recognizer,
            streaming_config=streaming_config,
        )
        while True:
            chunk = audio_q.get()
            if chunk is None:
                return
            yield StreamingRecognizeRequest(audio=chunk)

    # Set when the client sends stt_stop (telemetry: stop→final latency). The
    # worker thread only reads it; the receive loop only writes it once.
    stop_ts = {"v": None}
    # Every cross-thread emit future, so the bridge can DRAIN in-flight sends
    # before returning — the endpoint's terminal stt_done must never overtake a
    # trailing final still scheduled from the worker thread.
    emit_futs = []

    def _emit(payload: dict):
        # Thread -> loop. Schedule the send_json coroutine on the event loop.
        # Observe the returned future so a failed cross-thread send surfaces as
        # a log line instead of vanishing silently (telemetry-before-silent).
        fut = asyncio.run_coroutine_threadsafe(websocket.send_json(payload), loop)
        emit_futs.append(fut)

        def _log_emit_error(f):
            try:
                exc = f.exception()
            except Exception:
                return
            if exc is not None:
                print(f"[STT/WS] google emit failed: {exc!r}")

        fut.add_done_callback(_log_emit_error)

    def _run_google():
        print(f"[STT/WS] google worker start region={region} model={config.STT_GOOGLE_MODEL} "
              f"project={project_id} rate={sample_rate} recognizer={recognizer}")
        try:
            client = SpeechClient(
                client_options=ClientOptions(api_endpoint=f"{region}-speech.googleapis.com")
            )
            responses = client.streaming_recognize(requests=_request_gen())
            for response in responses:
                for result in response.results:
                    if not result.alternatives:
                        continue
                    text = result.alternatives[0].transcript or ""
                    is_final = bool(result.is_final)
                    if is_final and is_whisper_hallucination(text):
                        lat = _stop_latency_ms(stop_ts)
                        print(f"[STT/WS] google stt_final FILTERED (hallucination) "
                              f"text_len={len(text)} "
                              f"stop_to_final_ms={lat if lat is not None else 'pre-stop'}")
                        if stop_ts["v"] is not None:
                            # The trailing (post-stop) final was filtered: send an
                            # authoritative EMPTY final so the client discards, not
                            # resurrects, the filtered interim. stt_done follows.
                            _emit({"type": "stt_final", "text": "", "target": target})
                        continue
                    m = acc.google(text, is_final)
                    m["target"] = target
                    if is_final:
                        lat = _stop_latency_ms(stop_ts)
                        print(f"[STT/WS] google stt_final delivering text_len={len(text)} "
                              f"stop_to_final_ms={lat if lat is not None else 'pre-stop'}")
                    _emit(m)
            print("[STT/WS] google stream ended")
        except Exception as e:  # surface gRPC/auth errors to the client
            print(f"[STT/WS] google worker EXCEPTION: {e!r}")
            _emit({"type": "stt_error", "message": str(e)})

    # Run the blocking gRPC stream in a worker thread.
    worker = loop.run_in_executor(None, _run_google)
    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "stt_audio":
                pcm = msg.get("pcm", "")
                if pcm:
                    try:
                        audio_q.put(base64.b64decode(pcm))
                    except Exception:
                        pass
            elif mtype == "stt_stop":
                stop_ts["v"] = time.monotonic()
                break
            # If the worker thread died early (e.g. gRPC/auth error -> stt_error),
            # stop queueing audio against an abandoned generator.
            if worker.done():
                break
    except WebSocketDisconnect:
        pass
    finally:
        # Signal the request generator to finish, then await the worker — BOUNDED:
        # gRPC has no deadline on the trailing flush and the client only waits
        # ~10s for the terminal stt_done, so cap the drain rather than hanging
        # the terminal marker behind a stalled provider. (On expiry the worker
        # thread lingers; its late emits fail against the closed socket and are
        # logged by _log_emit_error.)
        audio_q.put(None)
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=_GOOGLE_STOP_FLUSH_TIMEOUT_S)
        except asyncio.TimeoutError:
            print(f"[STT/WS] google post-stop flush exceeded "
                  f"{_GOOGLE_STOP_FLUSH_TIMEOUT_S}s — proceeding to stt_done "
                  f"WITHOUT the trailing final "
                  f"(stop_to_now_ms={_stop_latency_ms(stop_ts)})")
        except Exception:
            pass
        # Drain in-flight cross-thread emits so the endpoint's stt_done is ALWAYS
        # sent AFTER any final the worker already scheduled onto the loop.
        pending = [asyncio.wrap_future(f) for f in list(emit_futs) if not f.done()]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=_GOOGLE_EMIT_DRAIN_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                pass
