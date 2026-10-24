"""Executor for gmail_send (migrated from blackbox_tools._execute_gmail_send)."""
import asyncio

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Compose and send a new email. Success comes from the Gmail result's shape
    (an {"error": ...} reply is a failure); the blocking Google call runs off the
    event loop."""
    import json
    from Orchestrator.gmail.service import send_email, is_error_result
    from Orchestrator.config import current_default
    operator = params.get("operator") or ctx.operator or current_default()
    to = params.get("to", "")
    subject = params.get("subject", "")
    body = params.get("body", "")
    cc = params.get("cc", "")
    if not to or not subject or not body:
        return ToolResult(success=False, result=f"to, subject, and body are all required. Got: to='{to}', subject='{subject}', body_len={len(body)}")
    result = await asyncio.to_thread(send_email, operator, to, subject, body, cc)
    ok = not is_error_result(result)
    # Never the recipient, subject or reply here: the journal is not a mail log.
    print(f"[GMAIL-SEND] operator={operator}, body_len={len(body)}, ok={ok}")
    return ToolResult(success=ok, result=json.dumps(result))
