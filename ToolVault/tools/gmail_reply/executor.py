"""Executor for gmail_reply (migrated from blackbox_tools._execute_gmail_reply)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Reply to an existing email thread."""
    import json
    from Orchestrator.gmail.service import send_email, get_message
    operator = params.get("operator", ctx.operator or "Brandon")
    message_id = params.get("message_id", "")
    thread_id = params.get("thread_id", "")
    body = params.get("body", "")
    if not message_id or not thread_id or not body:
        return ToolResult(success=False, result="message_id, thread_id, and body are all required")
    # Get original message to extract subject and sender for reply
    original = get_message(operator, message_id)
    to = original.get("from", "")
    subject = original.get("subject", "")
    if not subject.startswith("Re: "):
        subject = f"Re: {subject}"
    result = send_email(operator, to, subject, body, reply_to_message_id=message_id, thread_id=thread_id)
    return ToolResult(success=True, result=json.dumps(result))
