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

stt_delta.text is the CUMULATIVE interim transcript so far (client replaces the
interim region); stt_final.text is the full final (client commits). Providers
stream interim text with opposite semantics (OpenAI incremental, Google
cumulative), so InterimAccumulator normalizes both to this uniform contract.

The endpoint resolves a provider via resolve_stt_provider() and bridges to either
OpenAI's realtime transcription WS (gRPC-free, websockets lib) or Google Cloud
Speech-to-Text v2 streaming (gRPC, run in a thread executor so the asyncio event
loop is never blocked). Pure provider-event translation lives in
Orchestrator/stt/streaming.py; whisper hallucination filtering in whisper_filter.

SAMPLE-RATE WATCH-ITEM: OpenAI realtime audio is canonically 24kHz while web/
Android typically capture 16kHz, and Google v2 commonly expects 16kHz. We pass
the client-declared sample_rate straight through to each provider. This needs a
live A/B benchmark at integration time to confirm OpenAI accepts the declared
rate (vs. requiring resample to 24k) — flagged here, not silently assumed.

`resolve_stt_provider` and `run_stt_bridge` are referenced as MODULE GLOBALS so
the test suite can monkeypatch them.
"""

import asyncio
import base64
import json

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from Orchestrator.checkpoint import app
from Orchestrator import config
from Orchestrator.stt.resolve import resolve_stt_provider
from Orchestrator.stt.streaming import map_openai_event, map_google_result, InterimAccumulator
from Orchestrator.whisper_filter import is_whisper_hallucination


@app.websocket("/ws/stt")
async def ws_stt(websocket: WebSocket):
    await websocket.accept()
    try:
        start = await websocket.receive_json()
        if start.get("type") != "stt_start":
            await websocket.send_json({"type": "stt_error", "message": "expected stt_start"})
            return
        provider = resolve_stt_provider(start.get("provider"))
        if not provider:
            await websocket.send_json({"type": "stt_error", "message": "no STT provider configured"})
            return
        await run_stt_bridge(websocket, provider, start)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "stt_error", "message": str(e)})
        except Exception:
            pass


async def run_stt_bridge(websocket: WebSocket, provider: str, start: dict):
    """Dispatch to the per-provider streaming bridge."""
    target = start.get("target")
    lang = start.get("lang")
    sample_rate = start.get("sample_rate", 16000)
    if provider == "google":
        await _google_bridge(websocket, target=target, lang=lang, sample_rate=sample_rate)
    else:
        await _openai_bridge(websocket, target=target, lang=lang, sample_rate=sample_rate)


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

        # Set when the client requests a manual stop (commit sent). The relay
        # keeps draining until it delivers the final for the committed audio,
        # then exits — so a normal push-to-talk stop never drops the last
        # utterance's stt_final.
        stop_evt = asyncio.Event()

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
                    stop_evt.set()
                    return

        async def openai_to_client():
            """Relay OpenAI transcription events back to the client."""
            async for raw in openai_ws:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                m = acc.openai(event)
                if not m:
                    continue
                if m["type"] == "stt_final" and is_whisper_hallucination(m.get("text", "")):
                    continue
                m["target"] = target
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
# Google Cloud Speech-to-Text v2 streaming bridge (gRPC, off-loop)
# =============================================================================

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
        language_codes=[lang or "en-US"],
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

    def _emit(payload: dict):
        # Thread -> loop. Schedule the send_json coroutine on the event loop.
        # Observe the returned future so a failed cross-thread send surfaces as
        # a log line instead of vanishing silently (telemetry-before-silent).
        fut = asyncio.run_coroutine_threadsafe(websocket.send_json(payload), loop)

        def _log_emit_error(f):
            try:
                exc = f.exception()
            except Exception:
                return
            if exc is not None:
                print(f"[STT/WS] google emit failed: {exc!r}")

        fut.add_done_callback(_log_emit_error)

    def _run_google():
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
                        continue
                    m = acc.google(text, is_final)
                    m["target"] = target
                    _emit(m)
        except Exception as e:  # surface gRPC/auth errors to the client
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
                break
            # If the worker thread died early (e.g. gRPC/auth error -> stt_error),
            # stop queueing audio against an abandoned generator.
            if worker.done():
                break
    except WebSocketDisconnect:
        pass
    finally:
        # Signal the request generator to finish, then await the worker.
        audio_q.put(None)
        try:
            await worker
        except Exception:
            pass
