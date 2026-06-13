"""ElevenLabs capability + status routes. Single hydration point for all frontends."""
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
