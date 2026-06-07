"""Executor for gmail_send (migrated from blackbox_tools._execute_gmail_send)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Compose and send a new email."""
    import json
    from Orchestrator.gmail.service import send_email
    operator = params.get("operator", ctx.operator or "Brandon")
    to = params.get("to", "")
    subject = params.get("subject", "")
    body = params.get("body", "")
    cc = params.get("cc", "")
    print(f"[GMAIL-SEND] operator={operator}, to={to}, subject={subject[:50]}, body_len={len(body)}")
    if not to or not subject or not body:
        return ToolResult(success=False, result=f"to, subject, and body are all required. Got: to='{to}', subject='{subject}', body_len={len(body)}")
    result = send_email(operator, to, subject, body, cc)
    print(f"[GMAIL-SEND] Result: {json.dumps(result)}")
    return ToolResult(success=True, result=json.dumps(result))
