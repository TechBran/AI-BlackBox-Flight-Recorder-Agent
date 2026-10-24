"""Executor for analyze_video (migrated from blackbox_tools._execute_analyze_video)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Analyze a video by calling the multimodal model directly (Orchestrator.media_analysis).

    Not through /chat: that queues a chat turn which answers nothing here and
    later mints into the operator's ledger (audit O18-toolvault-modules-01).
    """
    from Orchestrator.media_analysis import MediaAnalysisError, analyze_media

    video_url = params.get("video_url", "")
    prompt = params.get("prompt") or "Describe what happens in this video"

    if not video_url:
        return ToolResult(False, "video_url is required")

    try:
        analysis, model = await analyze_media(video_url, prompt, "video")
    except MediaAnalysisError as e:
        return ToolResult(False, f"Analyze video error: {e}")
    return ToolResult(True, analysis, data={"provider": "gemini", "model": model})
