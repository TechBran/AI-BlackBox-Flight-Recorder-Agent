"""Executor for elevenlabs_sound_effects — short SFX generation via ElevenLabs.

SYNCHRONOUS: posts to the /generate/elevenlabs_sound_effect route, which generates
+ saves the clip in seconds and returns the audio_url directly (no task to poll).
"""
import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Generate a sound effect from a text description and return its audio_url."""
    text = (params.get("text") or "").strip()
    if not text:
        return ToolResult(False, "Provide a `text` description of the sound effect.")

    payload = {"text": text, "operator": ctx.operator}
    if params.get("duration_seconds") is not None:
        payload["duration_seconds"] = params["duration_seconds"]
    if params.get("loop"):
        payload["loop"] = params["loop"]
    if params.get("prompt_influence") is not None:
        payload["prompt_influence"] = params["prompt_influence"]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/generate/elevenlabs_sound_effect",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    audio_url = result.get("audio_url", "")
                    return ToolResult(
                        True,
                        f"Sound effect generated: {audio_url}",
                        data={"audio_url": audio_url},
                    )
                error_text = await resp.text()
                return ToolResult(False, f"Sound effect generation failed: {resp.status} - {error_text}")
    except Exception as e:
        return ToolResult(False, f"Sound effect generation error: {str(e)}")
