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
import os
import time
from typing import Optional, Dict, Any

# HTTP client for saving sessions
import aiohttp

# External library imports
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("[GROK-LIVE] websockets library not installed - run: pip install websockets")

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

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
    REALTIME_SNAPSHOT_CHARS_EACH,
    VOL_PATH
)
from Orchestrator.models import GrokLiveSession, GROK_LIVE_SESSIONS, TaskType
from Orchestrator.volume import now_utc_iso, read_text_safe
from Orchestrator.live_session_reaper import release_payload
from Orchestrator.fossils import (
    hybrid_retrieve,
    format_snapshot_for_delivery,
    get_recent_fossils_for_operator,
    get_recent_checkpoints_for_operator
)
from Orchestrator.context_builder import build_fossil_context
from Orchestrator.web_tools import perform_web_fetch
from Orchestrator.tasks import create_task
from Orchestrator.image_providers import IMAGE_TOOL_PROVIDERS
from Orchestrator.whisper_filter import is_whisper_hallucination
from Orchestrator.tools.tool_registry import get_openai_realtime_tools
from Orchestrator.voice_agents.registry import resolve_preset, merge_connect_params
from Orchestrator.behavioral_core import get_persona, VOICE_DELIVERY_NOTE
from Orchestrator.routes.voice_prompts import CU_CONTROL_BLOCK
from Orchestrator.routes.voice_ws_shared import (
    save_voice_transcript,
    send_openai_style_tool_error,
)


async def _safe_ws_send(websocket, data: dict) -> bool:
    """Send JSON to WebSocket, return False if connection is dead."""
    try:
        if websocket and hasattr(websocket, 'application_state') and websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_json(data)
            return True
    except Exception:
        pass
    return False

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
    Called on disconnect/cleanup to ensure all messages are captured.
    """
    if not session.conversation:
        print(f"[GROK-LIVE] No conversation to save for session {session.session_id}")
        return

    if not session.operator:
        print(f"[GROK-LIVE] No operator set, cannot save session {session.session_id}")
        return

    # Sort conversation by timestamp to ensure correct order
    sorted_conversation = sorted(
        session.conversation,
        key=lambda x: x.get("timestamp", "")
    )

    # Format conversation as readable transcript
    transcript_lines = []
    for msg in sorted_conversation:
        role = "User" if msg["role"] == "user" else "AI (Grok)"
        transcript_lines.append(f"[{role}]: {msg['content']}")

    transcript = "\n\n".join(transcript_lines)

    session_summary = f"""=== Grok Voice Agent Session ===
Session ID: {session.session_id}
Timestamp: {now_utc_iso()}
Voice: {session.voice}
Messages: {len(session.conversation)}

--- Transcript ---
{transcript}
--- End Session ---"""

    print(f"[GROK-LIVE] Saving session {session.session_id} with {len(session.conversation)} messages to BlackBox")

    saved = await save_voice_transcript(
        operator=session.operator,
        user_message=f"[Grok Voice Session Transcript] Grok voice session {session.session_id}",
        session_summary=session_summary,
        model_label="grok-live-voice",
        log_prefix="[GROK-LIVE]",
    )

    # P1b BUGFIX: previously cleared UNCONDITIONALLY — a failed save
    # permanently lost the transcript. Clear only on confirmed 200.
    if saved:
        session.conversation = []
    else:
        print(f"[GROK-LIVE] Save FAILED — keeping {len(session.conversation)} turns for a later retry")


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
    """
    Execute the search_snapshots tool.

    Uses hybrid retrieval (keyword + semantic) to find relevant snapshots.
    Returns formatted results for the model.
    """
    query = arguments.get("query", "")
    k = min(arguments.get("k", 3), 5)  # Cap at 5 results

    if not query:
        return "Error: No search query provided."

    try:
        vol_txt = read_text_safe(VOL_PATH)

        # Use hybrid retrieval for best results
        results = hybrid_retrieve(vol_txt, query, k=k, operator=session.operator)

        if not results:
            return f"No snapshots found matching: {query}"

        # Format results
        output_parts = [f"Found {len(results)} relevant snapshot(s) for: {query}\n"]
        for i, snap_text in enumerate(results, 1):
            # WI-10 (M7): deliver retrieved snapshots WHOLE — no delivery truncation
            output_parts.append(f"--- Result {i} ---\n{format_snapshot_for_delivery(snap_text)}")

        return "\n\n".join(output_parts)

    except Exception as e:
        print(f"[GROK-LIVE] Search error: {e}")
        return f"Search failed: {str(e)}"

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
        # websockets 15.x uses additional_headers instead of extra_headers
        # Add explicit ping settings to prevent connection drops
        session.grok_ws = await websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=10,       # 10s max to establish connection (prevents indefinite hang)
            ping_interval=20,      # Send ping every 20 seconds
            ping_timeout=30,       # Wait 30 seconds for pong response
            close_timeout=10,      # Wait 10 seconds for close handshake
        )
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

    # Validate voice
    if voice not in GROK_LIVE_VOICES:
        voice = GROK_LIVE_DEFAULT_VOICE
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
    context, provenance = build_context_for_operator(operator, user_text="")
    # Stash provenance on the session so the WS endpoint can emit it to
    # the client right after configuration.
    session.provenance = provenance
    is_system_operator = (operator == "system")

    # If custom_role is provided, use it as the primary system instruction
    if custom_role:
        system_instructions = f"""{custom_role}

TEMPORAL AWARENESS — FIRST ACTION:
Your VERY FIRST action must be to call get_current_time to anchor yourself in the present. Do this before any other tool calls or responses.

ESSENTIAL TOOLS:
You have access to search_snapshots and list_recent_snapshots for memory/context.
You can also generate images, videos, music, and send SMS or make phone calls.
You have search_contacts and save_contact for the contact book.
You can create, edit, and search scheduled cron jobs for automated tasks and reminders.
Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.

{CU_CONTROL_BLOCK}

VOICE INTERACTION:
This is a real-time voice conversation. Be concise and natural. The person on the phone cannot see text - speak clearly.
You're Grok - be witty, direct, and occasionally irreverent when appropriate."""

    else:
        # Default system prompt construction
        # Different identity section for system vs named operators
        if is_system_operator:
            identity_section = """OPERATOR IDENTITY:
This is an OUTBOUND CALL or system-initiated session. You may be calling someone on behalf of a user.
- Do NOT address the person as "system" - just speak naturally and conversationally
- Check recent snapshots IMMEDIATELY for task context (who to call, what to say, order details, etc.)
- You have access to ALL snapshots across all operators for context handoff
- Focus on completing the task that was set up before this call was initiated"""
            memory_section = """MEMORY ACCESS — YOUR MOST IMPORTANT CAPABILITY:
The BlackBox contains 1,600+ snapshots — your complete memory of every conversation, decision, and preference.
Search snapshots FIRST and OFTEN. Since this is a system session, you can see ALL operators' snapshots for context handoff.
Don't guess at history — the answers are in the snapshots. Call search_snapshots proactively."""
        else:
            identity_section = f"""OPERATOR IDENTITY:
You are currently speaking with: {operator}
Always address them by their name ({operator}) when appropriate. This is their personal AI session."""
            memory_section = f"""MEMORY ACCESS — YOUR MOST IMPORTANT CAPABILITY:
The BlackBox contains 1,600+ snapshots — your complete memory of {operator}'s history.
Search snapshots FIRST and OFTEN — before answering questions about past work, before guessing at context, before starting any task.
Everything about {operator}'s projects, preferences, past decisions, and recent activity lives in the snapshots.
Don't guess or hallucinate history — call search_snapshots proactively."""

        voice_persona = get_persona(operator, "voice") + "\n\n" + VOICE_DELIVERY_NOTE

        system_instructions = f"""{voice_persona}

IDENTITY:
You are Grok, the voice interface for the AI Black Box Flight Recorder, connected to an immutable snapshot ledger and a multimodal toolchain. The operator's memory lives in the snapshots — treat it as your external long-term memory.

TEMPORAL AWARENESS — FIRST ACTION:
Your VERY FIRST action must be to call get_current_time to anchor yourself in the present. Do this before any other tool calls or responses.

{identity_section}

{memory_section}

TOOL USAGE RULES:
CRITICAL: When you need to use a tool, you MUST invoke the actual function call.
DO NOT just say you're going to use it - ACTUALLY CALL IT using the function calling mechanism.
For example, when asked to generate an image, video, or music, invoke the function - don't just say "I'm calling it now".

MEDIA PIPELINES - CRITICAL:
You have tools to generate and find media. Here's how to use the different pipelines:

1. IMAGE-TO-VIDEO (animate an existing image):
   - First find the image URL using list_media or search_media
   - Call generate_video with the image_url parameter:
     generate_video(prompt="description of motion", image_url="/ui/uploads/image.png")
   - The image_url parameter triggers image-to-video mode - DO NOT just put the URL in the prompt

2. VIDEO EXTENSION (extend an existing video):
   - Find the video URL using list_media or search_media
   - Call generate_video with the video_url parameter:
     generate_video(prompt="continuation description", video_url="/ui/uploads/video.mp4", resolution="720p")
   - Must use 720p resolution for extensions

3. IMAGE-TO-IMAGE (use reference images):
   - Find image URLs using list_media or search_media
   - Call a per-provider image tool with the reference_images parameter:
     gemini_image(prompt="new image description", reference_images=["/ui/uploads/ref1.png", "/ui/uploads/ref2.png"])
   - Can include up to 10 reference images

4. TEXT-TO-VIDEO/IMAGE (from scratch):
   - Just use generate_video(prompt="...") or gemini_image(prompt="...") without URL parameters

FINDING MEDIA:
- Use list_media(media_type="image") to see available images
- Use list_media(media_type="video") to see available videos
- Use search_media(query="sunset") to find specific media by description

DISPLAYING MEDIA IN CHAT:
To show the user any media file directly in chat, output the full URL on its own line.
The frontend automatically renders media URLs as embedded players/images:
  /ui/uploads/sunset-mountains_abc123.png  (renders as image)
  /ui/uploads/racing-car_def456.mp4  (renders as video player)
  /ui/uploads/epic-music_ghi789.wav  (renders as audio player)
Use this to show the user which media you found with list_media or get_media BEFORE taking action on it.
This lets the user verify you have the right file before you extend a video or modify an image.

{CU_CONTROL_BLOCK}

CONTACT BOOK:
You have search_contacts and save_contact for the contact book.
Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.

SCHEDULED TASKS (CRON JOBS):
You can create, edit, and search scheduled cron jobs for automated tasks and reminders.

{"CONTEXT:" if is_system_operator else "OPERATOR-SPECIFIC CONTEXT:"}
{context if context else ("No recent context loaded yet. Use list_recent_snapshots immediately!" if is_system_operator else f"No recent context available for {operator} yet. This may be their first session or a fresh start.")}

SESSION START - CRITICAL:
IMMEDIATELY use list_recent_snapshots(count=3) at the START of EVERY session to catch up on recent context.
This is essential because:
- You may be continuing work started by another model or agent
- The snapshots contain the most recent conversations, decisions, and context
- {"For this outbound call: CHECK SNAPSHOTS for who you're calling, what task to complete, order details, etc." if is_system_operator else "For outbound calls: task details, order info, names, addresses may be in the snapshots"}
- Context handoff between models happens through snapshots - USE THEM!

Do this BEFORE responding to the user - check what happened recently so you're caught up."""

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
        # Execute tool call
        call_id = event.get("call_id", "")
        name = event.get("name", "")
        arguments_str = event.get("arguments", "{}")

        print(f"[GROK-LIVE] ===== FUNCTION CALL DETECTED =====")
        print(f"[GROK-LIVE] Call ID: {call_id}")
        print(f"[GROK-LIVE] Function name: {name}")
        print(f"[GROK-LIVE] Arguments string: {arguments_str}")

        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError as parse_err:
            # P1b: malformed arguments must NOT execute with {} — return a
            # parse error to the model so it can retry with valid JSON.
            print(f"[GROK-LIVE] Malformed tool arguments for {name}: {parse_err}")
            await send_openai_style_tool_error(
                session.grok_ws, session.portal_ws, event,
                ValueError(f"Malformed tool-call arguments JSON: {parse_err}. "
                           f"Raw arguments: {arguments_str[:200]}"),
            )
            return

        print(f"[GROK-LIVE] Tool call: {name} with args: {arguments}")

        # Notify Portal that tool is being called
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "tool_call",
                "data": {"name": name, "arguments": arguments}
            })

        # Execute the tool
        if name == "search_snapshots":
            result = await execute_grok_search_snapshots(session, arguments)
        elif name == "web_fetch":
            url = arguments.get("url", "")
            max_chars = arguments.get("max_chars", 80000)
            print(f"[GROK-LIVE] Executing web fetch: {url}")
            result = perform_web_fetch(url, max_chars)
            print(f"[GROK-LIVE] Web fetch result length: {len(result)} chars")
        elif name in IMAGE_TOOL_PROVIDERS:
            provider = IMAGE_TOOL_PROVIDERS[name]
            prompt = arguments.get("prompt", "")
            aspect_ratio = arguments.get("aspectRatio", "16:9")
            resolution = arguments.get("resolution", "1K")
            num_images = arguments.get("numberOfImages", 1)
            reference_images = arguments.get("reference_images", [])  # For image-to-image

            mode = "image-to-image" if reference_images else "text-to-image"
            print(f"[GROK-LIVE] Executing image generation ({mode}): {prompt[:100]}... ({num_images} @ {resolution})")

            # Create image generation task
            image_options = {
                "aspectRatio": aspect_ratio,
                "resolution": resolution,
                "numberOfImages": num_images,
                "provider": provider,
            }
            if reference_images:
                image_options["reference_images"] = reference_images

            task = create_task(
                TaskType.IMAGE_GENERATION,
                operator=session.operator or "system",
                prompt=prompt,
                result_data={"options": image_options}
            )

            img_count = num_images
            ref_desc = f" (using {len(reference_images)} reference image{'s' if len(reference_images) > 1 else ''})" if reference_images else ""
            result = f"Image generation started ({mode}){ref_desc}: {prompt[:100]}{'...' if len(prompt) > 100 else ''}. Creating {img_count} image{'s' if img_count > 1 else ''} at {resolution}."

            # Notify Portal about image task
            print(f"[GROK-LIVE] Sending image_task event to portal...")
            if session.portal_ws:
                await _safe_ws_send(session.portal_ws, {
                    "type": "image_task",
                    "data": {"task_id": task.task_id, "prompt": prompt, "count": num_images}
                })
                print(f"[GROK-LIVE] ✓ image_task event sent successfully")
            else:
                print(f"[GROK-LIVE] ✗ WARNING: No portal_ws connection!")

            print(f"[GROK-LIVE] Image generation task created: {task.task_id}")
        elif name == "generate_video":
            prompt = arguments.get("prompt", "")
            aspect_ratio = arguments.get("aspectRatio", "16:9")
            duration = arguments.get("duration", 8)
            resolution = arguments.get("resolution", "720p")
            negative_prompt = arguments.get("negativePrompt", "")
            image_url = arguments.get("image_url")  # For image-to-video
            video_url = arguments.get("video_url")  # For video extension

            mode = "text-to-video"
            if image_url:
                mode = "image-to-video"
            elif video_url:
                mode = "video-extension"

            print(f"[GROK-LIVE] Executing video generation ({mode}): {prompt[:100]}... ({duration}s @ {resolution})")

            # Create video generation task
            video_options = {
                "aspectRatio": aspect_ratio,
                "duration": duration,
                "resolution": resolution,
            }
            if negative_prompt:
                video_options["negativePrompt"] = negative_prompt
            if image_url:
                video_options["image_url"] = image_url
            if video_url:
                video_options["video_url"] = video_url

            task = create_task(
                TaskType.VIDEO_GENERATION,
                operator=session.operator or "system",
                prompt=prompt,
                result_data={"options": video_options}
            )

            mode_desc = f" (animating image: {image_url})" if image_url else (f" (extending video: {video_url})" if video_url else "")
            result = f"Video generation started ({mode}){mode_desc}: {prompt[:100]}{'...' if len(prompt) > 100 else ''}. Creating {duration}s video at {resolution}. Takes 5-20 minutes."

            # Notify Portal about video task
            print(f"[GROK-LIVE] Sending video_task event to portal...")
            if session.portal_ws:
                await _safe_ws_send(session.portal_ws, {
                    "type": "video_task",
                    "data": {"task_id": task.task_id, "prompt": prompt, "duration": duration, "resolution": resolution}
                })
                print(f"[GROK-LIVE] ✓ video_task event sent successfully")
            else:
                print(f"[GROK-LIVE] ✗ WARNING: No portal_ws connection!")

            print(f"[GROK-LIVE] Video generation task created: {task.task_id}")
        elif name == "lyria_music":
            prompt = arguments.get("prompt", "")
            negative_prompt = arguments.get("negativePrompt", "")
            sample_count = arguments.get("sampleCount", 1)
            print(f"[GROK-LIVE] Executing music generation: {prompt[:100]}... ({sample_count} variation(s))")

            # Create music generation task
            music_options = {
                "prompt": prompt,
                "operator": session.operator or "system",
            }
            if negative_prompt:
                music_options["negative_prompt"] = negative_prompt
            if sample_count and sample_count > 1:
                music_options["sample_count"] = sample_count

            task = create_task(
                TaskType.LYRIA_MUSIC,
                operator=session.operator or "system",
                prompt=prompt,
                result_data=music_options
            )

            variations_text = f" ({sample_count} variations)" if sample_count > 1 else ""
            result = f"Music generation started for: {prompt[:100]}{'...' if len(prompt) > 100 else ''}{variations_text}. 30-second track will be ready in 20-60 seconds."

            # Notify Portal about music task
            print(f"[GROK-LIVE] Sending music_task event to portal...")
            if session.portal_ws:
                await _safe_ws_send(session.portal_ws, {
                    "type": "music_task",
                    "data": {"task_id": task.task_id, "prompt": prompt, "sample_count": sample_count}
                })
                print(f"[GROK-LIVE] ✓ music_task event sent successfully")
            else:
                print(f"[GROK-LIVE] ✗ WARNING: No portal_ws connection!")

            print(f"[GROK-LIVE] Music generation task created: {task.task_id}")
        elif name == "get_media":
            from Orchestrator.routes.chat_routes import execute_get_media
            url = arguments.get("url")
            task_id_param = arguments.get("task_id")
            print(f"[GROK-LIVE] Executing get_media: url={url}, task_id={task_id_param}")
            media_result = execute_get_media(url=url, task_id=task_id_param)
            result = json.dumps(media_result, indent=2)
        elif name == "list_media":
            from Orchestrator.routes.chat_routes import execute_list_media
            media_type = arguments.get("media_type")
            limit = arguments.get("limit", 20)
            print(f"[GROK-LIVE] Executing list_media: type={media_type}, limit={limit}")
            list_result = execute_list_media(media_type=media_type, limit=limit)
            result = json.dumps(list_result, indent=2)
        elif name == "search_media":
            from Orchestrator.routes.chat_routes import execute_search_media
            query = arguments.get("query", "")
            media_type = arguments.get("media_type")
            limit = arguments.get("limit", 10)
            print(f"[GROK-LIVE] Executing search_media: query='{query}', type={media_type}")
            search_result = execute_search_media(query=query, media_type=media_type, limit=limit)
            result = json.dumps(search_result, indent=2)
        elif name == "send_sms":
            # Send SMS using unified tool executor
            from Orchestrator.tools import BlackBoxToolExecutor
            phone_number = arguments.get("phone_number", "")
            message = arguments.get("message", "")
            print(f"[GROK-LIVE] Executing send_sms to {phone_number}: {message[:50]}...")
            executor = BlackBoxToolExecutor(operator=session.operator or "system")
            tool_result = await executor.execute("send_sms", {"phone_number": phone_number, "message": message})
            result = tool_result.rich_result()
            print(f"[GROK-LIVE] SMS result: {result}")
        elif name == "make_phone_call":
            # Initiate outbound phone call
            import aiohttp
            phone_number = arguments.get("phone_number", "")
            greeting = arguments.get("greeting", "")
            role = arguments.get("role", "")
            backend = arguments.get("backend", "openai_realtime")
            print(f"[GROK-LIVE] Executing make_phone_call to {phone_number}")
            try:
                async with aiohttp.ClientSession() as http_session:
                    async with http_session.post(
                        "http://localhost:9091/twilio/call",
                        json={
                            "to": phone_number,
                            "backend": backend,
                            "operator": session.operator or "system",
                            "greeting": greeting,
                            "role": role
                        },
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        call_result = await resp.json()
                        if call_result.get("status") == "initiated":
                            result = f"Call initiated to {phone_number}. The recipient should receive a call shortly."
                        else:
                            error = call_result.get("error", "Unknown error")
                            result = f"Failed to initiate call: {error}"
            except Exception as e:
                result = f"Error making call: {str(e)}"
            print(f"[GROK-LIVE] Call result: {result}")
        elif name == "make_voice_call":
            # Call with pre-generated TTS message
            from Orchestrator.tools import BlackBoxToolExecutor
            phone_number = arguments.get("phone_number", "")
            message = arguments.get("message", "")
            voice = arguments.get("voice", "onyx")
            print(f"[GROK-LIVE] Executing make_voice_call to {phone_number}: {message[:50]}...")
            executor = BlackBoxToolExecutor(operator=session.operator or "system")
            tool_result = await executor.execute("make_voice_call", {
                "phone_number": phone_number,
                "message": message,
                "voice": voice
            })
            result = tool_result.rich_result()
            print(f"[GROK-LIVE] Voice call result: {result}")
        elif name in ("get_recent_snapshots", "list_recent_snapshots"):
            # list_recent_snapshots is the declared ToolVault name; legacy
            # get_recent_snapshots is kept as a dispatch alias for list_recent_snapshots
            # so this specialized handler (system-sees-all scoping for
            # outbound-call context handoff) serves both instead of falling
            # to the catch-all.
            count = min(arguments.get("count", 3), 5)
            operator = session.operator or "system"

            # System operator sees ALL snapshots (for outbound calls/context handoff)
            # Regular operators only see their own snapshots (no context bleed)
            see_all = (operator == "system")

            print(f"[GROK-LIVE] Getting {count} recent snapshots for {operator} (see_all={see_all})")
            try:
                from Orchestrator.fossils import load_snapshot_index, read_volume_bytes
                index = load_snapshot_index()

                if index:
                    # Sort all snapshots by ID (date + sequence)
                    def snapshot_sort_key(snap_id: str) -> tuple:
                        try:
                            parts = snap_id.split('-')
                            date_part = int(parts[1]) if len(parts) > 1 else 0
                            seq_part = int(parts[2]) if len(parts) > 2 else 0
                            return (date_part, seq_part)
                        except (ValueError, IndexError):
                            return (0, 0)

                    # Filter by operator unless system (which sees all)
                    if see_all:
                        matching = list(index.items())
                    else:
                        matching = [(sid, meta) for sid, meta in index.items()
                                   if meta.get("operator") == operator]

                    matching.sort(key=lambda x: snapshot_sort_key(x[0]), reverse=True)

                    # Get the most recent N
                    recent = matching[:count]

                    if not recent:
                        result = f"No recent snapshots found for operator '{operator}'."
                    else:
                        vol_bytes = read_volume_bytes(VOL_PATH)
                        scope_desc = "all operators" if see_all else f"operator: {operator}"
                        result_parts = [f"Found {len(recent)} recent snapshot(s) ({scope_desc}):\n"]

                        for snap_id, meta in recent:
                            start = meta.get("byte_start", 0)
                            end = meta.get("byte_end", start + 5000)
                            snap_bytes = vol_bytes[start:end]
                            snap_text = snap_bytes.decode('utf-8', errors='replace')
                            operator_name = meta.get("operator", "unknown")

                            # WI-10 (M7): deliver recent snapshots WHOLE — no delivery truncation
                            result_parts.append(f"--- {snap_id} (operator: {operator_name}) ---\n{snap_text}")

                        result = "\n\n".join(result_parts)
                        print(f"[GROK-LIVE] Retrieved {len(recent)} recent snapshots")
                else:
                    result = "No snapshots found in index."
            except Exception as e:
                print(f"[GROK-LIVE] Error getting recent snapshots: {e}")
                result = f"Error retrieving recent snapshots: {str(e)}"
        elif name == "search_contacts":
            from Orchestrator.contacts import search_contacts
            query = arguments.get("query", "")
            operator = session.operator or "system"
            print(f"[GROK-LIVE] Executing search_contacts: query='{query}', operator={operator}")
            contacts = search_contacts(query, operator)
            if contacts:
                result = json.dumps(contacts, indent=2)
            else:
                result = f"No contacts found matching '{query}'."
            print(f"[GROK-LIVE] search_contacts result: {len(contacts)} contacts found")
        elif name == "save_contact":
            from Orchestrator.contacts import upsert_contact
            operator = session.operator or "system"
            contact_name = arguments.get("name", "")
            notes = arguments.get("notes", "")
            tags = arguments.get("tags", [])
            phone = arguments.get("phone")
            email = arguments.get("email")
            relationship = arguments.get("relationship")
            print(f"[GROK-LIVE] Executing save_contact: name='{contact_name}', operator={operator}")
            saved = upsert_contact(
                name=contact_name, notes=notes, tags=tags,
                operator=operator, created_by="grok-live",
                phone=phone, email=email, relationship=relationship
            )
            result = f"Contact saved: {json.dumps(saved, indent=2)}"
            print(f"[GROK-LIVE] save_contact result: saved '{contact_name}'")
        elif name == "create_cron_job":
            from Orchestrator.tools import BlackBoxToolExecutor
            operator = session.operator or "system"
            executor = BlackBoxToolExecutor(operator=operator)
            cron_result = await executor.execute("create_cron_job", arguments)
            result = cron_result.rich_result()
            print(f"[GROK-LIVE] create_cron_job result: {result}")
        elif name == "edit_cron_job":
            from Orchestrator.tools import BlackBoxToolExecutor
            operator = session.operator or "system"
            executor = BlackBoxToolExecutor(operator=operator)
            cron_result = await executor.execute("edit_cron_job", arguments)
            result = cron_result.rich_result()
            print(f"[GROK-LIVE] edit_cron_job result: {result}")
        elif name == "search_cron_jobs":
            from Orchestrator.tools import BlackBoxToolExecutor
            operator = session.operator or "system"
            executor = BlackBoxToolExecutor(operator=operator)
            cron_result = await executor.execute("search_cron_jobs", arguments)
            result = cron_result.rich_result()
            print(f"[GROK-LIVE] search_cron_jobs result: {result}")
        elif name in ("use_computer", "list_devices", "control_android_device"):
            from Orchestrator.tools import BlackBoxToolExecutor
            operator = session.operator or "system"
            executor = BlackBoxToolExecutor(operator=operator)
            tool_result = await executor.execute(name, arguments)
            result = tool_result.result
            print(f"[GROK-LIVE] {name}: {result[:100]}")
        elif name == "get_current_time":
            from datetime import datetime
            now = datetime.now()
            result = f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}"
            print(f"[GROK-LIVE] get_current_time: {result}")
        else:
            # Catch-all: route ANY other tool (incl. the per-provider web
            # search tools and dynamically-injected ToolVault tools) through
            # BlackBoxToolExecutor instead of reporting "Unknown tool".
            from Orchestrator.tools import BlackBoxToolExecutor
            operator = session.operator or "system"
            executor = BlackBoxToolExecutor(operator=operator)
            tool_result = await executor.execute(name, arguments)
            result = tool_result.result if hasattr(tool_result, 'result') else str(tool_result)
            if not result:
                result = f"Tool '{name}' executed successfully (no output)."
            print(f"[GROK-LIVE] {name} (catch-all): {result[:100]}")

        # Send result back to Grok
        if session.grok_ws:
            tool_response = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result
                }
            }
            print(f"[GROK-LIVE] Sending tool response for {name}, call_id: {call_id}")
            await session.grok_ws.send(json.dumps(tool_response))
            # Request response with tool result
            await session.grok_ws.send(json.dumps({
                "type": "response.create"
            }))
            print(f"[GROK-LIVE] Requested response.create after tool result")

        # Notify Portal
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "tool_result",
                "data": {"name": name, "result_length": len(result)}
            })

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

# =============================================================================
# WebSocket Bridge Tasks
# =============================================================================

async def grok_reconnect(session: 'GrokLiveSession'):
    """
    Reconnect to xAI Grok Voice Agent API transparently.
    Uses exponential backoff, re-configures session on success.
    """
    if session.is_reconnecting:
        print(f"[GROK-LIVE] Already reconnecting, skipping")
        return

    if session.reconnect_count >= session.max_reconnects:
        print(f"[GROK-LIVE] Max reconnects ({session.max_reconnects}) reached, giving up")
        session.status = "disconnected"
        await _safe_ws_send(session.portal_ws, {
            "type": "disconnected",
            "data": "Connection lost after multiple reconnection attempts"
        })
        await save_grok_session_to_blackbox(session)
        # P1b: null the closed-but-non-None socket so the keepalive loop's
        # `if not session.grok_ws: break` fires once, instead of spinning on the
        # dead socket and re-triggering grok_reconnect every stale cycle (each
        # re-hitting this give-up branch → a redundant BlackBox save).
        if session.grok_ws:
            try:
                await session.grok_ws.close()
            except Exception:
                pass
            session.grok_ws = None
        return

    session.is_reconnecting = True
    session.reconnect_count += 1
    attempt = session.reconnect_count

    # Exponential backoff: 0.5s, 1s, 2s, 4s, 5s max (fast recovery for voice calls)
    delay = min(0.5 * (2 ** (attempt - 1)), 5)
    print(f"[GROK-LIVE] Reconnecting (attempt {attempt}/{session.max_reconnects}) in {delay}s...")

    # Notify Portal
    await _safe_ws_send(session.portal_ws, {
        "type": "reconnecting",
        "data": {"attempt": attempt, "max": session.max_reconnects, "delay": delay}
    })

    await asyncio.sleep(delay)

    # P1b (terminal guard): this coroutine runs DETACHED (bare create_task at
    # every trigger site) and just slept, so the WS endpoint may have torn the
    # session down during the backoff. Re-check here — otherwise a Portal drop
    # mid-reconnect would re-dial + respawn a listener + flip status back to
    # "connected", resurrecting a clientless session the reaper can never evict.
    if session.intentional_disconnect:
        print(f"[GROK-LIVE] Session closed during backoff — abandoning reconnect")
        session.is_reconnecting = False
        return

    try:
        # P1b: cancel the old listener FIRST — it is bound to the OLD ws
        # object; left running it would observe our close() below and emit a
        # spurious "disconnected" to the client mid-recovery.
        if session.listener_task and not session.listener_task.done():
            session.listener_task.cancel()

        # Close old connection
        if session.grok_ws:
            try:
                await session.grok_ws.close()
            except Exception:
                pass
            session.grok_ws = None

        # Reconnect — resume the server-side conversation when we have an id
        # (xAI replays cached turns; avoids a full context rebuild).
        resume_id = session.conversation_id
        if await connect_to_grok(session, model=session.model or None,
                                 conversation_id=resume_id):
            # P1b (terminal guard): teardown may have run during the dial. If so,
            # close the socket we just opened and bail — do NOT respawn a
            # listener onto a session that is being torn down.
            if session.intentional_disconnect:
                if session.grok_ws:
                    try:
                        await session.grok_ws.close()
                    except Exception:
                        pass
                    session.grok_ws = None
                session.is_reconnecting = False
                return

            if resume_id:
                print(f"[GROK-LIVE] Resumed conversation {resume_id} — session rebuild skipped")
            else:
                # No resumption id — full reconfigure (rebuilds context)
                await configure_grok_session(session, session.operator, session.voice)

                # Re-emit provenance after reconfigure so client UI stays in sync
                # with the newly-rebuilt system context (see Task 3 code review).
                if session.provenance:
                    await _safe_ws_send(session.portal_ws, {
                        "type": "provenance",
                        "data": session.provenance
                    })

            # P1b (terminal guard): final check at the point of no return.
            # configure + provenance each await, so teardown could have completed
            # during them — re-check before we respawn a listener and flip status
            # to "connected". This is the last guard; past it the session is live
            # again, so a hole here would still resurrect a reaper-immune session.
            if session.intentional_disconnect:
                if session.grok_ws:
                    try:
                        await session.grok_ws.close()
                    except Exception:
                        pass
                    session.grok_ws = None
                session.is_reconnecting = False
                return

            # P1b: respawn the listener on the NEW upstream ws. Without this the
            # previous grok_listener's `async for` (bound to the OLD closed
            # socket) has already exited, so NOTHING reads the new connection —
            # the session is a permanently mute one-way pipe still reporting
            # "reconnected" (parity with the OpenAI/Gemini fixes; the phone
            # bridge always had its own respawn loop).
            session.listener_task = asyncio.create_task(grok_listener(session))

            # Reset state
            session.reconnect_count = 0
            session.is_reconnecting = False
            session.last_ai_message_time = time.time()
            session.status = "connected"

            print(f"[GROK-LIVE] Reconnected successfully on attempt {attempt}")

            await _safe_ws_send(session.portal_ws, {
                "type": "reconnected",
                "data": {"attempt": attempt}
            })
        else:
            print(f"[GROK-LIVE] Reconnect attempt {attempt} failed")
            session.is_reconnecting = False
            asyncio.create_task(grok_reconnect(session))

    except Exception as e:
        print(f"[GROK-LIVE] Reconnect error: {e}")
        session.is_reconnecting = False
        asyncio.create_task(grok_reconnect(session))


async def grok_keepalive_loop(session: 'GrokLiveSession'):
    """
    Send periodic keepalive silence to prevent Grok idle timeout.
    Also monitors for stale connections (no AI message for 60s).
    """
    keepalive_interval = 15  # seconds
    stale_timeout = 60  # seconds without AI message = stale (standardized across all backends)

    session.last_ai_message_time = time.time()
    print(f"[GROK-LIVE] Keepalive loop started (interval={keepalive_interval}s, stale={stale_timeout}s)")

    try:
        while True:
            await asyncio.sleep(keepalive_interval)

            if not session.grok_ws or session.intentional_disconnect:
                break

            # Check for stale connection
            time_since_message = time.time() - session.last_ai_message_time
            if time_since_message > stale_timeout:
                print(f"[GROK-LIVE] STALE CONNECTION: No AI message for {time_since_message:.0f}s")
                if not session.is_reconnecting and not session.intentional_disconnect:
                    session.last_ai_message_time = time.time()
                    asyncio.create_task(grok_reconnect(session))
                continue

            # Send keepalive: 20ms of PCM16 silence at the declared input
            # rate (16kHz post-P2.15 — byte count below matches).
            # SKIPPED for SIP-attached calls (session.call_id set): the call's
            # audio flows xAI-side and injected buffer silence could corrupt it
            # (uncertain per xAI docs — live-validated in P5.8). Stale detection
            # above still applies.
            try:
                if session.grok_ws and not session.call_id:
                    # 20ms at 16kHz (input rate) = 320 samples, PCM16 = 640 bytes of zeros
                    silence_bytes = b'\x00' * 640
                    silence_b64 = base64.b64encode(silence_bytes).decode('ascii')
                    await session.grok_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": silence_b64
                    }))
            except websockets.exceptions.ConnectionClosed:
                print(f"[GROK-LIVE] Keepalive failed - connection closed")
                if not session.is_reconnecting and not session.intentional_disconnect:
                    asyncio.create_task(grok_reconnect(session))
                break
            except Exception as e:
                print(f"[GROK-LIVE] Keepalive error: {e}")

    except asyncio.CancelledError:
        pass
    finally:
        print(f"[GROK-LIVE] Keepalive loop stopped")


async def grok_listener(session: 'GrokLiveSession'):
    """
    Background task that listens for messages from Grok and forwards to Portal.
    """
    try:
        async for message in session.grok_ws:
            try:
                event = json.loads(message)
                await handle_grok_message(session, event)
            except json.JSONDecodeError:
                print(f"[GROK-LIVE] Invalid JSON from Grok: {message[:100]}")
            except Exception as e:
                print(f"[GROK-LIVE] Error handling Grok message: {e}")
                # P1b: never dangle a tool call — answer function-call events
                # with an error payload so Grok recovers instead of stalling.
                await send_openai_style_tool_error(
                    session.grok_ws, session.portal_ws, event, e)
    except websockets.exceptions.ConnectionClosed as e:
        print(f"[GROK-LIVE] Grok connection closed: {e}")
        if not session.intentional_disconnect and not session.is_reconnecting:
            print(f"[GROK-LIVE] Unexpected disconnect - triggering reconnect")
            asyncio.create_task(grok_reconnect(session))
        else:
            session.status = "disconnected"
            await _safe_ws_send(session.portal_ws, {
                "type": "disconnected",
                "data": "Grok connection closed"
            })
    except Exception as e:
        print(f"[GROK-LIVE] Grok listener error: {e}")
        session.status = "error"

# =============================================================================
# WebSocket Endpoint
# =============================================================================

@app.websocket("/ws/grok-live/{session_id}")
async def grok_live_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for xAI Grok Voice Agent API bridge.

    Bridges Portal <-> Orchestrator <-> xAI Grok Voice API.

    Message flow:
    1. Portal connects, sends 'connect' with operator and voice
    2. Orchestrator connects to Grok Voice Agent API
    3. Orchestrator configures session with tools and context
    4. Bidirectional message passing begins
    5. Tool calls are executed locally and results sent to Grok
    """
    print(f"[GROK-LIVE] WebSocket connection request for session: {session_id}")
    await websocket.accept()
    print(f"[GROK-LIVE] WebSocket accepted for session: {session_id}")

    # P4: Grok route reads only the preset id from the URL (other Grok URL
    # params are Phase 3 scope).
    url_agent = websocket.query_params.get("agent")

    # Check dependencies
    if not WEBSOCKETS_AVAILABLE:
        await _safe_ws_send(websocket, {
            "type": "error",
            "data": "Server missing 'websockets' library. Install with: pip install websockets"
        })
        await websocket.close()
        return

    if not XAI_API_KEY:
        await _safe_ws_send(websocket, {
            "type": "error",
            "data": "XAI_API_KEY not configured on server"
        })
        await websocket.close()
        return

    # Create or get session
    session = GROK_LIVE_SESSIONS.get(session_id)
    if not session:
        session = GrokLiveSession(
            session_id=session_id,
            created_at=now_utc_iso()
        )
        GROK_LIVE_SESSIONS[session_id] = session

    session.portal_ws = websocket
    session.last_activity = now_utc_iso()

    grok_task = None
    keepalive_task = None

    try:
        while True:
            # Receive with timeout — detect suspended/dead Android clients
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=120.0
                )
            except asyncio.TimeoutError:
                print(f"[GROK-LIVE] Client idle timeout (120s): {session_id}")
                await _safe_ws_send(websocket, {"type": "error", "data": "Idle timeout"})
                break

            # Per-message error isolation
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as e:
                print(f"[GROK-LIVE] Bad message from client: {e}")
                continue

            msg_type = data.get("type", "")

            if msg_type == "connect":
                # Initial connection - establish Grok WebSocket
                # Voice-agent preset (P4) — precedence: explicit > preset > defaults.
                # Client model/voice/greeting/role plus the Grok-specific
                # language_hint/keyterms feed the merge as the explicit inputs
                # (model keeps its JSON-wins-over-URL fallback — Android sends it
                # via query param), so an explicit value still wins over a
                # preset-supplied one. reasoning_effort/replace_map are not preset
                # fields — derived as before.
                agent_id = data.get("agent", url_agent)
                preset = resolve_preset(agent_id, provider="grok-live") if agent_id else None
                if agent_id and preset is None:
                    await _safe_ws_send(websocket, {
                        "type": "warning",
                        "data": f"Voice agent preset {agent_id!r} not found for provider 'grok-live' — continuing without preset"
                    })
                merged = merge_connect_params({
                    "model": data.get("model", websocket.query_params.get("model")),
                    "voice": data.get("voice"),
                    "greeting": data.get("greeting", ""),
                    "instructions": data.get("role", ""),
                    "language": data.get("language_hint", websocket.query_params.get("language_hint")),
                    "keyterms": data.get("keyterms") if isinstance(data.get("keyterms"), list) else None,
                }, preset)
                operator = data.get("operator", "")
                voice = merged["voice"] or GROK_LIVE_DEFAULT_VOICE
                greeting = merged["greeting"] or ""
                role = merged["instructions"] or ""
                model = merged["model"]
                tool_group_override = merged["tool_group_override"]
                reasoning_effort = data.get("reasoning_effort", websocket.query_params.get("reasoning_effort"))
                replace_map = data.get("replace") if isinstance(data.get("replace"), dict) else None
                session.operator = operator

                await _safe_ws_send(websocket, {
                    "type": "status",
                    "data": "Connecting to Grok..."
                })

                # Connect to Grok
                if await connect_to_grok(session, model=model):
                    # Configure session with tools, context, and voice
                    await configure_grok_session(session, operator, voice, custom_role=role,
                                                 reasoning_effort=reasoning_effort, replace_map=replace_map,
                                                 keyterms=merged["keyterms"], language_hint=merged["language"],
                                                 tool_group_override=tool_group_override)
                    print(f"[GROK-LIVE] Voice selected: {voice}")

                    # Emit provenance to the client once per session start so
                    # the Android/Portal UI can show which snapshots were used
                    # to seed the system message. Task 10 wires the parser.
                    if session.provenance:
                        await _safe_ws_send(websocket, {
                            "type": "provenance",
                            "data": session.provenance
                        })

                    # Start Grok listener task and keepalive. The listener task
                    # lives on the SESSION (session.listener_task): grok_reconnect
                    # respawns it, so this local would go stale after the first
                    # reconnect and leak the live task at teardown.
                    grok_task = asyncio.create_task(grok_listener(session))
                    session.listener_task = grok_task
                    keepalive_task = asyncio.create_task(grok_keepalive_loop(session))

                    # If greeting provided (outbound call), inject it so the AI speaks first
                    if greeting:
                        print(f"[GROK-LIVE] Injecting outbound greeting: {greeting[:80]}...")
                        try:
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
                        except Exception as e:
                            print(f"[GROK-LIVE] Greeting injection failed: {e}")

                    await _safe_ws_send(websocket, {
                        "type": "connected",
                        "data": {
                            "session_id": session_id,
                            "operator": operator,
                            "model": session.model,
                            "voice": voice
                        }
                    })
                else:
                    await _safe_ws_send(websocket, {
                        "type": "error",
                        "data": "Failed to connect to Grok Voice Agent API"
                    })

            elif msg_type == "disconnect":
                # Graceful disconnect
                session.intentional_disconnect = True
                break

            elif msg_type == "ping":
                # Keep-alive
                await _safe_ws_send(websocket, {"type": "pong"})

            else:
                # Isolate handler errors
                try:
                    await handle_grok_portal_message(session, data)
                except Exception as handler_err:
                    print(f"[GROK-LIVE] Handler error (non-fatal): {handler_err}")

    except WebSocketDisconnect:
        print(f"[GROK-LIVE] Portal WebSocket disconnected: {session_id}")
        session.portal_ws = None

    except Exception as e:
        print(f"[GROK-LIVE] WebSocket error: {e}")
        await _safe_ws_send(websocket, {
            "type": "error",
            "data": str(e)
        })

    finally:
        # P1b (terminal guard): mark the session terminal FIRST — before the
        # save await and before cancelling tasks — so any in-flight, detached
        # grok_reconnect (bare create_task, not tracked here) bails after its
        # backoff sleep instead of re-dialing and flipping status back to
        # "connected", which would resurrect a clientless session the reaper can
        # never evict (it only reaps status=="disconnected").
        session.intentional_disconnect = True

        # Save session to BlackBox before cleanup
        if session.conversation:
            await save_grok_session_to_blackbox(session)

        # Cleanup — cancel the CURRENT listener; after a reconnect this is a
        # different task than the locally-captured grok_task, so cancelling only
        # the local would leak the live listener.
        listener = session.listener_task or grok_task
        if listener:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
        session.listener_task = None

        if keepalive_task:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass

        if session.grok_ws:
            try:
                await session.grok_ws.close()
            except:
                pass
            session.grok_ws = None

        session.portal_ws = None
        session.status = "disconnected"
        session.last_activity = now_utc_iso()  # start reaper grace clock (live_session_reaper)
        release_payload(session)               # free audio/transcript buffers; conversation already saved
        print(f"[GROK-LIVE] Session {session_id} cleaned up")

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
