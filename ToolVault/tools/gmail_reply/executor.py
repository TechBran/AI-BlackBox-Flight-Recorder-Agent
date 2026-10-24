"""Executor for gmail_reply (migrated from blackbox_tools._execute_gmail_reply)."""
import asyncio

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Reply to an existing email thread (the Google calls run off the event loop)."""
    import json
    from Orchestrator.gmail.service import send_email, get_message, is_error_result
    from Orchestrator.config import current_default
    operator = params.get("operator") or ctx.operator or current_default()
    message_id = params.get("message_id", "")
    thread_id = params.get("thread_id", "")
    body = params.get("body", "")
    if not message_id or not thread_id or not body:
        return ToolResult(success=False, result="message_id, thread_id, and body are all required")
    # Get original message to extract subject and sender for reply
    original = await asyncio.to_thread(get_message, operator, message_id)
    if is_error_result(original):
        # No sender to reply to — never send a reply to an empty address.
        return ToolResult(success=False, result=json.dumps(original))
    to = (original.get("from") or "").strip()
    if not to:
        return ToolResult(success=False, result=json.dumps(
            {"error": f"Message {message_id} has no sender to reply to; nothing was sent."}))
    subject = original.get("subject", "")
    if not subject.startswith("Re: "):
        subject = f"Re: {subject}"
    result = await asyncio.to_thread(send_email, operator, to, subject, body,
                                     reply_to_message_id=message_id, thread_id=thread_id)
    return ToolResult(success=not is_error_result(result), result=json.dumps(result))
