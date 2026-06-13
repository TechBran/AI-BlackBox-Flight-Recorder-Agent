"""Executor for elevenlabs_clone_voice — Instant Voice Cloning with a consent gate.

Calls ``Orchestrator.elevenlabs.voices.clone_instant`` DIRECTLY (in-process, no
HTTP). The consent gate lives here: without ``confirm_consent=true`` we refuse and
no voice is ever created. Each ``audio_paths`` entry must exist on disk.
"""
import os

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    name = (params.get("name") or "").strip()
    audio_paths = params.get("audio_paths") or []
    confirm_consent = bool(params.get("confirm_consent"))
    description = params.get("description")

    if not name:
        return ToolResult(False, "name is required to clone a voice.")
    if not audio_paths:
        return ToolResult(False, "audio_paths is required (at least one local audio sample).")

    # Consent gate — refuse BEFORE any provider call when not explicitly confirmed.
    if not confirm_consent:
        return ToolResult(
            False,
            "I can't clone a voice without explicit confirmation you have the "
            "right to use it. Please confirm.",
        )

    # Every sample path must exist on disk.
    for path in audio_paths:
        if not path or not os.path.exists(path):
            return ToolResult(False, f"Audio file not found: {path}")

    from Orchestrator.elevenlabs import voices

    try:
        result = voices.clone_instant(name, audio_paths, description=description)
    except RuntimeError as exc:
        return ToolResult(False, str(exc))

    voice_id = result.get("voice_id")
    return ToolResult(
        True,
        f"Cloned '{name}' (elevenlabs:{voice_id}) — it's in your voice selector now.",
        data={"voice_id": voice_id, "requires_verification": result.get("requires_verification")},
    )
