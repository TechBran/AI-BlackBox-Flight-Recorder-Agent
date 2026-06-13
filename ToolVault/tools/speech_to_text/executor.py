"""Executor for speech_to_text (migrated from blackbox_tools._execute_speech_to_text).

provider='elevenlabs' (with diarize=true) yields a rich, speaker-attributed payload
from the /stt endpoint (segments/speakers/diarized_text). When mint=true a successful
transcript is persisted as a searchable BlackBox snapshot via /chat/save (auto-mint —
never call /mint afterward; that would duplicate). The plain Whisper path
(no provider / no diarize / no mint) is byte-for-byte unchanged for existing callers.
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult

_CONTENT_TYPES = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".flac": "audio/flac", ".webm": "audio/webm",
}


async def _post_stt(base_url, audio_file, content_type, provider, diarize):
    """POST the audio file to /stt; return the parsed JSON dict.

    Factored out as a module-level helper so tests can monkeypatch the HTTP hop
    without mocking aiohttp internals. Behavior matches the original inline POST.
    """
    import aiohttp
    # Read into memory so the file handle closes immediately (passing an open
    # handle to FormData leaks it until GC).
    with open(audio_file, 'rb') as fh:
        file_bytes = fh.read()
    data = aiohttp.FormData()
    data.add_field('file', file_bytes,
                   filename=audio_file.name, content_type=content_type)
    if provider:
        data.add_field('provider', provider)
    if diarize is not None:
        data.add_field('diarize', 'true' if diarize else 'false')

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base_url}/stt",
            data=data,
            timeout=aiohttp.ClientTimeout(total=120)
        ) as resp:
            return await resp.json()


async def _post_chat_save(base_url, payload):
    """POST a transcript turn to /chat/save (direct persistence + auto-mint).

    Returns the parsed JSON dict (carries snap_id). Module-level for testability.
    """
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base_url}/chat/save",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120)
        ) as resp:
            return await resp.json()


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Transcribe audio to text via the /stt endpoint.

    provider='elevenlabs' + diarize=true returns a speaker-attributed transcript;
    mint=true saves the transcript as a searchable snapshot.
    """
    audio_path = params.get("audio_path", "")

    if not audio_path:
        return ToolResult(False, "audio_path is required")

    try:
        from pathlib import Path
        audio_file = Path(audio_path)
        if not audio_file.exists():
            return ToolResult(False, f"Audio file not found: {audio_path}")

        provider = params.get("provider")
        diarize = params.get("diarize")
        content_type = _CONTENT_TYPES.get(audio_file.suffix.lower(), "audio/wav")

        result = await _post_stt(
            ctx.base_url, audio_file, content_type, provider, diarize,
        )

        # Diarized payload (ElevenLabs): build a speaker-attributed message and
        # attach the FULL rich dict so callers get segments/speakers programmatically.
        if result.get("segments"):
            transcript = result.get("diarized_text") or result.get("text") or ""
            speaker_count = len(result.get("speakers", []))
            message = f"Transcription ({speaker_count} speakers):\n{transcript}"

            # mint: persist the diarized transcript as a searchable snapshot.
            if params.get("mint"):
                message += await _maybe_mint(ctx, audio_file, transcript)

            return ToolResult(True, message, data=result)

        # Flat path (Whisper/Google, or non-diarized) — unchanged for existing callers.
        text = result.get("text", "")
        message = f"Transcription: {text}"
        if params.get("mint"):
            message += await _maybe_mint(ctx, audio_file, text)
        return ToolResult(True, message, data={"text": text})
    except Exception as e:
        return ToolResult(False, f"Speech to text error: {str(e)}")


async def _maybe_mint(ctx: ToolContext, audio_file, transcript: str) -> str:
    """Save a successful transcript as a snapshot; return a message suffix.

    On success returns ' (saved as <snap_id>)'. A save failure is NON-FATAL —
    the transcription already succeeded — so we return a note suffix instead of
    raising. NEVER call /mint here: /chat/save auto-mints (duplicate otherwise).
    """
    payload = {
        "operator": ctx.operator,
        "user_message": f"Transcribed audio: {audio_file.name}",
        "assistant_response": transcript or "",
        "model": "elevenlabs-scribe-v2",
        "tokens": {"prompt": 0, "completion": 0},
    }
    try:
        save_result = await _post_chat_save(ctx.base_url, payload)
        snap_id = save_result.get("snap_id")
        if snap_id:
            return f" (saved as {snap_id})"
        # 200 but no snap_id (auto-mint debounced/disabled) — still non-fatal.
        return " (note: snapshot save returned no snap_id)"
    except Exception as e:
        return f" (note: snapshot save failed: {str(e)})"
