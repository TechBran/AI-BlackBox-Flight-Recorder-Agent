"""Executor for analyze_video (migrated from blackbox_tools._execute_analyze_video)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Analyze a video using multimodal AI via /chat endpoint."""
    video_url = params.get("video_url", "")
    prompt = params.get("prompt", "Describe what happens in this video")

    if not video_url:
        return ToolResult(False, "video_url is required")

    try:
        import aiohttp
        payload = {
            "operator": ctx.operator,
            "provider": "gemini",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "video_url", "video_url": {"url": video_url}}
                ]
            }]
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=180)
            ) as resp:
                result = await resp.json()
                analysis = result.get("response", "")
                return ToolResult(True, analysis, data={"provider": "gemini"})
    except Exception as e:
        return ToolResult(False, f"Analyze video error: {str(e)}")
