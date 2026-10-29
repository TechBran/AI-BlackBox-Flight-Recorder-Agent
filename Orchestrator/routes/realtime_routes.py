#!/usr/bin/env python3
"""
realtime_routes.py - OpenAI Realtime API (GA) WebSocket Bridge

This module provides a WebSocket bridge between the Portal frontend and
OpenAI's Realtime API (gpt-realtime-2.1 generation), enabling real-time voice conversations with
semantic search capabilities over the BlackBox snapshot volume.

Architecture:
    Portal (Browser) <--WebSocket--> Orchestrator <--WebSocket--> OpenAI Realtime API

Features:
- Bidirectional audio/text streaming
- Tool calling (search_snapshots for semantic search)
- Automatic context injection (checkpoint + recent snapshots)
- Session management with reconnection support
"""

# Standard library imports
import base64
import json
import time
from typing import Optional, Dict, Any

# External library imports
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("[REALTIME] websockets library not installed - run: pip install websockets")

from fastapi import WebSocket

# Local imports
from Orchestrator.checkpoint import app
from Orchestrator.config import (
    OPENAI_API_KEY,
    OPENAI_REALTIME_URL,
    OPENAI_REALTIME_MODEL,
    OPENAI_REALTIME_MODELS,
    OPENAI_REALTIME_TRANSLATE_MODEL,
    OPENAI_REALTIME_VOICES,
    OPENAI_REALTIME_DEFAULT_VOICE,
    OPENAI_REALTIME_VAD_TYPES,
    OPENAI_REALTIME_VAD_EAGERNESS,
    OPENAI_REALTIME_NOISE_REDUCTION_TYPES,
    OPENAI_REALTIME_TRANSCRIPTION_DELAYS,
    REALTIME_CONTEXT_MAX_CHARS,
    STT_OPENAI_STREAM,
)
from Orchestrator.models import RealtimeSession, REALTIME_SESSIONS
from Orchestrator.volume import now_utc_iso
from Orchestrator.context_builder import build_fossil_context
from Orchestrator.utils.async_helpers import run_blocking
from Orchestrator.whisper_filter import is_whisper_hallucination
from Orchestrator.tools.tool_registry import get_openai_realtime_tools
from Orchestrator.voice_agents.registry import resolve_preset, merge_connect_params
from Orchestrator.behavioral_core import get_persona, VOICE_DELIVERY_NOTE
from Orchestrator.routes.voice_prompts import build_voice_instructions
from Orchestrator.routes.voice_translate import (
    resolve_translate_params,
    build_translate_instructions,
)
from Orchestrator.routes.voice_ws_shared import (
    adopt_upstream,
    safe_send,
    save_voice_transcript,
    send_openai_style_tool_error,
)
from Orchestrator.voice import tools as voice_tools
from Orchestrator.voice.bridge import LiveVoiceBridge, VoiceAdapter
from Orchestrator.voice.transcript import save_session_transcript

LOG_PREFIX = "[REALTIME]"

_safe_ws_send = safe_send

# =============================================================================
# Tool Definitions — read FRESH at session-configure time (P1b)
# =============================================================================
# No import-time snapshot here: get_openai_realtime_tools("realtime") is called
# inside configure_openai_session, so POST /toolvault/reload (which busts the
# registry cache) reaches the NEXT voice session/reconnect without a restart.

# =============================================================================
# Session Saving
# =============================================================================

async def save_session_to_blackbox(session: RealtimeSession):
    """
    Save the OpenAI Realtime session conversation to the BlackBox ledger.

    Called on disconnect/cleanup (endpoint finally, reconnect exhaustion,
    phone bridge teardown, live-session reaper). P1b: persists via POST
    /chat/save (direct persistence + auto-mint; no LLM round-trip) and clears
    session.conversation ONLY once the transcript is saved or durably in the
    retry spool, which the live-session reaper drains.
    """
    await save_session_transcript(
        session,
        title="OpenAI Realtime Voice Session",
        fields=[("Timestamp", now_utc_iso())],
        ai_label="AI",
        user_message=f"[Voice Session Transcript] OpenAI Realtime voice session {session.session_id}",
        model_label="openai-realtime-voice",
        log_prefix=LOG_PREFIX,
        persist=save_voice_transcript,
    )


# =============================================================================
# Context Injection
# =============================================================================

def build_context_for_operator(operator: str, user_text: str = "") -> tuple[str, dict]:
    """
    Build initial context for an OpenAI Realtime session.

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
        user_text, operator, log_prefix="[REALTIME]"
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

async def execute_search_snapshots(session: RealtimeSession, arguments: Dict) -> str:
    """The search_snapshots tool (Orchestrator/voice/tools.py)."""
    return await voice_tools.execute_search_snapshots(session, arguments, log_prefix=LOG_PREFIX)

# =============================================================================
# OpenAI Realtime API Connection
# =============================================================================

async def connect_to_openai(session: RealtimeSession, model: Optional[str] = None) -> bool:
    """
    Establish WebSocket connection to OpenAI Realtime API.

    Args:
        session: RealtimeSession object
        model: Optional model id override (e.g. "gpt-realtime-2", "gpt-realtime-mini-2025-12-15").
            Validated against OPENAI_REALTIME_MODELS — any category is accepted at this
            layer (UI does chat-only filtering). Invalid values fall back to the
            OPENAI_REALTIME_MODEL default with a logged warning. Per OpenAI API the
            model is bound at WS-connect via URL query, NOT via session.update
            (audit C3).

    Returns True if connection successful, False otherwise.
    """
    if not WEBSOCKETS_AVAILABLE:
        print("[REALTIME] Cannot connect - websockets library not installed")
        return False

    if not OPENAI_API_KEY:
        print("[REALTIME] Cannot connect - OPENAI_API_KEY not set")
        return False

    # Resolve + validate model (allowlist from OPENAI_REALTIME_MODELS)
    _allowed_model_ids = {m["id"] for m in OPENAI_REALTIME_MODELS}
    if model and model not in _allowed_model_ids:
        print(f"[REALTIME] WARNING: model {model!r} not in OPENAI_REALTIME_MODELS allowlist; falling back to default {OPENAI_REALTIME_MODEL!r}")
        model = None
    resolved_model = model or OPENAI_REALTIME_MODEL

    try:
        url = f"{OPENAI_REALTIME_URL}?model={resolved_model}"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        }

        print(f"[REALTIME] Connecting to OpenAI: {url}")
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
        if not await adopt_upstream(session, "openai_ws", ws, generation, "[REALTIME]"):
            return False
        session.status = "connected"
        session.last_activity = now_utc_iso()
        print(f"[REALTIME] Connected to OpenAI for session {session.session_id}")
        return True

    except Exception as e:
        print(f"[REALTIME] Connection failed: {e}")
        session.status = "error"
        return False

async def configure_openai_session(
    session: RealtimeSession,
    operator: str,
    voice: str = "ash",
    custom_role: str = "",
    vad_type: Optional[str] = None,
    vad_eagerness: Optional[str] = None,
    idle_timeout_ms: Optional[int] = None,
    interrupt_response: Optional[bool] = None,
    create_response: Optional[bool] = None,
    noise_reduction: Optional[str] = None,
    transcription_delay: Optional[str] = None,
    tool_group_override: Optional[str] = None,
    mode: Optional[str] = None,
    target_language: Optional[str] = None,
):
    """
    Configure the OpenAI Realtime session with tools and settings.
    Injects operator-specific context and personalization.

    Args:
        session: RealtimeSession object
        operator: Operator name for context
        voice: Voice to use (alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar)
        custom_role: Optional custom system prompt/persona for outbound calls
        vad_type: Optional VAD mode — "server_vad" (default) or "semantic_vad".
            Invalid values fall back to existing server_vad default with a logged warning.
        vad_eagerness: Optional semantic_vad eagerness — "low" | "medium" | "high" | "auto".
            Ignored unless vad_type == "semantic_vad". When None, server picks default.
        idle_timeout_ms: Optional server_vad idle timeout in ms (5000-300000 typical range).
            Per OpenAI SDK type stubs, idle_timeout_ms is server_vad-only — ignored when
            vad_type == "semantic_vad".
        interrupt_response: Optional bool — apply to BOTH server_vad and semantic_vad
            (audit I3 — both fields exist on both turn-detection shapes per SDK type stubs).
        create_response: Optional bool — apply to BOTH server_vad and semantic_vad.
        mode: Optional session mode — "translate" builds a minimal tool-free
            translation session (P6a); anything else = normal voice session.
        target_language: BCP-47 target for translate mode; malformed/missing
            values fall back to "en" with a logged warning.

        All new VAD-related kwargs default to None to preserve phone bridge
        positional-arg call sites (audit C2 — phone/bridge.py lines 744, 813, 848,
        1286, 1322, 1370 must keep working unchanged).
    """
    if not session.openai_ws:
        return

    # Allowlist validation of client-supplied VAD fields.
    if vad_type is not None and vad_type not in OPENAI_REALTIME_VAD_TYPES:
        print(f"[REALTIME] WARNING: vad_type {vad_type!r} not in {OPENAI_REALTIME_VAD_TYPES}; falling back to server_vad default")
        vad_type = None
    if vad_eagerness is not None and vad_eagerness not in OPENAI_REALTIME_VAD_EAGERNESS:
        print(f"[REALTIME] WARNING: vad_eagerness {vad_eagerness!r} not in {OPENAI_REALTIME_VAD_EAGERNESS}; ignoring")
        vad_eagerness = None
    # Server-side clamp on idle_timeout_ms. HTML dropdown enforces min=5000
    # max=300000, but JS parseInt() strips that constraint — a stale or
    # hostile client could otherwise send idle_timeout_ms=1 straight to
    # OpenAI. Range mirrors the HTML UI semantics. (T14 F2)
    if idle_timeout_ms is not None:
        if not isinstance(idle_timeout_ms, int) or not (5000 <= idle_timeout_ms <= 300000):
            print(f"[REALTIME] WARNING: idle_timeout_ms {idle_timeout_ms!r} out of range (5000-300000); ignoring")
            idle_timeout_ms = None

    # noise_reduction (GA 2026 schema) — allowlist-validated, then phone default.
    if noise_reduction is not None and noise_reduction not in OPENAI_REALTIME_NOISE_REDUCTION_TYPES:
        print(f"[REALTIME] WARNING: noise_reduction {noise_reduction!r} not in {OPENAI_REALTIME_NOISE_REDUCTION_TYPES}; ignoring")
        noise_reduction = None
    # Phone-bridge sessions are keyed "phone-<sid>" (phone/bridge.py) and call
    # this function positionally — telephony defaults to near_field (applied
    # upstream before VAD + model). Portal/Android stay unset unless the client
    # opts in via connect message / query param.
    if noise_reduction is None and session.session_id.startswith("phone-"):
        noise_reduction = "near_field"

    if transcription_delay is not None and transcription_delay not in OPENAI_REALTIME_TRANSCRIPTION_DELAYS:
        print(f"[REALTIME] WARNING: transcription_delay {transcription_delay!r} not in {OPENAI_REALTIME_TRANSCRIPTION_DELAYS}; ignoring")
        transcription_delay = None

    # ── Translation mode (P6a): minimal session — NO persona/context/tools ──
    # Branch BEFORE the persona/context build: fastest possible setup is the
    # entire point (design doc workstream 5). Session shape = GA voice shape
    # minus tools/tool_choice, confirmed by the P0 probe
    # (diagnostics/voice_probes/results/*-translate.json — combined P0.5 file,
    # openai gpt-realtime-translate entry).
    is_translate, resolved_target_language = resolve_translate_params(
        mode, target_language, log_prefix="[REALTIME]")
    if is_translate:
        session.provenance = {}  # no snapshot retrieval in translate mode
        config_event = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                "instructions": build_translate_instructions(resolved_target_language),
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": STT_OPENAI_STREAM},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.7,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 800,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": voice,
                        "speed": 1.0,
                    },
                },
                # NO tools / tool_choice — translation sessions are tool-free.
            },
        }
        await session.openai_ws.send(json.dumps(config_event))
        session.context_injected = True
        print(f"[REALTIME] TRANSLATE session configured "
              f"(target={resolved_target_language}, voice={voice})")
        return

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
        "openai", operator, context, custom_role,
        persona=lambda: get_persona(operator, "voice") + "\n\n" + VOICE_DELIVERY_NOTE)

    # Build turn_detection payload based on vad_type.
    # - server_vad (default): existing shape + optional idle_timeout_ms
    # - semantic_vad: {type, eagerness?, interrupt_response?, create_response?}
    #   (no idle_timeout_ms — per OpenAI SDK, server_vad-only).
    # interrupt_response / create_response apply to BOTH modes per SDK type stubs
    # (audit I3).
    if vad_type == "semantic_vad":
        turn_detection: Dict[str, Any] = {"type": "semantic_vad"}
        if vad_eagerness is not None:
            turn_detection["eagerness"] = vad_eagerness
        if interrupt_response is not None:
            turn_detection["interrupt_response"] = interrupt_response
        if create_response is not None:
            turn_detection["create_response"] = create_response
    else:
        # server_vad (None or explicit)
        turn_detection = {
            "type": "server_vad",
            "threshold": 0.7,           # Sensitivity (0.0-1.0) — raised from 0.5 to reduce noise triggers
            "prefix_padding_ms": 300,   # Audio to include before speech detected
            "silence_duration_ms": 800  # Silence before end of turn (raised from 700 for cleaner cuts)
        }
        if idle_timeout_ms is not None:
            turn_detection["idle_timeout_ms"] = idle_timeout_ms
        if interrupt_response is not None:
            turn_detection["interrupt_response"] = interrupt_response
        if create_response is not None:
            turn_detection["create_response"] = create_response

    # P1b: read tools FRESH (not at import) so /toolvault/reload reaches voice.
    # P4: a voice-agent preset can swap the tool group at configure time.
    realtime_tools = get_openai_realtime_tools(tool_group_override or "realtime")

    # Configure session — GA wire format (Beta deprecated 2026-05-19).
    # Per empirical probe of OpenAI Realtime GA endpoint without OpenAI-Beta header:
    # - session.type "realtime" is now required
    # - modalities renamed to output_modalities (audio-only on GA voice path)
    # - voice, *_audio_format, transcription, turn_detection all moved under
    #   session.audio.{input,output} nesting per GA wire-format diff
    # - temperature removed (not present in GA session shape)
    # See docs/plans/2026-05-19-live-api-ga-migration.md Track 1B for the diff table.
    config_event = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": system_instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": STT_OPENAI_STREAM},
                    "turn_detection": turn_detection,
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": voice,  # Voice selected by user - options: alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar
                    "speed": 1.0,
                },
            },
            "tools": realtime_tools,
            "tool_choice": "auto",
        }
    }

    # Additive GA field — omitted entirely when unset (provider default applies).
    if noise_reduction == "off":
        config_event["session"]["audio"]["input"]["noise_reduction"] = None
    elif noise_reduction is not None:
        config_event["session"]["audio"]["input"]["noise_reduction"] = {"type": noise_reduction}
    if transcription_delay is not None:
        config_event["session"]["audio"]["input"]["transcription"]["delay"] = transcription_delay

    await session.openai_ws.send(json.dumps(config_event))
    session.context_injected = True
    print(f"[REALTIME] Session configured for operator {operator}")

# =============================================================================
# Message Handlers
# =============================================================================

async def handle_portal_message(session: RealtimeSession, data: Dict):
    """
    Handle messages from Portal and forward to OpenAI.

    Message types from Portal:
    - audio_input: Base64 PCM16 audio chunk
    - audio_commit: End of audio input, request response
    - text_input: Text message
    - interrupt: Cancel current response
    - video_frame: Base64 JPEG frame for multimodal vision (screen sharing / camera)
    """
    msg_type = data.get("type", "")

    if msg_type == "audio_input":
        # Forward audio to OpenAI
        if session.openai_ws:
            await session.openai_ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": data.get("data", ""),  # Base64 PCM16
            }))
            session.is_recording = True
            session.last_activity = now_utc_iso()
    elif msg_type == "audio_commit":
        # In OpenAI Realtime GA, when turn_detection is set (server_vad or
        # semantic_vad — we always set one), OpenAI auto-commits the buffer on
        # detected speech-end AND auto-creates a response (per create_response:
        # true in turn_detection). Manual input_audio_buffer.commit +
        # response.create from the client COLLIDES with the auto-flow — OpenAI
        # returns "buffer too small: 0.00ms" because it already drained the
        # buffer, and the late response.create can cancel the in-flight
        # auto-generated response. So this handler is a no-op for the WS side.
        #
        # Tool-result response.create at the function-call-arguments-done
        # handler is unaffected — that path is correct because tool results
        # don't auto-trigger responses.
        session.is_recording = False
        session.last_activity = now_utc_iso()

    elif msg_type == "text_input":
        # Send text message
        text = data.get("text", "")
        if session.openai_ws and text:
            # Create conversation item
            await session.openai_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}]
                }
            }))
            # Request response
            await session.openai_ws.send(json.dumps({
                "type": "response.create"
            }))
            session.last_activity = now_utc_iso()

    elif msg_type == "interrupt":
        # Cancel current response (for barge-in)
        if session.openai_ws:
            await session.openai_ws.send(json.dumps({
                "type": "response.cancel"
            }))
            # Clear audio buffer for new input
            await session.openai_ws.send(json.dumps({
                "type": "input_audio_buffer.clear"
            }))
            session.is_speaking = False

    elif msg_type == "video_frame":
        # Forward video frame to OpenAI for multimodal vision (screen sharing / camera)
        # OpenAI Realtime API accepts images via conversation.item.create with input_image
        # Frame rate: 1 FPS recommended (same as Gemini Live)
        frame_data = data.get("data", "")
        if session.openai_ws and frame_data:
            print(f"[REALTIME] Received video_frame, size={len(frame_data)} bytes")
            # Create conversation item with image
            # OpenAI expects data URL format: data:image/jpeg;base64,{data}
            try:
                await session.openai_ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{
                            "type": "input_image",
                            "image_url": f"data:image/jpeg;base64,{frame_data}"
                        }]
                    }
                }))
                print(f"[REALTIME] Sent video frame to OpenAI")
                session.last_activity = now_utc_iso()
            except Exception as e:
                print(f"[REALTIME] Error sending video frame to OpenAI: {e}")
            # Note: Don't request response for each frame - let audio/text trigger responses
            # The model will consider the latest frame(s) when responding
        else:
            print(f"[REALTIME] video_frame received but ws={session.openai_ws is not None}, data_len={len(frame_data) if frame_data else 0}")

async def handle_openai_message(session: RealtimeSession, event: Dict):
    """
    Handle messages from OpenAI and forward to Portal.

    Key event types (GA — Beta names had no output_ infix):
    - response.output_audio.delta: Audio chunk to play
    - response.output_audio_transcript.delta: Text transcript of audio
    - response.output_text.delta: Text response (for text-only)
    - response.function_call_arguments.done: Execute tool
    - response.done: Response complete
    - error: Error occurred
    """
    session.last_ai_message_time = time.time()

    event_type = event.get("type", "")

    if event_type == "response.output_audio.delta":
        # Forward audio chunk to Portal
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "audio_delta",
                "data": event.get("delta", "")
            })
            session.is_speaking = True

    elif event_type == "response.output_audio_transcript.delta":
        # Forward transcript to Portal
        delta = event.get("delta", "")
        session.transcript_buffer += delta
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "transcript_delta",
                "data": delta
            })

    elif event_type == "response.output_text.delta":
        # Forward text response to Portal
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "text_delta",
                "data": event.get("delta", "")
            })

    elif event_type == "response.function_call_arguments.done":
        # The OpenAI-style function call (shared with xAI): malformed arguments
        # are answered with a parse error, never run with {}; the call runs OFF
        # this listener and is answered on the socket it came from.
        await voice_tools.dispatch_openai_style_call(
            session, event, ws_attr="openai_ws", log_prefix=LOG_PREFIX,
            execute=lambda name, arguments: _execute_openai_tool(session, name, arguments))

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
        # User's voice input was transcribed by Whisper
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
                print(f"[REALTIME] User voice transcription: {transcript[:100]}...")
                await _safe_ws_send(session.portal_ws, {
                    "type": "user_transcript",
                    "data": transcript
                })
        elif transcript:
            print(f"[REALTIME] Filtered Whisper hallucination: '{transcript[:80]}'")

    elif event_type == "conversation.item.input_audio_transcription.delta":
        # Incremental (interim) user transcription chunk — live word-by-word.
        # Mirrors the AI-side transcript_delta convention; .completed still
        # emits the authoritative final user_transcript below.
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

    elif event_type == "error":
        # Error from OpenAI
        error = event.get("error", {})
        error_msg = error.get("message", "Unknown error")
        print(f"[REALTIME] OpenAI error: {error_msg}")
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "error",
                "data": error_msg
            })


async def _execute_openai_tool(session: RealtimeSession, name: str, arguments: Dict) -> str:
    """Run ONE tool call of the session and return its result text (the shared
    voice tool ladder, Orchestrator/voice/tools.py)."""
    return await voice_tools.execute_voice_tool(session, name, arguments, log_prefix=LOG_PREFIX,
                                                created_by="openai-realtime")


# =============================================================================
# The OpenAI adapter of the shared voice bridge (Orchestrator/voice/bridge.py)
# =============================================================================

# 20 ms of PCM16 silence at 24 kHz (480 samples = 960 bytes).
_SILENCE_20MS_B64 = base64.b64encode(b'\x00' * 960).decode('ascii')


def _query_bool(value: Optional[str]) -> Optional[bool]:
    return value.lower() == "true" if value is not None else None


class OpenAIRealtimeAdapter(VoiceAdapter):
    """OpenAI Realtime's half of the shared LiveVoiceBridge: the GA
    session.update, the realtime events, the model bound at the WS URL, and
    translate sessions that rebind the translate model on reconnect."""

    name = "OpenAI"
    log_prefix = LOG_PREFIX
    ws_attr = "openai_ws"
    key_name = "OPENAI_API_KEY"
    connecting_status = "Connecting to OpenAI..."
    connect_failed = "Failed to connect to OpenAI Realtime API"

    def sessions(self):
        return REALTIME_SESSIONS

    def new_session(self, session_id):
        return RealtimeSession(session_id=session_id, created_at=now_utc_iso())

    def websockets_available(self):
        return WEBSOCKETS_AVAILABLE

    def api_key(self):
        return OPENAI_API_KEY

    def url_params(self, websocket):
        q = websocket.query_params
        url = {key: q.get(key) for key in (
            "operator", "voice", "model", "mode", "target_language", "vad_type",
            "vad_eagerness", "agent", "noise_reduction", "transcription_delay")}
        idle = q.get("idle_timeout_ms")
        if idle and idle.strip().isdigit():
            url["idle_timeout_ms"] = int(idle.strip())
        else:
            url["idle_timeout_ms"] = None
            if idle:
                print(f"{LOG_PREFIX} WARNING: idle_timeout_ms {idle!r} not a positive integer; ignoring")
        url["interrupt_response"] = _query_bool(q.get("interrupt_response"))
        url["create_response"] = _query_bool(q.get("create_response"))
        return url

    async def parse_connect(self, session, websocket, data, url):
        # Voice-agent preset (P4): ?agent=<id> or "agent" in the JSON connect
        # message. Precedence: explicit params > preset fields > defaults. An
        # unknown preset is a loud client warning; the session goes on without.
        agent_id = data.get("agent", url["agent"])
        preset = resolve_preset(agent_id, provider="realtime") if agent_id else None
        if agent_id and preset is None:
            await safe_send(websocket, {
                "type": "warning",
                "data": f"Voice agent preset {agent_id!r} not found for provider 'realtime' — continuing without preset"
            })
        merged = merge_connect_params({
            "model": data.get("model", url["model"]),
            "voice": data.get("voice", url["voice"]),
            "greeting": data.get("greeting", ""),
            "instructions": data.get("role", ""),
        }, preset)
        operator = data.get("operator", url["operator"] or "")
        # The model goes to connect_to_openai (URL query); the VAD, timeout and
        # response fields to configure_openai_session (session.update).
        model = merged["model"]
        mode = data.get("mode", url["mode"])
        target_language = data.get("target_language", url["target_language"])
        is_translate, _ = resolve_translate_params(mode, target_language, log_prefix=LOG_PREFIX)
        if is_translate:
            # Translate sessions ALWAYS bind the dedicated translate model —
            # the backend override beats any client model pick.
            model = OPENAI_REALTIME_TRANSLATE_MODEL
        # Persisted for the reconnect path (P6a).
        session.mode = mode if is_translate else ""
        session.target_language = target_language or "" if is_translate else ""
        session.operator = operator
        return {
            "operator": operator,
            "voice": merged["voice"] or "ash",          # route default unchanged
            "greeting": merged["greeting"] or "",
            "role": merged["instructions"] or "",       # preset instructions ride the custom_role branch
            "model": model,
            "tool_group_override": merged["tool_group_override"],
            "vad_type": data.get("vad_type", url["vad_type"]),
            "vad_eagerness": data.get("vad_eagerness", url["vad_eagerness"]),
            "idle_timeout_ms": data.get("idle_timeout_ms", url["idle_timeout_ms"]),
            "interrupt_response": data.get("interrupt_response", url["interrupt_response"]),
            "create_response": data.get("create_response", url["create_response"]),
            "noise_reduction": data.get("noise_reduction", url["noise_reduction"]),
            "transcription_delay": data.get("transcription_delay", url["transcription_delay"]),
            "mode": mode,
            "target_language": target_language,
        }

    async def dial(self, session, params):
        # The model is bound at the upstream WS URL (audit C3).
        return await connect_to_openai(session, model=params["model"])

    async def configure(self, session, params):
        await configure_openai_session(
            session,
            params["operator"],
            params["voice"],
            custom_role=params["role"],
            tool_group_override=params["tool_group_override"],
            vad_type=params["vad_type"],
            vad_eagerness=params["vad_eagerness"],
            idle_timeout_ms=params["idle_timeout_ms"],
            interrupt_response=params["interrupt_response"],
            create_response=params["create_response"],
            noise_reduction=params["noise_reduction"],
            transcription_delay=params["transcription_delay"],
            mode=params["mode"],
            target_language=params["target_language"],
        )
        print(f"{LOG_PREFIX} Voice selected: {params['voice']}; "
              f"model={params['model'] or OPENAI_REALTIME_MODEL}; "
              f"vad_type={params['vad_type'] or 'server_vad'}")

    async def inject_greeting(self, session, greeting):
        prompt = f"The user just answered the phone. Greet them and deliver this message: {greeting}"
        await session.openai_ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}]
            }
        }))
        await session.openai_ws.send(json.dumps({"type": "response.create"}))

    def connected_data(self, session, session_id, params):
        return {
            "session_id": session_id,
            "operator": params["operator"],
            "model": params["model"] or OPENAI_REALTIME_MODEL
        }

    async def handle_client(self, session, data):
        await handle_portal_message(session, data)

    async def handle_event(self, session, event):
        await handle_openai_message(session, event)

    async def answer_failed_event(self, session, event, exc):
        # A function call whose handling raised is answered with an error
        # payload + response.create, so the model recovers instead of waiting.
        await send_openai_style_tool_error(session.openai_ws, session.portal_ws, event, exc)

    def on_frame(self, session):
        if session.reconnect_count:
            # Proven-healthy: a real server frame arrived on this socket.
            session.reconnect_count = 0

    def keepalive_frame(self, session):
        return {"type": "input_audio_buffer.append", "audio": _SILENCE_20MS_B64}

    async def redial(self, session):
        # Translate sessions must rebind the translate model and must NOT
        # degrade into a full persona/tool session (P6a).
        return await connect_to_openai(
            session,
            model=OPENAI_REALTIME_TRANSLATE_MODEL if session.mode == "translate" else None,
        )

    async def reconfigure(self, session):
        await configure_openai_session(
            session,
            session.operator,
            mode=session.mode or None,
            target_language=session.target_language or None,
        )
        return True

    async def save(self, session):
        await save_session_to_blackbox(session)

    def reconnect(self, session):
        return openai_reconnect(session)

    def listener(self, session):
        return openai_listener(session)

    def keepalive(self, session):
        return openai_keepalive_loop(session)


REALTIME_BRIDGE = LiveVoiceBridge(OpenAIRealtimeAdapter())


async def openai_reconnect(session: RealtimeSession):
    """Reconnect to the OpenAI Realtime API transparently (backoff, reconfigure)."""
    await REALTIME_BRIDGE.reconnect(session)


async def openai_keepalive_loop(session: RealtimeSession):
    """Keepalive silence so OpenAI's idle timeout never ends a quiet session
    (OpenAI sends nothing while the user is quiet or a tool runs)."""
    await REALTIME_BRIDGE.keepalive_loop(session)


async def openai_listener(session: RealtimeSession):
    """Read OpenAI's frames and forward them to the client."""
    await REALTIME_BRIDGE.listener(session)


# =============================================================================
# WebSocket Endpoint
# =============================================================================

@app.websocket("/ws/realtime/{session_id}")
async def realtime_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for the OpenAI Realtime API bridge.

    Bridges Portal <-> Orchestrator <-> OpenAI Realtime API through the shared
    LiveVoiceBridge (Orchestrator/voice/bridge.py):
    1. Portal connects, sends 'connect' with operator
    2. Orchestrator connects to OpenAI Realtime API
    3. Orchestrator configures session with tools and context
    4. Bidirectional message passing begins
    5. Tool calls are executed locally and results sent to OpenAI
    """
    await REALTIME_BRIDGE.serve(websocket, session_id)


async def _end_portal_session(session: RealtimeSession, websocket, generation: int,
                              keepalive_task) -> None:
    """Teardown when this handler's client WS ends (voice_ws_shared.end_portal_session).

    Only the CURRENT owner tears the session down, and it stops touching the
    session the moment a re-attach claims it, even mid-teardown: a transcript
    save that outlasted the re-attach used to leave the re-attached session
    deaf and reapable (O09-live-voice-providers-01).
    """
    await REALTIME_BRIDGE.end_portal_session(session, websocket, generation, keepalive_task)

# =============================================================================
# HTTP Endpoints
# =============================================================================

@app.get("/realtime/status")
async def realtime_status():
    """Get status of Realtime API integration.

    Emits the locked status-shape from plan v2 Architecture section:
    - enabled: API key configured?
    - model_default / models[]: dropdown catalog, FILTERED to category=="chat"
      (audit I4 — whisper is STT-only, translate is specialized — both would
      silently fail in a voice-conversation dropdown).
    - voice_default / voices[]: flat string array (no descriptors — OpenAI
      voices have no character descriptors; that's a Gemini-only field).
    """
    return {
        "available": WEBSOCKETS_AVAILABLE and bool(OPENAI_API_KEY),
        "websockets_installed": WEBSOCKETS_AVAILABLE,
        "api_key_configured": bool(OPENAI_API_KEY),
        "enabled": bool(OPENAI_API_KEY),
        # Legacy single-value field — preserved for backwards compat with any
        # caller that hasn't migrated to model_default yet.
        "model": OPENAI_REALTIME_MODEL,
        # Locked v2 catalog shape:
        "model_default": OPENAI_REALTIME_MODEL,
        "models": [m for m in OPENAI_REALTIME_MODELS if m.get("category") == "chat"],
        "voice_default": OPENAI_REALTIME_DEFAULT_VOICE,
        "voices": list(OPENAI_REALTIME_VOICES),
        "active_sessions": len([s for s in REALTIME_SESSIONS.values() if s.status == "connected"]),
    }

@app.get("/realtime/sessions")
async def list_realtime_sessions():
    """List active Realtime sessions."""
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "operator": s.operator,
                "status": s.status,
                "created_at": s.created_at,
                "last_activity": s.last_activity
            }
            for s in REALTIME_SESSIONS.values()
        ]
    }
