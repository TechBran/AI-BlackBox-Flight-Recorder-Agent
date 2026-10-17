"""Executor for web_fetch (migrated from blackbox_tools._execute_web_fetch)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.utils.async_helpers import run_blocking


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Fetch and read content from a URL."""
    url = params.get("url", "")
    max_chars = params.get("max_chars", 80000)

    if not url:
        return ToolResult(False, "URL is required")

    try:
        from Orchestrator.web_tools import perform_web_fetch

        # perform_web_fetch returns formatted string. It blocks for up to its
        # whole time budget, so it runs off the event loop.
        result = await run_blocking(perform_web_fetch, url, max_chars)

        if "❌" in result:
            return ToolResult(False, result)

        return ToolResult(
            success=True,
            result=result,
            data={"url": url}
        )

    except Exception as e:
        return ToolResult(False, f"Web fetch error: {str(e)}")
