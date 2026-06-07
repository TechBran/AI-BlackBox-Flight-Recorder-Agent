"""Executor for generate_video (migrated from blackbox_tools._execute_generate_video)."""
import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Generate a video using Veo 3.1. Supports text-to-video, image-to-video, and video extension. Takes 5-20 minutes."""
    prompt = params.get("prompt", "")

    if not prompt:
        return ToolResult(False, "Video prompt is required")

    try:
        # Build payload with all supported parameters
        payload = {
            "prompt": prompt,
            "operator": ctx.operator
        }

        # Add optional parameters if provided
        if params.get("image_url"):
            payload["image_url"] = params["image_url"]
        if params.get("video_url"):
            payload["video_url"] = params["video_url"]
        if params.get("aspectRatio"):
            payload["aspectRatio"] = params["aspectRatio"]
        if params.get("duration"):
            payload["duration"] = params["duration"]
        if params.get("resolution"):
            payload["resolution"] = params["resolution"]
        if params.get("negativePrompt"):
            payload["negativePrompt"] = params["negativePrompt"]

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/generate/video",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    task_id = result.get("task_id", "")

                    # Determine mode for helpful message
                    mode = "Text-to-video"
                    if params.get("image_url"):
                        mode = "Image-to-video"
                    elif params.get("video_url"):
                        mode = "Video extension"

                    return ToolResult(
                        success=True,
                        result=f"{mode} generation started. Task ID: {task_id}. This will take 5-20 minutes. Use get_task_status to check progress.",
                        data={"task_id": task_id, "mode": mode}
                    )
                else:
                    error_text = await resp.text()
                    return ToolResult(False, f"Video generation failed: {resp.status} - {error_text}")

    except Exception as e:
        return ToolResult(False, f"Video generation error: {str(e)}")
