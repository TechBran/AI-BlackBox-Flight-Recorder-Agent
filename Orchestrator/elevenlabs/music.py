"""ElevenLabs Music: full-song generation via POST /v1/music.

A SEPARATE music provider alongside Lyria (Brandon's directive: provider-explicit
naming — both coexist). Unlike Lyria there is NO restricted vocabulary: any
genre/style/mood word is fair game, the output is commercially cleared, and songs
run up to 5 minutes WITH vocals + lyrics.

The endpoint is synchronous binary mp3: ``prompt`` and ``composition_plan`` are
mutually exclusive (exactly one required). Generation blocks for seconds (short)
up to ~a minute (5-min songs), so callers should drive this through the BlackBox
TASK pattern (background generation + get_task_status) like Lyria does.

All auth + error mapping flow through ``client`` so they exist exactly once. This
module is provider plumbing only — it does NOT wire the /generate/elevenlabs_music
route or the elevenlabs_music tool.
"""
from __future__ import annotations

import requests

from Orchestrator import config
from Orchestrator.elevenlabs import client


def _parse_body(resp: requests.Response) -> dict | None:
    """Defensively parse an error body that may not be JSON (map_error tolerates None)."""
    try:
        return resp.json()
    except Exception:
        return None


def compose(
    *,
    prompt: str | None = None,
    composition_plan: dict | None = None,
    music_length_ms: int | None = None,
    force_instrumental: bool = False,
    seed: int | None = None,
    output_format: str | None = None,
) -> bytes:
    """POST /v1/music and return the raw audio ``bytes``.

    Exactly ONE of ``prompt`` / ``composition_plan`` is required — passing both
    or neither raises ``ValueError`` (the API treats them as mutually exclusive).

    ``output_format`` defaults to ``config.ELEVENLABS_MUSIC_FORMAT_DEFAULT``
    (mp3_44100_128). The body carries only the fields actually provided.

    Any non-2xx raises ``RuntimeError(client.map_error(...))`` with the error body
    defensively parsed (it may not be JSON).
    """
    if (prompt is None) == (composition_plan is None):
        raise ValueError(
            "ElevenLabs Music requires exactly one of `prompt` or `composition_plan`"
        )

    output_format = output_format or config.ELEVENLABS_MUSIC_FORMAT_DEFAULT

    body: dict = {}
    if prompt is not None:
        body["prompt"] = prompt
    if composition_plan is not None:
        body["composition_plan"] = composition_plan
    if music_length_ms is not None:
        body["music_length_ms"] = music_length_ms
    if force_instrumental:
        body["force_instrumental"] = force_instrumental
    if seed is not None:
        body["seed"] = seed

    # Long songs take a while (5-min track ≈ ~a minute to render); generous timeout.
    resp = requests.post(
        f"{client.BASE_URL}/v1/music",
        headers=client.auth_headers(),
        params={"output_format": output_format},
        json=body,
        timeout=180,
    )
    if 200 <= resp.status_code < 300:
        return resp.content

    raise RuntimeError(client.map_error(resp.status_code, _parse_body(resp)))
