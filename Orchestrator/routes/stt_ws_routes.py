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

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from Orchestrator.checkpoint import app
from Orchestrator import config
from Orchestrator.elevenlabs.client import resolve_api_key, WS_BASE_URL, auth_headers, map_error
from Orchestrator.stt.resolve import resolve_stt_provider
from Orchestrator.stt.streaming import map_openai_event, map_google_result, InterimAccumulator
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
        print(f"[STT/WS] start provider={provider} sample_rate={start.get('sample_rate')} "
              f"lang={start.get('lang')} target={start.get('target')}")
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
    sample_rate = start.get("sample_rate", 24000)
    if provider == "google":
        await _google_bridge(websocket, target=target, lang=lang, sample_rate=sample_rate)
    elif provider == "elevenlabs":
        await _elevenlabs_bridge(websocket, target=target, lang=lang, sample_rate=sample_rate)
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
        print(f"[STT/WS] openai connected model={config.STT_OPENAI_STREAM} rate={sample_rate} "
              f"url={url}; session.update sent")

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


async def _elevenlabs_bridge(websocket: WebSocket, *, target, lang, sample_rate):
    """Bridge client PCM -> ElevenLabs Scribe realtime -> client deltas/finals.

    Mirrors _openai_bridge's lifecycle exactly: a pump task (client stt_audio ->
    provider input_audio_chunk) and a relay task (provider messages -> client
    stt_delta/stt_final), decoupled via asyncio so the client pump never blocks.
    On stt_stop we send a commit:true chunk and drain the relay for the single
    committed (final) transcript before tearing down.
    """
    key = resolve_api_key()
    if not key:
        await websocket.send_json({"type": "stt_error", "message": "ELEVENLABS_API_KEY not configured"})
        return

    # commit_strategy=manual: live-verified (2026-06-13) to STILL stream
    # partial_transcript during speech while letting us drive the single final
    # via an explicit commit:true chunk on stt_stop — parity with openai/google,
    # which commit on stt_stop and emit exactly one stt_final per push-to-talk.
    url = (
        f"{WS_BASE_URL}/v1/speech-to-text/realtime"
        f"?model_id={config.ELEVENLABS_STT_STREAM_MODEL}"
        f"&sample_rate={sample_rate}&commit_strategy=manual"
    )
    if lang and lang != "auto":
        url += f"&language_code={lang}"

    el_ws = await websockets.connect(
        url,
        additional_headers=auth_headers(key),
        open_timeout=10, ping_interval=20, ping_timeout=30, close_timeout=10,
    )
    try:
        print(f"[STT/WS] elevenlabs connected model={config.ELEVENLABS_STT_STREAM_MODEL} "
              f"rate={sample_rate} commit_strategy=manual url={url}")

        # Set when the client requests a manual stop (commit sent). The relay
        # keeps draining until it delivers the final for the committed audio,
        # then exits — so a normal push-to-talk stop never drops stt_final.
        stop_evt = asyncio.Event()

        # Normalize Scribe's (already-cumulative) partials into the uniform
        # cumulative interim contract + reset-on-final, same as the other bridges.
        acc = InterimAccumulator()

        async def client_to_el():
            """Pump client audio into Scribe; commit + signal stop on stt_stop."""
            while True:
                msg = await websocket.receive_json()
                mtype = msg.get("type")
                if mtype == "stt_audio":
                    pcm = msg.get("pcm", "")
                    if pcm:
                        await el_ws.send(_el_audio_msg(pcm, sample_rate, commit=False))
                elif mtype == "stt_stop":
                    # Empty chunk + commit:true flushes the tail -> committed_transcript.
                    await el_ws.send(_el_audio_msg("", sample_rate, commit=True))
                    stop_evt.set()
                    return

        async def el_to_client():
            """Relay Scribe transcription events back to the client."""
            async for raw in el_ws:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                # Surface Scribe error-class messages instead of swallowing them.
                # Error-shaped frames carry an "error"/"status" field or a
                # message_type containing "error" (the provider taxonomy:
                # auth_error, quota_exceeded, session_time_limit_exceeded, ...).
                # map_error reads the {"detail": {"status": ...}} shape, so wrap
                # the extracted code accordingly.
                mt = event.get("message_type", "")
                status = event.get("status") or event.get("error")
                if status or "error" in mt:
                    code = status or mt
                    print(f"[STT/WS] elevenlabs ERROR message: {json.dumps(event)[:500]}")
                    # session_time_limit_exceeded ends the session normally (the
                    # provider closes the socket); forward any final already sent
                    # and let the relay exit on the close — no client error.
                    if code != "session_time_limit_exceeded":
                        await websocket.send_json(
                            {"type": "stt_error", "message": map_error(0, {"detail": {"status": code}})}
                        )
                        return
                    continue
                m = acc.elevenlabs(event)
                if not m:
                    continue
                if m["type"] == "stt_final" and is_whisper_hallucination(m.get("text", "")):
                    continue
                m["target"] = target
                await websocket.send_json(m)
                # On a manual stop, the committed (final) transcript is the last
                # thing we need — stop draining once it's delivered.
                if m["type"] == "stt_final" and stop_evt.is_set():
                    return

        pump = asyncio.ensure_future(client_to_el())
        relay = asyncio.ensure_future(el_to_client())
        try:
            done, pending = await asyncio.wait(
                {pump, relay}, return_when=asyncio.FIRST_COMPLETED
            )
            if pump in done and relay not in done:
                # Client stopped: Scribe stays open and delivers the committed
                # transcript AFTER our commit. Drain for it instead of cancelling
                # synchronously. 5s backstop so we never hang if none arrives.
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
                # Scribe closed / error / disconnect first: cancel the pump.
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
        # Always close the ElevenLabs WS — billing + cleanup.
        try:
            await el_ws.close()
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
                        continue
                    m = acc.google(text, is_final)
                    m["target"] = target
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
