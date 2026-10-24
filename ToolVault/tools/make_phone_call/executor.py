"""Executor for make_phone_call (migrated from blackbox_tools._execute_make_phone_call)."""
import aiohttp
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Initiate a phone call through the box's call route (Asterisk/TG200 or Twilio)."""
    from Orchestrator.telephony import (
        call_endpoint, call_error, call_reference, call_result_ok, normalize_e164)

    raw_number = params.get("phone_number", "")
    greeting = params.get("greeting", "")
    role = params.get("role", "")
    backend = params.get("backend", "openai_realtime")

    if not raw_number:
        return ToolResult(False, "Phone number is required")
    phone_number = normalize_e164(raw_number)
    if not phone_number:
        return ToolResult(False, f"Not a dialable phone number: {raw_number!r} (use E.164, e.g. +15551234567)")

    # The route that places calls on this box (Orchestrator.telephony).
    call_url = call_endpoint(ctx.base_url)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                call_url,
                json={
                    "to": phone_number,
                    "backend": backend,
                    "operator": ctx.operator,
                    "greeting": greeting,
                    "role": role
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                result = await resp.json(content_type=None)

                if resp.status == 200 and call_result_ok(result):
                    call_id = call_reference(result)
                    return ToolResult(
                        success=True,
                        result=f"Phone call initiated to {phone_number}. Call ID: {call_id}",
                        data={"call_sid": call_id, "to": phone_number}
                    )
                return ToolResult(False, f"Failed to initiate call: {call_error(result, resp.status)}")

    except Exception as e:
        return ToolResult(False, f"Phone call error: {str(e)}")
