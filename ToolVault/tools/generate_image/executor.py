"""Executor for generate_image (migrated from blackbox_tools._execute_generate_image)."""
import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Generate an image using the BlackBox image generation endpoint (Gemini 3 Pro Image)."""
    prompt = params.get("prompt", "")

    if not prompt:
        return ToolResult(False, "Image prompt is required")

    try:
        # Build payload with all supported parameters
        payload = {
            "prompt": prompt,
            "operator": ctx.operator
        }

        # Add optional parameters if provided
        if params.get("reference_images"):
            payload["reference_images"] = params["reference_images"]
        if params.get("aspectRatio"):
            payload["aspectRatio"] = params["aspectRatio"]
        if params.get("resolution"):
            payload["resolution"] = params["resolution"]
        if params.get("numberOfImages"):
            payload["numberOfImages"] = params["numberOfImages"]

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
                            result=f"Image generation started. Task ID: {task_id}. The image will be available shortly.",
                            data={"task_id": task_id}
                        )
                    else:
                        url = result.get("url", "")
                        return ToolResult(
                            success=True,
                            result=f"Image generated: {url}",
                            data={"url": url}
                        )
                else:
                    error_text = await resp.text()
                    return ToolResult(False, f"Image generation failed: {resp.status} - {error_text}")

    except Exception as e:
        return ToolResult(False, f"Image generation error: {str(e)}")
