"""Executor for make_voice_call (migrated from blackbox_tools._execute_make_voice_call)."""
import aiohttp
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """
    Make a voice call with a pre-generated TTS message.

    Flow:
    1. Generate TTS audio first (no delay on call connect)
    2. Save audio to a file
    3. Initiate call with audio injection
    """
    phone_number = params.get("phone_number", "")
    message = params.get("message", "")
    voice = params.get("voice", "onyx")

    if not phone_number or not message:
        return ToolResult(False, "Phone number and message are required")

    # Normalize phone number
    if not phone_number.startswith("+"):
        if phone_number.startswith("1") and len(phone_number) == 11:
            phone_number = f"+{phone_number}"
        elif len(phone_number) == 10:
            phone_number = f"+1{phone_number}"

    try:
        from Orchestrator.config import OPENAI_API_KEY, OPENAI_TTS_URL

        # Step 1: Generate TTS audio FIRST
        print(f"[TOOL] Generating TTS for voice call: {message[:50]}...")

        async with aiohttp.ClientSession() as session:
            # Generate TTS
            async with session.post(
                OPENAI_TTS_URL,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "tts-1-hd",
                    "voice": voice,
                    "input": message,
                    "response_format": "pcm"  # Raw PCM for phone
                },
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    return ToolResult(False, f"TTS generation failed: {resp.status}")

                pcm_audio = await resp.read()
                print(f"[TOOL] TTS generated: {len(pcm_audio)} bytes")

            # Step 2: Initiate call with pre-generated greeting
            # The greeting will be converted to ULAW and played immediately
            # Route based on TELEPHONY_PROVIDER
            from Orchestrator.config import TELEPHONY_PROVIDER, CELLULAR_ENABLED, ASTERISK_ENABLED
            if TELEPHONY_PROVIDER == "asterisk" and ASTERISK_ENABLED:
                call_endpoint = f"{ctx.base_url}/asterisk/call"
            elif TELEPHONY_PROVIDER in ("cellular", "auto") and CELLULAR_ENABLED:
                call_endpoint = f"{ctx.base_url}/cellular/call"
            else:
                call_endpoint = f"{ctx.base_url}/twilio/call"
            async with session.post(
                call_endpoint,
                json={
                    "to": phone_number,
                    "backend": "openai_realtime",
                    "operator": ctx.operator,
                    "greeting": message  # Pass the message as greeting
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                result = await resp.json()

                if result.get("status") == "initiated":
                    call_sid = result.get("call_sid", "")
                    return ToolResult(
                        success=True,
                        result=f"Voice call initiated to {phone_number}. The message will be delivered when they answer.",
                        data={"call_sid": call_sid, "to": phone_number}
                    )
                else:
                    error = result.get("error", "Unknown error")
                    return ToolResult(False, f"Failed to initiate call: {error}")

    except Exception as e:
        return ToolResult(False, f"Voice call error: {str(e)}")
