"""Executor for gmail_labels (migrated from blackbox_tools._execute_gmail_labels)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """List labels or modify message labels."""
    import json
    from Orchestrator.gmail.service import get_labels, modify_message, is_error_result
    from Orchestrator.config import current_default
    operator = params.get("operator") or ctx.operator or current_default()
    action = params.get("action", "list")
    message_id = params.get("message_id", "")

    if action == "list":
        result = get_labels(operator)
        return ToolResult(success=not is_error_result(result), result=json.dumps(result, indent=2))

    if not message_id:
        return ToolResult(success=False, result="message_id required for modify actions")

    label_map = {
        "mark_read": ([], ["UNREAD"]),
        "mark_unread": (["UNREAD"], []),
        "archive": ([], ["INBOX"]),
        "star": (["STARRED"], []),
        "unstar": ([], ["STARRED"]),
    }
    add_labels, remove_labels = label_map.get(action, ([], []))
    result = modify_message(operator, message_id, add_labels, remove_labels)
    return ToolResult(success=not is_error_result(result), result=json.dumps(result))
