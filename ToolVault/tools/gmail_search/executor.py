"""Executor for gmail_search (migrated from blackbox_tools._execute_gmail_search)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Search/list emails in the operator's Gmail inbox."""
    import json
    from Orchestrator.gmail.service import list_messages, is_error_result
    from Orchestrator.config import current_default
    operator = params.get("operator") or ctx.operator or current_default()
    query = params.get("query", "")
    max_results = min(int(params.get("max_results", 10)), 20)
    results = list_messages(operator, query, max_results)
    # An error entry is a failed call; an empty list (no matches) is not.
    return ToolResult(success=not is_error_result(results), result=json.dumps(results, indent=2))
