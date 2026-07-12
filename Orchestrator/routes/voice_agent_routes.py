"""voice_agent_routes.py — CRUD for voice-agent presets (P4).

APIRouter module (onboarding_routes/credentials_routes precedent) so tests can
mount it on a minimal FastAPI app; registered in app.py via include_router.
Registry semantics live in Orchestrator/voice_agents/registry.py — this layer
adds only HTTP mapping + provider-catalog model validation.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from Orchestrator.voice_agents import registry

logger = logging.getLogger(__name__)
router = APIRouter()

# Provider -> config catalog attr. getattr at REQUEST time (not import) so a
# catalog added later (e.g. GROK_LIVE_MODELS in Phase 2) is picked up without
# touching this module. Missing/empty catalog -> model validation skipped.
_CATALOG_ATTRS = {
    "realtime": "OPENAI_REALTIME_MODELS",
    "gemini-live": "GEMINI_LIVE_MODELS",
    "grok-live": "GROK_LIVE_MODELS",
}


def _validate_model(provider: str, model: Optional[str]) -> None:
    if not model:
        return
    from Orchestrator import config
    catalog = getattr(config, _CATALOG_ATTRS.get(provider, ""), None)
    if not catalog:
        return  # no live catalog for this provider (yet) — accept as-is
    known = {m["id"] for m in catalog if isinstance(m, dict) and m.get("id")}
    if model not in known:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown {provider} model {model!r}. Known models: {sorted(known)}")


class PresetCreate(BaseModel):
    name: str
    provider: str
    model: str = ""
    voice: str = ""
    instructions: str = ""
    tool_group_override: str = ""
    greeting: str = ""
    language: str = ""
    keyterms: List[str] = []
    created_by: str = ""


class PresetPatch(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    voice: Optional[str] = None
    instructions: Optional[str] = None
    tool_group_override: Optional[str] = None
    greeting: Optional[str] = None
    language: Optional[str] = None
    keyterms: Optional[List[str]] = None


@router.get("/voice-agents")
def list_voice_agents(provider: Optional[str] = None) -> dict:
    """All presets (no secrets stored — full records are safe to return)."""
    return {"agents": registry.list_presets(provider=provider)}


@router.post("/voice-agents")
def create_voice_agent(req: PresetCreate) -> dict:
    if req.provider not in registry.PROVIDERS:
        raise HTTPException(status_code=400,
                            detail=f"provider must be one of {registry.PROVIDERS}")
    _validate_model(req.provider, req.model)
    try:
        agent = registry.add_preset(
            name=req.name, provider=req.provider, created_by=req.created_by,
            model=req.model, voice=req.voice, instructions=req.instructions,
            tool_group_override=req.tool_group_override, greeting=req.greeting,
            language=req.language, keyterms=req.keyterms)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("voice-agents add: id=%s name=%s provider=%s",
                agent["id"], agent["name"], agent["provider"])
    return {"agent": agent}


@router.patch("/voice-agents/{preset_id}")
def patch_voice_agent(preset_id: str, req: PresetPatch) -> dict:
    patch = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    if "model" in patch or "provider" in patch:
        current = registry.get_preset(preset_id)
        if current is None:
            raise HTTPException(status_code=404,
                                detail=f"No voice agent preset {preset_id!r}")
        provider = patch.get("provider", current.get("provider"))
        _validate_model(provider, patch.get("model", current.get("model")))
    try:
        agent = registry.update_preset(preset_id, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info("voice-agents patch: id=%s fields=%s", preset_id, sorted(patch))
    return {"agent": agent}


@router.delete("/voice-agents/{preset_id}")
def delete_voice_agent(preset_id: str) -> dict:
    try:
        registry.delete_preset(preset_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info("voice-agents delete: id=%s", preset_id)
    return {"ok": True}
