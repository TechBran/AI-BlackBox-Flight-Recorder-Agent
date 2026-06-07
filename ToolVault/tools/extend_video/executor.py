"""Executor for extend_video (migrated from blackbox_tools._execute_extend_video)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Extend an existing video using Veo 3.1 via /extend/video (async task)."""
    video_url = params.get("video_url", "")
    prompt = params.get("prompt", "")

    if not video_url:
        return ToolResult(False, "video_url is required")

    try:
        import aiohttp
        payload = {
            "video_url": video_url,
            "prompt": prompt,
            "operator": ctx.operator
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/extend/video",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                result = await resp.json()
                task_id = result.get("task_id")
                if task_id:
                    return ToolResult(
                        True,
                        f"Video extension started. Task ID: {task_id}. The new clip will continue from where the original ended. Use get_task_status to check progress (5-20 minutes).",
                        data={"task_id": task_id}
                    )
                return ToolResult(False, f"Failed to start video extension: {result}")
    except Exception as e:
        return ToolResult(False, f"Extend video error: {str(e)}")
