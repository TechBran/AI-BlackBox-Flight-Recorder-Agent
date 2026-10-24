"""Executor for elevenlabs_clone_voice — Instant Voice Cloning with a consent gate.

Calls ``Orchestrator.elevenlabs.voices.clone_instant`` DIRECTLY (in-process, no
HTTP). The consent gate lives here: without ``confirm_consent=true`` we refuse and
no voice is ever created. Each ``audio_paths`` entry must be an audio file inside
Portal/uploads (a /ui/uploads URL or a session-upload path): the samples are
uploaded to ElevenLabs, so a model-supplied path may not name anything else.
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.utils.paths import AUDIO_EXTS, UploadNotFoundError, UploadPathError, resolve_upload_path


def execute(params: dict, ctx: ToolContext) -> ToolResult:
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

    # Every sample must be a contained audio upload.
    if isinstance(audio_paths, str):
        audio_paths = [audio_paths]
    samples = []
    for path in audio_paths:
        try:
            samples.append(str(resolve_upload_path(path, allowed_exts=AUDIO_EXTS)))
        except UploadNotFoundError:
            return ToolResult(False, f"Audio file not found: {path}")
        except UploadPathError as exc:
            return ToolResult(False, f"Audio sample refused (must be an audio upload): {exc}")

    from Orchestrator.elevenlabs import voices

    try:
        result = voices.clone_instant(name, samples, description=description)
    except RuntimeError as exc:
        return ToolResult(False, str(exc))

    voice_id = result.get("voice_id")
    return ToolResult(
        True,
        f"Cloned '{name}' (elevenlabs:{voice_id}) — it's in your voice selector now.",
        data={"voice_id": voice_id, "requires_verification": result.get("requires_verification")},
    )
