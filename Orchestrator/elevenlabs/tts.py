"""ElevenLabs TTS synthesis: quality-first speech generation + per-model limits.

Quality-first (Brandon's directive): defaults to the flagship model
``eleven_v3`` at the highest output quality the plan allows (``mp3_44100_192``).
Cheaper/faster tiers are NEVER silent -- if the plan tier rejects the requested
output format, ``synthesize`` retries ONCE at ``mp3_44100_128`` and PRINTS a
one-line downgrade notice so the degradation is visible, not hidden.

PROVIDER-API-AS-SoT: per-model character limits come from the live ``/v1/models``
catalog (``maximum_text_length_per_request``), never from a hardcoded table --
``max_chars_for`` reads ``catalog.get_models()`` and only falls back to a
conservative constant when the model isn't in the live list.

All auth + error mapping flow through ``client`` so they exist exactly once. This
module is provider plumbing only -- it does NOT wire any /tts route or the
text_to_speech tool (Task 18 owns the routing that USES ``synthesize``).
"""
from __future__ import annotations

import requests

from Orchestrator import config
from Orchestrator.elevenlabs import catalog, client

# The guaranteed-available fallback format every paid tier supports; also the
# sentinel we retry at exactly once and never re-downgrade below.
_FALLBACK_FORMAT = "mp3_44100_128"

# HTTP statuses that can indicate a plan-tier rejection of the output format.
_FORMAT_GATE_STATUSES = (400, 401, 403, 422)

# Conservative per-request char cap when the live catalog can't tell us better.
_DEFAULT_MAX_CHARS = 5000


def _parse_body(resp: requests.Response) -> dict | None:
    """Defensively parse an error body that may not be JSON (map_error tolerates None)."""
    try:
        return resp.json()
    except Exception:
        return None


def _post(text: str, voice_id: str, model_id: str, output_format: str,
          voice_settings: dict | None) -> requests.Response:
    """One TTS POST. ``voice_id`` is already the RAW id (no ``elevenlabs:`` prefix)."""
    body: dict = {"text": text, "model_id": model_id}
    if voice_settings is not None:
        body["voice_settings"] = voice_settings
    return requests.post(
        f"{client.BASE_URL}/v1/text-to-speech/{voice_id}",
        headers=client.auth_headers(),
        params={"output_format": output_format},
        json=body,
        timeout=120,  # ElevenLabs TTS can be slow to generate; 60s cut off auto-TTS (Brandon 2026-06-20)
    )


def _post_stream(text: str, voice_id: str, model_id: str, output_format: str,
                 voice_settings: dict | None) -> requests.Response:
    """One STREAMING TTS POST to the /stream endpoint. The (connect, read) timeout
    makes `read` a per-chunk IDLE timeout: it fires only after that many seconds
    with NO bytes — not a total cap — so long-but-progressing generations succeed."""
    body: dict = {"text": text, "model_id": model_id}
    if voice_settings is not None:
        body["voice_settings"] = voice_settings
    return requests.post(
        f"{client.BASE_URL}/v1/text-to-speech/{voice_id}/stream",
        headers=client.auth_headers(),
        params={"output_format": output_format},
        json=body,
        stream=True,
        timeout=(10, config.ELEVENLABS_TTS_STREAM_IDLE_S),
    )


def synthesize_stream(
    text: str,
    voice_id: str,
    *,
    model_id: str | None = None,
    output_format: str | None = None,
    voice_settings: dict | None = None,
):
    """Yield audio chunks from ElevenLabs' streaming TTS endpoint.

    Same quality-first defaults + one-time plan-tier format downgrade as
    ``synthesize``, but streamed so each chunk is proof of progress and the only
    failure is a true stall (idle timeout in ``_post_stream``)."""
    model_id = model_id or config.ELEVENLABS_TTS_MODEL_DEFAULT
    output_format = output_format or config.ELEVENLABS_TTS_FORMAT_DEFAULT
    raw_voice_id = voice_id.split("elevenlabs:")[-1]

    resp = _post_stream(text, raw_voice_id, model_id, output_format, voice_settings)
    if not (200 <= resp.status_code < 300):
        if resp.status_code in _FORMAT_GATE_STATUSES and output_format != _FALLBACK_FORMAT:
            print(f"[ELEVENLABS] TTS(stream) output format downgraded to {_FALLBACK_FORMAT} (plan tier)")
            try: resp.close()
            except Exception: pass
            resp = _post_stream(text, raw_voice_id, model_id, _FALLBACK_FORMAT, voice_settings)
        if not (200 <= resp.status_code < 300):
            err = client.map_error(resp.status_code, _parse_body(resp))
            try: resp.close()
            except Exception: pass
            raise RuntimeError(err)
    try:
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                yield chunk
    finally:
        try: resp.close()
        except Exception: pass


def synthesize(
    text: str,
    voice_id: str,
    *,
    model_id: str | None = None,
    output_format: str | None = None,
    voice_settings: dict | None = None,
) -> bytes:
    """Synthesize speech and return the raw audio ``bytes``.

    Quality-first: defaults to ``eleven_v3`` @ ``mp3_44100_192`` (both
    env-overridable via config). ``voice_id`` may carry the ``elevenlabs:``
    prefix; it is stripped here so the raw id reaches the API path.

    On a 4xx that rejects the requested ``output_format`` (a plan-tier gate),
    retries ONCE at ``mp3_44100_128`` and PRINTS a visible downgrade notice. Any
    other non-2xx (or a still-failing retry) raises
    ``RuntimeError(client.map_error(...))`` with the error body defensively
    parsed (it may not be JSON).
    """
    model_id = model_id or config.ELEVENLABS_TTS_MODEL_DEFAULT
    output_format = output_format or config.ELEVENLABS_TTS_FORMAT_DEFAULT
    # The API path wants the RAW id; tolerate ids that already lack the prefix.
    raw_voice_id = voice_id.split("elevenlabs:")[-1]

    resp = _post(text, raw_voice_id, model_id, output_format, voice_settings)
    if 200 <= resp.status_code < 300:
        return resp.content

    # Tier-gate downgrade: retry ONCE at the guaranteed fallback format, but only
    # if we weren't already there (no infinite/redundant retry).
    if resp.status_code in _FORMAT_GATE_STATUSES and output_format != _FALLBACK_FORMAT:
        print(f"[ELEVENLABS] TTS output format downgraded to {_FALLBACK_FORMAT} (plan tier)")
        resp = _post(text, raw_voice_id, model_id, _FALLBACK_FORMAT, voice_settings)
        if 200 <= resp.status_code < 300:
            return resp.content

    raise RuntimeError(client.map_error(resp.status_code, _parse_body(resp)))


def max_chars_for(model_id: str) -> int:
    """Per-model max request length from the live ``/v1/models`` catalog.

    Reads ``maximum_text_length_per_request`` for ``model_id``. Falls back to a
    conservative ``5000`` when the model isn't found or the catalog is
    unavailable (no key / network down -> ``get_models`` returns None or raises).
    """
    try:
        models = catalog.get_models() or []
    except Exception:
        return _DEFAULT_MAX_CHARS
    for m in models:
        if m.get("model_id") == model_id:
            limit = m.get("maximum_text_length_per_request")
            if isinstance(limit, int) and limit > 0:
                return limit
            break
    return _DEFAULT_MAX_CHARS
