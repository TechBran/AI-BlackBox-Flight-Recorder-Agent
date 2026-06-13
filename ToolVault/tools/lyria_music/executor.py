"""Executor for lyria_music (migrated from blackbox_tools._execute_generate_music)."""
import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Generate 30 seconds of music using Lyria."""
    prompt = params.get("prompt", "")

    if not prompt:
        return ToolResult(False, "Music prompt is required")

    try:
        # Build payload with all supported parameters
        payload = {
            "prompt": prompt,
            "operator": ctx.operator
        }

        # Add optional parameters if provided
        if params.get("negativePrompt"):
            payload["negativePrompt"] = params["negativePrompt"]
        if params.get("sampleCount"):
            payload["sampleCount"] = params["sampleCount"]

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/generate/lyria_music",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    task_id = result.get("task_id", "")

                    sample_info = ""
                    if params.get("sampleCount", 1) > 1:
                        sample_info = f" Generating {params['sampleCount']} samples."

                    return ToolResult(
                        success=True,
                        result=f"Music generation started. Task ID: {task_id}.{sample_info} Use get_task_status to check when it's ready.",
                        data={"task_id": task_id}
                    )
                else:
                    error_text = await resp.text()
                    return ToolResult(False, f"Music generation failed: {resp.status} - {error_text}")

    except Exception as e:
        return ToolResult(False, f"Music generation error: {str(e)}")
