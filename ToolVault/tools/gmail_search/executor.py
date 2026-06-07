"""Executor for gmail_search (migrated from blackbox_tools._execute_gmail_search)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Search/list emails in the operator's Gmail inbox."""
    import json
    from Orchestrator.gmail.service import list_messages
    operator = params.get("operator", ctx.operator or "Brandon")
    query = params.get("query", "")
    max_results = min(int(params.get("max_results", 10)), 20)
    results = list_messages(operator, query, max_results)
    return ToolResult(success=True, result=json.dumps(results, indent=2))
