#!/usr/bin/env python3
"""
phone_routes.py - Phone Integration API Endpoints

Provides REST and WebSocket endpoints for phone call management:
- GET /phone/status - Phone system health check
- GET /phone/sessions - List active calls
- GET /phone/session/{session_id} - Get call details
- POST /phone/call - Initiate outbound call
- POST /phone/hangup/{session_id} - End a call
- POST /phone/webhook/3cx - 3CX event webhooks
- WS /ws/phone/{session_id} - Audio bridge WebSocket
"""

import asyncio
import json
import uuid
import base64
from typing import Optional, Dict, Any, List
from pydantic import BaseModel

from fastapi import WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse

# Local imports
from Orchestrator.checkpoint import app
from Orchestrator.volume import now_utc_iso
from Orchestrator.phone.session import (
    PhoneSession,
    PhoneStatus,
    AIBackend,
    CallDirection,
    PHONE_SESSIONS,
    get_session,
    get_session_by_call_id,
    get_session_by_freeswitch_uuid,
    get_active_sessions,
    cleanup_session,
)
from Orchestrator.phone.audio_converter import AudioConverter
from Orchestrator.phone.dtmf_handler import IVRState, DTMFEvent, parse_freeswitch_dtmf
from Orchestrator.phone.bridge import PhoneAIBridge, save_session_to_blackbox
from Orchestrator.phone.sip_client import (
    FreeSwitchClient,
    get_freeswitch_client,
    init_freeswitch,
    GREENSWITCH_AVAILABLE,
)
from Orchestrator.phone.ivr_prompts import (
    IVR_WELCOME,
    get_confirmation_prompt,
    get_backend_id,
    IVR_TTS_VOICE,
)
from Orchestrator.config import (
    PHONE_ENABLED,
    DRACHTIO_HOST,
    DRACHTIO_PORT,
    DRACHTIO_SECRET,
    FREESWITCH_HOST,
    FREESWITCH_ESL_PORT,
    FREESWITCH_ESL_PASSWORD,
    IVR_TIMEOUT_MS,
    IVR_MAX_RETRIES,
    IVR_DEFAULT_BACKEND,
    PHONE_SAMPLE_RATE,
    PHONE_AUDIO_FORMAT,
    PBX_OUTBOUND_CALLER_ID,
)


# =============================================================================
# Request/Response Models
# =============================================================================

class OutboundCallRequest(BaseModel):
    """Request to initiate an outbound call."""
    to: str  # E.164 phone number
    operator: str = ""  # BlackBox operator
    backend: str = "openai_realtime"  # AI backend to use
    message: Optional[str] = None  # Optional greeting message


class PhoneSessionResponse(BaseModel):
    """Phone session response."""
    session_id: str
    call_id: str
    caller_id: str
    callee_id: str
    direction: str
    status: str
    ivr_selection: int
    ai_backend: str
    ai_session_id: str
    operator: str
    created_at: str
    last_activity: str
    call_start: str
    call_end: str
    message_count: int


class PhoneStatusResponse(BaseModel):
    """Phone system status response."""
    enabled: bool
    freeswitch_connected: bool
    greenswitch_available: bool
    active_sessions: int
    config: Dict[str, Any]


# =============================================================================
# HTTP Endpoints
# =============================================================================

@app.get("/phone/status")
async def phone_status() -> PhoneStatusResponse:
    """
    Get phone system status.

    Returns phone integration health and configuration.
    """
    freeswitch = get_freeswitch_client()

    return PhoneStatusResponse(
        enabled=PHONE_ENABLED,
        freeswitch_connected=freeswitch.is_connected if freeswitch else False,
        greenswitch_available=GREENSWITCH_AVAILABLE,
        active_sessions=len(get_active_sessions()),
        config={
            "freeswitch_host": FREESWITCH_HOST,
            "freeswitch_port": FREESWITCH_ESL_PORT,
            "drachtio_host": DRACHTIO_HOST,
            "drachtio_port": DRACHTIO_PORT,
            "ivr_timeout_ms": IVR_TIMEOUT_MS,
            "ivr_max_retries": IVR_MAX_RETRIES,
            "ivr_default_backend": IVR_DEFAULT_BACKEND,
            "phone_sample_rate": PHONE_SAMPLE_RATE,
            "phone_audio_format": PHONE_AUDIO_FORMAT,
        }
    )


@app.get("/phone/sessions")
async def list_phone_sessions() -> Dict[str, Any]:
    """
    List all phone sessions.

    Returns both active and recently completed sessions.
    """
    sessions = []
    for session in PHONE_SESSIONS.values():
        sessions.append(session.to_dict())

    return {
        "sessions": sessions,
        "active_count": len(get_active_sessions()),
        "total_count": len(sessions),
    }


@app.get("/phone/session/{session_id}")
async def get_phone_session(session_id: str) -> Dict[str, Any]:
    """
    Get details of a specific phone session.

    Args:
        session_id: Session ID
    """
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    return {
        "session": session.to_dict(),
        "conversation": session.conversation,
    }


@app.post("/phone/call")
async def initiate_outbound_call(request: OutboundCallRequest) -> Dict[str, Any]:
    """
    Initiate an outbound phone call.

    The call will use the specified AI backend and optionally
    deliver a greeting message.
    """
    if not PHONE_ENABLED:
        raise HTTPException(status_code=503, detail="Phone integration not enabled")

    freeswitch = get_freeswitch_client()
    if not freeswitch or not freeswitch.is_connected:
        raise HTTPException(status_code=503, detail="FreeSwitch not connected")

    # Validate backend
    backend_map = {
        "claude_code": AIBackend.CLAUDE_CODE,
        "gemini_live": AIBackend.GEMINI_LIVE,
        "openai_realtime": AIBackend.OPENAI_REALTIME,
        "grok_live": AIBackend.GROK_LIVE,
    }
    if request.backend not in backend_map:
        raise HTTPException(status_code=400, detail=f"Invalid backend: {request.backend}")

    # Create session
    session_id = f"phone-{uuid.uuid4().hex[:12]}"
    session = PhoneSession(
        session_id=session_id,
        callee_id=request.to,
        direction=CallDirection.OUTBOUND,
        status=PhoneStatus.RINGING,
        ai_backend=backend_map[request.backend],
        operator=request.operator or "system",
        created_at=now_utc_iso(),
        last_activity=now_utc_iso(),
    )
    PHONE_SESSIONS[session_id] = session

    # Originate call via FreeSwitch
    try:
        channel_uuid = await freeswitch.originate_call(
            destination=request.to,
            caller_id=PBX_OUTBOUND_CALLER_ID,
        )

        if channel_uuid:
            session.freeswitch_uuid = channel_uuid
            session.call_id = channel_uuid

            return {
                "success": True,
                "session_id": session_id,
                "call_id": channel_uuid,
                "status": "ringing",
                "backend": request.backend,
            }
        else:
            session.status = PhoneStatus.FAILED
            raise HTTPException(status_code=500, detail="Failed to originate call")

    except Exception as e:
        session.status = PhoneStatus.FAILED
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/phone/hangup/{session_id}")
async def hangup_call(session_id: str) -> Dict[str, Any]:
    """
    Hang up a phone call.

    Args:
        session_id: Session ID to hang up
    """
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")

    freeswitch = get_freeswitch_client()
    if freeswitch and session.freeswitch_uuid:
        await freeswitch.hangup_call(session.freeswitch_uuid)

    session.status = PhoneStatus.COMPLETED
    session.call_end = now_utc_iso()

    # Save to BlackBox
    await save_session_to_blackbox(session)

    return {
        "success": True,
        "session_id": session_id,
        "status": "completed",
    }


@app.get("/phone/cli-session/{operator}")
async def get_operator_cli_session(operator: str) -> Dict[str, Any]:
    """
    Get the persistent CLI session for an operator.

    Returns the Claude Code CLI session that persists across phone calls.
    This is the SAME session used by the Portal UI agent.
    """
    from Orchestrator.state import PERSISTED_AGENT_SESSIONS

    session = PERSISTED_AGENT_SESSIONS.get(operator)
    if not session:
        return {
            "operator": operator,
            "has_session": False,
            "session": None
        }

    return {
        "operator": operator,
        "has_session": True,
        "session": {
            "claude_session_id": session.get("claude_session_id", ""),
            "message_count": session.get("message_count", 0),
            "model": session.get("model", ""),
            "last_activity": session.get("last_activity", ""),
            "last_caller_id": session.get("last_caller_id", ""),
        }
    }


@app.delete("/phone/cli-session/{operator}")
async def clear_operator_cli_session(operator: str) -> Dict[str, Any]:
    """
    Clear/reset an operator's persistent CLI session.

    This ends the Claude Code session - the next phone call or Portal
    interaction will start a fresh session.
    """
    from Orchestrator.state import PERSISTED_AGENT_SESSIONS, save_operator_state

    if operator not in PERSISTED_AGENT_SESSIONS:
        return {
            "success": False,
            "message": f"No session found for operator: {operator}"
        }

    old_session = PERSISTED_AGENT_SESSIONS[operator]
    old_id = old_session.get("claude_session_id", "")[:8]
    msg_count = old_session.get("message_count", 0)

    del PERSISTED_AGENT_SESSIONS[operator]
    save_operator_state()

    print(f"[PHONE] Cleared CLI session for {operator}: {old_id}... ({msg_count} messages)")

    return {
        "success": True,
        "message": f"Cleared CLI session for {operator}",
        "cleared_session": {
            "claude_session_id": old_id + "...",
            "message_count": msg_count
        }
    }


@app.post("/phone/webhook/3cx")
async def handle_3cx_webhook(request: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle 3CX webhook events.

    Processes call events from 3CX Cloud PBX:
    - Incoming call notifications
    - Call answered
    - Call ended
    - DTMF events
    """
    event_type = request.get("event", "")
    call_id = request.get("callId", "")

    print(f"[PHONE] 3CX webhook: {event_type} for call {call_id}")

    if event_type == "incoming":
        # New incoming call
        caller = request.get("callerNumber", "")
        callee = request.get("calleeNumber", "")

        session_id = f"phone-{uuid.uuid4().hex[:12]}"
        session = PhoneSession(
            session_id=session_id,
            call_id=call_id,
            caller_id=caller,
            callee_id=callee,
            direction=CallDirection.INBOUND,
            status=PhoneStatus.RINGING,
            created_at=now_utc_iso(),
            last_activity=now_utc_iso(),
        )
        PHONE_SESSIONS[session_id] = session

        return {"ack": True, "session_id": session_id}

    elif event_type == "answered":
        # Call answered - start IVR
        session = get_session_by_call_id(call_id)
        if session:
            session.status = PhoneStatus.IVR
            session.last_activity = now_utc_iso()

        return {"ack": True}

    elif event_type == "ended":
        # Call ended
        session = get_session_by_call_id(call_id)
        if session:
            session.status = PhoneStatus.COMPLETED
            session.call_end = now_utc_iso()
            await save_session_to_blackbox(session)

        return {"ack": True}

    elif event_type == "dtmf":
        # DTMF digit
        digit = request.get("digit", "")
        session = get_session_by_call_id(call_id)
        if session:
            # Route to IVR handler
            pass

        return {"ack": True}

    return {"ack": True}


# =============================================================================
# WebSocket Endpoint for Audio Bridge
# =============================================================================

@app.websocket("/ws/phone/{session_id}")
async def phone_audio_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for phone audio bridging.

    This endpoint is used internally by the FreeSwitch audio stream
    to exchange audio with the AI backend.

    Protocol:
    - Client sends: {"type": "audio", "data": "<base64 ULAW>"}
    - Client sends: {"type": "dtmf", "digit": "1"}
    - Server sends: {"type": "audio", "data": "<base64 ULAW>"}
    - Server sends: {"type": "status", "status": "bridged"}
    """
    print(f"[PHONE-WS] Connection request for session: {session_id}")
    await websocket.accept()

    # Get or create session
    session = get_session(session_id)
    if not session:
        # May be a new connection before HTTP setup
        session = PhoneSession(
            session_id=session_id,
            created_at=now_utc_iso(),
            last_activity=now_utc_iso(),
        )
        PHONE_SESSIONS[session_id] = session

    # IVR state
    ivr = IVRState(
        session=session,
        timeout_ms=IVR_TIMEOUT_MS,
        max_retries=IVR_MAX_RETRIES,
        default_backend=IVR_DEFAULT_BACKEND,
    )

    # AI bridge (created after IVR selection)
    bridge: Optional[PhoneAIBridge] = None

    async def play_tts_prompt(text: str):
        """Play TTS prompt via WebSocket."""
        # Scrub non-spoken symbols (markdown/emoji) before synthesis.
        from Orchestrator.tts_sanitize import sanitize_for_speech
        text = sanitize_for_speech(text)
        # Generate TTS audio
        try:
            from Orchestrator.config import OPENAI_API_KEY, OPENAI_TTS_URL
            import aiohttp

            async with aiohttp.ClientSession() as http_session:
                async with http_session.post(
                    OPENAI_TTS_URL,
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "tts-1-hd",
                        "voice": IVR_TTS_VOICE,
                        "input": text,
                        "response_format": "pcm"
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        pcm_data = await resp.read()
                        # Convert to ULAW
                        ulaw_data = AudioConverter.ai_to_phone(pcm_data, 24000)
                        # Send to phone
                        await websocket.send_json({
                            "type": "audio",
                            "data": base64.b64encode(ulaw_data).decode()
                        })
        except Exception as e:
            print(f"[PHONE-WS] TTS error: {e}")

    async def on_ivr_selection(selection: int, backend_id: str):
        """Handle IVR selection complete."""
        nonlocal bridge

        print(f"[PHONE-WS] IVR selection: {selection} -> {backend_id}")

        # Create AI bridge
        bridge = PhoneAIBridge(session)

        # Set up callbacks
        async def on_ai_audio(ulaw_data: bytes):
            await websocket.send_json({
                "type": "audio",
                "data": base64.b64encode(ulaw_data).decode()
            })

        async def on_ai_transcript(transcript: str):
            await websocket.send_json({
                "type": "transcript",
                "data": transcript
            })

        bridge.on_ai_audio = on_ai_audio
        bridge.on_ai_transcript = on_ai_transcript

        # Start bridge
        success = await bridge.start(session.operator)

        await websocket.send_json({
            "type": "status",
            "status": "bridged" if success else "error",
            "backend": backend_id,
        })

    ivr.on_play_prompt = play_tts_prompt
    ivr.on_selection_complete = on_ivr_selection

    try:
        # Initial connection message
        data = await websocket.receive_json()
        msg_type = data.get("type", "")

        if msg_type == "connect":
            # Get operator and start IVR
            session.operator = data.get("operator", "")
            session.caller_id = data.get("caller_id", "")
            session.freeswitch_uuid = data.get("uuid", "")

            await websocket.send_json({
                "type": "status",
                "status": "connected",
                "session_id": session_id
            })

            # Start IVR
            await ivr.start()

        # Main message loop
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "audio":
                # Audio from phone
                audio_b64 = data.get("data", "")
                if audio_b64:
                    ulaw_data = base64.b64decode(audio_b64)

                    # Route based on state
                    if session.status == PhoneStatus.BRIDGED and bridge:
                        await bridge.send_audio(ulaw_data)

            elif msg_type == "dtmf":
                # DTMF digit
                digit = data.get("digit", "")
                if digit and session.status == PhoneStatus.IVR:
                    event = DTMFEvent(digit=digit)
                    await ivr.handle_dtmf(event)

            elif msg_type == "hangup":
                # Call ended
                break

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        print(f"[PHONE-WS] Disconnected: {session_id}")

    except Exception as e:
        print(f"[PHONE-WS] Error: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "data": str(e)
            })
        except:
            pass

    finally:
        # Cleanup
        if bridge:
            await bridge.stop()

        session.status = PhoneStatus.COMPLETED
        session.call_end = now_utc_iso()

        # Save to BlackBox
        await save_session_to_blackbox(session)

        print(f"[PHONE-WS] Session {session_id} ended")


# =============================================================================
# Phone System Initialization
# =============================================================================

async def init_phone_system():
    """
    Initialize the phone system.

    Called on application startup if PHONE_ENABLED is True.
    """
    if not PHONE_ENABLED:
        print("[PHONE] Phone integration disabled (PHONE_ENABLED=false)")
        return

    print("[PHONE] Initializing phone system...")

    # Initialize FreeSwitch client
    freeswitch = await init_freeswitch(
        host=FREESWITCH_HOST,
        port=FREESWITCH_ESL_PORT,
        password=FREESWITCH_ESL_PASSWORD,
    )

    if freeswitch and freeswitch.is_connected:
        # Set up event handlers
        async def on_incoming_call(event: Dict):
            """Handle incoming call from FreeSwitch."""
            uuid = event.get("uuid", "")
            caller = event.get("caller", "")
            callee = event.get("callee", "")

            print(f"[PHONE] Incoming call: {caller} -> {callee}")

            # Create session
            session_id = f"phone-{uuid[:12]}"
            session = PhoneSession(
                session_id=session_id,
                call_id=uuid,
                freeswitch_uuid=uuid,
                caller_id=caller,
                callee_id=callee,
                direction=CallDirection.INBOUND,
                status=PhoneStatus.RINGING,
                created_at=now_utc_iso(),
                last_activity=now_utc_iso(),
            )
            PHONE_SESSIONS[session_id] = session

            # Answer call
            await freeswitch.answer_call(uuid)

            # Start audio streaming to our WebSocket endpoint
            ws_url = f"ws://localhost:9091/ws/phone/{session_id}"
            await freeswitch.bridge_to_ws(uuid, ws_url)

        async def on_dtmf(uuid: str, digit: str):
            """Handle DTMF from FreeSwitch."""
            session = get_session_by_freeswitch_uuid(uuid)
            if session:
                print(f"[PHONE] DTMF {digit} for session {session.session_id}")
                # DTMF will be handled via WebSocket

        async def on_hangup(uuid: str):
            """Handle hangup from FreeSwitch."""
            session = get_session_by_freeswitch_uuid(uuid)
            if session:
                print(f"[PHONE] Hangup for session {session.session_id}")
                session.status = PhoneStatus.COMPLETED
                session.call_end = now_utc_iso()
                await save_session_to_blackbox(session)

        freeswitch.on_incoming_call = on_incoming_call
        freeswitch.on_dtmf = on_dtmf
        freeswitch.on_hangup = on_hangup

        # Start event listener
        asyncio.create_task(freeswitch.listen_events())

        print("[PHONE] Phone system initialized successfully")
    else:
        print("[PHONE] FreeSwitch connection failed - phone system not available")
