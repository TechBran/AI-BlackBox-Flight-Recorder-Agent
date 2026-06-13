"""ElevenLabs capability + status routes. Single hydration point for all frontends.

Also hosts the Voice Lab routes (Task 22) the Portal/Android UI calls: instant
voice cloning, voice design (preview + save), grouped voice listing, and delete.
These are thin consumers of ``Orchestrator.elevenlabs.voices`` (the DONE provider
module) -- the routes own ONLY transport concerns (multipart parsing, temp-file
lifecycle, error->HTTP mapping) and the CONSENT GATE on cloning. Cloning is
refused with HTTP 422 unless the caller passes ``consent="true"``.
"""
import os
import tempfile

from fastapi import Body, File, Form, HTTPException, UploadFile

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


# =============================================================================
# Voice Lab (Task 22) — clone / design / list / delete
# =============================================================================


@app.post("/elevenlabs/voices/clone")
async def elevenlabs_voices_clone(
    name: str = Form(...),
    files: list[UploadFile] = File(...),
    consent: str = Form(...),
    description: str = Form(None),
    remove_background_noise: str = Form("true"),
):
    """Instant Voice Cloning (multipart). CONSENT GATE: ``consent`` must be the
    literal string ``"true"`` or this 422s WITHOUT touching the provider -- the UI
    must collect an explicit "I own / have permission to use this voice"
    confirmation first.

    Each uploaded audio file is streamed to a temp file, handed to
    ``voices.clone_instant``, then the temp files are removed in a finally (so we
    never leak the uploaded audio to disk). No key -> 400; provider RuntimeError
    (mapped ElevenLabs error) -> 400 with the human message.
    """
    if consent != "true":
        raise HTTPException(status_code=422, detail="Voice cloning requires consent confirmation")

    from Orchestrator.elevenlabs import voices
    from Orchestrator.elevenlabs.client import resolve_api_key
    if not resolve_api_key():
        raise HTTPException(status_code=400, detail="ElevenLabs not configured")

    temp_paths: list[str] = []
    try:
        for upload in files:
            suffix = os.path.splitext(upload.filename or "")[1] or ".mp3"
            fd, temp_path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as out:
                out.write(await upload.read())
            temp_paths.append(temp_path)

        try:
            result = voices.clone_instant(
                name,
                temp_paths,
                description=description,
                remove_background_noise=(remove_background_noise == "true"),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        for path in temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass

    return {
        "voice_id": result.get("voice_id"),
        "requires_verification": result.get("requires_verification"),
    }


@app.post("/elevenlabs/voices/design")
async def elevenlabs_voices_design(payload: dict = Body(...)):
    """Voice Design step 1: generate preview voices from a text description.

    Body ``{voice_description, text?, model_id?}`` -> ``{text, previews}`` (each
    preview has a ``generated_voice_id`` + a servable ``audio_url``). No account
    voice is created yet -- the chosen preview is persisted via .../design/save.
    Provider RuntimeError -> 400.
    """
    from Orchestrator.elevenlabs import voices
    voice_description = (payload or {}).get("voice_description")
    if not voice_description:
        raise HTTPException(status_code=400, detail="voice_description is required")
    text = (payload or {}).get("text")
    model_id = (payload or {}).get("model_id")
    kwargs: dict = {"text": text}
    if model_id:
        kwargs["model_id"] = model_id
    try:
        result = voices.design_previews(voice_description, **kwargs)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"text": result.get("text"), "previews": result.get("previews")}


@app.post("/elevenlabs/voices/design/save")
async def elevenlabs_voices_design_save(payload: dict = Body(...)):
    """Voice Design step 2: persist a chosen preview as a real account voice.

    Body requires ``generated_voice_id`` + ``name`` (``description`` optional) ->
    ``{voice_id}``. Missing either required field -> 400. Provider RuntimeError ->
    400.
    """
    generated_voice_id = (payload or {}).get("generated_voice_id")
    name = (payload or {}).get("name")
    if not generated_voice_id or not name:
        raise HTTPException(
            status_code=400, detail="generated_voice_id and name are required"
        )
    from Orchestrator.elevenlabs import voices
    description = (payload or {}).get("description") or ""
    try:
        result = voices.design_save(generated_voice_id, name, description)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"voice_id": result.get("voice_id")}


@app.get("/elevenlabs/voices")
async def elevenlabs_voices_list():
    """Grouped account voices ``{"my_voices": [...], "premade": [...]}`` for the
    Voice Lab UI. No key -> a graceful empty grouping (matches the browse UI's
    no-key contract)."""
    from Orchestrator.elevenlabs import catalog
    result = catalog.get_voices()
    if result is None:
        return {"my_voices": [], "premade": []}
    return result


@app.delete("/elevenlabs/voices/{voice_id}")
async def elevenlabs_voices_delete(voice_id: str):
    """Delete an account voice. Returns ``{"ok": True, "in_use": [...]}`` so the UI
    can warn that the just-deleted voice was an operator's saved TTS preference.
    The in-use check is best-effort (fail-open) and advisory only -- it never
    blocks the delete. Provider RuntimeError -> 400."""
    from Orchestrator.elevenlabs import voices
    in_use = voices.voice_in_use(voice_id)
    try:
        voices.delete_voice(voice_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "in_use": in_use}
