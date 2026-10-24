"""Executor for gmail_read (migrated from blackbox_tools._execute_gmail_read)."""
import asyncio

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Read full email content by message ID (the Google call runs off the event loop)."""
    import json
    from Orchestrator.gmail.service import get_message, is_error_result
    from Orchestrator.config import current_default
    operator = params.get("operator") or ctx.operator or current_default()
    message_id = params.get("message_id", "")
    if not message_id:
        return ToolResult(success=False, result="message_id is required")
    result = await asyncio.to_thread(get_message, operator, message_id)
    return ToolResult(success=not is_error_result(result), result=json.dumps(result, indent=2))
