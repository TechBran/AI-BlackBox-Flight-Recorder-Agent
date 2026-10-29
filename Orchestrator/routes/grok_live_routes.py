#!/usr/bin/env python3
"""
grok_live_routes.py - xAI Grok Voice Agent API WebSocket Bridge

This module provides a WebSocket bridge between the Portal frontend and
xAI's Grok Voice Agent API, enabling real-time voice conversations with
semantic search capabilities over the BlackBox snapshot volume.

Architecture:
    Portal (Browser) <--WebSocket--> Orchestrator <--WebSocket--> xAI Grok Voice API

Features:
- Bidirectional audio/text streaming
- Tool calling (search_snapshots for semantic search)
- Automatic context injection (checkpoint + recent snapshots)
- Session management with reconnection support
- Grok voices: Ara, Rex, Sal, Eve, Leo
"""

# Standard library imports
import asyncio
import base64
import json
import time
from typing import Optional, Dict

# External library imports
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("[GROK-LIVE] websockets library not installed - run: pip install websockets")

from fastapi import WebSocket

# Local imports
from Orchestrator.checkpoint import app
from Orchestrator.config import (
    XAI_API_KEY,
    GROK_LIVE_URL,
    GROK_LIVE_MODEL,
    GROK_LIVE_MODELS,
    GROK_LIVE_REASONING_EFFORTS,
    GROK_LIVE_REASONING_CAPABLE_MODELS,
    GROK_LIVE_VOICES,
    GROK_LIVE_DEFAULT_VOICE,
    GROK_LIVE_SAMPLE_RATE,
    GROK_LIVE_INPUT_SAMPLE_RATE,
    GROK_LIVE_OUTPUT_SAMPLE_RATE,
    REALTIME_CONTEXT_MAX_CHARS,
)
from Orchestrator.models import GrokLiveSession, GROK_LIVE_SESSIONS
from Orchestrator.volume import now_utc_iso
from Orchestrator.context_builder import build_fossil_context
from Orchestrator.utils.async_helpers import run_blocking
from Orchestrator.whisper_filter import is_whisper_hallucination
from Orchestrator.tools.tool_registry import get_openai_realtime_tools
from Orchestrator.voice_agents.registry import resolve_preset, merge_connect_params
from Orchestrator.behavioral_core import get_persona, VOICE_DELIVERY_NOTE
from Orchestrator.routes.voice_prompts import build_voice_instructions
from Orchestrator.routes.voice_ws_shared import (
    adopt_upstream,
    safe_send,
    save_voice_transcript,
    send_openai_style_tool_error,
    voice_debug_enabled,
)
from Orchestrator.voice import tools as voice_tools
from Orchestrator.voice.bridge import LiveVoiceBridge, VoiceAdapter
from Orchestrator.voice.transcript import save_session_transcript

LOG_PREFIX = "[GROK-LIVE]"

_safe_ws_send = safe_send

# =============================================================================
# Tool Definitions — read FRESH at session-configure time (P1b)
# =============================================================================
# No import-time snapshot here: get_openai_realtime_tools("grok_live") is
# called inside configure_grok_session, so POST /toolvault/reload reaches the
# NEXT voice session/reconnect without a restart.

# =============================================================================
# Session Saving
# =============================================================================

async def save_grok_session_to_blackbox(session: 'GrokLiveSession'):
    """
    Save the Grok Voice session conversation to BlackBox.
    Called on disconnect/cleanup to ensure all messages are captured. The
    conversation is cleared only once the transcript is safe (P1b: it used to
    be cleared even after a failed save).
    """
    await save_session_transcript(
        session,
        title="Grok Voice Agent Session",
        fields=[("Timestamp", now_utc_iso()), ("Voice", session.voice)],
        ai_label="AI (Grok)",
        user_message=f"[Grok Voice Session Transcript] Grok voice session {session.session_id}",
        model_label="grok-live-voice",
        log_prefix=LOG_PREFIX,
        persist=save_voice_transcript,
    )


# =============================================================================
# Context Injection
# =============================================================================

def build_context_for_operator(operator: str, user_text: str = "") -> tuple[str, dict]:
    """
    Build initial context for an xAI Grok Live session.

    Delegates to the shared `build_fossil_context` so voice sessions get the
    same four-source retrieval (recent + keyword + semantic + checkpoint) as
    `/chat/stream`. At session-open time the caller passes user_text="" so
    only recent + checkpoint populate; per-turn refresh is out of scope.

    Args:
        operator: Operator scope for retrieval (must be non-empty).
        user_text: Optional last-user-text to drive keyword + semantic search.
            Empty string is valid — both will be skipped.

    Returns:
        (text_block, provenance_dict) — provenance has keys
        recent / keyword / semantic / checkpoint, each a list of snap_ids.

    Raises:
        ValueError: If operator is empty/whitespace.
    """
    text_block, provenance = build_fossil_context(
        user_text, operator, log_prefix="[GROK-LIVE]"
    )
    # Honor existing REALTIME_CONTEXT_MAX_CHARS cap (voice has tighter token
    # budgets than /chat/stream — layered on top of the 30k cap inside
    # build_fossil_context).
    if len(text_block) > REALTIME_CONTEXT_MAX_CHARS:
        text_block = text_block[:REALTIME_CONTEXT_MAX_CHARS] + "\n... [context truncated]"
    return text_block, provenance

# =============================================================================
# Tool Execution
# =============================================================================

async def execute_grok_search_snapshots(session: 'GrokLiveSession', arguments: Dict) -> str:
    """The search_snapshots tool (Orchestrator/voice/tools.py)."""
    return await voice_tools.execute_search_snapshots(session, arguments, log_prefix=LOG_PREFIX)

# =============================================================================
# xAI Grok Voice Agent API Connection
# =============================================================================

async def connect_to_grok(session: 'GrokLiveSession',
                          model: Optional[str] = None,
                          conversation_id: Optional[str] = None,
                          call_id: Optional[str] = None) -> bool:
    """
    Establish WebSocket connection to xAI Grok Voice Agent API.

    URL parameterization — call attach XOR model dial:
      * call_id — attach to a live SIP call (wss://.../realtime?call_id=...).
        The call_id session IS the call's audio path: audio flows xAI-side,
        there is no local audio pump, and xAI binds the model + conversation
        server-side — so model/conversation_id are EXCLUDED from the URL.
        Passing call_id together with model or conversation_id raises
        ValueError (caller bug). Persisted to session.call_id so reconnects
        rejoin the SAME call.
      * model — validated against GROK_LIVE_MODELS (P2.8); invalid values
        fall back to GROK_LIVE_MODEL with a logged warning; the resolved id
        is stamped on session.model and bound at the WS URL (?model=).
      * conversation_id — xAI session resumption (P2.13): appended as
        &conversation_id= so xAI replays cached turns.

    Fallback precedence: when call_id is NOT passed but session.call_id is
    set (a phone-xai-* session), the call attach WINS over any
    model/conversation_id args — grok_reconnect's post-P2.13 call shape
    (model=session.model or None, conversation_id=resume_id) must rejoin the
    live call, never silently demote it to a non-call ?model= session. The
    swallowed args are logged, not raised (they come from generic reconnect
    code, not a caller bug).

    Returns True if connection successful, False otherwise.
    """
    if call_id and (model or conversation_id):
        raise ValueError(
            "connect_to_grok: call_id is mutually exclusive with "
            "model/conversation_id (a SIP call attach carries no model or "
            "resumption params — xAI binds them server-side)")

    if not WEBSOCKETS_AVAILABLE:
        print("[GROK-LIVE] Cannot connect - websockets library not installed")
        return False

    if not XAI_API_KEY:
        print("[GROK-LIVE] Cannot connect - XAI_API_KEY not set")
        return False

    effective_call_id = call_id or getattr(session, "call_id", "")
    resolved_model = ""
    if effective_call_id:
        if model or conversation_id:
            print(f"[GROK-LIVE] session {session.session_id} has "
                  f"call_id={effective_call_id!r} — ignoring model/conversation_id "
                  f"(call-attach precedence)")
    else:
        # Resolve + validate model (allowlist from GROK_LIVE_MODELS) — P2.8
        _allowed_model_ids = {m["id"] for m in GROK_LIVE_MODELS}
        if model and model not in _allowed_model_ids:
            print(f"[GROK-LIVE] WARNING: model {model!r} not in GROK_LIVE_MODELS allowlist; falling back to default {GROK_LIVE_MODEL!r}")
            model = None
        resolved_model = model or GROK_LIVE_MODEL
        session.model = resolved_model

    try:
        headers = {
            "Authorization": f"Bearer {XAI_API_KEY}",
            "Content-Type": "application/json"
        }

        if effective_call_id:
            # SIP call attach — call_id is the ONLY query param (XOR above).
            url = f"{GROK_LIVE_URL}?call_id={effective_call_id}"
            session.call_id = effective_call_id
        else:
            url = f"{GROK_LIVE_URL}?model={resolved_model}"
            if conversation_id:
                # Resumption: xAI replays cached turns for this conversation — P2.13
                url += f"&conversation_id={conversation_id}"

        print(f"[GROK-LIVE] Connecting to xAI: {url}")
        # The attach this dial is for: a re-attach during the dial owns the
        # session by the time it returns, and its upstream must stay in place.
        generation = getattr(session, "generation", 0)
        # websockets 15.x uses additional_headers instead of extra_headers
        # Add explicit ping settings to prevent connection drops
        ws = await websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=10,       # 10s max to establish connection (prevents indefinite hang)
            ping_interval=20,      # Send ping every 20 seconds
            ping_timeout=30,       # Wait 30 seconds for pong response
            close_timeout=10,      # Wait 10 seconds for close handshake
        )
        if not await adopt_upstream(session, "grok_ws", ws, generation, "[GROK-LIVE]"):
            return False
        session.status = "connected"
        session.last_activity = now_utc_iso()
        if effective_call_id:
            print(f"[GROK-LIVE] Connected to xAI for session {session.session_id} (call_id={effective_call_id})")
        else:
            print(f"[GROK-LIVE] Connected to xAI for session {session.session_id} (model={resolved_model})")
        return True

    except Exception as e:
        print(f"[GROK-LIVE] Connection failed: {e}")
        session.status = "error"
        return False

def _contact_keyterms(operator: str, limit: int = 100) -> list:
    """Operator's contact names for xAI ASR keyterm biasing (caps: 100 x 50 chars).

    Best-effort — any failure (missing file, fresh box, unknown operator)
    returns []. Uses load_contacts (read-only; no seed-book write)."""
    try:
        from Orchestrator.contacts import load_contacts
        book = load_contacts().get(operator, {}) or {}
        names: list = []
        for contact in book.values():
            if not isinstance(contact, dict):
                continue
            name = (contact.get("name") or "").strip()
            if name and len(name) <= 50 and name not in names:
                names.append(name)
            if len(names) >= limit:
                break
        return names
    except Exception as e:
        print(f"[GROK-LIVE] contact keyterms unavailable: {e}")
        return []


async def resolve_grok_voice(voice: str) -> str:
    """Resolve a requested session voice: a built-in catalog voice passes
    through; any other id is accepted ONLY if it verifies as a cloned xAI
    custom voice (GET /v1/custom-voices via xai_voices.is_custom_voice —
    60s cache, FAIL-OPEN to the catalog default when xAI is unreachable or
    unconfigured). Anything unverifiable falls back to the default voice.
    """
    if voice in GROK_LIVE_VOICES:
        return voice
    from Orchestrator import xai_voices
    try:
        if await asyncio.to_thread(xai_voices.is_custom_voice, voice):
            return voice
    except Exception:
        pass  # fail-open to catalog-only
    return GROK_LIVE_DEFAULT_VOICE


async def configure_grok_session(session: 'GrokLiveSession', operator: str, voice: str = "Ara", custom_role: str = "", reasoning_effort: Optional[str] = None,
                                 replace_map: Optional[Dict[str, str]] = None,
                                 keyterms: Optional[list] = None,
                                 language_hint: Optional[str] = None,
                                 tool_group_override: Optional[str] = None):
    """
    Configure the Grok Voice Agent session with tools and settings.
    Injects operator-specific context and personalization.

    Args:
        session: GrokLiveSession object
        operator: Operator name for context
        voice: Voice to use (Ara, Rex, Sal, Eve, Leo)
        custom_role: Optional custom system prompt/persona for outbound calls
    """
    if not session.grok_ws:
        return

    # Validate voice — built-in catalog OR a verified cloned xAI custom voice id.
    voice = await resolve_grok_voice(voice)
    session.voice = voice

    # reasoning.effort — allowlist + capability gate (think-fast generation only).
    if reasoning_effort is not None and reasoning_effort not in GROK_LIVE_REASONING_EFFORTS:
        print(f"[GROK-LIVE] WARNING: reasoning_effort {reasoning_effort!r} not in {GROK_LIVE_REASONING_EFFORTS}; ignoring")
        reasoning_effort = None
    if reasoning_effort is not None and session.model not in GROK_LIVE_REASONING_CAPABLE_MODELS:
        print(f"[GROK-LIVE] reasoning_effort ignored — model {session.model!r} is not reasoning-capable")
        reasoning_effort = None

    # ASR biasing — seed keyterms from the operator's contact book when the
    # client didn't supply any (names are what voice ASR most often mangles).
    if keyterms is None:
        keyterms = _contact_keyterms(operator)
    keyterms = [k for k in keyterms if isinstance(k, str) and 0 < len(k) <= 50][:100]

    # Build system instructions with operator-specific context.
    # `operator` is request-scoped — comes from the WS connect handshake
    # (`data.get("operator", "")`) at the bottom of this file, then stored
    # on session.operator. At session open we have no user_text yet, so
    # keyword/semantic will be empty; recent + checkpoint still populate.
    context, provenance = await run_blocking(build_context_for_operator, operator, user_text="")  # retrieval: off the loop
    # Stash provenance on the session so the WS endpoint can emit it to
    # the client right after configuration.
    session.provenance = provenance
    # One builder for the three voice providers (voice_prompts); a custom_role
    # (outbound call) replaces the default persona prompt.
    system_instructions = build_voice_instructions(
        "grok", operator, context, custom_role,
        persona=lambda: get_persona(operator, "voice") + "\n\n" + VOICE_DELIVERY_NOTE)

    # P1b: read tools FRESH (not at import) so /toolvault/reload reaches voice.
    # P4: a voice-agent preset can swap the tool group at configure time.
    grok_live_tools = get_openai_realtime_tools(tool_group_override or "grok_live")

    # Configure session - Grok uses nested audio format structure
    config_event = {
        "type": "session.update",
        "session": {
            "instructions": system_instructions,
            "voice": voice,
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.7,           # Raised from 0.6 to reduce noise floor triggers
                "prefix_padding_ms": 300,   # Audio to include before speech detected
                "silence_duration_ms": 900  # Wait 900ms of silence before responding (longer than OpenAI)
            },
            "audio": {
                "input": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": GROK_LIVE_INPUT_SAMPLE_RATE
                    },
                    # Explicit input-transcription opt-in (P0.4 transcription_shape
                    # probe 2026-07-11 — accepted shape echoed in session.updated).
                    # Without it, conversation.item.input_audio_transcription.*
                    # events are not guaranteed and saved transcripts lose all
                    # user turns. Shape per docs.x.ai voice-agent session schema
                    # (language_hint merged in by the ASR-biasing params task).
                    "transcription": {}
                },
                "output": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": GROK_LIVE_OUTPUT_SAMPLE_RATE
                    }
                }
            },
            "tools": grok_live_tools,
            "tool_choice": "auto",  # Force Grok to actually use tools when appropriate
            # Session resumption (xAI): reconnect with ?conversation_id= replays
            # cached turns server-side instead of a full context rebuild.
            "resumption": {"enabled": True}
        }
    }

    if reasoning_effort is not None:
        config_event["session"]["reasoning"] = {"effort": reasoning_effort}

    if keyterms:
        config_event["session"]["keyterms"] = keyterms
    if replace_map and isinstance(replace_map, dict):
        config_event["session"]["replace"] = replace_map
    if language_hint:
        config_event["session"]["audio"]["input"]["transcription"]["language_hint"] = language_hint

    print(f"[GROK-LIVE] ===== SENDING SESSION CONFIG =====")
    print(f"[GROK-LIVE] Number of tools: {len(grok_live_tools)}")
    print(f"[GROK-LIVE] Tool names: {[t['name'] for t in grok_live_tools]}")
    print(f"[GROK-LIVE] Instructions: {len(system_instructions)} chars")
    # The full session.update carries the operator's snapshot memory: opt-in only.
    if voice_debug_enabled():
        print(f"[GROK-LIVE] Full config: {json.dumps(config_event, indent=2)}")

    await session.grok_ws.send(json.dumps(config_event))
    session.context_injected = True
    print(f"[GROK-LIVE] ✓ Session configured for operator {operator} with voice {voice}")

# =============================================================================
# Message Handlers
# =============================================================================

async def handle_grok_portal_message(session: 'GrokLiveSession', data: Dict):
    """
    Handle messages from Portal and forward to Grok.

    Message types from Portal:
    - audio_input: Base64 PCM16 audio chunk
    - audio_commit: End of audio input, request response
    - text_input: Text message
    - interrupt: Cancel current response
    """
    msg_type = data.get("type", "")

    if msg_type == "audio_input":
        # Forward audio to Grok
        if session.grok_ws:
            await session.grok_ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": data.get("data", "")  # Base64 PCM16
            }))
            session.is_recording = True
            session.last_activity = now_utc_iso()

    elif msg_type == "audio_commit":
        # Commit audio buffer and request response
        if session.grok_ws:
            await session.grok_ws.send(json.dumps({
                "type": "input_audio_buffer.commit"
            }))
            await session.grok_ws.send(json.dumps({
                "type": "response.create"
            }))
            session.is_recording = False
            session.last_activity = now_utc_iso()

    elif msg_type == "text_input":
        # Send text message
        text = data.get("text", "")
        if session.grok_ws and text:
            # Create conversation item
            await session.grok_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}]
                }
            }))
            # Request response
            await session.grok_ws.send(json.dumps({
                "type": "response.create"
            }))
            session.last_activity = now_utc_iso()

    elif msg_type == "interrupt":
        # Cancel current response (for barge-in)
        if session.grok_ws:
            await session.grok_ws.send(json.dumps({
                "type": "response.cancel"
            }))
            # Clear audio buffer for new input
            await session.grok_ws.send(json.dumps({
                "type": "input_audio_buffer.clear"
            }))
            session.is_speaking = False

    elif msg_type == "video_frame":
        # NOTE: Grok Voice Agent API does NOT support vision/image input
        # It only supports audio and text. Screen sharing is not available for Grok Live.
        # We silently ignore video frames to prevent crashes - the Android overlay
        # should disable screen share button when Grok Live is selected.
        if not hasattr(session, '_vision_warning_logged'):
            print(f"[GROK-LIVE] Screen sharing not supported - Grok Voice API is audio/text only")
            session._vision_warning_logged = True
            # Notify Portal that vision isn't supported
            if session.portal_ws:
                await _safe_ws_send(session.portal_ws, {
                    "type": "warning",
                    "data": "Screen sharing not available - Grok Voice API only supports audio and text"
                })

async def handle_grok_message(session: 'GrokLiveSession', event: Dict):
    """
    Handle messages from Grok and forward to Portal.

    Key event types (Grok-specific):
    - response.output_audio.delta: Audio chunk to play
    - response.output_audio.done: Audio stream complete
    - response.output_audio_transcript.delta: AI speech transcript (incremental)
    - response.output_audio_transcript.done: AI speech transcript complete
    - response.text.delta: Text response (text-only mode)
    - response.function_call_arguments.done: Execute tool
    - response.done: Response complete
    - error: Error occurred
    """
    session.last_ai_message_time = time.time()
    event_type = event.get("type", "")

    if event_type == "response.output_audio.delta":
        # Forward audio chunk to Portal (Grok uses response.output_audio.delta)
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "audio_delta",
                "data": event.get("delta", "")
            })
            session.is_speaking = True

    elif event_type == "response.output_audio.done":
        # Audio stream complete
        session.is_speaking = False

    elif event_type == "response.output_audio_transcript.delta":
        # Forward transcript to Portal (Grok uses response.output_audio_transcript.delta)
        delta = event.get("delta", "")
        session.transcript_buffer += delta
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "transcript_delta",
                "data": delta
            })

    elif event_type == "response.output_audio_transcript.done":
        # Transcript generation complete for this turn
        print(f"[GROK-LIVE] AI transcript complete: {session.transcript_buffer[:100]}...")

    elif event_type == "response.text.delta":
        # Forward text response to Portal
        delta = event.get("delta", "")
        session.transcript_buffer += delta
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "text_delta",
                "data": delta
            })

    elif event_type == "response.function_call_arguments.done":
        # The OpenAI-style function call (shared with OpenAI Realtime): it runs
        # OFF this listener and is answered on the socket it came from.
        print(f"[GROK-LIVE] Function call: {event.get('name', '')} "
              f"(call_id={event.get('call_id', '')}) arguments={event.get('arguments', '{}')}")
        await voice_tools.dispatch_openai_style_call(
            session, event, ws_attr="grok_ws", log_prefix=LOG_PREFIX,
            execute=lambda name, arguments: _execute_grok_tool(session, name, arguments))

    elif event_type == "response.done":
        # Response complete
        session.is_speaking = False

        # Add AI response to conversation for BlackBox snapshot
        if session.transcript_buffer.strip():
            session.conversation.append({
                "role": "assistant",
                "content": session.transcript_buffer.strip(),
                "timestamp": now_utc_iso()
            })

        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "response_complete",
                "data": {
                    "transcript": session.transcript_buffer
                }
            })
        session.transcript_buffer = ""

    elif event_type == "conversation.item.input_audio_transcription.completed":
        # User's voice input was transcribed
        transcript = event.get("transcript", "")
        if transcript and not is_whisper_hallucination(transcript):
            # Add user message to conversation for BlackBox snapshot
            session.conversation.append({
                "role": "user",
                "content": transcript,
                "timestamp": now_utc_iso(),
                "source": "voice"
            })

            if session.portal_ws:
                print(f"[GROK-LIVE] User voice transcription: {transcript[:100]}...")
                await _safe_ws_send(session.portal_ws, {
                    "type": "user_transcript",
                    "data": transcript
                })

    elif event_type == "conversation.item.input_audio_transcription.delta":
        # Incremental (interim) user transcription chunk — live word-by-word.
        # Grok mirrors OpenAI's realtime schema; handled defensively — if Grok
        # never emits .delta, behavior is unchanged (.completed still commits
        # the final user_transcript above).
        delta_chunk = event.get("delta", "")
        if delta_chunk and session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "user_transcript_delta",
                "data": delta_chunk
            })

    elif event_type == "input_audio_buffer.speech_started":
        # User started speaking (VAD detected)
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "speech_started"
            })

    elif event_type == "input_audio_buffer.speech_stopped":
        # User stopped speaking (VAD detected)
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "speech_stopped"
            })

    elif event_type == "session.updated":
        # Session configuration confirmed
        print(f"[GROK-LIVE] ===== SESSION CONFIGURATION CONFIRMED =====")
        session_data = event.get("session", {})
        tools = session_data.get("tools", [])
        print(f"[GROK-LIVE] Confirmed tools: {[t.get('name') for t in tools]}")
        print(f"[GROK-LIVE] Confirmed instructions: {len(session_data.get('instructions') or '')} chars")
        if voice_debug_enabled():
            print(f"[GROK-LIVE] Full session data: {json.dumps(session_data, indent=2)}")

    elif event_type == "conversation.created":
        # Conversation initialized — capture the id for session resumption
        # (grok_reconnect dials ?conversation_id= to replay cached turns).
        conv_id = (event.get("conversation") or {}).get("id") or event.get("conversation_id")
        if conv_id:
            session.conversation_id = conv_id
            print(f"[GROK-LIVE] Conversation created: {conv_id} (resumption armed)")
        else:
            print(f"[GROK-LIVE] Conversation created (no id in event)")

    elif event_type == "error":
        # Error from Grok
        error = event.get("error", {})
        error_msg = error.get("message", "Unknown error")
        print(f"[GROK-LIVE] Grok error: {error_msg}")
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "error",
                "data": error_msg
            })
    else:
        # Log unhandled event types to help debug
        print(f"[GROK-LIVE] Unhandled event type: {event_type}")


async def _execute_grok_tool(session: 'GrokLiveSession', name: str, arguments: Dict) -> str:
    """Run ONE tool call of the session and return its result text (the shared
    voice tool ladder, Orchestrator/voice/tools.py)."""
    return await voice_tools.execute_voice_tool(session, name, arguments, log_prefix=LOG_PREFIX,
                                                created_by="grok-live")


# =============================================================================
# The Grok adapter of the shared voice bridge (Orchestrator/voice/bridge.py)
# =============================================================================

# 20 ms of PCM16 silence at the 16 kHz input rate (320 samples = 640 bytes).
_SILENCE_20MS_B64 = base64.b64encode(b'\x00' * 640).decode('ascii')


class GrokLiveAdapter(VoiceAdapter):
    """xAI Grok's half of the shared LiveVoiceBridge: session.update, the
    OpenAI-style realtime events, conversation resumption on reconnect and the
    SIP call attach (call_id sessions)."""

    name = "Grok"
    log_prefix = LOG_PREFIX
    ws_attr = "grok_ws"
    key_name = "XAI_API_KEY"
    connecting_status = "Connecting to Grok..."
    connect_failed = "Failed to connect to Grok Voice Agent API"

    def sessions(self):
        return GROK_LIVE_SESSIONS

    def new_session(self, session_id):
        return GrokLiveSession(session_id=session_id, created_at=now_utc_iso())

    def websockets_available(self):
        return WEBSOCKETS_AVAILABLE

    def api_key(self):
        return XAI_API_KEY

    def url_params(self, websocket):
        q = websocket.query_params
        return {key: q.get(key) for key in ("agent", "model", "language_hint", "reasoning_effort")}

    async def parse_connect(self, session, websocket, data, url):
        # Voice-agent preset (P4) — precedence: explicit > preset > defaults.
        # Client model/voice/greeting/role plus the Grok-specific
        # language_hint/keyterms feed the merge as the explicit inputs (model
        # keeps its JSON-wins-over-URL fallback: Android sends it in the query),
        # so an explicit value still wins over a preset-supplied one.
        # reasoning_effort/replace_map are not preset fields.
        agent_id = data.get("agent", url["agent"])
        preset = resolve_preset(agent_id, provider="grok-live") if agent_id else None
        if agent_id and preset is None:
            await safe_send(websocket, {
                "type": "warning",
                "data": f"Voice agent preset {agent_id!r} not found for provider 'grok-live' — continuing without preset"
            })
        merged = merge_connect_params({
            "model": data.get("model", url["model"]),
            "voice": data.get("voice"),
            "greeting": data.get("greeting", ""),
            "instructions": data.get("role", ""),
            "language": data.get("language_hint", url["language_hint"]),
            "keyterms": data.get("keyterms") if isinstance(data.get("keyterms"), list) else None,
        }, preset)
        operator = data.get("operator", "")
        session.operator = operator
        return {
            "operator": operator,
            "voice": merged["voice"] or GROK_LIVE_DEFAULT_VOICE,
            "greeting": merged["greeting"] or "",
            "role": merged["instructions"] or "",
            "model": merged["model"],
            "tool_group_override": merged["tool_group_override"],
            "reasoning_effort": data.get("reasoning_effort", url["reasoning_effort"]),
            "replace_map": data.get("replace") if isinstance(data.get("replace"), dict) else None,
            "keyterms": merged["keyterms"],
            "language_hint": merged["language"],
        }

    async def dial(self, session, params):
        return await connect_to_grok(session, model=params["model"])

    async def configure(self, session, params):
        await configure_grok_session(session, params["operator"], params["voice"],
                                     custom_role=params["role"],
                                     reasoning_effort=params["reasoning_effort"],
                                     replace_map=params["replace_map"],
                                     keyterms=params["keyterms"],
                                     language_hint=params["language_hint"],
                                     tool_group_override=params["tool_group_override"])
        print(f"{LOG_PREFIX} Voice selected: {params['voice']}")

    async def inject_greeting(self, session, greeting):
        prompt = f"The user just answered the phone. Greet them and deliver this message: {greeting}"
        await session.grok_ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}]
            }
        }))
        await session.grok_ws.send(json.dumps({"type": "response.create"}))

    def connected_data(self, session, session_id, params):
        return {
            "session_id": session_id,
            "operator": params["operator"],
            "model": session.model,
            "voice": params["voice"]
        }

    async def handle_client(self, session, data):
        await handle_grok_portal_message(session, data)

    async def handle_event(self, session, event):
        await handle_grok_message(session, event)

    async def answer_failed_event(self, session, event, exc):
        await send_openai_style_tool_error(session.grok_ws, session.portal_ws, event, exc)

    def on_frame(self, session):
        if session.reconnect_count:
            # Proven-healthy: a real server frame arrived on this socket.
            session.reconnect_count = 0

    def keepalive_frame(self, session):
        # SKIPPED for SIP-attached calls (session.call_id set): the call's
        # audio flows xAI-side and injected buffer silence could corrupt it.
        # The socket's ping/pong still detects a dead connection.
        if session.call_id:
            return None
        return {"type": "input_audio_buffer.append", "audio": _SILENCE_20MS_B64}

    async def redial(self, session):
        # Resume the server-side conversation when there is an id (xAI replays
        # cached turns; no full context rebuild). A call_id session rejoins its
        # call (connect_to_grok's call-attach precedence).
        return await connect_to_grok(session, model=session.model or None,
                                     conversation_id=session.conversation_id)

    async def reconfigure(self, session):
        if session.conversation_id:
            print(f"{LOG_PREFIX} Resumed conversation {session.conversation_id} — session rebuild skipped")
            return False
        # No resumption id: a full reconfigure (rebuilds the context).
        await configure_grok_session(session, session.operator, session.voice)
        return True

    async def save(self, session):
        await save_grok_session_to_blackbox(session)

    def reconnect(self, session):
        return grok_reconnect(session)

    def listener(self, session):
        return grok_listener(session)

    def keepalive(self, session):
        return grok_keepalive_loop(session)


GROK_LIVE_BRIDGE = LiveVoiceBridge(GrokLiveAdapter())


async def grok_reconnect(session: 'GrokLiveSession'):
    """Reconnect to the xAI Voice Agent API transparently (resumption, backoff)."""
    await GROK_LIVE_BRIDGE.reconnect(session)


async def grok_keepalive_loop(session: 'GrokLiveSession'):
    """Keepalive silence so xAI's idle timeout never ends a quiet session."""
    await GROK_LIVE_BRIDGE.keepalive_loop(session)


async def grok_listener(session: 'GrokLiveSession'):
    """Read Grok's frames and forward them to the client."""
    await GROK_LIVE_BRIDGE.listener(session)


# =============================================================================
# WebSocket Endpoint
# =============================================================================

@app.websocket("/ws/grok-live/{session_id}")
async def grok_live_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for xAI Grok Voice Agent API bridge.

    Bridges Portal <-> Orchestrator <-> xAI Grok Voice API through the shared
    LiveVoiceBridge (Orchestrator/voice/bridge.py):
    1. Portal connects, sends 'connect' with operator and voice
    2. Orchestrator connects to Grok Voice Agent API
    3. Orchestrator configures session with tools and context
    4. Bidirectional message passing begins
    5. Tool calls are executed locally and results sent to Grok
    """
    await GROK_LIVE_BRIDGE.serve(websocket, session_id)


async def _end_portal_session(session: 'GrokLiveSession', websocket, generation: int,
                              keepalive_task) -> None:
    """Teardown when this handler's client WS ends (voice_ws_shared.end_portal_session).

    Only the CURRENT owner tears the session down, and it stops touching the
    session the moment a re-attach claims it, even mid-teardown: a transcript
    save that outlasted the re-attach used to leave the re-attached session
    deaf and reapable (O09-live-voice-providers-01).
    """
    await GROK_LIVE_BRIDGE.end_portal_session(session, websocket, generation, keepalive_task)

# =============================================================================
# HTTP Endpoints
# =============================================================================

@app.get("/grok-live/status")
async def grok_live_status():
    """Get status of Grok Voice Agent API integration."""
    return {
        "available": WEBSOCKETS_AVAILABLE and bool(XAI_API_KEY),
        "websockets_installed": WEBSOCKETS_AVAILABLE,
        "api_key_configured": bool(XAI_API_KEY),
        "model_default": GROK_LIVE_MODEL,
        "models": GROK_LIVE_MODELS,
        "voices": GROK_LIVE_VOICES,
        "default_voice": GROK_LIVE_DEFAULT_VOICE,
        "sample_rate": GROK_LIVE_SAMPLE_RATE,
        "input_sample_rate": GROK_LIVE_INPUT_SAMPLE_RATE,
        "output_sample_rate": GROK_LIVE_OUTPUT_SAMPLE_RATE,
        "active_sessions": len([s for s in GROK_LIVE_SESSIONS.values() if s.status == "connected"])
    }

@app.get("/grok-live/sessions")
async def list_grok_live_sessions():
    """List active Grok Voice sessions."""
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "operator": s.operator,
                "voice": s.voice,
                "status": s.status,
                "created_at": s.created_at,
                "last_activity": s.last_activity
            }
            for s in GROK_LIVE_SESSIONS.values()
        ]
    }
