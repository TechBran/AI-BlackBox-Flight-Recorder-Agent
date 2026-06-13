"""Executor for elevenlabs_delete_voice — delete an account voice (confirm-gated).

Calls ``Orchestrator.elevenlabs.voices`` DIRECTLY (in-process, no HTTP). Refuses
unless ``confirm=true``. Checks ``voice_in_use`` (advisory, fail-open) BEFORE
deleting so the success message can warn that the voice was an operator's saved
TTS preference.
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    voice_id = (params.get("voice_id") or "").strip()
    confirm = bool(params.get("confirm"))

    if not voice_id:
        return ToolResult(False, "voice_id is required.")
    if not confirm:
        return ToolResult(False, "Set confirm=true to delete.")

    from Orchestrator.elevenlabs import voices

    in_use = voices.voice_in_use(voice_id)
    try:
        voices.delete_voice(voice_id)
    except RuntimeError as exc:
        return ToolResult(False, str(exc))

    raw_id = voice_id.split("elevenlabs:")[-1]
    message = f"Deleted elevenlabs:{raw_id}."
    if in_use:
        message += f" WARNING: was in use by {in_use}."
    return ToolResult(True, message, data={"ok": True, "in_use": in_use})
