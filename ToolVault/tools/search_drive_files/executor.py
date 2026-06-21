"""Executor for search_drive_files."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import drive

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    query = params.get("query")
    page_size = params.get("page_size", 20)
    result = drive.search_drive_files(operator, query=query, page_size=page_size)
    ok = not (isinstance(result, dict) and "error" in result)
    return ToolResult(ok, json.dumps(result))
