"""Executor for analyze_image (migrated from blackbox_tools._execute_analyze_image)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Analyze an image by calling the vision model directly (Orchestrator.media_analysis).

    Not through /chat: that queues a chat turn which answers nothing here and
    later mints into the operator's ledger (audit O18-toolvault-modules-01).
    """
    from Orchestrator.media_analysis import MediaAnalysisError, analyze_media

    image_url = params.get("image_url", "")
    prompt = params.get("prompt") or "Describe this image in detail"

    if not image_url:
        return ToolResult(False, "image_url is required")

    try:
        analysis, model = await analyze_media(image_url, prompt, "image")
    except MediaAnalysisError as e:
        return ToolResult(False, f"Analyze image error: {e}")
    return ToolResult(True, analysis, data={"provider": "gemini", "model": model})
