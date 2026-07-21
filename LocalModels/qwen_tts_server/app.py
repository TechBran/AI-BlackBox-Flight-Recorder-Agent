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
import base64
import io
import wave
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse

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
