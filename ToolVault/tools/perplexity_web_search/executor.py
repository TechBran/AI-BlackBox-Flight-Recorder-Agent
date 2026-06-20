"""Executor for perplexity_web_search."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    query = params.get("query", "")
    if not query:
        return ToolResult(False, "Search query is required")
    recency = params.get("search_recency_filter", "month")
    try:
        from Orchestrator.web_tools import perform_provider_search
        result = perform_provider_search("perplexity", query, search_recency_filter=recency)
        return ToolResult(True, result)
    except Exception as e:
        return ToolResult(False, f"Web search error: {e}")
