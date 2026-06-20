"""Executor for gemini_image."""
import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Queue a gemini-provider image generation task via /generate/image."""
    prompt = params.get("prompt", "")
    if not prompt:
        return ToolResult(False, "Image prompt is required")
    try:
        payload = {"prompt": prompt, "operator": ctx.operator, "provider": "gemini"}
        for k in ("reference_images", "aspectRatio", "resolution", "numberOfImages",):
            if params.get(k) is not None:
                payload[k] = params[k]
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/generate/image",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    task_id = result.get("task_id", "")
                    if task_id:
                        return ToolResult(
                            success=True,
                            result=f"Image generation started (gemini). Task ID: {task_id}. The image will be available shortly.",
                            data={"task_id": task_id}
                        )
                    url = result.get("url", "")
                    return ToolResult(
                        success=True,
                        result=f"Image generated: {url}",
                        data={"url": url}
                    )
                error_text = await resp.text()
                return ToolResult(False, f"Image generation failed: {resp.status} - {error_text}")
    except Exception as e:
        return ToolResult(False, f"Image generation error: {e}")
