"""Executor for send_sms (migrated from blackbox_tools._execute_send_sms)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Send an SMS message via the TG200 cellular gateway (Asterisk)."""
    from Orchestrator.telephony import normalize_e164, uses_asterisk

    raw_number = params.get("phone_number", "")
    message = params.get("message", "")
    from_number = params.get("from_number") or None

    if not raw_number or not message:
        return ToolResult(False, "Phone number and message are required")
    phone_number = normalize_e164(raw_number)
    if not phone_number:
        return ToolResult(False, f"Not a valid phone number: {raw_number!r} (use E.164, e.g. +15551234567)")

    # Truncate message if too long
    if len(message) > 1600:
        message = message[:1597] + "..."

    # TG200/Asterisk is the only SMS path (TELEPHONY_PROVIDER asterisk or auto,
    # with Asterisk enabled). Route through the SMS router's send_manual so
    # from-number selection, span resolution and outbound storage all live in
    # one place.
    if uses_asterisk():
        try:
            from Orchestrator.sms import get_router
            sms_router = get_router()
            if sms_router is None:
                return ToolResult(False, "SMS system not connected (router down)")

            result = await sms_router.send_manual(
                operator=ctx.operator or "system",
                to=phone_number,
                message=message,
                from_number=from_number,
            )
            if result.get("success"):
                return ToolResult(True, f"SMS sent to {phone_number} via TG200.", data={"to": phone_number, "provider": "asterisk"})
            return ToolResult(False, f"TG200 SMS failed: {result.get('error', 'Unknown error')}")
        except Exception as e:
            return ToolResult(False, f"TG200 SMS error: {str(e)}")

    # TG200/Asterisk is the only supported SMS path. If it is not the
    # configured provider (or is disabled), SMS is unavailable.
    return ToolResult(
        False,
        "SMS unavailable: TG200/Asterisk is not configured "
        "(set TELEPHONY_PROVIDER=asterisk or auto and enable the gateway).",
    )
