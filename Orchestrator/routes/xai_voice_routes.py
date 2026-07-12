"""xAI Custom Voices routes — the Voice Lab xAI section + Grok cloned voices.

Thin consumers of ``Orchestrator.xai_voices`` (the provider module) — these
routes own ONLY transport concerns (multipart parsing, temp-file lifecycle,
error->HTTP mapping) and the CONSENT GATE on cloning, mirroring
elevenlabs_routes.py exactly. Cloning is refused with HTTP 422 unless the
caller passes ``consent="true"``. xAI enforces the <=120s reference-clip limit
server-side (surfaced as a 400 with the provider message).

GET /xai/voices doubles as the frontends' gating probe: no XAI_API_KEY ->
{"configured": false, "voices": []} and the Portal/Android xAI zones hide.
"""
import os
import tempfile

from fastapi import File, Form, HTTPException, UploadFile

from Orchestrator.checkpoint import app


@app.get("/xai/voices")
async def xai_voices_list():
    """List cloned voices. No key -> graceful unconfigured payload (zone hides)."""
    from Orchestrator import xai_voices
    try:
        voices = xai_voices.list_custom_voices()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if voices is None:
        return {"configured": False, "voices": []}
    return {"configured": True, "voices": voices}


@app.post("/xai/voices")
async def xai_voices_clone(
    name: str = Form(...),
    file: UploadFile = File(...),
    consent: str = Form(...),
    description: str = Form(None),
):
    """Clone a custom voice (multipart, ONE reference clip <=120s — xAI enforces
    the duration). CONSENT GATE: ``consent`` must be the literal string "true"
    or this 422s WITHOUT touching the provider — the UI must collect an explicit
    "I own / have permission to use this voice" confirmation first.

    The upload is streamed to a temp file, handed to ``clone_voice``, then
    removed in a finally (never leak the audio to disk). No key -> 400;
    provider RuntimeError -> 400 with the human message.
    """
    if consent != "true":
        raise HTTPException(status_code=422, detail="Voice cloning requires consent confirmation")

    from Orchestrator import xai_voices
    if not xai_voices.resolve_api_key():
        raise HTTPException(status_code=400, detail="xAI not configured - set XAI_API_KEY (onboarding wizard)")

    suffix = os.path.splitext(file.filename or "")[1] or ".mp3"
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(await file.read())
        try:
            result = xai_voices.clone_voice(name, temp_path, description=description)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    return {
        "voice_id": xai_voices.voice_id_of(result),
        "name": result.get("name", name),
    }


@app.delete("/xai/voices/{voice_id}")
async def xai_voices_delete(voice_id: str):
    """Delete a cloned voice. Provider RuntimeError -> 400."""
    from Orchestrator import xai_voices
    try:
        xai_voices.delete_voice(voice_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}
