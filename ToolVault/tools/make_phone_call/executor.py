"""Executor for make_phone_call (migrated from blackbox_tools._execute_make_phone_call)."""
import aiohttp
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Initiate a phone call via cellular modem or Twilio."""
    phone_number = params.get("phone_number", "")
    greeting = params.get("greeting", "")
    role = params.get("role", "")
    backend = params.get("backend", "openai_realtime")

    if not phone_number:
        return ToolResult(False, "Phone number is required")

    # Normalize phone number
    if not phone_number.startswith("+"):
        if phone_number.startswith("1") and len(phone_number) == 11:
            phone_number = f"+{phone_number}"
        elif len(phone_number) == 10:
            phone_number = f"+1{phone_number}"

    # Determine call endpoint based on TELEPHONY_PROVIDER
    from Orchestrator.config import TELEPHONY_PROVIDER, CELLULAR_ENABLED, ASTERISK_ENABLED
    if TELEPHONY_PROVIDER == "asterisk" and ASTERISK_ENABLED:
        call_endpoint = f"{ctx.base_url}/asterisk/call"
    elif TELEPHONY_PROVIDER in ("cellular", "auto") and CELLULAR_ENABLED:
        call_endpoint = f"{ctx.base_url}/cellular/call"
    else:
        call_endpoint = f"{ctx.base_url}/twilio/call"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                call_endpoint,
                json={
                    "to": phone_number,
                    "backend": backend,
                    "operator": ctx.operator,
                    "greeting": greeting,
                    "role": role
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                result = await resp.json()

                if result.get("status") == "initiated":
                    call_sid = result.get("call_sid", "")
                    return ToolResult(
                        success=True,
                        result=f"Phone call initiated to {phone_number}. Call SID: {call_sid}",
                        data={"call_sid": call_sid, "to": phone_number}
                    )
                else:
                    error = result.get("error", "Unknown error")
                    return ToolResult(False, f"Failed to initiate call: {error}")

    except Exception as e:
        return ToolResult(False, f"Phone call error: {str(e)}")
