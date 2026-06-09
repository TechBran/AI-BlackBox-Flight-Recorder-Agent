#!/usr/bin/env python3
"""
asterisk_routes.py - Asterisk PBX / Yeastar TG200 Telephony Endpoints

Mirrors the interface of cellular_routes.py but routes calls through
Asterisk PBX (via ARI) and TG200 GSM-to-SIP gateway.

Audio: 16kHz PCM16 (slin16) via AudioSocket — 2x quality of the SIM7600 8kHz path.

Endpoints:
  POST /asterisk/call                  — Outbound voice call via TG200
  POST /asterisk/sms                   — Send SMS via TG200 HTTP API
  GET  /asterisk/status                — Asterisk + TG200 health
  POST /asterisk/hangup/{session_id}   — End call
  GET  /asterisk/channels              — List active Asterisk channels

Gateway Management:
  GET    /asterisk/gateways            — List configured gateways
  POST   /asterisk/gateways            — Add a new gateway
  PUT    /asterisk/gateways/{id}       — Update gateway config
  DELETE /asterisk/gateways/{id}       — Remove a gateway
  POST   /asterisk/gateways/discover   — Auto-discover TG200s on LAN
  GET    /asterisk/gateways/{id}/status — Detailed gateway status
  POST   /asterisk/gateways/{id}/test  — Test SIP connectivity
"""

import asyncio
import uuid
from typing import Optional, Dict, List

from pydantic import BaseModel

from Orchestrator.checkpoint import app
from Orchestrator.volume import now_utc_iso
from Orchestrator.phone.session import (
    PhoneSession,
    PhoneStatus,
    AIBackend,
    CallDirection,
    PHONE_SESSIONS,
    get_session,
)
from Orchestrator.phone.bridge import PhoneAIBridge
from Orchestrator.asterisk.ivr import AsteriskIVR



# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AsteriskCallRequest(BaseModel):
    """Request to initiate an outbound call via Asterisk/TG200."""
    to: str
    backend: str = "openai_realtime"
    operator: str = ""
    greeting: str = ""
    role: str = ""
    claude_session_id: str = ""
    trunk: str = ""  # Specific trunk name (empty = auto-select)


class AsteriskSMSRequest(BaseModel):
    """Request to send an SMS via TG200 HTTP API."""
    to: str
    message: str
    gateway_id: str = ""  # Empty = first available
    port: int = 1  # SIM slot (1 or 2)


class GatewayAddRequest(BaseModel):
    """Request to add a new gateway."""
    name: str
    ip: str
    sip_port: int = 5060
    http_port: int = 80
    http_user: str = "admin"
    http_password: str = "password"
    phone_numbers: List[str] = []
    capacity: int = 2
    codec: str = "g722"


class GatewayUpdateRequest(BaseModel):
    """Request to update a gateway."""
    name: Optional[str] = None
    ip: Optional[str] = None
    sip_port: Optional[int] = None
    http_port: Optional[int] = None
    http_user: Optional[str] = None
    http_password: Optional[str] = None
    phone_numbers: Optional[List[str]] = None
    capacity: Optional[int] = None
    codec: Optional[str] = None
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BACKEND_MAP = {
    "claude_code": AIBackend.CLAUDE_CODE,
    "gemini_live": AIBackend.GEMINI_LIVE,
    "openai_realtime": AIBackend.OPENAI_REALTIME,
    "grok_live": AIBackend.GROK_LIVE,
}


def _normalize_phone(number: str) -> str:
    """Normalize phone number to E.164."""
    number = number.strip()
    if not number.startswith("+"):
        if number.startswith("1") and len(number) == 11:
            number = f"+{number}"
        elif len(number) == 10:
            number = f"+1{number}"
    return number


# Active call tracking per session (for cleanup)
_active_bridges: Dict[str, PhoneAIBridge] = {}


# =============================================================================
# Outbound Calls
# =============================================================================

@app.post("/asterisk/call")
async def asterisk_outbound_call(call_request: AsteriskCallRequest):
    """Initiate an outbound voice call via Asterisk → TG200 → GSM."""
    from Orchestrator.asterisk.client import get_ari_client
    from Orchestrator.asterisk.config import TG200_PHONE_NUMBER, TG200_TRUNK_NAME

    client = get_ari_client()
    if not client or not client.is_connected:
        return {"error": "Asterisk ARI not connected"}

    to_number = _normalize_phone(call_request.to)
    ai_backend = BACKEND_MAP.get(call_request.backend, AIBackend.OPENAI_REALTIME)
    trunk = call_request.trunk or TG200_TRUNK_NAME

    # Create session
    session_id = f"ast-out-{uuid.uuid4().hex[:12]}"
    session = PhoneSession(
        session_id=session_id,
        caller_id=TG200_PHONE_NUMBER,
        callee_id=to_number,
        direction=CallDirection.OUTBOUND,
        status=PhoneStatus.RINGING,
        created_at=now_utc_iso(),
        last_activity=now_utc_iso(),
        operator=call_request.operator or "system",
        ai_backend=ai_backend,
        outbound_greeting=call_request.greeting,
        outbound_role=call_request.role,
    )
    if call_request.claude_session_id:
        session.claude_session_id = call_request.claude_session_id

    PHONE_SESSIONS[session_id] = session

    # Launch outbound call in background
    asyncio.create_task(_handle_outbound_call(session, to_number, trunk))

    return {
        "status": "initiated",
        "session_id": session_id,
        "to": to_number,
        "from": TG200_PHONE_NUMBER,
        "backend": ai_backend.value,
        "operator": session.operator,
    }


async def _handle_outbound_call(session: PhoneSession, to_number: str, trunk: str):
    """Background task: originate call via ARI, wait for answer, bridge to AI."""
    from Orchestrator.asterisk.client import get_ari_client
    from Orchestrator.asterisk.audio_ipc import get_ipc_client

    client = get_ari_client()
    ipc_client = get_ipc_client()
    if not client or not ipc_client:
        session.status = PhoneStatus.FAILED
        return

    voice_bridge = None
    channel_id = None
    call_uuid = str(uuid.uuid4())
    stasis_event = asyncio.Event()
    stasis_channel_id_holder = [None]

    # Intercept StasisStart for our outbound call
    original_on_stasis = client.on_stasis_start

    async def on_outbound_stasis(ch_id, caller, callee, args):
        if args and len(args) >= 2 and args[0] == "outbound" and args[1] == call_uuid:
            stasis_channel_id_holder[0] = ch_id
            stasis_event.set()
        elif original_on_stasis:
            await original_on_stasis(ch_id, caller, callee, args)

    client.on_stasis_start = on_outbound_stasis

    call_ended = asyncio.Event()

    try:
        endpoint = f"PJSIP/{to_number}@{trunk}"
        print(f"[ASTERISK-ROUTE] Originating: {endpoint} (uuid={call_uuid})")

        channel_id = await client.originate(
            endpoint=endpoint,
            callerid=session.caller_id,
            timeout=45,
            app_args=f"outbound,{call_uuid}",
        )

        if not channel_id:
            print("[ASTERISK-ROUTE] Originate failed")
            session.status = PhoneStatus.FAILED
            return

        session.asterisk_channel_id = channel_id

        # Wait for callee to answer (StasisStart fires on answer)
        print(f"[ASTERISK-ROUTE] Waiting for answer: {channel_id}")
        try:
            await asyncio.wait_for(stasis_event.wait(), timeout=50.0)
        except asyncio.TimeoutError:
            print("[ASTERISK-ROUTE] No answer timeout")
            await client.hangup_channel(channel_id)
            session.status = PhoneStatus.FAILED
            return

        answered_channel = stasis_channel_id_holder[0] or channel_id
        print(f"[ASTERISK-ROUTE] Callee answered: {answered_channel}")

        # Set UUID and continue to AudioSocket context
        await client.set_variable(answered_channel, "CALL_UUID", call_uuid)
        ipc_client.expect_channel(call_uuid)
        await client.continue_in_dialplan(answered_channel, context="blackbox-audiosocket", extension="s")

        # Wait for AudioSocket connection
        connected = await ipc_client.wait_for_channel(call_uuid, timeout=15.0)
        if not connected:
            print("[ASTERISK-ROUTE] AudioSocket timeout")
            await client.hangup_channel(answered_channel)
            session.status = PhoneStatus.FAILED
            return

        session.status = PhoneStatus.BRIDGED
        session.call_start = now_utc_iso()

        actual_rate = ipc_client.get_channel_rate(call_uuid)
        print(f"[ASTERISK-ROUTE] AudioSocket connected, rate={actual_rate}Hz")

        # Create voice bridge (same proven path as inbound calls)
        from Orchestrator.asterisk.voice_bridge import AsteriskVoiceBridge

        voice_bridge = AsteriskVoiceBridge(
            ipc_client=ipc_client,
            channel_uuid=call_uuid,
            backend=session.ai_backend.value if hasattr(session.ai_backend, 'value') else session.ai_backend,
            operator=session.operator or "phone-caller",
            asterisk_rate=actual_rate,
            greeting=session.outbound_greeting or "",
            role=session.outbound_role or "",
        )
        _active_bridges[session.session_id] = voice_bridge

        success = await voice_bridge.start()
        if not success:
            print("[ASTERISK-ROUTE] Voice bridge failed to start for outbound")
            await client.hangup_channel(answered_channel)
            session.status = PhoneStatus.FAILED
            return

        print(f"[ASTERISK-ROUTE] Outbound bridged: {to_number} <-> {session.ai_backend.value} (via AsteriskVoiceBridge)")

        # Wait until bridge stops (phone hangup or AI disconnect)
        while voice_bridge.is_running:
            await asyncio.sleep(0.5)

    except Exception as e:
        print(f"[ASTERISK-ROUTE] Outbound error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        client.on_stasis_start = original_on_stasis
        if voice_bridge:
            await voice_bridge.stop()
        _active_bridges.pop(session.session_id, None)
        if channel_id and client and client.is_connected:
            try:
                await client.hangup_channel(channel_id)
            except Exception:
                pass
        session.status = PhoneStatus.COMPLETED
        session.call_end = now_utc_iso()
        print(f"[ASTERISK-ROUTE] Outbound ended: {session.session_id}")


# =============================================================================
# Inbound Call Handler (triggered by ARI StasisStart event)
# =============================================================================

async def handle_inbound_call(channel_id: str, caller_id: str, callee_id: str, args: list = None):
    """
    Handle an incoming call from TG200 via Asterisk ARI.
    Triggered by on_stasis_start callback when dialplan sends call to Stasis(blackbox,inbound,...).
    """
    from Orchestrator.asterisk.audio_ipc import get_ipc_client
    from Orchestrator.asterisk.client import get_ari_client
    from Orchestrator.asterisk.config import TG200_PHONE_NUMBER

    client = get_ari_client()
    ipc_client = get_ipc_client()
    if not client or not ipc_client:
        return

    bridge = None
    session_id = f"ast-in-{uuid.uuid4().hex[:12]}"
    call_uuid = str(uuid.uuid4())

    session = PhoneSession(
        session_id=session_id,
        caller_id=caller_id,
        callee_id=callee_id or TG200_PHONE_NUMBER,
        direction=CallDirection.INBOUND,
        status=PhoneStatus.RINGING,
        created_at=now_utc_iso(),
        last_activity=now_utc_iso(),
    )
    session.asterisk_channel_id = channel_id
    PHONE_SESSIONS[session_id] = session

    call_ended = asyncio.Event()

    try:
        print(f"[ASTERISK-ROUTE] Inbound: {caller_id} → {callee_id} (channel={channel_id})")

        # Answer via ARI (channel stays in Stasis for DTMF events)
        answered = await client.answer_channel(channel_id)
        print(f"[ASTERISK-ROUTE] Answer result: {answered}")
        if not answered:
            print(f"[ASTERISK-ROUTE] FAILED to answer channel {channel_id}!")
        session.status = PhoneStatus.IVR
        session.call_start = now_utc_iso()

        # --- Run IVR while channel is in Stasis (ARI controls DTMF) ---
        ivr = AsteriskIVR(ari_client=client, channel_id=channel_id, caller_id=caller_id)
        ivr_result = await ivr.run()

        if not ivr_result:
            print("[ASTERISK-ROUTE] IVR failed or caller hung up, ending call")
            try:
                await client.hangup_channel(channel_id)
            except Exception:
                pass
            session.status = PhoneStatus.FAILED
            return

        # Extract IVR selections
        ivr_operator = ivr_result["operator"]
        ivr_backend = ivr_result["backend"]
        ivr_voice = ivr_result["voice"]

        # Handle claude_code: no WS streaming endpoint, fall back to openai_realtime
        if ivr_backend == "claude_code":
            print("[ASTERISK-ROUTE] Claude Code has no WS streaming endpoint, falling back to openai_realtime")
            ivr_backend = "openai_realtime"
            ivr_voice = "ash"

        session.operator = ivr_operator
        session.ai_backend = BACKEND_MAP.get(ivr_backend, AIBackend.OPENAI_REALTIME)
        session.status = PhoneStatus.BRIDGED

        print(f"[ASTERISK-ROUTE] IVR complete: operator={ivr_operator}, backend={ivr_backend}, voice={ivr_voice}")

        # --- Continue channel from Stasis to AudioSocket dialplan ---
        # Set UUID as channel variable for dialplan AudioSocket
        await client.set_variable(channel_id, "CALL_UUID", call_uuid)

        # Expect AudioSocket connection with this UUID
        ipc_client.expect_channel(call_uuid)

        # Send channel from Stasis to AudioSocket dialplan context
        await client.continue_in_dialplan(channel_id, context="blackbox-audiosocket", extension="s")

        # Wait for AudioSocket TCP connection
        connected = await ipc_client.wait_for_channel(call_uuid, timeout=10.0)
        if not connected:
            print("[ASTERISK-ROUTE] AudioSocket timeout for inbound call")
            try:
                await client.hangup_channel(channel_id)
            except Exception:
                pass
            session.status = PhoneStatus.FAILED
            return

        # Detect actual sample rate from AudioSocket
        actual_rate = ipc_client.get_channel_rate(call_uuid)
        print(f"[ASTERISK-ROUTE] AudioSocket connected: {call_uuid} (rate={actual_rate}Hz)")

        # Create voice bridge with IVR-selected backend/operator/voice
        from Orchestrator.asterisk.voice_bridge import AsteriskVoiceBridge

        voice_bridge = AsteriskVoiceBridge(
            ipc_client=ipc_client,
            channel_uuid=call_uuid,
            backend=ivr_backend,
            operator=session.operator,
            voice=ivr_voice,
            asterisk_rate=actual_rate,
        )

        success = await voice_bridge.start()
        if not success:
            print("[ASTERISK-ROUTE] Voice bridge failed to start")
            try:
                await client.hangup_channel(channel_id)
            except Exception:
                pass
            session.status = PhoneStatus.FAILED
            return

        print(f"[ASTERISK-ROUTE] Inbound bridged via IVR pipeline: {caller_id} <-> {ivr_backend} (operator={ivr_operator})")

        # Wait until bridge stops (phone hangup or AI disconnect)
        while voice_bridge.is_running:
            await asyncio.sleep(0.5)

    except Exception as e:
        print(f"[ASTERISK-ROUTE] Inbound error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if 'voice_bridge' in dir() and voice_bridge:
            await voice_bridge.stop()
        if client and client.is_connected:
            try:
                await client.hangup_channel(channel_id)
            except Exception:
                pass
        session.status = PhoneStatus.COMPLETED
        session.call_end = now_utc_iso()
        print(f"[ASTERISK-ROUTE] Inbound ended: {session.session_id}")


# =============================================================================
# SMS
# =============================================================================

@app.post("/asterisk/sms")
async def asterisk_send_sms(sms_request: AsteriskSMSRequest):
    """Send an SMS via TG200 HTTP API."""
    from Orchestrator.asterisk.gateway_manager import (
        load_gateways, send_sms_via_gateway, get_gateway
    )

    # Select gateway
    if sms_request.gateway_id:
        gateway = get_gateway(sms_request.gateway_id)
        if not gateway:
            return {"error": f"Gateway not found: {sms_request.gateway_id}"}
    else:
        gateways = load_gateways()
        enabled = [gw for gw in gateways if gw.get("enabled", True)]
        if not enabled:
            return {"error": "No gateways configured"}
        gateway = enabled[0]

    to_number = _normalize_phone(sms_request.to)
    result = await send_sms_via_gateway(
        gateway=gateway,
        to=to_number,
        message=sms_request.message,
        port=sms_request.port,
    )

    return {
        "success": result["success"],
        "error": result.get("error"),
        "to": to_number,
        "gateway": gateway["name"],
    }


# =============================================================================
# Status & Management
# =============================================================================

@app.get("/asterisk/status")
async def asterisk_status():
    """Get Asterisk + TG200 system status."""
    from Orchestrator.asterisk.client import get_ari_client
    from Orchestrator.asterisk.audio_ipc import get_ipc_client
    from Orchestrator.asterisk.config import ASTERISK_ENABLED

    status = {
        "enabled": ASTERISK_ENABLED,
        "ari_connected": False,
        "audiosocket_running": False,
        "asterisk_version": None,
        "active_channels": 0,
        "active_sessions": [],
    }

    client = get_ari_client()
    if client and client.is_connected:
        status["ari_connected"] = True
        info = await client.get_asterisk_info()
        if info:
            status["asterisk_version"] = info.get("system", {}).get("version")
        channels = await client.list_channels()
        status["active_channels"] = len(channels)

    ipc_client = get_ipc_client()
    if ipc_client and ipc_client.is_running:
        status["audiosocket_running"] = True

    # List active Asterisk phone sessions
    for sid, session in PHONE_SESSIONS.items():
        if sid.startswith("ast-") and session.status in (PhoneStatus.RINGING, PhoneStatus.IVR, PhoneStatus.BRIDGED):
            status["active_sessions"].append({
                "session_id": sid,
                "caller": session.caller_id,
                "callee": session.callee_id,
                "status": session.status.value,
                "backend": session.ai_backend.value if session.ai_backend else None,
                "operator": session.operator,
                "started": session.call_start,
            })

    return status


@app.post("/asterisk/hangup/{session_id}")
async def asterisk_hangup(session_id: str):
    """Hang up an Asterisk call by session ID."""
    from Orchestrator.asterisk.client import get_ari_client

    session = get_session(session_id)
    if not session:
        return {"error": "Session not found"}

    client = get_ari_client()
    if client and client.is_connected and session.asterisk_channel_id:
        await client.hangup_channel(session.asterisk_channel_id)

    bridge = _active_bridges.get(session_id)
    if bridge:
        await bridge.stop()

    return {"status": "hangup_sent", "session_id": session_id}


@app.get("/asterisk/channels")
async def asterisk_list_channels():
    """List active Asterisk channels."""
    from Orchestrator.asterisk.client import get_ari_client

    client = get_ari_client()
    if not client or not client.is_connected:
        return {"error": "ARI not connected", "channels": []}

    channels = await client.list_channels()
    return {"channels": channels}


# =============================================================================
# Gateway Management
# =============================================================================

@app.get("/asterisk/gateways")
async def list_gateways():
    """List all configured gateways with live status."""
    from Orchestrator.asterisk.gateway_manager import (
        load_gateways, check_gateway_status, redact_gateway
    )

    gateways = load_gateways()
    result = []
    for gw in gateways:
        status = await check_gateway_status(gw)
        gw_with_status = {**redact_gateway(gw), "status": status}
        result.append(gw_with_status)

    return {"gateways": result}


@app.post("/asterisk/gateways")
async def add_gateway_endpoint(req: GatewayAddRequest):
    """Add a new TG200 gateway."""
    from Orchestrator.asterisk.gateway_manager import (
        _new_gateway, add_gateway, redact_gateway
    )

    gateway = _new_gateway(
        name=req.name,
        ip=req.ip,
        sip_port=req.sip_port,
        http_port=req.http_port,
        http_user=req.http_user,
        http_password=req.http_password,
        phone_numbers=req.phone_numbers,
        capacity=req.capacity,
        codec=req.codec,
    )
    add_gateway(gateway)
    return {"gateway": redact_gateway(gateway)}


@app.put("/asterisk/gateways/{gateway_id}")
async def update_gateway_endpoint(gateway_id: str, req: GatewayUpdateRequest):
    """Update a gateway configuration."""
    from Orchestrator.asterisk.gateway_manager import update_gateway

    from Orchestrator.asterisk.gateway_manager import redact_gateway

    updates = {k: v for k, v in req.dict().items() if v is not None}
    # Send-on-change: drop a blank password so an empty field doesn't clobber
    # the stored secret. (Legacy flat shape; nested-shape conversion is Task 6.2.)
    if updates.get("http_password", None) == "":
        updates.pop("http_password", None)
    result = update_gateway(gateway_id, updates)
    if result:
        return {"gateway": redact_gateway(result)}
    return {"error": "Gateway not found"}


@app.delete("/asterisk/gateways/{gateway_id}")
async def delete_gateway_endpoint(gateway_id: str):
    """Remove a gateway."""
    from Orchestrator.asterisk.gateway_manager import remove_gateway

    if remove_gateway(gateway_id):
        return {"status": "removed"}
    return {"error": "Gateway not found"}


@app.post("/asterisk/gateways/discover")
async def discover_gateways_endpoint():
    """Auto-discover TG200 gateways on the local network."""
    from Orchestrator.asterisk.gateway_manager import discover_gateways

    discovered = await discover_gateways()
    return {"discovered": discovered, "count": len(discovered)}


@app.get("/asterisk/gateways/{gateway_id}/status")
async def gateway_status_endpoint(gateway_id: str):
    """Get detailed status for a specific gateway."""
    from Orchestrator.asterisk.gateway_manager import (
        get_gateway, check_gateway_status, redact_gateway
    )

    gateway = get_gateway(gateway_id)
    if not gateway:
        return {"error": "Gateway not found"}

    status = await check_gateway_status(gateway)
    return {"gateway": redact_gateway(gateway), "status": status}


@app.post("/asterisk/gateways/{gateway_id}/test")
async def test_gateway_endpoint(gateway_id: str):
    """Test SIP connectivity to a gateway."""
    from Orchestrator.asterisk.gateway_manager import get_gateway, check_gateway_status

    gateway = get_gateway(gateway_id)
    if not gateway:
        return {"error": "Gateway not found"}

    status = await check_gateway_status(gateway)
    return {
        "gateway_id": gateway_id,
        "reachable": status["reachable"],
        "sip_registered": status["sip_registered"],
        "sim_slots": status["sim_slots"],
    }
