"""Executor for text_to_speech (migrated from blackbox_tools._execute_text_to_speech)."""
from Orchestrator import config
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Speak text through POST /tts with the voice and model the caller named.

    Either may be omitted: /tts then applies the box default -- the voice GET
    /tts/catalog publishes as "default", and that provider's model -- so no
    provider is assumed and an ``elevenlabs:<id>`` voice never travels with an
    OpenAI model id. The reply names the voice/model the server actually used.
    """
    text = params.get("text", "")
    voice = str(params.get("voice") or "").strip()
    model = str(params.get("model") or "").strip()

    if not text:
        return ToolResult(False, "text is required")

    try:
        import aiohttp
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        if model:
            payload["model"] = model
        payload["return_json"] = True
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
                    used_voice = result.get("voice") or voice or "box default"
                    used_model = result.get("model") or model or "box default"
                    return ToolResult(
                        True,
                        f"Speech generated. Audio URL: {audio_url} (voice: {used_voice}, model: {used_model})",
                        data={"audio_url": audio_url, "voice": used_voice, "model": used_model}
                    )
                return ToolResult(False, f"TTS failed: {result}")
    except Exception as e:
        return ToolResult(False, f"Text to speech error: {str(e)}")
