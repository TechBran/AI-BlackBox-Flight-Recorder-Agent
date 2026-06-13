"""ElevenLabs Sound Effects: short SFX generation via POST /v1/sound-generation.

A SYNCHRONOUS binary-mp3 endpoint (NOT the docs' deprecated
``/v1/text-to-sound-effects`` path — the live route is ``/v1/sound-generation``,
verified). Given a text description ("rain on a tin roof", "sci-fi door whoosh")
it returns a short mp3 (a 3s clip ≈ 49KB). ``duration_seconds`` is optional
(0.1-30; the model picks a fitting length when omitted) and ``loop=True`` yields
a seamlessly loopable clip for ambience (rain, engine hum).

Generation blocks for only seconds, so callers invoke this DIRECTLY (no task
queue). All auth + error mapping flow through ``client`` so they exist exactly
once. This module is provider plumbing only — it does NOT wire the route or tool.
"""
from __future__ import annotations

import requests

from Orchestrator.elevenlabs import client


def _parse_body(resp: requests.Response) -> dict | None:
    """Defensively parse an error body that may not be JSON (map_error tolerates None)."""
    try:
        return resp.json()
    except Exception:
        return None


def generate(
    text: str,
    *,
    duration_seconds: float | None = None,
    prompt_influence: float | None = None,
    loop: bool = False,
    output_format: str | None = None,
) -> bytes:
    """POST /v1/sound-generation and return the raw mp3 ``bytes``.

    ``duration_seconds`` is optional (0.1-30); the model auto-picks a length when
    omitted. ``prompt_influence`` (0-1) trades prompt-adherence vs. creativity.
    ``loop=True`` produces a seamlessly loopable clip. ``output_format`` is sent
    as a query param only when provided (the API has a sane default).

    The body carries only the fields actually provided. Any non-2xx raises
    ``RuntimeError(client.map_error(...))`` with the error body defensively parsed
    (it may not be JSON).
    """
    body: dict = {"text": text}
    if duration_seconds is not None:
        body["duration_seconds"] = duration_seconds
    if prompt_influence is not None:
        body["prompt_influence"] = prompt_influence
    if loop:
        body["loop"] = loop

    params: dict = {}
    if output_format:
        params["output_format"] = output_format

    resp = requests.post(
        f"{client.BASE_URL}/v1/sound-generation",
        headers=client.auth_headers(),
        params=params,
        json=body,
        timeout=120,
    )
    if 200 <= resp.status_code < 300:
        return resp.content

    raise RuntimeError(client.map_error(resp.status_code, _parse_body(resp)))
