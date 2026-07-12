"""Executor for xai_clone_voice — xAI Custom Voices cloning with a consent gate.

Calls ``Orchestrator.xai_voices.clone_voice`` DIRECTLY (in-process, no HTTP).
The consent gate lives here: without ``confirm_consent=true`` we refuse and no
voice is ever created. ``audio_path`` must exist on disk (ONE clip, <=120s —
xAI enforces the duration server-side).
"""
import os

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    name = (params.get("name") or "").strip()
    audio_path = (params.get("audio_path") or "").strip()
    confirm_consent = bool(params.get("confirm_consent"))
    description = params.get("description")

    if not name:
        return ToolResult(False, "name is required to clone a voice.")
    if not audio_path:
        return ToolResult(False, "audio_path is required (one local audio sample, max 120 seconds).")

    # Consent gate — refuse BEFORE any provider call when not explicitly confirmed.
    if not confirm_consent:
        return ToolResult(
            False,
            "I can't clone a voice without explicit confirmation you have the "
            "right to use it. Please confirm.",
        )

    if not os.path.exists(audio_path):
        return ToolResult(False, f"Audio file not found: {audio_path}")

    import asyncio

    from Orchestrator import xai_voices

    try:
        result = await asyncio.to_thread(
            xai_voices.clone_voice, name, audio_path, description=description
        )
    except RuntimeError as exc:
        return ToolResult(False, str(exc))

    voice_id = xai_voices.voice_id_of(result)
    return ToolResult(
        True,
        f"Cloned '{name}' (xAI voice_id {voice_id}) — selectable as a Grok session voice now.",
        data={"voice_id": voice_id},
    )
