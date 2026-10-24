"""Audit rank 46 (O07-tts-voice-02/-03): TTS, STT and Voice Lab routes never
block the event loop on a provider call.

These routes are async (they await uploads and request bodies), but called
ElevenLabs, xAI, Google, OpenAI, the local stack and the ledger through sync
helpers (requests/httpx, timeouts up to 120 s) right on the loop, so one Voice
Lab clone or a long transcription froze every chat stream, STT socket and
voice call on the box. The calls now run in asyncio.to_thread -- not on the
8-worker _tts_executor, which /tts/batch fills with one future per chunk, so
an STT request queued there would wait behind a long synthesis.

Each case stubs the route's provider helper with a 0.3 s blocking sleep and
calls the route coroutine directly while a 20 ms ticker runs on the loop.
"""
import asyncio
import base64
import io
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from Orchestrator.routes import elevenlabs_routes as el_routes
from Orchestrator.routes import tts_routes
from Orchestrator.routes import xai_voice_routes as xai_routes

SLEEP = 0.3


def _slow(ret=None):
    def stub(*a, **kw):
        time.sleep(SLEEP)
        return ret
    return stub


def _upload(name="clip.webm", data=b"\x00" * 256, ctype="audio/webm"):
    return UploadFile(file=io.BytesIO(data), filename=name,
                      headers=Headers({"content-type": ctype}))


class _Resp:
    status_code = 200
    text = ""
    content = b"RIFF"

    def json(self):
        return {"ok": True, "voice_id": "v1", "previews": []}


async def _ticks_during(make_call):
    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        await make_call()
    except HTTPException:
        pass  # some stubs lead to a 4xx/5xx; only the loop matters here
    finally:
        stop.set()
        await t
    return ticks


# --- case setup: (name, patcher(monkeypatch, tmp_path) -> coroutine factory) --

def _stt(mp, tmp):
    import Orchestrator.stt.file_transcribe as ft
    mp.setattr(ft, "transcribe_bytes", _slow("hello"))
    return lambda: tts_routes.stt(file=_upload(), provider="openai", diarize=False)


def _stt_resolve(mp, tmp):
    import Orchestrator.stt.file_transcribe as ft
    import Orchestrator.stt.resolve as resolve
    mp.setattr(resolve, "resolve_stt_provider", _slow("openai"))
    mp.setattr(ft, "transcribe_bytes", lambda *a, **k: "hello")
    return lambda: tts_routes.stt(file=_upload(), provider=None, diarize=False)


def _stt_diarized(mp, tmp):
    import Orchestrator.elevenlabs.stt as el_stt
    rich = {"text": "hi", "segments": [], "speakers": [], "events": [], "language": "en"}
    mp.setattr(el_stt, "transcribe_bytes", _slow(rich))
    mp.setattr(el_stt, "format_diarized", lambda r: "hi")
    return lambda: tts_routes.stt(file=_upload(), provider="elevenlabs", diarize=True)


def _stt_json(mp, tmp):
    import Orchestrator.stt.file_transcribe as ft
    mp.setattr(ft, "transcribe_bytes", _slow("hello"))
    body = {"audio": base64.b64encode(b"\x00\x01" * 400).decode()}
    return lambda: tts_routes.stt_json(body)


def _stt_translate(mp, tmp):
    import Orchestrator.stt.translate as tr
    mp.setattr(tr, "translate_text", _slow("hola"))
    return lambda: tts_routes.stt_translate({"text": "hello", "target_lang": "es"})


def _stt_translate_audio(mp, tmp):
    import Orchestrator.stt.file_transcribe as ft
    import Orchestrator.stt.translate as tr
    mp.setattr(ft, "transcribe_bytes", _slow("hello"))
    mp.setattr(tr, "translate_text", lambda t, lang: "hola")
    body = {"audio": base64.b64encode(b"\x00" * 64).decode(), "target_lang": "es"}
    return lambda: tts_routes.stt_translate(body)


def _stt_catalog(mp, tmp):
    import Orchestrator.stt.catalog as cat
    import Orchestrator.stt.resolve as resolve
    mp.setattr(resolve, "resolve_stt_provider", _slow("openai"))
    mp.setattr(cat, "build_stt_catalog", lambda: [])
    mp.setattr(cat, "build_stt_resolution", lambda: {
        "resolved_file": "openai", "resolved_streaming": "openai",
        "overridden_from_file": None, "overridden_from_streaming": None})
    return lambda: tts_routes.stt_catalog()


def _google_voices(mp, tmp):
    mp.setattr(tts_routes, "call_google_tts_voices", _slow({"voices": []}))
    return lambda: tts_routes.get_google_voices()


def _tts_queue(mp, tmp):
    import Orchestrator.qwen_tts as qwen
    mp.setattr(qwen, "_tts_available", _slow(True))
    mp.setattr(tts_routes._tts_queue, "submit", lambda **kw: {
        "task_id": "t1", "status": "queued", "queue_position": 0, "eta_s": 1})
    return lambda: tts_routes.tts_queue_submit({"text": "hi", "voice": "qwen:alpha"})


def _tts_batch_elevenlabs(mp, tmp):
    import Orchestrator.elevenlabs.tts as el_tts
    mp.setattr(el_tts, "max_chars_for", _slow(1000))
    mp.setattr(el_tts, "synthesize", lambda *a, **k: b"ID3")
    body = {"text": "hello there", "provider": "elevenlabs", "voice": "v1"}
    return lambda: tts_routes.tts_batch(body, None)


def _last_response(mp, tmp):
    mp.setattr(tts_routes, "load_snapshot_index", _slow({}))
    return lambda: tts_routes.get_last_response("Nobody")


def _media_route(mp, tmp):
    import Orchestrator.media_index as media_index
    mp.setattr(tts_routes, "UPLOADS_DIR", tmp)
    mp.setattr(media_index, "add_media_entry", lambda **kw: None)
    mp.setattr(tts_routes, "_contained_audio_path", lambda p: p)


def _sfx(mp, tmp):
    import Orchestrator.elevenlabs.sfx as sfx
    _media_route(mp, tmp)
    mp.setattr(sfx, "generate", _slow(b"ID3"))
    return lambda: tts_routes.generate_elevenlabs_sound_effect({"text": "rain on a roof"})


def _voice_changer(mp, tmp):
    import Orchestrator.elevenlabs.transform as tf
    _media_route(mp, tmp)
    mp.setattr(tf, "change_voice", _slow(b"ID3"))
    return lambda: tts_routes.elevenlabs_voice_changer(
        {"audio_path": "/ui/uploads/a.mp3", "target_voice": "elevenlabs:v1"})


def _isolate(mp, tmp):
    import Orchestrator.elevenlabs.transform as tf
    _media_route(mp, tmp)
    mp.setattr(tf, "isolate", _slow(b"ID3"))
    return lambda: tts_routes.elevenlabs_isolate({"audio_path": "/ui/uploads/a.mp3"})


def _qwen_design(mp, tmp):
    import Orchestrator.qwen_tts as qwen
    mp.setattr(qwen, "upstream_url", lambda p: "http://127.0.0.1:1" + p)
    mp.setattr(tts_routes.requests, "post", _slow(_Resp()))
    return lambda: tts_routes.qwen_voices_design({"voice_description": "warm"})


def _qwen_list(mp, tmp):
    import Orchestrator.qwen_tts as qwen
    mp.setattr(qwen, "list_profiles", _slow([]))
    return lambda: tts_routes.qwen_voices_list()


def _el_key(mp):
    import Orchestrator.elevenlabs.client as client
    mp.setattr(client, "resolve_api_key", lambda *a, **k: "fake-key")


def _el_status(mp, tmp):
    import Orchestrator.elevenlabs.catalog as catalog
    _el_key(mp)
    mp.setattr(catalog, "get_user", _slow({}))
    return lambda: el_routes.elevenlabs_status()


def _el_library(mp, tmp):
    import Orchestrator.elevenlabs.catalog as catalog
    mp.setattr(catalog, "get_shared_voices", _slow(None))
    return lambda: el_routes.elevenlabs_library(search="x", page_size=10, gender=None, category=None)


def _el_library_add(mp, tmp):
    import Orchestrator.elevenlabs.catalog as catalog
    mp.setattr(catalog, "add_shared_voice", _slow({"voice_id": "v1"}))
    return lambda: el_routes.elevenlabs_library_add(
        {"public_owner_id": "o", "voice_id": "v1", "name": "n"})


def _el_clone(mp, tmp):
    import Orchestrator.elevenlabs.voices as voices
    _el_key(mp)
    mp.setattr(voices, "clone_instant", _slow({"voice_id": "v1"}))
    return lambda: el_routes.elevenlabs_voices_clone(
        name="V", files=[_upload("a.mp3")], consent="true", description=None,
        remove_background_noise="true")


def _el_design(mp, tmp):
    import Orchestrator.elevenlabs.voices as voices
    mp.setattr(voices, "design_previews", _slow({"text": "t", "previews": []}))
    return lambda: el_routes.elevenlabs_voices_design({"voice_description": "gravelly"})


def _el_design_save(mp, tmp):
    import Orchestrator.elevenlabs.voices as voices
    mp.setattr(voices, "design_save", _slow({"voice_id": "v1"}))
    return lambda: el_routes.elevenlabs_voices_design_save(
        {"generated_voice_id": "g1", "name": "n"})


def _el_list(mp, tmp):
    import Orchestrator.elevenlabs.catalog as catalog
    mp.setattr(catalog, "get_voices", _slow(None))
    return lambda: el_routes.elevenlabs_voices_list()


def _el_delete(mp, tmp):
    import Orchestrator.elevenlabs.voices as voices
    mp.setattr(voices, "voice_in_use", lambda v: [])
    mp.setattr(voices, "delete_voice", _slow({"ok": True}))
    return lambda: el_routes.elevenlabs_voices_delete("v1")


def _xai_list(mp, tmp):
    import Orchestrator.xai_voices as xv
    mp.setattr(xv, "list_custom_voices", _slow([]))
    return lambda: xai_routes.xai_voices_list()


def _xai_clone(mp, tmp):
    import Orchestrator.xai_voices as xv
    mp.setattr(xv, "resolve_api_key", lambda *a, **k: "fake-key")
    mp.setattr(xv, "clone_voice", _slow({"voice_id": "x1", "name": "V"}))
    return lambda: xai_routes.xai_voices_clone(
        name="V", file=_upload("a.mp3"), consent="true", description=None)


def _xai_delete(mp, tmp):
    import Orchestrator.xai_voices as xv
    mp.setattr(xv, "delete_voice", _slow(None))
    return lambda: xai_routes.xai_voices_delete("x1")


CASES = [
    ("stt", _stt), ("stt_resolve", _stt_resolve), ("stt_diarized", _stt_diarized),
    ("stt_json", _stt_json), ("stt_translate", _stt_translate),
    ("stt_translate_audio", _stt_translate_audio), ("stt_catalog", _stt_catalog),
    ("tts_google_voices", _google_voices), ("tts_queue_submit", _tts_queue),
    ("tts_batch_elevenlabs_cap", _tts_batch_elevenlabs),
    ("last_response", _last_response), ("elevenlabs_sound_effect", _sfx),
    ("elevenlabs_voice_changer", _voice_changer), ("elevenlabs_isolate", _isolate),
    ("qwen_design", _qwen_design), ("qwen_list", _qwen_list),
    ("elevenlabs_status", _el_status), ("elevenlabs_library", _el_library),
    ("elevenlabs_library_add", _el_library_add), ("elevenlabs_clone", _el_clone),
    ("elevenlabs_design", _el_design), ("elevenlabs_design_save", _el_design_save),
    ("elevenlabs_voices", _el_list), ("elevenlabs_delete", _el_delete),
    ("xai_voices", _xai_list), ("xai_clone", _xai_clone), ("xai_delete", _xai_delete),
]


@pytest.mark.parametrize("name,setup", CASES, ids=[c[0] for c in CASES])
def test_route_keeps_the_loop_running_during_a_slow_provider(monkeypatch, tmp_path, name, setup):
    make_call = setup(monkeypatch, tmp_path)
    ticks = asyncio.run(_ticks_during(make_call))
    # ~15 ticks fit in 0.3 s; a route blocking the loop gets 0-1.
    assert ticks >= 8, f"{name}: the provider call stalled the event loop (ticks={ticks})"


def test_stt_does_not_queue_behind_a_busy_tts_executor(monkeypatch, tmp_path):
    """A long /tts/batch fan-out fills _tts_executor; a transcription must not
    wait for those workers to free up."""
    monkeypatch.setattr(tts_routes, "_tts_executor", ThreadPoolExecutor(max_workers=1))
    busy = tts_routes._tts_executor.submit(time.sleep, 1.5)
    import Orchestrator.stt.file_transcribe as ft
    monkeypatch.setattr(ft, "transcribe_bytes", lambda *a, **k: "hello")
    t0 = time.monotonic()
    out = asyncio.run(tts_routes.stt(file=_upload(), provider="openai", diarize=False))
    elapsed = time.monotonic() - t0
    busy.result()
    assert out == {"text": "hello"}
    assert elapsed < 0.8, f"/stt waited {elapsed:.2f}s for the TTS chunk pool"


def test_voice_lab_calls_do_not_hold_tts_chunk_workers(monkeypatch):
    """A Qwen design/clone call can run for minutes of GPU synthesis; it runs on
    its own thread, so it neither waits for nor occupies a TTS chunk worker."""
    import Orchestrator.qwen_tts as qwen
    monkeypatch.setattr(tts_routes, "_tts_executor", ThreadPoolExecutor(max_workers=1))
    busy = tts_routes._tts_executor.submit(time.sleep, 1.5)
    monkeypatch.setattr(qwen, "upstream_url", lambda p: "http://127.0.0.1:1" + p)
    monkeypatch.setattr(tts_routes.requests, "post", lambda *a, **k: _Resp())
    t0 = time.monotonic()
    asyncio.run(tts_routes.qwen_voices_design({"voice_description": "warm"}))
    elapsed = time.monotonic() - t0
    busy.result()
    assert elapsed < 0.8, f"the design call waited {elapsed:.2f}s for the TTS chunk pool"
