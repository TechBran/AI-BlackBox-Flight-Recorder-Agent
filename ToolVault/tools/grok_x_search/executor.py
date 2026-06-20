"""Executor for grok_x_search."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    query = params.get("query", "")
    if not query:
        return ToolResult(False, "Search query is required")
    try:
        from Orchestrator.web_tools import perform_provider_search
        result = perform_provider_search("grok_x", query)
        return ToolResult(True, result)
    except Exception as e:
        return ToolResult(False, f"Web search error: {e}")
