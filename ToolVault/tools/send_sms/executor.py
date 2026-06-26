"""Executor for send_sms (migrated from blackbox_tools._execute_send_sms)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Send an SMS message via the TG200 cellular gateway (Asterisk)."""
    phone_number = params.get("phone_number", "")
    message = params.get("message", "")
    from_number = params.get("from_number") or None

    if not phone_number or not message:
        return ToolResult(False, "Phone number and message are required")

    # Normalize phone number
    if not phone_number.startswith("+"):
        if phone_number.startswith("1") and len(phone_number) == 11:
            phone_number = f"+{phone_number}"
        elif len(phone_number) == 10:
            phone_number = f"+1{phone_number}"

    # Truncate message if too long
    if len(message) > 1600:
        message = message[:1597] + "..."

    # Route based on TELEPHONY_PROVIDER config
    from Orchestrator.config import TELEPHONY_PROVIDER, CELLULAR_ENABLED, ASTERISK_ENABLED

    # Asterisk/TG200 path (preferred when enabled).
    # Route through the SMS router's send_manual so from-number selection,
    # span resolution and outbound storage all live in one place.
    if TELEPHONY_PROVIDER == "asterisk" and ASTERISK_ENABLED:
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
        "(set TELEPHONY_PROVIDER=asterisk and enable the gateway).",
    )
