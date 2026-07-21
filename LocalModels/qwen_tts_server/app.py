"""qwen-tts — standalone FastAPI server exposing the three Qwen3-TTS 1.7B
variants behind an OpenAI-compatible audio surface, plus consent-gated cloning
and 2-step voice design (§5.4). Runs as the `qwen-tts` member of
blackbox-models.service's llama-swap front door. STANDALONE: no Orchestrator
import (own lean venv — the MCP lean-venv lesson).

Orchestrator (M7) path contract:
  * /health, /v1/audio/speech, /v1/audio/voices are OpenAI-shaped paths that
    llama-swap body-`model` auto-routes — the Orchestrator calls them at
    http://127.0.0.1:9098/v1/... (front door).
  * /v1/voices/clone, /v1/voices/design, /v1/voices/design/save are NON-OpenAI
    paths llama-swap does NOT auto-route (it extracts `model` only from known
    endpoints, open #245) — the Orchestrator MUST call them through
    /upstream/qwen-tts/v1/voices/... so the member auto-loads and group
    swap/exclusivity are honored (correction [18]). See README (Task 6.8).
"""
import io
import wave
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from . import profile_store, settings
from .variant_manager import VariantManager, VramError


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # torch-free construction — variants load lazily on first synth.
    app.state.manager = VariantManager()
    app.state.design_cache = {}   # generated_voice_id -> {description, params}
    yield


app = FastAPI(title="qwen-tts", version="1.0", lifespan=_lifespan)


def get_manager() -> VariantManager:
    mgr = getattr(app.state, "manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="qwen-tts manager not initialized")
    return mgr


@app.get("/health")
def health():
    """llama-swap checkEndpoint — STARTUP readiness ONLY (§6). Cheap; never
    loads a model (health is a one-time startup gate, never re-probed)."""
    return {"status": "ok"}


# ---- helpers ---------------------------------------------------------------
def _resolve_voice(voice: str):
    """(variant, synth_kwargs, resolved_id). Accepts a bare preset name,
    'qwen:<Preset>', or a saved profile slug. 422 if missing; 404 if unknown."""
    if not voice:
        raise HTTPException(status_code=422, detail="voice is required")
    v = voice.split(":", 1)[1] if voice.startswith("qwen:") else voice
    if v in settings.PRESET_VOICES:
        return settings.VARIANT_CUSTOM_VOICE, {"preset": v}, v
    # Not a preset -> treat as a saved-profile slug. Sanitize BEFORE any
    # filesystem lookup so a crafted value ('../secretdir') can never escape
    # voices_dir (the 'slug sanitization prevents path traversal' gate — §5.4).
    try:
        slug = profile_store.sanitize_slug(v)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"unknown voice {voice!r}")
    prof = profile_store.get_profile(slug)
    if prof is None:
        raise HTTPException(status_code=404, detail=f"unknown voice {voice!r}")
    variant = prof.get("variant")
    if variant == settings.VARIANT_BASE:
        ref = profile_store.ref_audio_path(slug)
        if not ref:
            raise HTTPException(status_code=422, detail=f"voice {slug!r} has no reference audio")
        return settings.VARIANT_BASE, {"ref_audio": ref}, slug
    if variant == settings.VARIANT_VOICE_DESIGN:
        return settings.VARIANT_VOICE_DESIGN, {"design_params": prof.get("design")}, slug
    raise HTTPException(status_code=422, detail=f"voice {slug!r} has an unknown variant")


def _pcm_to_wav(pcm: bytes, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)   # sr FROM MODEL OUTPUT — never hardcoded (correction [23])
        w.writeframes(pcm)
    return buf.getvalue()


def _frame_iter(pcm: bytes, sr: int):
    """Chunk full PCM into 12Hz frames (tokenizer is Qwen3-TTS-Tokenizer-12Hz,
    §5.4) for the StreamingResponse-over-full-generation fallback (correction [8])."""
    samples_per_frame = max(1, sr // 12)
    step = samples_per_frame * 2   # int16
    for i in range(0, len(pcm), step):
        yield pcm[i:i + step]


# ---- request model ---------------------------------------------------------
class SpeechRequest(BaseModel):
    """OpenAI-shaped speech request. `model` is consumed by llama-swap for
    routing (present on the wire, unused here); we synthesize `input` in
    `voice`. Optional fields carry server defaults so the route logic stays
    declarative (no manual `.get(...)` plumbing)."""

    model: Optional[str] = None
    input: Optional[str] = None
    voice: Optional[str] = None
    response_format: Optional[str] = "wav"
    stream: bool = False


# ---- endpoints -------------------------------------------------------------
@app.post("/v1/audio/speech")
async def audio_speech(req: SpeechRequest, mgr: VariantManager = Depends(get_manager)):
    """OpenAI-shaped {model, input, voice, response_format, stream}. `model` is
    consumed by llama-swap for routing; we synthesize `input` in `voice`.
    (The Orchestrator applies sanitize_for_speech BEFORE calling — §5.4 — so
    this server trusts `input`.) sr is read from the model output."""
    text = req.input
    if not text or not str(text).strip():
        raise HTTPException(status_code=422, detail="input is required")
    response_format = req.response_format or "wav"
    if response_format not in ("wav", "pcm"):
        raise HTTPException(status_code=400, detail="response_format must be 'wav' or 'pcm'")
    stream = bool(req.stream)
    variant, kwargs, _id = _resolve_voice(req.voice)

    if stream and settings.streaming_enabled():
        # G3-gated TRUE chunked streaming (OFF by default) — Task 6.5.
        try:
            sr, aiter = await mgr.stream_true(variant, str(text), **kwargs)
        except VramError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return StreamingResponse(
            aiter, media_type="application/octet-stream",
            headers={"X-Sample-Rate": str(sr), "X-Audio-Format": "pcm_s16le"},
        )

    try:
        pcm, sr = await mgr.synthesize_full(variant, str(text), **kwargs)
    except VramError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if stream:
        # Default shipping path: StreamingResponse OVER a full generation (correction [8]).
        return StreamingResponse(
            _frame_iter(pcm, sr), media_type="application/octet-stream",
            headers={"X-Sample-Rate": str(sr), "X-Audio-Format": "pcm_s16le"},
        )
    if response_format == "pcm":
        return Response(
            content=pcm, media_type="application/octet-stream",
            headers={"X-Sample-Rate": str(sr), "X-Audio-Format": "pcm_s16le"},
        )
    return Response(content=_pcm_to_wav(pcm, sr), media_type="audio/wav")
