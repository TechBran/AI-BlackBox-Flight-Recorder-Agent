"""Executor for elevenlabs_voice_changer — speech-to-speech re-voicing.

SYNCHRONOUS: posts to the /elevenlabs/voice-changer route, which converts the
recording and saves the result in seconds, returning the audio_url directly.
``target_voice`` accepts an 'elevenlabs:<id>' or a raw id (the server strips the
prefix).
"""
import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Re-voice a recording into the target voice and return the new audio_url."""
    audio_path = (params.get("audio_path") or "").strip()
    target_voice = (params.get("target_voice") or "").strip()
    if not audio_path:
        return ToolResult(False, "`audio_path` is required (the recording to re-voice).")
    if not target_voice:
        return ToolResult(False, "`target_voice` is required (e.g. 'elevenlabs:<id>').")

    payload = {
        "audio_path": audio_path,
        "target_voice": target_voice,
        "operator": ctx.operator,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/elevenlabs/voice-changer",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    audio_url = result.get("audio_url", "")
                    return ToolResult(
                        True,
                        f"Voice changed: {audio_url}",
                        data={"audio_url": audio_url},
                    )
                error_text = await resp.text()
                return ToolResult(False, f"Voice changer failed: {resp.status} - {error_text}")
    except Exception as e:
        return ToolResult(False, f"Voice changer error: {str(e)}")
