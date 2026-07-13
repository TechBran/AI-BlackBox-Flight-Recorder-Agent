#!/usr/bin/env python3
"""
twilio_routes.py - Twilio Voice Webhooks and Media Streams

Handles Twilio Programmable Voice integration:
- POST /twilio/voice - Incoming call webhook (returns TwiML)
- POST /twilio/gather - IVR DTMF collection webhook
- POST /twilio/status - Call status callback
- WS /twilio/media/{session_id} - Media Streams WebSocket for audio

This provides an alternative to FreeSwitch ESL for phone-AI bridging
when using Twilio Elastic SIP Trunking with 3CX.
"""

import asyncio
import json
import uuid
import base64
from typing import Optional, Dict, Any
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import WebSocket, WebSocketDisconnect, Request, Form
from fastapi.responses import Response, PlainTextResponse

from Orchestrator.checkpoint import app
from Orchestrator.utils.paths import resolve
from Orchestrator.volume import now_utc_iso
from Orchestrator.config import (
    PHONE_ENABLED,
    IVR_TIMEOUT_MS,
    IVR_MAX_RETRIES,
    IVR_DEFAULT_BACKEND,
    TWILIO_WEBHOOK_BASE_URL,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_PHONE_NUMBER,
    USERS_LIST,
    PHONE_PIN_ENABLED,
    PHONE_PIN_CODE,
    PHONE_PIN_MAX_ATTEMPTS,
)
from pydantic import BaseModel

# Twilio REST client for outbound calls
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


class OutboundCallRequest(BaseModel):
    """Request to initiate an outbound call via Twilio."""
    to: str  # Phone number to call (E.164 format)
    backend: str = "openai_realtime"  # AI backend to use
    operator: str = ""  # Operator name
    greeting: str = ""  # Optional greeting message (task instructions for the agent)
    role: str = ""  # Optional custom system prompt/persona for the AI agent
    claude_session_id: str = ""  # Optional: Resume existing Claude CLI session
from Orchestrator.phone.session import (
    PhoneSession,
    PhoneStatus,
    AIBackend,
    CallDirection,
    PHONE_SESSIONS,
    get_session,
    get_active_sessions,
)
from Orchestrator.phone.audio_converter import AudioConverter
from Orchestrator.phone.bridge import PhoneAIBridge, save_session_to_blackbox
from Orchestrator.phone.ivr_prompts import (
    IVR_WELCOME,
    IVR_RETRY,
    IVR_INVALID,
    IVR_TIMEOUT,
    IVR_OPERATOR_TIMEOUT,
    IVR_OPERATOR_INVALID,
    get_confirmation_prompt,
    get_backend_id,
    get_backend_name,
    build_operator_selection_prompt,
    build_operator_retry_prompt,
    get_operator_confirmation,
)


# =============================================================================
# TwiML Response Helpers
# =============================================================================

def twiml_response(content: str) -> Response:
    """Return a TwiML XML response."""
    return Response(content=content, media_type="application/xml")


def create_twiml_pin_prompt(session_id: str, retry_count: int = 0, invalid: bool = False) -> str:
    """
    Create TwiML for PIN entry prompt.

    Args:
        session_id: Phone session ID
        retry_count: Number of failed attempts
        invalid: Whether the last PIN was invalid

    Returns:
        TwiML XML string
    """
    response = Element("Response")

    # Gather 4-digit PIN
    gather = SubElement(response, "Gather", {
        "numDigits": "4",
        "action": f"/twilio/pin?session_id={session_id}&retry={retry_count}",
        "timeout": "10",
        "method": "POST",
    })

    # Say the prompt
    say = SubElement(gather, "Say", {"voice": "Polly.Matthew"})
    if invalid:
        say.text = "Invalid PIN. Please enter your 4 digit PIN."
    else:
        say.text = "Welcome to AI BlackBox. Please enter your 4 digit PIN."

    # If no input, redirect to try again
    if retry_count < PHONE_PIN_MAX_ATTEMPTS - 1:
        redirect = SubElement(response, "Redirect", {"method": "POST"})
        redirect.text = f"/twilio/pin?session_id={session_id}&retry={retry_count + 1}&timeout=true"
    else:
        # Max retries - hang up
        say_fail = SubElement(response, "Say", {"voice": "Polly.Matthew"})
        say_fail.text = "Too many failed attempts. Goodbye."
        SubElement(response, "Hangup")

    return tostring(response, encoding="unicode")


def create_twiml_ivr(session_id: str, message: str, retry_count: int = 0) -> str:
    """
    Create TwiML for IVR menu with Gather.

    Args:
        session_id: Phone session ID
        message: Text to speak
        retry_count: Number of retries so far

    Returns:
        TwiML XML string
    """
    response = Element("Response")

    # Gather DTMF with timeout
    gather = SubElement(response, "Gather", {
        "numDigits": "1",
        "action": f"/twilio/gather?session_id={session_id}&retry={retry_count}",
        "timeout": str(IVR_TIMEOUT_MS // 1000),
        "method": "POST",
    })

    # Say the prompt
    say = SubElement(gather, "Say", {"voice": "Polly.Matthew"})
    say.text = message

    # If no input, redirect to gather again or timeout
    if retry_count < IVR_MAX_RETRIES:
        redirect = SubElement(response, "Redirect", {"method": "POST"})
        redirect.text = f"/twilio/gather?session_id={session_id}&retry={retry_count + 1}&timeout=true"
    else:
        # Max retries - default to GPT, then go to operator selection
        say_timeout = SubElement(response, "Say", {"voice": "Polly.Matthew"})
        say_timeout.text = IVR_TIMEOUT
        # Redirect to operator selection
        redirect = SubElement(response, "Redirect", {"method": "POST"})
        redirect.text = f"/twilio/operator?session_id={session_id}&retry=0"

    return tostring(response, encoding="unicode")


def create_twiml_operator_selection(session_id: str, retry_count: int = 0) -> str:
    """
    Create TwiML for operator selection menu.

    Args:
        session_id: Phone session ID
        retry_count: Number of retries so far

    Returns:
        TwiML XML string
    """
    response = Element("Response")

    # Get live operator list
    operators = USERS_LIST

    # Build the prompt message
    if retry_count == 0:
        message = build_operator_selection_prompt(operators)
    else:
        message = build_operator_retry_prompt(operators)

    # Determine number of digits needed (1-9 for operators, 0 for other)
    num_digits = "1"

    # Gather DTMF with timeout
    gather = SubElement(response, "Gather", {
        "numDigits": num_digits,
        "action": f"/twilio/operator?session_id={session_id}&retry={retry_count}",
        "timeout": str(IVR_TIMEOUT_MS // 1000),
        "method": "POST",
    })

    # Say the prompt
    say = SubElement(gather, "Say", {"voice": "Polly.Matthew"})
    say.text = message

    # If no input, redirect to gather again or timeout
    if retry_count < IVR_MAX_RETRIES:
        redirect = SubElement(response, "Redirect", {"method": "POST"})
        redirect.text = f"/twilio/operator?session_id={session_id}&retry={retry_count + 1}&timeout=true"
    else:
        # Max retries - default to first operator or guest
        say_timeout = SubElement(response, "Say", {"voice": "Polly.Matthew"})
        say_timeout.text = IVR_OPERATOR_TIMEOUT
        # Connect as guest with default operator
        return create_twiml_connect(session_id, operators[0] if operators else "Guest")

    return tostring(response, encoding="unicode")


def create_twiml_connect(session_id: str, operator_name: str) -> str:
    """
    Create TwiML to connect to AI via Media Streams.

    Args:
        session_id: Phone session ID
        operator_name: Selected operator name

    Returns:
        TwiML XML string
    """
    response = Element("Response")

    # Get the session to know which backend was selected
    session = get_session(session_id)
    backend_name = get_backend_name(session.ivr_selection) if session else "GPT"

    # Say personalized confirmation
    say = SubElement(response, "Say", {"voice": "Polly.Matthew"})
    say.text = get_operator_confirmation(operator_name, backend_name)

    # Use Connect with Stream for BIDIRECTIONAL audio
    # This allows us to both receive AND send audio through the WebSocket
    connect = SubElement(response, "Connect")
    stream = SubElement(connect, "Stream", {
        "url": f"{TWILIO_WEBHOOK_BASE_URL}/twilio/media/{session_id}",
    })

    return tostring(response, encoding="unicode")


def create_twiml_hangup(message: str = "") -> str:
    """Create TwiML to hang up the call."""
    response = Element("Response")
    if message:
        say = SubElement(response, "Say", {"voice": "Polly.Matthew"})
        say.text = message
    SubElement(response, "Hangup")
    return tostring(response, encoding="unicode")


# =============================================================================
# Twilio Voice Webhooks
# =============================================================================

@app.post("/twilio/voice")
async def twilio_voice_webhook(
    request: Request,
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    To: str = Form(default=""),
    Direction: str = Form(default=""),
):
    """
    Twilio incoming call webhook.

    This is called when a call comes in to the Twilio phone number.
    Returns TwiML to play the IVR menu and gather DTMF input.
    """
    if not PHONE_ENABLED:
        return twiml_response(create_twiml_hangup("Phone service is not available."))

    print(f"[TWILIO] Incoming call: {From} -> {To} (SID: {CallSid})")

    # Create phone session
    session_id = f"phone-{uuid.uuid4().hex[:12]}"
    session = PhoneSession(
        session_id=session_id,
        call_id=CallSid,
        caller_id=From,
        callee_id=To,
        direction=CallDirection.INBOUND,
        status=PhoneStatus.IVR,
        created_at=now_utc_iso(),
        last_activity=now_utc_iso(),
        operator="",  # Will be set based on context
    )
    PHONE_SESSIONS[session_id] = session

    # If PIN is enabled, require PIN before IVR menu
    if PHONE_PIN_ENABLED:
        print(f"[TWILIO] PIN required for inbound call: {session_id}")
        twiml = create_twiml_pin_prompt(session_id, retry_count=0, invalid=False)
        return twiml_response(twiml)

    # No PIN required - go directly to IVR menu
    twiml = create_twiml_ivr(session_id, IVR_WELCOME.strip(), 0)
    return twiml_response(twiml)


@app.post("/twilio/pin")
async def twilio_pin_webhook(
    request: Request,
    session_id: str = "",
    retry: int = 0,
    timeout: bool = False,
    Digits: str = Form(default=""),
    CallSid: str = Form(default=""),
):
    """
    Twilio PIN verification webhook.

    Called when user enters their 4-digit PIN to gate inbound calls.
    """
    session = get_session(session_id)
    if not session:
        return twiml_response(create_twiml_hangup("Session not found."))

    session.last_activity = now_utc_iso()

    # Handle timeout (no input)
    if timeout or not Digits:
        if retry >= PHONE_PIN_MAX_ATTEMPTS - 1:
            print(f"[TWILIO] PIN timeout, max retries reached: {session_id}")
            return twiml_response(create_twiml_hangup("Too many failed attempts. Goodbye."))
        else:
            # Retry prompt
            print(f"[TWILIO] PIN timeout, retry {retry + 1}: {session_id}")
            return twiml_response(create_twiml_pin_prompt(session_id, retry + 1, invalid=False))

    # Validate PIN
    if Digits == PHONE_PIN_CODE:
        # PIN correct - proceed to IVR menu
        print(f"[TWILIO] PIN verified for session: {session_id}")
        return twiml_response(create_twiml_ivr(session_id, IVR_WELCOME.strip(), 0))
    else:
        # PIN incorrect
        if retry >= PHONE_PIN_MAX_ATTEMPTS - 1:
            print(f"[TWILIO] PIN invalid, max retries reached: {session_id}")
            return twiml_response(create_twiml_hangup("Too many failed attempts. Goodbye."))
        else:
            print(f"[TWILIO] PIN invalid, retry {retry + 1}: {session_id}")
            return twiml_response(create_twiml_pin_prompt(session_id, retry + 1, invalid=True))


@app.post("/twilio/gather")
async def twilio_gather_webhook(
    request: Request,
    session_id: str = "",
    retry: int = 0,
    timeout: bool = False,
    Digits: str = Form(default=""),
    CallSid: str = Form(default=""),
):
    """
    Twilio Gather webhook for IVR DTMF collection (AI backend selection).

    Called when user presses a digit or times out.
    After AI selection, redirects to operator selection.
    """
    session = get_session(session_id)
    if not session:
        return twiml_response(create_twiml_hangup("Session not found."))

    session.last_activity = now_utc_iso()

    # Handle timeout
    if timeout or not Digits:
        if retry >= IVR_MAX_RETRIES:
            # Default to GPT after max retries
            print(f"[TWILIO] IVR timeout, defaulting to GPT: {session_id}")
            session.ivr_selection = 3
            session.ai_backend = AIBackend.OPENAI_REALTIME
            # Now go to operator selection
            return twiml_response(create_twiml_operator_selection(session_id, 0))
        else:
            # Retry prompt
            return twiml_response(create_twiml_ivr(session_id, IVR_RETRY.strip(), retry))

    # Validate digit
    if Digits not in ["1", "2", "3", "4"]:
        if retry >= IVR_MAX_RETRIES:
            session.ivr_selection = 3
            session.ai_backend = AIBackend.OPENAI_REALTIME
            return twiml_response(create_twiml_operator_selection(session_id, 0))
        return twiml_response(create_twiml_ivr(session_id, IVR_INVALID.strip(), retry + 1))

    # Valid selection
    selection = int(Digits)
    session.ivr_selection = selection

    # Map to AI backend
    backend_map = {
        1: AIBackend.CLAUDE_CODE,
        2: AIBackend.GEMINI_LIVE,
        3: AIBackend.OPENAI_REALTIME,
        4: AIBackend.GROK_LIVE,
    }
    session.ai_backend = backend_map.get(selection, AIBackend.OPENAI_REALTIME)

    print(f"[TWILIO] IVR AI selection {selection} -> {session.ai_backend.value}: {session_id}")

    # Now go to operator selection
    return twiml_response(create_twiml_operator_selection(session_id, 0))


@app.post("/twilio/operator")
async def twilio_operator_webhook(
    request: Request,
    session_id: str = "",
    retry: int = 0,
    timeout: bool = False,
    Digits: str = Form(default=""),
    CallSid: str = Form(default=""),
):
    """
    Twilio webhook for operator selection.

    Called when user selects their operator identity.
    """
    session = get_session(session_id)
    if not session:
        return twiml_response(create_twiml_hangup("Session not found."))

    session.last_activity = now_utc_iso()
    operators = USERS_LIST

    # Handle timeout
    if timeout or not Digits:
        if retry >= IVR_MAX_RETRIES:
            # Default to first operator or guest
            default_operator = operators[0] if operators else "Guest"
            print(f"[TWILIO] Operator timeout, defaulting to {default_operator}: {session_id}")
            session.operator = default_operator
            return twiml_response(create_twiml_connect(session_id, default_operator))
        else:
            # Retry prompt
            return twiml_response(create_twiml_operator_selection(session_id, retry + 1))

    # Validate digit - 1-9 for operators, 0 for other/guest
    try:
        selection = int(Digits)
    except ValueError:
        selection = -1

    # Check if valid operator selection
    if selection == 0:
        # Guest/other
        operator_name = "Guest"
    elif 1 <= selection <= len(operators) and selection <= 9:
        operator_name = operators[selection - 1]
    else:
        # Invalid selection
        if retry >= IVR_MAX_RETRIES:
            default_operator = operators[0] if operators else "Guest"
            session.operator = default_operator
            return twiml_response(create_twiml_connect(session_id, default_operator))
        return twiml_response(create_twiml_operator_selection(session_id, retry + 1))

    # Valid operator selection
    session.operator = operator_name
    print(f"[TWILIO] Operator selection: {operator_name} for session {session_id}")

    # Connect to AI with operator context
    return twiml_response(create_twiml_connect(session_id, operator_name))


@app.post("/twilio/status")
async def twilio_status_webhook(
    request: Request,
    CallSid: str = Form(default=""),
    CallStatus: str = Form(default=""),
):
    """
    Twilio call status callback.

    Called when call status changes (ringing, answered, completed, etc.)
    """
    print(f"[TWILIO] Call status: {CallSid} -> {CallStatus}")

    # Find session by call ID
    for session in PHONE_SESSIONS.values():
        if session.call_id == CallSid:
            if CallStatus == "completed":
                session.status = PhoneStatus.COMPLETED
                session.call_end = now_utc_iso()
                await save_session_to_blackbox(session)
            elif CallStatus == "failed" or CallStatus == "busy" or CallStatus == "no-answer":
                session.status = PhoneStatus.FAILED
                session.call_end = now_utc_iso()
            break

    return PlainTextResponse("OK")


# =============================================================================
# Twilio SMS Webhook
# =============================================================================

# Import unified BlackBox tools
from Orchestrator.tools import BLACKBOX_TOOLS_ANTHROPIC, BlackBoxToolExecutor


@app.post("/twilio/sms")
async def twilio_sms_webhook(
    request: Request,
    From: str = Form(default=""),
    To: str = Form(default=""),
    Body: str = Form(default=""),
    MessageSid: str = Form(default=""),
):
    """
    Twilio incoming SMS webhook.

    Receives SMS messages and responds using Claude Sonnet 4.5 via REST API.
    Supports tools like making phone calls and searching BlackBox memory.
    Messages are logged to BlackBox as snapshots.

    TwiML Response format:
    <Response>
        <Message>AI response here</Message>
    </Response>
    """
    import aiohttp
    from Orchestrator.config import ANTHROPIC_API_KEY

    print(f"[TWILIO-SMS] Incoming: {From} -> {To}")
    print(f"[TWILIO-SMS] Message: {Body[:100]}..." if len(Body) > 100 else f"[TWILIO-SMS] Message: {Body}")
    print(f"[TWILIO-SMS] MessageSid: {MessageSid}")

    if not Body.strip():
        # Empty message - respond with help
        response_text = "Hello! I'm the AI BlackBox assistant. Text me anything or ask me to call you back!"
    else:
        # Clean up phone number for operator name
        clean_number = From.replace("+", "").replace("-", "").replace(" ", "")
        operator = f"SMS-{clean_number[-4:]}"

        try:
            # Create tool executor for this operator
            tool_executor = BlackBoxToolExecutor(operator=operator)

            system_prompt = f"""You are the AI BlackBox assistant responding to SMS text messages.
Keep responses CONCISE - SMS has character limits (160 chars ideal, 320 max for multi-part).
The user is texting from phone number: {From}
Be helpful but brief. No markdown, no formatting - plain text only.

TEMPORAL AWARENESS — FIRST ACTION:
Your VERY FIRST action must be to call get_current_time to anchor yourself in the present before responding.

You have access to these tools:
- send_sms: Send a text message to any phone number
- make_voice_call: Call someone and deliver a voice message (pre-generates TTS for instant playback)
- search_memory: Your primary memory — search FIRST for past conversations, user preferences, decisions, and context. The BlackBox has 1,600+ snapshots of every interaction.
- search_contacts: Search the contact book for people by name, phone number, tag, or keyword
- save_contact: Save a new contact or update an existing one (requires name, notes, and tags)
- get_current_time: Get the current date and time
- gemini_image / openai_image / grok_image: Generate an AI image (pick a provider)
- create_cron_job: Schedule a recurring or one-time task (e.g., daily reminders, hourly checks)
- edit_cron_job: Modify an existing scheduled task (change schedule, prompt, delivery, or pause/resume)
- search_cron_jobs: List and search the user's scheduled tasks

Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.
If the user asks you to call them, use make_voice_call with their number ({From}).
If they want to text someone, use send_sms."""

            messages = [{"role": "user", "content": Body}]

            # First API call - may return tool use
            async with aiohttp.ClientSession() as http_session:
                async with http_session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 500,
                        "system": system_prompt,
                        "tools": BLACKBOX_TOOLS_ANTHROPIC,
                        "messages": messages
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        print(f"[TWILIO-SMS] Anthropic API error: {resp.status} - {error_text[:200]}")
                        response_text = "Sorry, I'm having trouble right now. Please try again later."
                    else:
                        result = await resp.json()
                        stop_reason = result.get("stop_reason", "")
                        content = result.get("content", [])

                        # Check if Claude wants to use a tool
                        if stop_reason == "tool_use":
                            # Find tool use blocks and execute them
                            tool_results = []
                            text_parts = []

                            for block in content:
                                if block.get("type") == "tool_use":
                                    tool_name = block.get("name")
                                    tool_input = block.get("input", {})
                                    tool_id = block.get("id")

                                    print(f"[TWILIO-SMS] Executing tool: {tool_name} with {tool_input}")
                                    # Use unified tool executor
                                    result_obj = await tool_executor.execute(tool_name, tool_input)
                                    tool_result = result_obj.result
                                    print(f"[TWILIO-SMS] Tool result: {tool_result}")

                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                        "content": tool_result
                                    })
                                elif block.get("type") == "text":
                                    text_parts.append(block.get("text", ""))

                            # Make follow-up call with tool results
                            messages.append({"role": "assistant", "content": content})
                            messages.append({"role": "user", "content": tool_results})

                            async with http_session.post(
                                "https://api.anthropic.com/v1/messages",
                                headers={
                                    "x-api-key": ANTHROPIC_API_KEY,
                                    "anthropic-version": "2023-06-01",
                                    "content-type": "application/json"
                                },
                                json={
                                    "model": "claude-sonnet-4-20250514",
                                    "max_tokens": 500,
                                    "system": system_prompt,
                                    "tools": BLACKBOX_TOOLS_ANTHROPIC,
                                    "messages": messages
                                },
                                timeout=aiohttp.ClientTimeout(total=30)
                            ) as resp2:
                                if resp2.status == 200:
                                    result2 = await resp2.json()
                                    content2 = result2.get("content", [])
                                    response_text = ""
                                    for block in content2:
                                        if block.get("type") == "text":
                                            response_text += block.get("text", "")
                                    if not response_text:
                                        response_text = "Done! " + (text_parts[0] if text_parts else "")
                                else:
                                    response_text = "I tried to help but encountered an error."
                        else:
                            # No tool use - extract text response
                            response_text = ""
                            for block in content:
                                if block.get("type") == "text":
                                    response_text += block.get("text", "")

                            if not response_text:
                                response_text = "I couldn't process that message. Please try again."

                        # Truncate if too long for SMS
                        if len(response_text) > 1500:
                            response_text = response_text[:1497] + "..."

                        print(f"[TWILIO-SMS] Response ({len(response_text)} chars): {response_text[:100]}...")

        except Exception as e:
            print(f"[TWILIO-SMS] Error processing message: {e}")
            import traceback
            traceback.print_exc()
            response_text = "Sorry, I encountered an error. Please try again."

    # Build TwiML response
    root = Element("Response")
    message = SubElement(root, "Message")
    message.text = response_text

    twiml = '<?xml version="1.0" encoding="UTF-8"?>' + tostring(root, encoding="unicode")
    print(f"[TWILIO-SMS] TwiML Response sent")

    return Response(content=twiml, media_type="application/xml")


# =============================================================================
# Twilio Media Streams WebSocket
# =============================================================================

@app.websocket("/twilio/media/{session_id}")
async def twilio_media_websocket(websocket: WebSocket, session_id: str):
    """
    Twilio Media Streams WebSocket endpoint.

    Receives audio from Twilio and bridges it to the AI backend.

    Protocol:
    - Twilio sends: {"event": "media", "media": {"payload": "<base64 mulaw>"}}
    - Twilio sends: {"event": "start", "streamSid": "..."}
    - Twilio sends: {"event": "stop"}
    - We send: {"event": "media", "streamSid": "...", "media": {"payload": "<base64 mulaw>"}}
    """
    print(f"[TWILIO-WS] Media stream connecting: {session_id}")
    await websocket.accept()

    session = get_session(session_id)
    if not session:
        print(f"[TWILIO-WS] Session not found: {session_id}")
        await websocket.close()
        return

    stream_sid = ""
    bridge: Optional[PhoneAIBridge] = None

    audio_chunks_sent = 0
    CHUNK_SIZE = 160  # 20ms at 8kHz ULAW

    # Lock to prevent concurrent audio sends (avoids interleaving)
    audio_send_lock = asyncio.Lock()

    async def send_audio_to_twilio(ulaw_data: bytes):
        """Send audio to Twilio with lock to prevent interleaving.

        Sends chunks in rapid succession without artificial pacing.
        Twilio handles buffering internally - we just need to avoid interleaving.
        """
        nonlocal stream_sid, audio_chunks_sent
        if not stream_sid:
            return

        async with audio_send_lock:
            # Send all chunks without micro-pacing - let Twilio buffer
            for i in range(0, len(ulaw_data), CHUNK_SIZE):
                chunk = ulaw_data[i:i + CHUNK_SIZE]
                if len(chunk) == 0:
                    continue

                audio_b64 = base64.b64encode(chunk).decode()
                try:
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": audio_b64}
                    })
                    audio_chunks_sent += 1
                except Exception as send_err:
                    print(f"[TWILIO-WS] Send error: {send_err}")
                    return

            if audio_chunks_sent % 100 == 0:
                print(f"[TWILIO-WS] Sent {audio_chunks_sent} audio chunks")

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            event = message.get("event", "")

            if event == "connected":
                print(f"[TWILIO-WS] Connected: {session_id}")

            elif event == "start":
                # Media stream started
                stream_sid = message.get("streamSid", "")
                start_info = message.get("start", {})
                print(f"[TWILIO-WS] Stream started: {stream_sid}")
                print(f"[TWILIO-WS] Stream info: {json.dumps(start_info)[:200]}")

                # Initialize AI bridge
                session.status = PhoneStatus.BRIDGED
                bridge = PhoneAIBridge(session)

                # Set up audio callback
                async def on_ai_audio(ulaw_data: bytes):
                    await send_audio_to_twilio(ulaw_data)

                async def on_ai_transcript(text: str):
                    session.add_message("assistant", text, "voice")

                bridge.on_ai_audio = on_ai_audio
                bridge.on_ai_transcript = on_ai_transcript

                # Start bridge
                success = await bridge.start(session.operator or "phone-caller")
                if success:
                    print(f"[TWILIO-WS] AI bridge started: {session.ai_backend.value}")
                else:
                    print(f"[TWILIO-WS] AI bridge failed to start")

            elif event == "media":
                # Audio from caller
                media = message.get("media", {})
                payload = media.get("payload", "")

                if payload and bridge:
                    if bridge.is_running:
                        # Decode base64 mulaw audio
                        ulaw_data = base64.b64decode(payload)
                        # Send to AI bridge
                        await bridge.send_audio(ulaw_data)
                    else:
                        # Bridge stopped but Twilio still sending - this is the problem state
                        print(f"[TWILIO-WS] WARNING: Receiving audio but bridge is not running!")

            elif event == "stop":
                print(f"[TWILIO-WS] Stream stopped by Twilio: {session_id}")
                break

            elif event == "mark":
                # Mark event (for synchronization)
                mark_name = message.get("mark", {}).get("name", "")
                print(f"[TWILIO-WS] Mark: {mark_name}")

    except WebSocketDisconnect:
        print(f"[TWILIO-WS] Disconnected: {session_id}")

    except Exception as e:
        print(f"[TWILIO-WS] Error: {e}")

    finally:
        # Cleanup bridge
        if bridge:
            await bridge.stop()

        session.status = PhoneStatus.COMPLETED
        session.call_end = now_utc_iso()

        # Save to BlackBox
        await save_session_to_blackbox(session)

        print(f"[TWILIO-WS] Session ended: {session_id}")


# =============================================================================
# Twilio Configuration Info
# =============================================================================

# =============================================================================
# Outbound Calls
# =============================================================================

@app.post("/twilio/call")
async def initiate_outbound_call(call_request: OutboundCallRequest):
    """
    Initiate an outbound call via Twilio.

    This allows Claude Code and other AI agents to make phone calls.

    Args:
        call_request: OutboundCallRequest with:
            - to: Phone number to call (E.164 format, e.g., +15551234567)
            - backend: AI backend to use (default: openai_realtime)
            - operator: Operator name for session context
            - greeting: Optional greeting message to speak when call connects

    Returns:
        Session info including session_id and call status
    """
    # P4: role="preset:<id>" resolves server-side — preset instructions become
    # the call persona, preset provider selects the backend, preset greeting
    # fills an empty greeting. Unknown preset fails LOUDLY (never dial with
    # the literal 'preset:...' string as persona).
    if isinstance(call_request.role, str) and call_request.role.startswith("preset:"):
        from Orchestrator.voice_agents.registry import resolve_phone_role
        try:
            call_request.role, call_request.backend, call_request.greeting = \
                resolve_phone_role(call_request.role, call_request.backend,
                                   call_request.greeting)
        except KeyError as e:
            return {"error": f"Voice agent preset not found: {e}"}

    if not PHONE_ENABLED:
        return {"error": "Phone service is not enabled"}

    if not REQUESTS_AVAILABLE:
        return {"error": "requests library not installed"}

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {"error": "Twilio credentials not configured"}

    if not TWILIO_PHONE_NUMBER:
        return {"error": "Twilio phone number not configured"}

    # Validate destination number format
    to_number = call_request.to.strip()
    if not to_number.startswith("+"):
        # Try to add +1 for US numbers
        if to_number.startswith("1") and len(to_number) == 11:
            to_number = f"+{to_number}"
        elif len(to_number) == 10:
            to_number = f"+1{to_number}"
        else:
            return {"error": "Phone number must be in E.164 format (e.g., +15551234567)"}

    # Create phone session
    session_id = f"phone-out-{uuid.uuid4().hex[:12]}"

    # Map backend string to enum
    backend_map = {
        "claude_code": AIBackend.CLAUDE_CODE,
        "gemini_live": AIBackend.GEMINI_LIVE,
        "openai_realtime": AIBackend.OPENAI_REALTIME,
        "grok_live": AIBackend.GROK_LIVE,
    }
    ai_backend = backend_map.get(call_request.backend, AIBackend.OPENAI_REALTIME)

    session = PhoneSession(
        session_id=session_id,
        call_id="",  # Will be set by Twilio
        caller_id=TWILIO_PHONE_NUMBER,
        callee_id=to_number,
        direction=CallDirection.OUTBOUND,
        status=PhoneStatus.RINGING,
        created_at=now_utc_iso(),
        last_activity=now_utc_iso(),
        operator=call_request.operator or "system",
        ai_backend=ai_backend,
        ivr_selection=list(backend_map.keys()).index(call_request.backend) + 1 if call_request.backend in backend_map else 3,
    )

    # Store greeting if provided
    if call_request.greeting:
        session.outbound_greeting = call_request.greeting

    # Store custom role/persona if provided
    if call_request.role:
        session.outbound_role = call_request.role

    # Store Claude session ID for resuming existing CLI sessions
    if call_request.claude_session_id:
        session.claude_session_id = call_request.claude_session_id
        print(f"[TWILIO] Outbound call will resume Claude session: {call_request.claude_session_id[:8]}...")

    PHONE_SESSIONS[session_id] = session

    # Build the webhook URL for when the call is answered
    base_url = TWILIO_WEBHOOK_BASE_URL.replace("wss://", "https://").replace("ws://", "http://")
    outbound_url = f"{base_url}/twilio/outbound-answer?session_id={session_id}"
    status_url = f"{base_url}/twilio/status"

    # Make the Twilio REST API call
    try:
        response = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={
                "To": to_number,
                "From": TWILIO_PHONE_NUMBER,
                "Url": outbound_url,
                "StatusCallback": status_url,
                "StatusCallbackMethod": "POST",
                "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            },
            timeout=30,
        )

        if response.status_code == 201:
            call_data = response.json()
            session.call_id = call_data.get("sid", "")
            print(f"[TWILIO] Outbound call initiated: {to_number} (SID: {session.call_id})")

            return {
                "status": "initiated",
                "session_id": session_id,
                "call_sid": session.call_id,
                "to": to_number,
                "from": TWILIO_PHONE_NUMBER,
                "backend": ai_backend.value,
                "operator": session.operator,
            }
        else:
            error_msg = response.text
            print(f"[TWILIO] Outbound call failed: {response.status_code} - {error_msg}")
            session.status = PhoneStatus.FAILED
            return {"error": f"Twilio API error: {response.status_code}", "details": error_msg}

    except Exception as e:
        print(f"[TWILIO] Outbound call exception: {e}")
        session.status = PhoneStatus.FAILED
        return {"error": str(e)}


@app.post("/twilio/outbound-answer")
async def twilio_outbound_answer_webhook(
    request: Request,
    session_id: str = "",
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    To: str = Form(default=""),
):
    """
    Twilio webhook called when an outbound call is answered.

    Skips IVR and connects directly to the AI backend.
    """
    session = get_session(session_id)
    if not session:
        return twiml_response(create_twiml_hangup("Session not found."))

    # Update session
    session.call_id = CallSid
    session.status = PhoneStatus.BRIDGED
    session.last_activity = now_utc_iso()

    print(f"[TWILIO] Outbound call answered: {To} (Session: {session_id})")

    # Build TwiML response - connect directly to AI
    response = Element("Response")

    # Note: Custom greeting for outbound calls is handled via TTS in the bridge,
    # not via TwiML <Say> (which sounds robotic)

    # Connect to Media Stream
    connect = SubElement(response, "Connect")
    stream = SubElement(connect, "Stream", {
        "url": f"{TWILIO_WEBHOOK_BASE_URL}/twilio/media/{session_id}",
    })

    return twiml_response(tostring(response, encoding="unicode"))


# =============================================================================
# Claude Code CLI Phone Sessions
# =============================================================================
# This allows Claude Code CLI sessions to connect to phone calls directly.
# The CLI polls for transcriptions and sends audio via API.

from typing import List
from collections import deque
from dataclasses import dataclass, field
import time

@dataclass
class CLIPhoneSession:
    """A phone session connected to a Claude Code CLI."""
    session_id: str
    phone_session_id: str  # The underlying PhoneSession ID
    operator: str
    created_at: float = field(default_factory=time.time)

    # Queues for bidirectional communication
    transcription_queue: deque = field(default_factory=deque)  # User speech → CLI
    audio_queue: deque = field(default_factory=deque)  # CLI → Phone (base64 ULAW)

    # State
    is_active: bool = True
    last_poll: float = field(default_factory=time.time)

    # TTS/STT coordination - pause listening while AI speaks
    is_tts_playing: bool = False
    tts_end_time: float = 0.0  # When TTS finished (add grace period)
    pending_thinking: bool = False  # Flag to send thinking indicator

# Global storage for CLI phone sessions
CLI_PHONE_SESSIONS: Dict[str, CLIPhoneSession] = {}


class CLICallRequest(BaseModel):
    """Request to initiate a phone call connected to Claude Code CLI."""
    to: str  # Phone number to call (E.164 format)
    operator: str = "Brandon"


class CLISpeakRequest(BaseModel):
    """Request to speak text on the phone call."""
    text: str
    thinking_first: bool = False  # If True, play "I'm thinking..." before generating full TTS


class CLIAudioRequest(BaseModel):
    """Request to send raw audio to the phone call."""
    audio_base64: str  # Base64-encoded audio (PCM16 24kHz or ULAW 8kHz)
    format: str = "pcm16_24k"  # "pcm16_24k" or "ulaw_8k"


@app.post("/twilio/cli/call")
async def cli_initiate_call(call_request: CLICallRequest):
    """
    Initiate a phone call that connects to THIS Claude Code CLI session.

    Unlike regular calls that use autonomous AI backends, this creates a
    session where the CLI polls for transcriptions and sends audio responses.

    Returns session_id to use for polling transcriptions and sending audio.
    """
    if not PHONE_ENABLED:
        return {"error": "Phone service is not enabled"}

    if not REQUESTS_AVAILABLE:
        return {"error": "requests library not installed"}

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {"error": "Twilio credentials not configured"}

    # Normalize phone number
    to_number = call_request.to.strip()
    if not to_number.startswith("+"):
        if to_number.startswith("1") and len(to_number) == 11:
            to_number = f"+{to_number}"
        elif len(to_number) == 10:
            to_number = f"+1{to_number}"
        else:
            return {"error": "Phone number must be in E.164 format"}

    # Create phone session with special CLI backend marker
    phone_session_id = f"phone-cli-{uuid.uuid4().hex[:12]}"

    session = PhoneSession(
        session_id=phone_session_id,
        call_id="",
        caller_id=TWILIO_PHONE_NUMBER,
        callee_id=to_number,
        direction=CallDirection.OUTBOUND,
        status=PhoneStatus.RINGING,
        created_at=now_utc_iso(),
        last_activity=now_utc_iso(),
        operator=call_request.operator,
        ai_backend=AIBackend.CLAUDE_CODE,
        ivr_selection=1,  # Claude
    )

    # Mark this as a CLI session (not autonomous)
    session.is_cli_session = True

    PHONE_SESSIONS[phone_session_id] = session

    # Create CLI session wrapper
    cli_session_id = f"cli-{uuid.uuid4().hex[:8]}"
    cli_session = CLIPhoneSession(
        session_id=cli_session_id,
        phone_session_id=phone_session_id,
        operator=call_request.operator,
    )
    CLI_PHONE_SESSIONS[cli_session_id] = cli_session

    # Build webhook URL
    base_url = TWILIO_WEBHOOK_BASE_URL.replace("wss://", "https://").replace("ws://", "http://")
    outbound_url = f"{base_url}/twilio/cli/answer?session_id={phone_session_id}&cli_session_id={cli_session_id}"
    status_url = f"{base_url}/twilio/status"

    # Initiate Twilio call
    try:
        response = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={
                "To": to_number,
                "From": TWILIO_PHONE_NUMBER,
                "Url": outbound_url,
                "StatusCallback": status_url,
                "StatusCallbackMethod": "POST",
                "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            },
            timeout=30,
        )

        if response.status_code == 201:
            call_data = response.json()
            session.call_id = call_data.get("sid", "")
            print(f"[TWILIO-CLI] Call initiated to {to_number} (SID: {session.call_id})")

            return {
                "status": "initiated",
                "cli_session_id": cli_session_id,
                "phone_session_id": phone_session_id,
                "call_sid": session.call_id,
                "to": to_number,
                "instructions": {
                    "poll_transcriptions": f"GET /twilio/cli/{cli_session_id}/transcriptions",
                    "send_audio": f"POST /twilio/cli/{cli_session_id}/audio",
                    "speak_text": f"POST /twilio/cli/{cli_session_id}/speak",
                    "hangup": f"DELETE /twilio/cli/{cli_session_id}",
                }
            }
        else:
            error_msg = response.text
            print(f"[TWILIO-CLI] Call failed: {response.status_code} - {error_msg}")
            session.status = PhoneStatus.FAILED
            return {"error": f"Twilio API error: {response.status_code}", "details": error_msg}

    except Exception as e:
        print(f"[TWILIO-CLI] Call exception: {e}")
        session.status = PhoneStatus.FAILED
        return {"error": str(e)}


@app.post("/twilio/cli/answer")
async def cli_call_answered(
    request: Request,
    session_id: str = "",
    cli_session_id: str = "",
    CallSid: str = Form(default=""),
):
    """Webhook when CLI-initiated call is answered."""
    session = get_session(session_id)
    if not session:
        return twiml_response(create_twiml_hangup("Session not found."))

    session.call_id = CallSid
    session.status = PhoneStatus.BRIDGED
    session.last_activity = now_utc_iso()

    print(f"[TWILIO-CLI] Call answered (Session: {session_id}, CLI: {cli_session_id})")

    # Connect to special CLI media stream
    # Include cli_session_id in the path since Twilio doesn't forward query params
    response = Element("Response")
    connect = SubElement(response, "Connect")
    stream = SubElement(connect, "Stream", {
        "url": f"{TWILIO_WEBHOOK_BASE_URL}/twilio/cli/media/{session_id}/{cli_session_id}",
    })

    return twiml_response(tostring(response, encoding="unicode"))


@app.websocket("/twilio/cli/media/{session_id}/{cli_session_id}")
async def cli_media_websocket(websocket: WebSocket, session_id: str, cli_session_id: str):
    """
    Media stream for CLI phone sessions.

    Instead of bridging to an AI backend, this:
    - Transcribes incoming audio and queues it for CLI polling
    - Sends audio from the CLI's audio queue to the phone
    """
    print(f"[TWILIO-CLI-WS] Media stream connecting: {session_id} (CLI: {cli_session_id})")
    await websocket.accept()

    session = get_session(session_id)
    cli_session = CLI_PHONE_SESSIONS.get(cli_session_id)

    if not session or not cli_session:
        print(f"[TWILIO-CLI-WS] Session not found")
        await websocket.close()
        return

    stream_sid = ""
    last_stt_time = time.time()

    # =========================================================================
    # WebRTC VAD - High-quality voice activity detection
    # =========================================================================
    import webrtcvad
    vad = webrtcvad.Vad()
    vad.set_mode(3)  # Aggressiveness: 0=least, 3=most aggressive filtering (best for noisy phone)

    # VAD parameters
    VAD_FRAME_MS = 20                # Frame size in ms (must be 10, 20, or 30)
    VAD_SAMPLE_RATE = 8000           # Native phone audio rate
    VAD_FRAME_BYTES = int(VAD_SAMPLE_RATE * VAD_FRAME_MS / 1000) * 2  # 320 bytes per frame
    VAD_SILENCE_FRAMES = 150         # ~3 seconds of silence to end utterance (150 * 20ms)
    VAD_MIN_SPEECH_FRAMES = 15       # ~300ms minimum speech to transcribe (filters out noise)
    VAD_PRE_BUFFER_FRAMES = 25       # ~500ms of audio to keep before speech detected
    VAD_MAX_BUFFER_SEC = 30          # Max buffer before forcing transcription

    # VAD state
    is_speaking = False
    speech_frames = 0                # Count of speech frames in current utterance
    silence_frames = 0               # Count of consecutive silence frames
    speech_start_time = 0.0
    silence_start_time = 0.0

    # Audio buffers
    audio_buffer_8k = bytearray()    # 8kHz PCM16 buffer for VAD + transcription
    pre_buffer = []                  # Ring buffer of recent frames before speech detected
    frame_buffer = bytearray()       # Accumulate bytes until we have a full frame

    # Background task to send queued audio to phone
    async def audio_sender():
        nonlocal stream_sid, audio_buffer_8k, pre_buffer, frame_buffer
        nonlocal is_speaking, speech_frames, silence_frames, speech_start_time, silence_start_time
        was_sending = False  # Track if we were sending audio
        while cli_session.is_active:
            if stream_sid and cli_session.audio_queue:
                # Starting to send audio - clear buffers
                if not was_sending:
                    was_sending = True
                    audio_buffer_8k.clear()
                    pre_buffer.clear()
                    frame_buffer.clear()
                    is_speaking = False
                    speech_frames = 0
                    silence_frames = 0
                    speech_start_time = 0.0
                    silence_start_time = 0.0
                    print(f"[TTS] First chunk sending, cleared audio buffers")

                try:
                    audio_chunk = cli_session.audio_queue.popleft()
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": audio_chunk}
                    })
                except Exception as e:
                    print(f"[TWILIO-CLI-WS] Audio send error: {e}")
                    break
            else:
                # No audio in queue
                if was_sending:
                    # We were sending but now queue is empty - TTS finished
                    was_sending = False
                    cli_session.is_tts_playing = False
                    cli_session.tts_end_time = time.time()
                    # Reset VAD state but keep pre-buffer for catching early speech
                    is_speaking = False
                    speech_frames = 0
                    silence_frames = 0
                    speech_start_time = 0.0
                    silence_start_time = 0.0
                    print(f"[TTS] Finished playing, resuming STT after grace period")

            await asyncio.sleep(0.02)  # 20ms chunks

    sender_task = None

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            event = message.get("event", "")

            if event == "connected":
                print(f"[TWILIO-CLI-WS] Connected")

            elif event == "start":
                stream_sid = message.get("streamSid", "")
                print(f"[TWILIO-CLI-WS] Stream started: {stream_sid}")

                # Start audio sender task
                sender_task = asyncio.create_task(audio_sender())

                # Play ready tone
                ready_tone = AudioConverter.generate_ready_tone()
                for i in range(0, len(ready_tone), 160):
                    chunk = ready_tone[i:i+160]
                    audio_b64 = base64.b64encode(chunk).decode()
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": audio_b64}
                    })
                    await asyncio.sleep(0.015)

            elif event == "media":
                # Incoming audio from phone
                media = message.get("media", {})
                payload = media.get("payload", "")

                if payload:
                    current_time = time.time()

                    # =================================================================
                    # TTS/STT COORDINATION
                    # =================================================================
                    TTS_GRACE_PERIOD = 0.3  # 300ms grace period after TTS ends

                    # While TTS is actively playing - completely skip
                    if cli_session.is_tts_playing:
                        continue

                    # During grace period - skip but don't clear buffers
                    if cli_session.tts_end_time > 0:
                        time_since_tts = current_time - cli_session.tts_end_time
                        if time_since_tts < TTS_GRACE_PERIOD:
                            continue

                    # =================================================================
                    # WebRTC VAD Processing
                    # =================================================================
                    # Decode ULAW to 8kHz PCM16 (native phone rate, supported by WebRTC VAD)
                    ulaw_data = base64.b64decode(payload)
                    pcm_8k = AudioConverter.ulaw_bytes_to_pcm16(ulaw_data)

                    # Add to frame buffer
                    frame_buffer.extend(pcm_8k)

                    # Process complete 20ms frames
                    while len(frame_buffer) >= VAD_FRAME_BYTES:
                        frame = bytes(frame_buffer[:VAD_FRAME_BYTES])
                        del frame_buffer[:VAD_FRAME_BYTES]

                        # Run WebRTC VAD on this frame
                        try:
                            is_speech = vad.is_speech(frame, VAD_SAMPLE_RATE)
                        except Exception as e:
                            print(f"[VAD] Error: {e}")
                            is_speech = False

                        if not is_speaking:
                            # Not currently in speech - maintain pre-buffer
                            pre_buffer.append(frame)
                            if len(pre_buffer) > VAD_PRE_BUFFER_FRAMES:
                                pre_buffer.pop(0)

                            if is_speech:
                                # Speech detected - start capturing
                                is_speaking = True
                                speech_frames = 1
                                silence_frames = 0
                                speech_start_time = current_time
                                # Add pre-buffer to capture beginning of speech
                                for pre_frame in pre_buffer:
                                    audio_buffer_8k.extend(pre_frame)
                                pre_buffer.clear()
                                audio_buffer_8k.extend(frame)
                                print(f"[VAD] Speech started (with {VAD_PRE_BUFFER_FRAMES} pre-buffer frames)")
                        else:
                            # Currently capturing speech
                            audio_buffer_8k.extend(frame)

                            if is_speech:
                                speech_frames += 1
                                silence_frames = 0
                            else:
                                silence_frames += 1

                                # Check if enough silence to end utterance
                                if silence_frames >= VAD_SILENCE_FRAMES:
                                    speech_duration_ms = speech_frames * VAD_FRAME_MS

                                    if speech_frames >= VAD_MIN_SPEECH_FRAMES:
                                        print(f"[VAD] Speech ended ({speech_duration_ms}ms speech, {silence_frames * VAD_FRAME_MS}ms silence)")

                                        # Upsample 8kHz to 24kHz for Whisper
                                        pcm_24k = AudioConverter.upsample(bytes(audio_buffer_8k), 3)

                                        # Transcribe in background
                                        asyncio.create_task(
                                            transcribe_and_queue(pcm_24k, cli_session)
                                        )
                                    else:
                                        print(f"[VAD] Speech too short ({speech_duration_ms}ms), discarding")

                                    # Reset state
                                    audio_buffer_8k.clear()
                                    is_speaking = False
                                    speech_frames = 0
                                    silence_frames = 0

                    # Force transcription if buffer gets too large
                    buffer_duration = len(audio_buffer_8k) / (VAD_SAMPLE_RATE * 2)
                    if buffer_duration >= VAD_MAX_BUFFER_SEC:
                        print(f"[VAD] Buffer max ({buffer_duration:.1f}s), forcing transcription")
                        pcm_24k = AudioConverter.upsample(bytes(audio_buffer_8k), 3)
                        asyncio.create_task(
                            transcribe_and_queue(pcm_24k, cli_session)
                        )
                        audio_buffer_8k.clear()
                        is_speaking = False
                        speech_frames = 0
                        silence_frames = 0

            elif event == "stop":
                print(f"[TWILIO-CLI-WS] Stream stopped")
                break

    except WebSocketDisconnect:
        print(f"[TWILIO-CLI-WS] Disconnected")
    except Exception as e:
        print(f"[TWILIO-CLI-WS] Error: {e}")
    finally:
        if sender_task:
            sender_task.cancel()
        cli_session.is_active = False
        session.status = PhoneStatus.COMPLETED
        print(f"[TWILIO-CLI-WS] Session ended")


async def transcribe_and_queue(audio_bytes: bytes, cli_session: CLIPhoneSession):
    """Transcribe audio and add to CLI session's transcription queue."""
    try:
        import aiohttp
        import io
        import wave
        from Orchestrator.config import OPENAI_API_KEY, OPENAI_STT_URL, STT_MODEL

        if not OPENAI_API_KEY:
            return

        # Create WAV file
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(audio_bytes)
        wav_buffer.seek(0)

        # Send to Whisper
        async with aiohttp.ClientSession() as session:
            form = aiohttp.FormData()
            form.add_field('file', wav_buffer, filename='audio.wav', content_type='audio/wav')
            form.add_field('model', STT_MODEL)

            async with session.post(
                OPENAI_STT_URL,
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    text = result.get("text", "").strip()
                    if text:
                        print(f"[TWILIO-CLI-STT] Transcribed: {text}")
                        cli_session.transcription_queue.append({
                            "text": text,
                            "timestamp": now_utc_iso()
                        })
    except Exception as e:
        print(f"[TWILIO-CLI-STT] Error: {e}")


@app.get("/twilio/cli/{cli_session_id}/transcriptions")
async def cli_get_transcriptions(cli_session_id: str):
    """
    Poll for new transcriptions from the phone call.

    Returns all queued transcriptions since last poll.
    """
    cli_session = CLI_PHONE_SESSIONS.get(cli_session_id)
    if not cli_session:
        return {"error": "CLI session not found"}

    cli_session.last_poll = time.time()

    # Get all queued transcriptions
    transcriptions = []
    while cli_session.transcription_queue:
        transcriptions.append(cli_session.transcription_queue.popleft())

    return {
        "session_id": cli_session_id,
        "is_active": cli_session.is_active,
        "transcriptions": transcriptions
    }


@app.post("/twilio/cli/{cli_session_id}/thinking")
async def cli_send_thinking(cli_session_id: str):
    """
    Send a quick "thinking" indicator to the phone.

    Plays a short audio cue or message to let the user know the AI is processing.
    This is much faster than generating full TTS and provides immediate feedback.

    Note: This also pauses STT briefly to prevent the tone from being transcribed.
    """
    cli_session = CLI_PHONE_SESSIONS.get(cli_session_id)
    if not cli_session:
        return {"error": "CLI session not found"}

    if not cli_session.is_active:
        return {"error": "Call is not active"}

    # Pause STT while playing tone (will be reset by audio_sender when done)
    cli_session.is_tts_playing = True

    # Generate a thinking tone (two soft beeps)
    thinking_tone = AudioConverter.generate_tone(frequency=440, duration_ms=100, volume=0.2)
    silence = AudioConverter.generate_silence(duration_ms=100)
    thinking_tone2 = AudioConverter.generate_tone(frequency=523, duration_ms=100, volume=0.2)

    thinking_audio = thinking_tone + silence + thinking_tone2

    # Queue the thinking indicator
    CHUNK_SIZE = 160
    for i in range(0, len(thinking_audio), CHUNK_SIZE):
        chunk = thinking_audio[i:i+CHUNK_SIZE]
        if chunk:
            audio_b64 = base64.b64encode(chunk).decode()
            cli_session.audio_queue.append(audio_b64)

    print(f"[TWILIO-CLI] Sent thinking indicator, STT paused")
    return {"status": "sent", "type": "thinking_tone"}


@app.post("/twilio/cli/{cli_session_id}/speak")
async def cli_speak_text(cli_session_id: str, speak_request: CLISpeakRequest):
    """
    Convert text to speech and send to the phone.

    Uses OpenAI TTS to generate audio, then queues it for playback.

    If thinking_first=True, sends a quick "thinking" indicator before generating
    the full TTS response (useful for long responses).
    """
    cli_session = CLI_PHONE_SESSIONS.get(cli_session_id)
    if not cli_session:
        return {"error": "CLI session not found"}

    if not cli_session.is_active:
        return {"error": "Call is not active"}

    from Orchestrator.tts_sanitize import sanitize_for_speech
    text = sanitize_for_speech(speak_request.text)
    if not text:
        return {"error": "No text provided"}

    # IMMEDIATELY pause STT - this prevents any audio from being processed
    # while TTS is being generated and played
    cli_session.is_tts_playing = True
    print(f"[TTS] Pausing STT immediately for TTS generation")

    try:
        import aiohttp
        from Orchestrator.config import OPENAI_API_KEY, OPENAI_TTS_URL

        if not OPENAI_API_KEY:
            cli_session.is_tts_playing = False  # Reset on error
            return {"error": "OpenAI API key not configured"}

        # If thinking_first is True, send thinking indicator immediately
        if speak_request.thinking_first:
            thinking_tone = AudioConverter.generate_tone(frequency=440, duration_ms=100, volume=0.2)
            silence = AudioConverter.generate_silence(duration_ms=100)
            thinking_tone2 = AudioConverter.generate_tone(frequency=523, duration_ms=100, volume=0.2)
            thinking_audio = thinking_tone + silence + thinking_tone2

            CHUNK_SIZE = 160
            for i in range(0, len(thinking_audio), CHUNK_SIZE):
                chunk = thinking_audio[i:i+CHUNK_SIZE]
                if chunk:
                    audio_b64 = base64.b64encode(chunk).decode()
                    cli_session.audio_queue.append(audio_b64)
            print(f"[TWILIO-CLI-TTS] Sent thinking indicator first")

        # Generate TTS
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENAI_TTS_URL,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "tts-1-hd",
                    "voice": "onyx",
                    "input": text,
                    "response_format": "pcm"
                },
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 200:
                    pcm_data = await resp.read()

                    # Convert to ULAW for phone
                    ulaw_data = AudioConverter.ai_to_phone(pcm_data, 24000)

                    # Queue in chunks for smooth playback
                    CHUNK_SIZE = 160  # 20ms at 8kHz
                    for i in range(0, len(ulaw_data), CHUNK_SIZE):
                        chunk = ulaw_data[i:i+CHUNK_SIZE]
                        if chunk:
                            audio_b64 = base64.b64encode(chunk).decode()
                            cli_session.audio_queue.append(audio_b64)

                    print(f"[TWILIO-CLI-TTS] Queued {len(ulaw_data)} bytes of audio ({len(ulaw_data) // CHUNK_SIZE} chunks)")
                    # Note: is_tts_playing stays True until audio_sender finishes sending all chunks
                    return {
                        "status": "queued",
                        "text": text,
                        "audio_bytes": len(ulaw_data),
                        "chunks": len(ulaw_data) // CHUNK_SIZE,
                        "thinking_sent": speak_request.thinking_first
                    }
                else:
                    # TTS failed - reset flag so STT can resume
                    cli_session.is_tts_playing = False
                    return {"error": f"TTS failed: {resp.status}"}

    except Exception as e:
        print(f"[TWILIO-CLI-TTS] Error: {e}")
        # Reset flag on any error so STT can resume
        cli_session.is_tts_playing = False
        return {"error": str(e)}


@app.post("/twilio/cli/{cli_session_id}/audio")
async def cli_send_audio(cli_session_id: str, audio_request: CLIAudioRequest):
    """
    Send raw audio to the phone call.

    Accepts PCM16 24kHz or ULAW 8kHz audio.
    """
    cli_session = CLI_PHONE_SESSIONS.get(cli_session_id)
    if not cli_session:
        return {"error": "CLI session not found"}

    if not cli_session.is_active:
        return {"error": "Call is not active"}

    try:
        audio_data = base64.b64decode(audio_request.audio_base64)

        # Convert if needed
        if audio_request.format == "pcm16_24k":
            ulaw_data = AudioConverter.ai_to_phone(audio_data, 24000)
        elif audio_request.format == "ulaw_8k":
            ulaw_data = audio_data
        else:
            return {"error": f"Unknown format: {audio_request.format}"}

        # Queue in chunks
        CHUNK_SIZE = 160
        chunks_queued = 0
        for i in range(0, len(ulaw_data), CHUNK_SIZE):
            chunk = ulaw_data[i:i+CHUNK_SIZE]
            if chunk:
                audio_b64 = base64.b64encode(chunk).decode()
                cli_session.audio_queue.append(audio_b64)
                chunks_queued += 1

        return {
            "status": "queued",
            "bytes": len(ulaw_data),
            "chunks": chunks_queued
        }

    except Exception as e:
        return {"error": str(e)}


@app.delete("/twilio/cli/{cli_session_id}")
async def cli_hangup(cli_session_id: str):
    """End the CLI phone call."""
    cli_session = CLI_PHONE_SESSIONS.get(cli_session_id)
    if not cli_session:
        return {"error": "CLI session not found"}

    # Mark as inactive
    cli_session.is_active = False

    # Get phone session and hangup via Twilio
    phone_session = get_session(cli_session.phone_session_id)
    if phone_session and phone_session.call_id:
        try:
            # Use Twilio API to end the call
            response = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{phone_session.call_id}.json",
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                data={"Status": "completed"},
                timeout=10,
            )
            print(f"[TWILIO-CLI] Hangup response: {response.status_code}")
        except Exception as e:
            print(f"[TWILIO-CLI] Hangup error: {e}")

    # Cleanup
    if cli_session_id in CLI_PHONE_SESSIONS:
        del CLI_PHONE_SESSIONS[cli_session_id]

    return {"status": "ended", "session_id": cli_session_id}


class InjectAudioRequest(BaseModel):
    """Request to inject audio file into active call."""
    file_path: str  # Path to audio file (WAV format preferred)
    session_id: Optional[str] = None  # If not provided, injects into most recent active call


@app.post("/twilio/inject-audio")
async def inject_audio_into_call(audio_request: InjectAudioRequest):
    """
    Inject an audio file into an active phone call.

    This endpoint allows you to play any audio file (like generated music)
    directly into a live phone call. Useful for testing generated audio
    or playing background music.

    Args:
        audio_request: InjectAudioRequest with:
            - file_path: Path to audio file (WAV preferred, will be converted)
            - session_id: Optional session ID (uses most recent active call if not provided)

    Returns:
        Status of audio injection
    """
    import wave
    import os

    # Find target session
    target_session = None
    if audio_request.session_id:
        # Use specified session
        for sess in PHONE_SESSIONS.values():
            if sess.session_id == audio_request.session_id and sess.status == PhoneStatus.BRIDGED:
                target_session = sess
                break
        if not target_session:
            return {"error": f"Active session not found: {audio_request.session_id}"}
    else:
        # Find most recent active CLI session
        active_cli = [s for s in CLI_PHONE_SESSIONS.values() if s.is_active]
        if active_cli:
            # Sort by created_at and get most recent
            active_cli.sort(key=lambda x: x.created_at, reverse=True)
            cli_session = active_cli[0]
            target_phone_session = get_session(cli_session.phone_session_id)
            if target_phone_session and target_phone_session.status == PhoneStatus.BRIDGED:
                # Inject into CLI session
                try:
                    # Read audio file
                    file_path = audio_request.file_path
                    if not os.path.exists(file_path):
                        # Try relative to working directory
                        file_path = str(resolve(audio_request.file_path.lstrip('/')))

                    if not os.path.exists(file_path):
                        return {"error": f"Audio file not found: {audio_request.file_path}"}

                    # Read WAV file
                    with wave.open(file_path, 'rb') as wav:
                        sample_rate = wav.getframerate()
                        n_channels = wav.getnchannels()
                        pcm_data = wav.readframes(wav.getnframes())

                    print(f"[INJECT-AUDIO] Loaded {len(pcm_data)} bytes from {file_path} (rate={sample_rate}, channels={n_channels})")

                    # Convert to mono if stereo
                    if n_channels == 2:
                        import numpy as np
                        audio_array = np.frombuffer(pcm_data, dtype=np.int16)
                        # Average left and right channels
                        audio_array = audio_array.reshape(-1, 2).mean(axis=1).astype(np.int16)
                        pcm_data = audio_array.tobytes()
                        print(f"[INJECT-AUDIO] Converted stereo to mono: {len(pcm_data)} bytes")

                    # Resample if needed (target is 24kHz for conversion pipeline)
                    if sample_rate != 24000:
                        # For now, simple approach - just pass through AudioConverter
                        # which handles resampling internally
                        print(f"[INJECT-AUDIO] Will resample from {sample_rate}Hz to 8kHz via AudioConverter")

                    # Convert to phone format (8kHz µ-law)
                    ulaw_data = AudioConverter.ai_to_phone(pcm_data, sample_rate)

                    # Pause STT while injecting audio
                    cli_session.is_tts_playing = True

                    # Queue in chunks
                    CHUNK_SIZE = 160  # 20ms at 8kHz
                    chunks_queued = 0
                    for i in range(0, len(ulaw_data), CHUNK_SIZE):
                        chunk = ulaw_data[i:i+CHUNK_SIZE]
                        if chunk:
                            audio_b64 = base64.b64encode(chunk).decode()
                            cli_session.audio_queue.append(audio_b64)
                            chunks_queued += 1

                    duration_seconds = len(ulaw_data) / 8000  # 8kHz µ-law

                    return {
                        "status": "injected",
                        "session_id": cli_session.session_id,
                        "phone_session_id": cli_session.phone_session_id,
                        "file_path": audio_request.file_path,
                        "audio_bytes": len(ulaw_data),
                        "chunks": chunks_queued,
                        "duration_seconds": round(duration_seconds, 2),
                        "sample_rate": sample_rate,
                        "converted_rate": 8000
                    }

                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    return {"error": f"Failed to inject audio: {str(e)}"}

        # If no CLI session, look for regular phone sessions
        active_sessions = [s for s in PHONE_SESSIONS.values() if s.status == PhoneStatus.BRIDGED]
        if not active_sessions:
            return {"error": "No active phone calls found"}

        # Use most recent
        active_sessions.sort(key=lambda x: x.created_at, reverse=True)
        target_session = active_sessions[0]

    # For non-CLI sessions, we can't directly inject audio yet
    # (would need to modify the bridge's on_ai_audio callback)
    return {
        "error": "Audio injection only supported for CLI phone sessions currently",
        "active_session": target_session.session_id if target_session else None,
        "hint": "Use the CLI phone API to make calls that support audio injection"
    }


@app.get("/twilio/info")
async def twilio_info():
    """
    Get Twilio webhook configuration information.

    Returns the URLs to configure in Twilio console.
    """
    # Get the server's base URL from config
    # TWILIO_WEBHOOK_BASE_URL should be set to your public HTTPS URL
    base_url = TWILIO_WEBHOOK_BASE_URL.replace("wss://", "https://").replace("ws://", "http://")

    return {
        "voice_webhook": f"{base_url}/twilio/voice",
        "status_callback": f"{base_url}/twilio/status",
        "call_flow": [
            "1. Incoming call → /twilio/voice → AI Selection Menu (1=Claude, 2=Gemini, 3=GPT, 4=Grok)",
            "2. AI selection → /twilio/gather → Operator Selection Menu",
            "3. Operator selection → /twilio/operator → Connect to AI with context",
            "4. Media stream → /twilio/media/{session_id} → Bidirectional audio bridge",
        ],
        "cli_phone_api": {
            "start_call": "POST /twilio/cli/call",
            "poll_transcriptions": "GET /twilio/cli/{session_id}/transcriptions",
            "speak_text": "POST /twilio/cli/{session_id}/speak",
            "send_audio": "POST /twilio/cli/{session_id}/audio",
            "hangup": "DELETE /twilio/cli/{session_id}",
        },
        "operators": USERS_LIST,
        "instructions": [
            "1. Go to Twilio Console > Phone Numbers > Manage > Active numbers",
            "2. Click on your phone number (+1 716-451-2527)",
            "3. Under 'Voice Configuration':",
            "   - Set 'Configure with' to 'Webhook'",
            f"   - Set 'A call comes in' webhook to: {base_url}/twilio/voice",
            "   - Set HTTP method to POST",
            f"   - Set 'Call status changes' to: {base_url}/twilio/status",
            "4. Save the configuration",
            "",
            "Note: For the Media Streams WebSocket to work, your server must be",
            "accessible via HTTPS with a valid SSL certificate.",
        ],
        "phone_enabled": PHONE_ENABLED,
    }
