"""Executor for gemini_pro_tts (migrated from blackbox_tools._execute_gemini_pro_tts)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Generate speech using Gemini Pro TTS via /generate/gemini_tts (async task).

    The voice travels only when the caller names one, and no model is sent: the
    route's GeminiProTTSIn already carries the Pro model and the default voice,
    so the tool keeps no copy of either to rot when an id moves
    (O18-toolvault-modules-14).
    """
    text = params.get("text", "")
    voice = str(params.get("voice") or "").strip()

    if not text:
        return ToolResult(False, "text is required")

    try:
        import aiohttp
        payload = {
            "text": text,
            "operator": ctx.operator,
            "multi_speaker": False,
        }
        if voice:
            payload["voice_name"] = voice
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/generate/gemini_tts",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                result = await resp.json()
                task_id = result.get("task_id")
                if task_id:
                    shown = voice or "box default"
                    return ToolResult(
                        True,
                        f"Gemini Pro TTS generation started. Task ID: {task_id}. Voice: {shown}. Use get_task_status to check progress.",
                        data={"task_id": task_id, "voice": shown}
                    )
                return ToolResult(False, f"Failed to start Gemini Pro TTS: {result}")
    except Exception as e:
        return ToolResult(False, f"Gemini Pro TTS error: {str(e)}")
