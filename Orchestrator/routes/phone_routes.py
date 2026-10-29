#!/usr/bin/env python3
"""
phone_routes.py - Phone Integration API Endpoints

Endpoints shared by the telephony backends (Twilio, Asterisk/TG200):
- GET /phone/status - Phone system health check
- GET /phone/sessions - List active calls
- GET /phone/session/{session_id} - Get call details
- GET /phone/pin - Inbound-call PIN gate status (never the PIN)
- POST /phone/pin/generate - Generate the phone PIN when none is set
- POST /phone/pin/lockout/reset - Clear the PIN lockouts (callers and the global backstop)
- GET/DELETE /phone/cli-session/{operator} - The operator's persistent CLI session

The 3CX/Drachtio/FreeSWITCH generation (POST /phone/call, /phone/hangup,
/phone/webhook/3cx, WS /ws/phone and init_phone_system) is gone: nothing ever
called init_phone_system, its ESL dependency was never installed and its
commands never carried the channel uuid, so those routes could only answer 503
(audit O10-telephony-twilio-15). Calls are placed by /asterisk/call and
/twilio/call (Orchestrator/telephony.py picks one).
"""

from typing import Any, Dict
from pydantic import BaseModel

from fastapi import HTTPException

# Local imports
from Orchestrator.checkpoint import app
from Orchestrator.phone.session import (
    PHONE_SESSIONS,
    get_session,
    get_active_sessions,
)
from Orchestrator.phone import pin_gate
from Orchestrator.config import (
    PHONE_ENABLED,
    IVR_TIMEOUT_MS,
    IVR_MAX_RETRIES,
    IVR_DEFAULT_BACKEND,
    PHONE_SAMPLE_RATE,
    PHONE_AUDIO_FORMAT,
)


# =============================================================================
# Request/Response Models
# =============================================================================

class PhoneStatusResponse(BaseModel):
    """Phone system status response."""
    enabled: bool
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
    return PhoneStatusResponse(
        enabled=PHONE_ENABLED,
        active_sessions=len(get_active_sessions()),
        config={
            "ivr_timeout_ms": IVR_TIMEOUT_MS,
            "ivr_max_retries": IVR_MAX_RETRIES,
            "ivr_default_backend": IVR_DEFAULT_BACKEND,
            "phone_sample_rate": PHONE_SAMPLE_RATE,
            "phone_audio_format": PHONE_AUDIO_FORMAT,
        }
    )


# =============================================================================
# Phone PIN (the inbound-call gate every IVR shares; see phone/pin_gate.py)
# =============================================================================

@app.get("/phone/pin")
async def phone_pin_status() -> Dict[str, Any]:
    """PIN gate status: enabled, configured, digit count. Never returns the PIN."""
    return pin_gate.describe()


@app.post("/phone/pin/generate")
async def phone_pin_generate() -> Dict[str, Any]:
    """Generate a random phone PIN into .env and make it live (no restart).

    Only when none is configured: inbound calls are refused until a PIN
    exists. The new PIN is returned once so the operator can note it. An
    existing PIN is never replaced here (edit PHONE_PIN_CODE in .env).
    """
    if not pin_gate.pin_enabled():
        raise HTTPException(status_code=409, detail="The phone PIN gate is disabled (PHONE_PIN_ENABLED=false)")
    if pin_gate.pin_configured():
        raise HTTPException(status_code=409, detail="A phone PIN is already set (PHONE_PIN_CODE in .env)")
    return pin_gate.ensure_pin()


@app.post("/phone/pin/lockout/reset")
async def phone_pin_lockout_reset() -> Dict[str, Any]:
    """Clear every PIN lockout, including the global backstop.

    For the operator when wrong guesses (for example from rotating caller
    IDs) have locked callers out. Numbers that entered the right PIN recently
    are already exempt from the global lock; this clears the rest.
    """
    return {"cleared": pin_gate.reset_lockouts()}


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
