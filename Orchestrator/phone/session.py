#!/usr/bin/env python3
"""
session.py - Phone Session Model

Defines the PhoneSession dataclass for managing phone call state,
including SIP call details, IVR selection, AI backend bridging,
and conversation logging for BlackBox snapshots.
"""

from dataclasses import dataclass, field
from typing import Optional, Any, List, Dict
from enum import Enum


class PhoneStatus(str, Enum):
    """Phone session status states."""
    RINGING = "ringing"
    IVR = "ivr"
    BRIDGED = "bridged"
    COMPLETED = "completed"
    FAILED = "failed"


class AIBackend(str, Enum):
    """Available AI backends for phone calls."""
    CLAUDE_CODE = "claude_code"
    GEMINI_LIVE = "gemini_live"
    OPENAI_REALTIME = "openai_realtime"
    GROK_LIVE = "grok_live"


class CallDirection(str, Enum):
    """Call direction."""
    INBOUND = "inbound"
    OUTBOUND = "outbound"


@dataclass
class PhoneSession:
    """
    Represents a phone call session bridged to an AI backend.

    Attributes:
        session_id: Unique BlackBox session identifier
        call_id: SIP Call-ID header value
        caller_id: E.164 phone number of caller
        direction: Inbound or outbound call
        status: Current call status

        ivr_selection: IVR menu selection (1-4)
        ivr_retries: Number of IVR retry attempts
        pin_verified: Caller passed the PIN gate on this call
        pin_attempts: PIN attempts used on this call (server-side count)
        media_authorized: A media stream URL was issued for this call
        media_connected: That media stream already attached

        ai_backend: Selected AI backend
        ai_session_id: Session ID of the bridged AI session
        ai_ws: WebSocket connection to AI backend

        audio_format: Phone audio format (ulaw)
        phone_sample_rate: Phone audio sample rate (8000 Hz)
        ai_sample_rate: AI audio sample rate (16000-24000 Hz)

        conversation: Full conversation log for BlackBox snapshot
        operator: BlackBox operator name

        created_at: Session creation timestamp
        last_activity: Last activity timestamp
        call_start: Call answered timestamp
        call_end: Call ended timestamp
    """
    # Core identifiers
    session_id: str
    call_id: str = ""
    caller_id: str = ""
    callee_id: str = ""  # For outbound calls
    direction: CallDirection = CallDirection.INBOUND
    status: PhoneStatus = PhoneStatus.RINGING

    # IVR state
    ivr_selection: int = 0  # 1=Claude, 2=Gemini, 3=OpenAI, 4=Grok
    ivr_retries: int = 0
    ivr_input_buffer: str = ""  # Buffer for DTMF input

    # PIN gate + media binding (server-side; never trust caller-supplied counters)
    pin_verified: bool = False  # Caller entered the correct PIN on this call
    pin_attempts: int = 0  # PIN attempts used on this call (incl. timeouts)
    media_authorized: bool = False  # A <Connect><Stream> for this call was issued
    media_connected: bool = False  # Its media stream already attached (one per call)

    # AI backend connection
    ai_backend: AIBackend = AIBackend.OPENAI_REALTIME  # Default
    ai_session_id: str = ""
    ai_ws: Optional[Any] = None  # WebSocket to AI backend

    # Audio configuration
    audio_format: str = "ulaw"  # G.711 mu-law
    phone_sample_rate: int = 8000  # 8kHz (phone standard)
    ai_sample_rate: int = 24000  # 24kHz (AI standard)

    # Asterisk state
    asterisk_channel_id: str = ""  # Asterisk channel ID
    asterisk_bridge_id: str = ""  # Asterisk bridge ID (if using mixing bridge)

    # Outbound call settings
    outbound_greeting: str = ""  # Optional greeting for outbound calls
    outbound_role: str = ""  # Custom system prompt/persona for outbound calls
    is_cli_session: bool = False  # True if connected to Claude Code CLI
    claude_session_id: str = ""  # Resume existing Claude CLI session (passed from caller)

    # Logging
    conversation: List[Dict] = field(default_factory=list)
    operator: str = ""

    # Timestamps
    created_at: str = ""
    last_activity: str = ""
    call_start: str = ""
    call_end: str = ""

    # Audio buffers
    inbound_audio_buffer: bytes = field(default_factory=bytes)  # Phone → AI
    outbound_audio_buffer: bytes = field(default_factory=bytes)  # AI → Phone

    def add_message(self, role: str, content: str, source: str = "voice"):
        """Add a message to the conversation log."""
        from Orchestrator.volume import now_utc_iso
        self.conversation.append({
            "role": role,
            "content": content,
            "timestamp": now_utc_iso(),
            "source": source
        })
        self.last_activity = now_utc_iso()

    def get_ivr_backend(self) -> AIBackend:
        """Map IVR selection to AI backend."""
        mapping = {
            1: AIBackend.CLAUDE_CODE,
            2: AIBackend.GEMINI_LIVE,
            3: AIBackend.OPENAI_REALTIME,
            4: AIBackend.GROK_LIVE,
        }
        return mapping.get(self.ivr_selection, AIBackend.OPENAI_REALTIME)

    def to_dict(self) -> Dict:
        """Convert session to dictionary for API responses."""
        return {
            "session_id": self.session_id,
            "call_id": self.call_id,
            "caller_id": self.caller_id,
            "callee_id": self.callee_id,
            "direction": self.direction.value,
            "status": self.status.value,
            "ivr_selection": self.ivr_selection,
            "ai_backend": self.ai_backend.value,
            "ai_session_id": self.ai_session_id,
            "operator": self.operator,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "call_start": self.call_start,
            "call_end": self.call_end,
            "message_count": len(self.conversation),
        }


# Global storage for phone sessions
PHONE_SESSIONS: Dict[str, PhoneSession] = {}


# Note: Persistent CLI sessions are managed via PERSISTED_AGENT_SESSIONS in state.py
# This is the same system used by the Portal UI agent, allowing seamless switching
# between phone calls and web UI for the same operator's session.
#
# To clear an operator's session, use:
#   from Orchestrator.state import PERSISTED_AGENT_SESSIONS, save_operator_state
#   if operator in PERSISTED_AGENT_SESSIONS:
#       del PERSISTED_AGENT_SESSIONS[operator]
#       save_operator_state()


def get_session(session_id: str) -> Optional[PhoneSession]:
    """Get a phone session by ID."""
    return PHONE_SESSIONS.get(session_id)


def get_session_by_call_id(call_id: str) -> Optional[PhoneSession]:
    """Get a phone session by SIP Call-ID."""
    for session in PHONE_SESSIONS.values():
        if session.call_id == call_id:
            return session
    return None


def get_active_sessions() -> List[PhoneSession]:
    """Get all active (non-completed) phone sessions."""
    return [
        s for s in PHONE_SESSIONS.values()
        if s.status not in (PhoneStatus.COMPLETED, PhoneStatus.FAILED)
    ]


def cleanup_session(session_id: str):
    """Remove a session from the global storage."""
    if session_id in PHONE_SESSIONS:
        del PHONE_SESSIONS[session_id]
