#!/usr/bin/env python3
"""
phone/__init__.py - AI BlackBox Phone Integration Module

This module provides phone calling capabilities for the AI BlackBox system
(Twilio, and the Asterisk/TG200 gateway in Orchestrator/asterisk/):
- Inbound and outbound AI calls
- The inbound-call PIN gate every IVR shares
- Phone-to-AI WebSocket bridging

Components:
- session.py: PhoneSession model and state management
- audio_converter.py: 8kHz ULAW <-> 24kHz PCM16 conversion
- ivr_prompts.py: Twilio IVR prompt text
- pin_gate.py: the phone PIN gate and its cross-call lockout
- twilio_signature.py: Twilio webhook signature checks
- bridge.py: Phone-to-AI WebSocket bridge

The 3CX/Drachtio/FreeSWITCH generation (sip_client.py, dtmf_handler.py) was
never initialised and is gone (audit O10-telephony-twilio-15).
"""

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
from Orchestrator.phone.bridge import PhoneAIBridge

__all__ = [
    # Session management
    "PhoneSession",
    "PhoneStatus",
    "AIBackend",
    "CallDirection",
    "PHONE_SESSIONS",
    "get_session",
    "get_active_sessions",
    # Audio conversion
    "AudioConverter",
    # Bridge
    "PhoneAIBridge",
]
