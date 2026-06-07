"""Executor for get_media (migrated from blackbox_tools._execute_get_media)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Retrieve media by URL or task_id."""
    url = params.get("url", "")
    task_id = params.get("task_id", "")

    if not url and not task_id:
        return ToolResult(False, "Either url or task_id is required")

    try:
        from Orchestrator.routes.chat_routes import execute_get_media
        result = execute_get_media(url, task_id)

        if result.get("error"):
            return ToolResult(False, result["error"])

        return ToolResult(
            success=True,
            result=f"Media found: {result.get('url', 'unknown')} ({result.get('type', 'unknown')})",
            data=result
        )
    except Exception as e:
        return ToolResult(False, f"Get media error: {str(e)}")
