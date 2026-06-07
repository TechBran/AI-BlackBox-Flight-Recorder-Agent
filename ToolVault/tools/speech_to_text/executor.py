"""Executor for speech_to_text (migrated from blackbox_tools._execute_speech_to_text)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Transcribe audio to text using OpenAI Whisper via /stt endpoint."""
    audio_path = params.get("audio_path", "")

    if not audio_path:
        return ToolResult(False, "audio_path is required")

    try:
        from pathlib import Path
        audio_file = Path(audio_path)
        if not audio_file.exists():
            return ToolResult(False, f"Audio file not found: {audio_path}")

        ext = audio_file.suffix.lower()
        content_types = {
            ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
            ".ogg": "audio/ogg", ".flac": "audio/flac", ".webm": "audio/webm"
        }
        content_type = content_types.get(ext, "audio/wav")

        import aiohttp
        data = aiohttp.FormData()
        data.add_field('file', open(audio_file, 'rb'),
                      filename=audio_file.name, content_type=content_type)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/stt",
                data=data,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                result = await resp.json()
                text = result.get("text", "")
                return ToolResult(True, f"Transcription: {text}", data={"text": text})
    except Exception as e:
        return ToolResult(False, f"Speech to text error: {str(e)}")
