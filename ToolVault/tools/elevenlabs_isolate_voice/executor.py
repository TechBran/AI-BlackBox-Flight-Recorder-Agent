"""Executor for elevenlabs_isolate_voice — background-noise removal / voice isolation.

SYNCHRONOUS: posts to the /elevenlabs/isolate route, which cleans the recording
and saves the result in seconds, returning the audio_url directly.
"""
import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Strip background noise from a recording and return the cleaned audio_url."""
    audio_path = (params.get("audio_path") or "").strip()
    if not audio_path:
        return ToolResult(False, "`audio_path` is required (the recording to clean).")

    payload = {"audio_path": audio_path, "operator": ctx.operator}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/elevenlabs/isolate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    audio_url = result.get("audio_url", "")
                    return ToolResult(
                        True,
                        f"Voice isolated (background noise removed): {audio_url}",
                        data={"audio_url": audio_url},
                    )
                error_text = await resp.text()
                return ToolResult(False, f"Voice isolation failed: {resp.status} - {error_text}")
    except Exception as e:
        return ToolResult(False, f"Voice isolation error: {str(e)}")
