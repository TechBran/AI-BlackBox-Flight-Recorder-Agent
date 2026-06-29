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
from Orchestrator.elevenlabs.client import (
    resolve_api_key, WS_BASE_URL, auth_headers, map_error, classify_realtime_frame,
)
from Orchestrator.stt.resolve import resolve_stt_provider
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


# Absolute backstop on consecutive session rotations (storm guard). Only
# PROGRESSING sessions count toward it (a no-progress instant rotate bails early),
# so the real ceiling on continuous dictation is ~30 x Scribe's per-session limit
# (minutes each) — comfortably beyond any realistic single utterance. Tunable.
_EL_MAX_ROTATIONS = 30


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
                if kind == "audio":
                    await el_ws.send(_el_audio_msg(pcm, sample_rate, commit=False))
                else:  # stop
                    await el_ws.send(_el_audio_msg("", sample_rate, commit=True))
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
                    continue
                progressed["v"] = True
                if m["type"] == "stt_delta":
                    last_interim["text"] = m["text"]
                    m = {"type": "stt_delta", "text": join_transcript_segments(prefix, m["text"])}
                else:
                    m = {"type": "stt_final", "text": join_transcript_segments(prefix, m["text"])}
                m["target"] = target
                await websocket.send_json(m)
                if m["type"] == "stt_final" and stop_evt.is_set():
                    return

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
            if feeder_task.done() and not reader_task.done() and not rotate["v"]:
                # client committed (stop): drain for the single final, 5s backstop.
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
