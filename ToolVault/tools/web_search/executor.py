"""Executor for web_search (migrated from blackbox_tools._execute_web_search)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Search the web using Perplexity Sonar (with DuckDuckGo fallback)."""
    query = params.get("query", "")
    max_results = params.get("max_results", 5)
    search_recency_filter = params.get("search_recency_filter", "month")

    if not query:
        return ToolResult(False, "Search query is required")

    try:
        from Orchestrator.web_tools import perform_web_search

        # perform_web_search returns formatted string
        result = perform_web_search(query, min(max_results, 10), search_recency_filter=search_recency_filter)

        if "❌" in result or "No results" in result:
            return ToolResult(
                success=True,
                result=result
            )

        return ToolResult(
            success=True,
            result=result
        )

    except Exception as e:
        return ToolResult(False, f"Web search error: {str(e)}")
