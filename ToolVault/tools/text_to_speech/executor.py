"""Executor for text_to_speech (migrated from blackbox_tools._execute_text_to_speech)."""
from Orchestrator import config
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Convert text to speech using OpenAI TTS via /tts endpoint."""
    text = params.get("text", "")
    voice = params.get("voice", "onyx")
    model = params.get("model", "tts-1-hd")

    if not text:
        return ToolResult(False, "text is required")

    try:
        import aiohttp
        payload = {
            "text": text,
            "voice": voice,
            "model": model,
            "return_json": True
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/tts",
                json=payload,
                # Generous backstop only: the server self-bounds long ElevenLabs
                # generations via a per-chunk stream idle timeout, so this no longer
                # guillotines a still-progressing synth at 60s.
                timeout=aiohttp.ClientTimeout(total=config.TTS_TOOL_BACKSTOP_S, sock_connect=15)
            ) as resp:
                result = await resp.json()
                audio_url = result.get("audio_url", "")
                if audio_url:
                    return ToolResult(
                        True,
                        f"Speech generated. Audio URL: {audio_url} (voice: {voice}, model: {model})",
                        data={"audio_url": audio_url, "voice": voice, "model": model}
                    )
                return ToolResult(False, f"TTS failed: {result}")
    except Exception as e:
        return ToolResult(False, f"Text to speech error: {str(e)}")
