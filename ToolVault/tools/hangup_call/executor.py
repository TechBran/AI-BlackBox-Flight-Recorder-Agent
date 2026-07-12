"""Executor for hangup_call — end the active xAI phone-line call."""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.xai_phone.call_control import hangup_call


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    call_id = str(params.get("call_id", "")).strip() or None
    ok, message = await hangup_call(call_id=call_id)
    return ToolResult(success=ok, result=message)
