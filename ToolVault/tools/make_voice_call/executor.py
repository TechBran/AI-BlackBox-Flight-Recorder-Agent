"""Executor for make_voice_call (migrated from blackbox_tools._execute_make_voice_call)."""
import aiohttp
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Call a number and have the voice agent speak ``message`` when they answer.

    The call route's realtime voice agent says the greeting itself, so nothing is
    rendered here. (This used to synthesize the message with OpenAI TTS first and
    then discard the audio: a paid request per call that nobody heard; audit
    O18-toolvault-modules-07.) The route picks the voice backend.
    """
    from Orchestrator.telephony import (
        call_endpoint, call_error, call_reference, call_result_ok, normalize_e164)

    raw_number = params.get("phone_number", "")
    message = params.get("message", "")

    if not raw_number or not message:
        return ToolResult(False, "Phone number and message are required")
    phone_number = normalize_e164(raw_number)
    if not phone_number:
        return ToolResult(False, f"Not a dialable phone number: {raw_number!r} (use E.164, e.g. +15551234567)")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                call_endpoint(ctx.base_url),
                json={
                    "to": phone_number,
                    "operator": ctx.operator,
                    "greeting": message  # Spoken first when the call connects
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                result = await resp.json(content_type=None)

                if resp.status == 200 and call_result_ok(result):
                    call_id = call_reference(result)
                    return ToolResult(
                        success=True,
                        result=f"Voice call initiated to {phone_number}. The message will be delivered when they answer.",
                        data={"call_sid": call_id, "to": phone_number}
                    )
                return ToolResult(False, f"Failed to initiate call: {call_error(result, resp.status)}")

    except Exception as e:
        return ToolResult(False, f"Voice call error: {str(e)}")
