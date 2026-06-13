"""ElevenLabs capability + status routes. Single hydration point for all frontends."""
from fastapi import Body, HTTPException

from Orchestrator.checkpoint import app


@app.get("/elevenlabs/status")
async def elevenlabs_status():
    """No key -> {"configured": False} and every ElevenLabs UI hides.
    Uses the provider's EXPLICIT capability booleans (provider-API-as-SoT),
    not tier-name inference."""
    from Orchestrator.elevenlabs.client import resolve_api_key
    from Orchestrator.elevenlabs import catalog
    if not resolve_api_key():
        return {"configured": False}
    user = catalog.get_user() or {}
    return {
        "configured": True,
        "tier": user.get("tier", "unknown"),
        "credits_remaining": user.get("credits_remaining"),
        "credits_limit": user.get("credits_limit"),
        "features": {
            "tts": True, "stt": True, "music": True, "sound_effects": True,
            "voice_changer": True, "voice_isolator": True, "voice_design": True,
            "instant_voice_cloning": bool(user.get("can_use_instant_voice_cloning")),
            "professional_voice_cloning": bool(user.get("can_use_professional_voice_cloning")),
        },
    }


@app.get("/elevenlabs/library")
async def elevenlabs_library(
    search: str | None = None,
    page_size: int = 30,
    gender: str | None = None,
    category: str | None = None,
):
    """Proxy GET /v1/shared-voices so the public community library is searchable
    from the Portal WITHOUT exposing the key client-side. No key -> empty result
    (the browse UI is gated on /elevenlabs/status, but this stays graceful)."""
    from Orchestrator.elevenlabs import catalog
    result = catalog.get_shared_voices(
        search=search, page_size=page_size, gender=gender, category=category
    )
    if result is None:
        return {"voices": [], "has_more": False}
    return result


@app.post("/elevenlabs/library/add")
async def elevenlabs_library_add(payload: dict = Body(...)):
    """Copy a shared-library voice into this account. Body requires all three of
    {public_owner_id, voice_id, name}. Busts the voices cache (in the provider)
    so the new voice surfaces in /tts/catalog immediately. Provider RuntimeError
    (mapped ElevenLabs error) -> HTTP 400 with the human message."""
    from Orchestrator.elevenlabs import catalog
    public_owner_id = (payload or {}).get("public_owner_id")
    voice_id = (payload or {}).get("voice_id")
    name = (payload or {}).get("name")
    if not public_owner_id or not voice_id or not name:
        raise HTTPException(
            status_code=400,
            detail="public_owner_id, voice_id and name are all required",
        )
    try:
        result = catalog.add_shared_voice(public_owner_id, voice_id, name)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=400, detail="ElevenLabs not configured")
    return {"ok": True, "voice_id": result.get("voice_id")}
