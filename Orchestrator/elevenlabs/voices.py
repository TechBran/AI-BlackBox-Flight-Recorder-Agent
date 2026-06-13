"""ElevenLabs Voice Lab provider: instant voice cloning, voice design, delete, in-use.

This is provider plumbing only -- it owns the HTTP shapes for the four voice
mutation/creation surfaces the account's Starter tier permits, plus a best-effort
"who is using this voice" check. It does NOT wire any route or agent tool
(Tasks 22-26 own everything that USES these functions).

Capability scope: the live account has ``can_use_instant_voice_cloning=true`` and
``can_use_professional_voice_cloning=false`` -- so IVC (``clone_instant``) and
text-to-voice design (``design_previews``/``design_save``) are in scope; PVC is NOT.

All auth + error mapping flow through ``client`` so they exist exactly once. Every
mutator (clone/design-save/delete) busts the voices cache so a new/removed voice is
reflected in ``/tts/catalog`` on the very next fetch -- mirroring
``catalog.add_shared_voice``.
"""
from __future__ import annotations

import json
import uuid

import requests

from Orchestrator.elevenlabs import catalog, client

# Default text-to-voice DESIGN model (the design endpoint's own family --
# distinct from the TTS model in config). Probed live: design 400s unless you
# pass auto_generate_text=true OR a 100-1000 char text.
_DESIGN_MODEL_DEFAULT = "eleven_ttv_v3"

# Generous timeouts: an IVC upload streams audio files; design renders 3 previews.
_CLONE_TIMEOUT = 120
_DESIGN_TIMEOUT = 90
_MUTATE_TIMEOUT = 15


def _strip_prefix(voice_id: str) -> str:
    """Return the RAW voice id, tolerating an optional ``elevenlabs:`` prefix."""
    return voice_id.split("elevenlabs:")[-1]


def _parse_body(resp: requests.Response) -> dict | None:
    """Defensively parse an error body that may not be JSON (map_error tolerates None)."""
    try:
        return resp.json()
    except Exception:
        return None


def _raise_for_status(resp: requests.Response) -> None:
    """Raise ``RuntimeError(map_error(...))`` on any non-2xx; no-op otherwise."""
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(client.map_error(resp.status_code, _parse_body(resp)))


def clone_instant(
    name: str,
    file_paths: list[str],
    *,
    description: str | None = None,
    labels: dict | str | None = None,
    remove_background_noise: bool = True,
) -> dict:
    """Instant Voice Cloning: ``POST /v1/voices/add`` (multipart).

    Uploads one or more audio ``file_paths`` under the voice ``name``. Optional
    ``description`` and ``labels`` (a dict is JSON-encoded for the multipart
    field; a pre-encoded string is sent verbatim). ``remove_background_noise`` is
    sent as the API's ``"true"``/``"false"`` string.

    Returns ``{"voice_id", "requires_verification"}`` and busts the voices cache
    so the new voice appears in ``/tts/catalog`` immediately. Raises
    ``RuntimeError(client.map_error(...))`` on non-2xx.
    """
    data: dict = {
        "name": name,
        "remove_background_noise": "true" if remove_background_noise else "false",
    }
    if description:
        data["description"] = description
    if labels is not None:
        data["labels"] = labels if isinstance(labels, str) else json.dumps(labels)

    # Each audio file becomes its own ``files`` part. We hold the handles open for
    # the duration of the POST, then close them in finally.
    opened: list = []
    try:
        files = []
        for path in file_paths:
            fh = open(path, "rb")
            opened.append(fh)
            files.append(("files", (str(path).rsplit("/", 1)[-1], fh, "audio/mpeg")))

        resp = requests.post(
            f"{client.BASE_URL}/v1/voices/add",
            headers=client.auth_headers(),
            data=data,
            files=files,
            timeout=_CLONE_TIMEOUT,
        )
    finally:
        for fh in opened:
            try:
                fh.close()
            except Exception:
                pass

    _raise_for_status(resp)
    catalog.bust_voices_cache()
    body = resp.json()
    return {
        "voice_id": body.get("voice_id"),
        "requires_verification": bool(body.get("requires_verification")),
    }


def design_previews(
    voice_description: str,
    *,
    text: str | None = None,
    model_id: str = _DESIGN_MODEL_DEFAULT,
) -> dict:
    """Voice Design: ``POST /v1/text-to-voice/design`` -- generate preview voices.

    When ``text`` is None, sends ``auto_generate_text=true`` (the API picks sample
    text); otherwise the provided ``text`` is sent (must be 100-1000 chars per the
    API). Each returned preview's base64 audio is decoded to a unique ``.mp3`` in
    the shared uploads dir.

    Returns ``{"text": <sample text>, "previews": [{"generated_voice_id",
    "audio_path", "audio_url", "duration_secs", "language"}]}``. This is NOT a
    mutator (no account voice is created yet) so it does NOT bust the cache.
    Raises ``RuntimeError(client.map_error(...))`` on non-2xx.
    """
    body: dict = {"voice_description": voice_description, "model_id": model_id}
    if text is None:
        body["auto_generate_text"] = True
    else:
        body["text"] = text

    resp = requests.post(
        f"{client.BASE_URL}/v1/text-to-voice/design",
        headers=client.auth_headers(),
        json=body,
        timeout=_DESIGN_TIMEOUT,
    )
    _raise_for_status(resp)
    data = resp.json()

    previews_out: list[dict] = []
    for p in data.get("previews") or []:
        audio_path, audio_url = _decode_preview_audio(p.get("audio_base_64") or "")
        previews_out.append({
            "generated_voice_id": p.get("generated_voice_id"),
            "audio_path": audio_path,
            "audio_url": audio_url,
            "duration_secs": p.get("duration_secs"),
            "language": p.get("language"),
        })

    return {"text": data.get("text"), "previews": previews_out}


def _decode_preview_audio(audio_base_64: str) -> tuple[str, str]:
    """Decode a preview's base64 mp3 to a unique file in UPLOADS_DIR.

    Returns ``(absolute_path, servable_url)`` -- mirroring how the image/lyria
    generators save to ``UPLOADS_DIR`` and return ``/ui/uploads/<file>``. Imported
    lazily so this module loads with no key/config side effects at import time.
    """
    import base64

    from Orchestrator.config import UPLOADS_DIR

    filename = f"{uuid.uuid4()}_voice_design.mp3"
    save_path = UPLOADS_DIR / filename
    with open(save_path, "wb") as f:
        f.write(base64.b64decode(audio_base_64))
    return str(save_path), f"/ui/uploads/{filename}"


def design_save(generated_voice_id: str, name: str, description: str) -> dict:
    """Persist a designed preview as a real account voice.

    ``POST /v1/text-to-voice`` with ``{generated_voice_id, voice_name,
    voice_description}``. Busts the voices cache. Returns ``{"voice_id"}``. Raises
    ``RuntimeError(client.map_error(...))`` on non-2xx.
    """
    resp = requests.post(
        f"{client.BASE_URL}/v1/text-to-voice",
        headers=client.auth_headers(),
        json={
            "generated_voice_id": generated_voice_id,
            "voice_name": name,
            "voice_description": description,
        },
        timeout=_MUTATE_TIMEOUT,
    )
    _raise_for_status(resp)
    catalog.bust_voices_cache()
    return {"voice_id": resp.json().get("voice_id")}


def delete_voice(voice_id: str) -> dict:
    """``DELETE /v1/voices/{voice_id}`` -- remove a voice from the account.

    Strips any ``elevenlabs:`` prefix before hitting the API. Busts the voices
    cache so the removal is reflected in ``/tts/catalog`` immediately. Returns
    ``{"ok": True}``. Raises ``RuntimeError(client.map_error(...))`` on non-2xx.
    """
    raw_id = _strip_prefix(voice_id)
    resp = requests.delete(
        f"{client.BASE_URL}/v1/voices/{raw_id}",
        headers=client.auth_headers(),
        timeout=_MUTATE_TIMEOUT,
    )
    _raise_for_status(resp)
    catalog.bust_voices_cache()
    return {"ok": True}


def voice_in_use(voice_id: str) -> list:
    """Best-effort: which operators have this ElevenLabs voice as their TTS pref.

    Scans the in-memory operator-preferences store (``Orchestrator.state``) for a
    ``tts_voice`` equal to ``elevenlabs:{voice_id}`` (the exact value the Portal
    saves -- see ``setOperatorVoice`` in tts-stt.js). Returns the list of operator
    names using it.

    FAIL-OPEN: any failure to read the store (import error, unexpected shape)
    returns ``[]`` -- this is an advisory "are you sure?" check, never a gate, so
    it must never raise. ``voice_id`` is normalized to the prefixed form before
    comparison, tolerating callers that pass it either way.
    """
    raw_id = _strip_prefix(voice_id)
    target = f"elevenlabs:{raw_id}"
    try:
        from Orchestrator.state import OPERATOR_PREFERENCES

        users: list[str] = []
        for operator, prefs in (OPERATOR_PREFERENCES or {}).items():
            if isinstance(prefs, dict) and prefs.get("tts_voice") == target:
                users.append(operator)
        return users
    except Exception:
        return []
