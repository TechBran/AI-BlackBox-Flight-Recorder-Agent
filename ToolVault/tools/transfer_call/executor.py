"""Executor for transfer_call — SIP REFER on the active xAI phone-line call."""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.xai_phone.call_control import transfer_call


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    target_uri = str(params.get("target_uri", "")).strip()
    call_id = str(params.get("call_id", "")).strip() or None
    ok, message = await transfer_call(target_uri, call_id=call_id)
    return ToolResult(success=ok, result=message)
