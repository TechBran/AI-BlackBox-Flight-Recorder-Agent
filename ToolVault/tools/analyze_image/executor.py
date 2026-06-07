"""Executor for analyze_image (migrated from blackbox_tools._execute_analyze_image)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Analyze an image using multimodal AI via /chat endpoint."""
    image_url = params.get("image_url", "")
    prompt = params.get("prompt", "Describe this image in detail")

    if not image_url:
        return ToolResult(False, "image_url is required")

    try:
        import aiohttp
        payload = {
            "operator": ctx.operator,
            "provider": "gemini",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }]
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                result = await resp.json()
                analysis = result.get("response", "")
                return ToolResult(True, analysis, data={"provider": "gemini"})
    except Exception as e:
        return ToolResult(False, f"Analyze image error: {str(e)}")
