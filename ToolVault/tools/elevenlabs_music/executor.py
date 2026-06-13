"""Executor for elevenlabs_music — full-song generation via ElevenLabs Music.

A SEPARATE music tool alongside generate_music (Lyria); both coexist
(provider-explicit naming). Mirrors generate_music's task-dispatch shape: POST to
the /generate/elevenlabs_music route, get a task_id back, tell the caller to poll
get_task_status. Either `prompt` or `composition_plan` is required (enforced here
AND server-side).
"""
import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Start an ElevenLabs Music generation task (full song, up to 5 minutes)."""
    prompt = params.get("prompt")
    composition_plan = params.get("composition_plan")

    if not prompt and not composition_plan:
        return ToolResult(False, "Provide a prompt or a composition_plan.")

    try:
        payload = {"operator": ctx.operator}
        if prompt:
            payload["prompt"] = prompt
        if composition_plan:
            payload["composition_plan"] = composition_plan
        if params.get("music_length_ms") is not None:
            payload["music_length_ms"] = params["music_length_ms"]
        if params.get("force_instrumental"):
            payload["force_instrumental"] = params["force_instrumental"]

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/generate/elevenlabs_music",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    task_id = result.get("task_id", "")
                    return ToolResult(
                        success=True,
                        result=f"Music generation started. Task ID: {task_id}. Use get_task_status to check when it's ready.",
                        data={"task_id": task_id}
                    )
                else:
                    error_text = await resp.text()
                    return ToolResult(False, f"Music generation failed: {resp.status} - {error_text}")

    except Exception as e:
        return ToolResult(False, f"Music generation error: {str(e)}")
