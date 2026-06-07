"""Executor for search_media (migrated from blackbox_tools._execute_search_media)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Search media by description, prompt, or filename."""
    query = params.get("query", "")
    media_type = params.get("media_type")
    limit = params.get("limit", 10)

    if not query:
        return ToolResult(False, "Search query is required")

    try:
        from Orchestrator.routes.chat_routes import execute_search_media
        result = execute_search_media(query, media_type, limit)

        # Function returns "results" not "files"
        media = result.get("results", [])
        if not media:
            return ToolResult(
                success=True,
                result=f"No media found matching '{query}'",
                data=result
            )

        # Format output
        summary = f"Found {len(media)} matching file(s) for '{query}':\n"
        for m in media[:15]:
            desc = m.get('description', '')[:50] if m.get('description') else 'no description'
            summary += f"- {m.get('url', 'unknown')} ({m.get('type', 'unknown')}) - {desc}\n"

        return ToolResult(success=True, result=summary, data=result)
    except Exception as e:
        return ToolResult(False, f"Search media error: {str(e)}")
