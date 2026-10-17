"""Executor for gmail_send (migrated from blackbox_tools._execute_gmail_send)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Compose and send a new email."""
    import json
    from Orchestrator.gmail.service import send_email, is_error_result
    from Orchestrator.config import current_default
    operator = params.get("operator") or ctx.operator or current_default()
    to = params.get("to", "")
    subject = params.get("subject", "")
    body = params.get("body", "")
    cc = params.get("cc", "")
    print(f"[GMAIL-SEND] operator={operator}, to={to}, subject={subject[:50]}, body_len={len(body)}")
    if not to or not subject or not body:
        return ToolResult(success=False, result=f"to, subject, and body are all required. Got: to='{to}', subject='{subject}', body_len={len(body)}")
    result = send_email(operator, to, subject, body, cc)
    print(f"[GMAIL-SEND] Result: {json.dumps(result)}")
    return ToolResult(success=not is_error_result(result), result=json.dumps(result))
