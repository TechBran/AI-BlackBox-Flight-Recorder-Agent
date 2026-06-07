"""Executor for gmail_read (migrated from blackbox_tools._execute_gmail_read)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Read full email content by message ID."""
    import json
    from Orchestrator.gmail.service import get_message
    operator = params.get("operator", ctx.operator or "Brandon")
    message_id = params.get("message_id", "")
    if not message_id:
        return ToolResult(success=False, result="message_id is required")
    result = get_message(operator, message_id)
    return ToolResult(success=True, result=json.dumps(result, indent=2))
