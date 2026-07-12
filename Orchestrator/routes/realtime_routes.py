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
    print("[REALTIME] websockets library not installed - run: pip install websockets")

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

# Local imports
from Orchestrator.checkpoint import app
from Orchestrator.config import (
    OPENAI_API_KEY,
    OPENAI_REALTIME_URL,
    OPENAI_REALTIME_MODEL,
    OPENAI_REALTIME_MODELS,
    OPENAI_REALTIME_VOICES,
    OPENAI_REALTIME_DEFAULT_VOICE,
    OPENAI_REALTIME_VAD_TYPES,
    OPENAI_REALTIME_VAD_EAGERNESS,
    OPENAI_REALTIME_NOISE_REDUCTION_TYPES,
    OPENAI_REALTIME_TRANSCRIPTION_DELAYS,
    REALTIME_CONTEXT_MAX_CHARS,
    REALTIME_SNAPSHOT_CHARS_EACH,
    STT_OPENAI_STREAM,
    VOL_PATH
)
from Orchestrator.models import RealtimeSession, REALTIME_SESSIONS, TaskType
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
from Orchestrator.routes.voice_translate import (
    resolve_translate_params,
    build_translate_instructions,
)
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
    phone bridge teardown). P1b: persists via POST /chat/save (direct
    persistence + auto-mint; no LLM round-trip) and clears
    session.conversation ONLY after a confirmed 200 so a failed save can be
    retried by a later teardown path.
    """
    if not session.conversation:
        print(f"[REALTIME] No conversation to save for session {session.session_id}")
        return

    if not session.operator:
        print(f"[REALTIME] No operator set, cannot save session {session.session_id}")
        return

    # Sort conversation by timestamp to ensure correct order
    sorted_conversation = sorted(
        session.conversation,
        key=lambda x: x.get("timestamp", "")
    )

    # Format conversation as readable transcript
    transcript_lines = []
    for msg in sorted_conversation:
        role = "User" if msg["role"] == "user" else "AI"
        transcript_lines.append(f"[{role}]: {msg['content']}")

    transcript = "\n\n".join(transcript_lines)

    session_summary = f"""=== OpenAI Realtime Voice Session ===
Session ID: {session.session_id}
Timestamp: {now_utc_iso()}
Messages: {len(session.conversation)}

--- Transcript ---
{transcript}
--- End Session ---"""

    print(f"[REALTIME] Saving session {session.session_id} with {len(session.conversation)} messages to BlackBox")

    saved = await save_voice_transcript(
        operator=session.operator,
        user_message=f"[Voice Session Transcript] OpenAI Realtime voice session {session.session_id}",
        session_summary=session_summary,
        model_label="openai-realtime-voice",
        log_prefix="[REALTIME]",
    )

    # Clear ONLY after a confirmed 200 (previously cleared unconditionally,
    # permanently losing the transcript after a failed save).
    if saved:
        session.conversation = []
    else:
        print(f"[REALTIME] Save FAILED — keeping {len(session.conversation)} turns for a later retry")


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
        print(f"[REALTIME] Search error: {e}")
        return f"Search failed: {str(e)}"

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
        # websockets 15.x uses additional_headers instead of extra_headers
        # Add explicit ping settings to prevent connection drops
        session.openai_ws = await websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=10,       # 10s max to establish connection (prevents indefinite hang)
            ping_interval=20,      # Send ping every 20 seconds
            ping_timeout=30,       # Wait 30 seconds for pong response
            close_timeout=10,      # Wait 10 seconds for close handshake
        )
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
    context, provenance = build_context_for_operator(operator, user_text="")
    # Stash provenance on the session so the WS endpoint can emit it to
    # the client right after configuration.
    session.provenance = provenance
    is_system_operator = (operator == "system")

    # If custom_role is provided, use it as the primary system instruction
    # This allows REST endpoint models to set up the WebSocket model's persona before the call
    if custom_role:
        # Custom role provided - use it with essential context appended
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
This is a real-time voice conversation. Be concise and natural. The person on the phone cannot see text - speak clearly."""

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
            context_section = f"CONTEXT:\n{context if context else 'No recent context loaded yet. Use list_recent_snapshots immediately!'}"
        else:
            identity_section = f"""OPERATOR IDENTITY:
You are currently speaking with: {operator}
Always address them by their name ({operator}) when appropriate. This is their personal AI session."""
            memory_section = f"""MEMORY ACCESS — YOUR MOST IMPORTANT CAPABILITY:
The BlackBox contains 1,600+ snapshots — your complete memory of {operator}'s history.
Search snapshots FIRST and OFTEN — before answering questions about past work, before guessing at context, before starting any task.
Everything about {operator}'s projects, preferences, past decisions, and recent activity lives in the snapshots.
Don't guess or hallucinate history — call search_snapshots proactively."""
            context_section = f"OPERATOR-SPECIFIC CONTEXT:\n{context if context else f'No recent context available for {operator} yet. This may be their first session or a fresh start.'}"

        voice_persona = get_persona(operator, "voice") + "\n\n" + VOICE_DELIVERY_NOTE

        system_instructions = f"""{voice_persona}

IDENTITY:
You are the voice interface for the AI Black Box Flight Recorder, connected to an immutable snapshot ledger and a multimodal toolchain. The operator's memory lives in the snapshots — treat it as your external long-term memory.

TEMPORAL AWARENESS — FIRST ACTION:
Your VERY FIRST action must be to call get_current_time to anchor yourself in the present. Do this before any other tool calls or responses.

{identity_section}

{memory_section}

{context_section}

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

SESSION START - CRITICAL:
IMMEDIATELY use list_recent_snapshots(count=3) at the START of EVERY session to catch up on recent context.
This is essential because:
- You may be continuing work started by another model or agent
- The snapshots contain the most recent conversations, decisions, and context
- {"For this outbound call: CHECK SNAPSHOTS for who you're calling, what task to complete, order details, etc." if is_system_operator else "For outbound calls: task details, order info, names, addresses may be in the snapshots"}
- Context handoff between models happens through snapshots - USE THEM!

Do this BEFORE responding to the user - check what happened recently so you're caught up."""

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
        # Execute tool call
        call_id = event.get("call_id", "")
        name = event.get("name", "")
        arguments_str = event.get("arguments", "{}")

        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError as parse_err:
            # P1b: malformed arguments must NOT execute with {} — that masks
            # the real cause (e.g. search_snapshots "No search query provided").
            # Return a parse error so the model can retry with valid JSON.
            print(f"[REALTIME] Malformed tool arguments for {name}: {parse_err}")
            await send_openai_style_tool_error(
                session.openai_ws, session.portal_ws, event,
                ValueError(f"Malformed tool-call arguments JSON: {parse_err}. "
                           f"Raw arguments: {arguments_str[:200]}"),
            )
            return

        print(f"[REALTIME] Tool call: {name} with args: {arguments}")

        # Notify Portal that tool is being called
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "tool_call",
                "data": {"name": name, "arguments": arguments}
            })

        # Execute the tool
        if name == "search_snapshots":
            result = await execute_search_snapshots(session, arguments)
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

            print(f"[REALTIME] Getting {count} recent snapshots for {operator} (see_all={see_all})")
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
                        print(f"[REALTIME] Retrieved {len(recent)} recent snapshots")
                else:
                    result = "No snapshots found in index."
            except Exception as e:
                print(f"[REALTIME] Error getting recent snapshots: {e}")
                result = f"Error retrieving recent snapshots: {str(e)}"
        elif name == "web_fetch":
            url = arguments.get("url", "")
            max_chars = arguments.get("max_chars", 80000)
            print(f"[REALTIME] Executing web fetch: {url}")
            result = perform_web_fetch(url, max_chars)
            print(f"[REALTIME] Web fetch result length: {len(result)} chars")
        elif name in IMAGE_TOOL_PROVIDERS:
            provider = IMAGE_TOOL_PROVIDERS[name]
            prompt = arguments.get("prompt", "")
            aspect_ratio = arguments.get("aspectRatio", "16:9")
            resolution = arguments.get("resolution", "1K")
            num_images = arguments.get("numberOfImages", 1)
            reference_images = arguments.get("reference_images", [])  # For image-to-image

            mode = "image-to-image" if reference_images else "text-to-image"
            print(f"[REALTIME] Executing image generation ({mode}): {prompt[:100]}... ({num_images} @ {resolution})")

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
            if session.portal_ws:
                await _safe_ws_send(session.portal_ws, {
                    "type": "image_task",
                    "data": {"task_id": task.task_id, "prompt": prompt, "count": num_images}
                })

            print(f"[REALTIME] Image generation task created: {task.task_id}")
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

            print(f"[REALTIME] Executing video generation ({mode}): {prompt[:100]}... ({duration}s @ {resolution})")

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
            if session.portal_ws:
                await _safe_ws_send(session.portal_ws, {
                    "type": "video_task",
                    "data": {"task_id": task.task_id, "prompt": prompt, "duration": duration, "resolution": resolution}
                })

            print(f"[REALTIME] Video generation task created: {task.task_id}")
        elif name == "lyria_music":
            prompt = arguments.get("prompt", "")
            negative_prompt = arguments.get("negativePrompt", "")
            sample_count = arguments.get("sampleCount", 1)
            print(f"[REALTIME] Executing music generation: {prompt[:100]}... ({sample_count} variation(s))")

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
            print(f"[REALTIME] Sending music_task event to portal, portal_ws={session.portal_ws is not None}")
            if session.portal_ws:
                await _safe_ws_send(session.portal_ws, {
                    "type": "music_task",
                    "data": {"task_id": task.task_id, "prompt": prompt, "sample_count": sample_count}
                })
                print(f"[REALTIME] ✓ music_task event sent successfully: task_id={task.task_id}")
            else:
                print(f"[REALTIME] ✗ WARNING: No portal_ws connection for music_task!")

            print(f"[REALTIME] Music generation task created: {task.task_id}")
        elif name == "get_media":
            from Orchestrator.routes.chat_routes import execute_get_media
            url = arguments.get("url")
            task_id_param = arguments.get("task_id")
            print(f"[REALTIME] Executing get_media: url={url}, task_id={task_id_param}")
            media_result = execute_get_media(url=url, task_id=task_id_param)
            result = json.dumps(media_result, indent=2)
        elif name == "list_media":
            from Orchestrator.routes.chat_routes import execute_list_media
            media_type = arguments.get("media_type")
            limit = arguments.get("limit", 20)
            print(f"[REALTIME] Executing list_media: type={media_type}, limit={limit}")
            list_result = execute_list_media(media_type=media_type, limit=limit)
            result = json.dumps(list_result, indent=2)
        elif name == "search_media":
            from Orchestrator.routes.chat_routes import execute_search_media
            query = arguments.get("query", "")
            media_type = arguments.get("media_type")
            limit = arguments.get("limit", 10)
            print(f"[REALTIME] Executing search_media: query='{query}', type={media_type}")
            search_result = execute_search_media(query=query, media_type=media_type, limit=limit)
            result = json.dumps(search_result, indent=2)
        elif name == "send_sms":
            # Send SMS using unified tool executor
            from Orchestrator.tools import BlackBoxToolExecutor
            phone_number = arguments.get("phone_number", "")
            message = arguments.get("message", "")
            print(f"[REALTIME] Executing send_sms to {phone_number}: {message[:50]}...")
            executor = BlackBoxToolExecutor(operator=session.operator or "system")
            tool_result = await executor.execute("send_sms", {"phone_number": phone_number, "message": message})
            result = tool_result.rich_result()
            print(f"[REALTIME] SMS result: {result}")
        elif name == "make_phone_call":
            # Initiate outbound phone call
            import aiohttp
            phone_number = arguments.get("phone_number", "")
            greeting = arguments.get("greeting", "")
            role = arguments.get("role", "")
            backend = arguments.get("backend", "openai_realtime")
            print(f"[REALTIME] Executing make_phone_call to {phone_number} with backend {backend}")
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
            print(f"[REALTIME] Call result: {result}")
        elif name == "make_voice_call":
            # Call with pre-generated TTS message (no delay on connect)
            from Orchestrator.tools import BlackBoxToolExecutor
            phone_number = arguments.get("phone_number", "")
            message = arguments.get("message", "")
            voice = arguments.get("voice", "onyx")
            print(f"[REALTIME] Executing make_voice_call to {phone_number}: {message[:50]}...")
            executor = BlackBoxToolExecutor(operator=session.operator or "system")
            tool_result = await executor.execute("make_voice_call", {
                "phone_number": phone_number,
                "message": message,
                "voice": voice
            })
            result = tool_result.rich_result()
            print(f"[REALTIME] Voice call result: {result}")
        elif name == "search_contacts":
            from Orchestrator.contacts import search_contacts
            query = arguments.get("query", "")
            operator = session.operator or "system"
            print(f"[REALTIME] Executing search_contacts: query='{query}', operator={operator}")
            contacts = search_contacts(query, operator)
            if contacts:
                result = json.dumps(contacts, indent=2)
            else:
                result = f"No contacts found matching '{query}'."
            print(f"[REALTIME] search_contacts result: {len(contacts)} contacts found")
        elif name == "save_contact":
            from Orchestrator.contacts import upsert_contact
            operator = session.operator or "system"
            contact_name = arguments.get("name", "")
            notes = arguments.get("notes", "")
            tags = arguments.get("tags", [])
            phone = arguments.get("phone")
            email = arguments.get("email")
            relationship = arguments.get("relationship")
            print(f"[REALTIME] Executing save_contact: name='{contact_name}', operator={operator}")
            saved = upsert_contact(
                name=contact_name, notes=notes, tags=tags,
                operator=operator, created_by="openai-realtime",
                phone=phone, email=email, relationship=relationship
            )
            result = f"Contact saved: {json.dumps(saved, indent=2)}"
            print(f"[REALTIME] save_contact result: saved '{contact_name}'")
        elif name == "create_cron_job":
            from Orchestrator.tools import BlackBoxToolExecutor
            operator = session.operator or "system"
            executor = BlackBoxToolExecutor(operator=operator)
            cron_result = await executor.execute("create_cron_job", arguments)
            result = cron_result.rich_result()
            print(f"[REALTIME] create_cron_job result: {result}")
        elif name == "edit_cron_job":
            from Orchestrator.tools import BlackBoxToolExecutor
            operator = session.operator or "system"
            executor = BlackBoxToolExecutor(operator=operator)
            cron_result = await executor.execute("edit_cron_job", arguments)
            result = cron_result.rich_result()
            print(f"[REALTIME] edit_cron_job result: {result}")
        elif name == "search_cron_jobs":
            from Orchestrator.tools import BlackBoxToolExecutor
            operator = session.operator or "system"
            executor = BlackBoxToolExecutor(operator=operator)
            cron_result = await executor.execute("search_cron_jobs", arguments)
            result = cron_result.rich_result()
            print(f"[REALTIME] search_cron_jobs result: {result}")
        elif name in ("use_computer", "list_devices", "control_android_device"):
            from Orchestrator.tools import BlackBoxToolExecutor
            operator = session.operator or "system"
            executor = BlackBoxToolExecutor(operator=operator)
            tool_result = await executor.execute(name, arguments)
            result = tool_result.result
            print(f"[REALTIME] {name}: {result[:100]}")
        elif name == "get_current_time":
            from datetime import datetime
            now = datetime.now()
            result = f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}"
            print(f"[REALTIME] get_current_time: {result}")
        else:
            # Catch-all: route ANY other tool (incl. the per-provider web search
            # tools and dynamically-injected ToolVault tools) through
            # BlackBoxToolExecutor instead of reporting "Unknown tool".
            from Orchestrator.tools import BlackBoxToolExecutor
            operator = session.operator or "system"
            executor = BlackBoxToolExecutor(operator=operator)
            tool_result = await executor.execute(name, arguments)
            result = tool_result.result if hasattr(tool_result, 'result') else str(tool_result)
            if not result:
                result = f"Tool '{name}' executed successfully (no output)."
            print(f"[REALTIME] {name} (catch-all): {result[:100]}")

        # Send result back to OpenAI
        if session.openai_ws:
            tool_response = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result
                }
            }
            print(f"[REALTIME] Sending tool response for {name}, call_id: {call_id}")
            await session.openai_ws.send(json.dumps(tool_response))
            # Request response with tool result
            await session.openai_ws.send(json.dumps({
                "type": "response.create"
            }))
            print(f"[REALTIME] Requested response.create after tool result")

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

# =============================================================================
# WebSocket Bridge Tasks
# =============================================================================

async def openai_reconnect(session: RealtimeSession):
    """
    Reconnect to OpenAI Realtime API transparently.
    Uses exponential backoff, re-configures session on success.
    """
    if session.is_reconnecting:
        print(f"[REALTIME] Already reconnecting, skipping")
        return

    if session.reconnect_count >= session.max_reconnects:
        print(f"[REALTIME] Max reconnects ({session.max_reconnects}) reached, giving up")
        session.status = "disconnected"
        await _safe_ws_send(session.portal_ws, {
            "type": "disconnected",
            "data": "Connection lost after multiple reconnection attempts"
        })
        await save_session_to_blackbox(session)
        # P1b: null the closed-but-non-None socket so the keepalive loop's
        # `if not session.openai_ws: break` fires once, instead of spinning on
        # the dead socket and re-triggering openai_reconnect every stale cycle
        # (each re-hitting this give-up branch → a redundant BlackBox save).
        if session.openai_ws:
            try:
                await session.openai_ws.close()
            except Exception:
                pass
            session.openai_ws = None
        return

    session.is_reconnecting = True
    session.reconnect_count += 1
    attempt = session.reconnect_count

    # Exponential backoff: 0.5s, 1s, 2s, 4s, 5s max (fast recovery for voice calls)
    delay = min(0.5 * (2 ** (attempt - 1)), 5)
    print(f"[REALTIME] Reconnecting (attempt {attempt}/{session.max_reconnects}) in {delay}s...")

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
        print(f"[REALTIME] Session closed during backoff — abandoning reconnect")
        session.is_reconnecting = False
        return

    try:
        # P1b: cancel the old listener FIRST — it is bound to the OLD ws
        # object; left running it would observe our close() below and emit a
        # spurious "disconnected" to the client mid-recovery.
        if session.listener_task and not session.listener_task.done():
            session.listener_task.cancel()

        # Close old connection
        if session.openai_ws:
            try:
                await session.openai_ws.close()
            except Exception:
                pass
            session.openai_ws = None

        # Reconnect
        if await connect_to_openai(session):
            # P1b (terminal guard): teardown may have run during the dial. If so,
            # close the socket we just opened and bail — do NOT respawn a
            # listener onto a session that is being torn down.
            if session.intentional_disconnect:
                if session.openai_ws:
                    try:
                        await session.openai_ws.close()
                    except Exception:
                        pass
                    session.openai_ws = None
                session.is_reconnecting = False
                return

            # Reconfigure session
            await configure_openai_session(session, session.operator)

            # Re-emit provenance after reconfigure so client UI stays in sync with the
            # newly-rebuilt system context (see Task 3 code review).
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
                if session.openai_ws:
                    try:
                        await session.openai_ws.close()
                    except Exception:
                        pass
                    session.openai_ws = None
                session.is_reconnecting = False
                return

            # P1b: respawn the listener on the NEW upstream ws. Without this
            # the previous openai_listener's `async for` (bound to the OLD closed
            # socket) has already exited, so NOTHING reads the new connection —
            # the session is a permanently mute one-way pipe still reporting
            # "reconnected" (same defect class as the Gemini P1a fix; the phone
            # bridge always had its own respawn loop).
            session.listener_task = asyncio.create_task(openai_listener(session))

            # Reset state
            session.reconnect_count = 0
            session.is_reconnecting = False
            session.last_ai_message_time = time.time()
            session.status = "connected"

            print(f"[REALTIME] Reconnected successfully on attempt {attempt}")

            await _safe_ws_send(session.portal_ws, {
                "type": "reconnected",
                "data": {"attempt": attempt}
            })
        else:
            print(f"[REALTIME] Reconnect attempt {attempt} failed")
            session.is_reconnecting = False
            asyncio.create_task(openai_reconnect(session))

    except Exception as e:
        print(f"[REALTIME] Reconnect error: {e}")
        session.is_reconnecting = False
        asyncio.create_task(openai_reconnect(session))


async def openai_keepalive_loop(session: RealtimeSession):
    """
    Send periodic keepalive silence to prevent OpenAI idle timeout.
    Also monitors for stale connections (no AI message for 60s).
    """
    keepalive_interval = 15  # seconds
    stale_timeout = 60  # seconds without AI message = stale (standardized across all backends)

    session.last_ai_message_time = time.time()
    print(f"[REALTIME] Keepalive loop started (interval={keepalive_interval}s, stale={stale_timeout}s)")

    try:
        while True:
            await asyncio.sleep(keepalive_interval)

            if not session.openai_ws or session.intentional_disconnect:
                break

            # Check for stale connection
            time_since_message = time.time() - session.last_ai_message_time
            if time_since_message > stale_timeout:
                print(f"[REALTIME] STALE CONNECTION: No AI message for {time_since_message:.0f}s")
                if not session.is_reconnecting and not session.intentional_disconnect:
                    session.last_ai_message_time = time.time()
                    asyncio.create_task(openai_reconnect(session))
                continue

            # Send keepalive: 20ms of silence as PCM16@24kHz
            try:
                if session.openai_ws:
                    # 20ms at 24kHz = 480 samples, PCM16 = 960 bytes of zeros
                    silence_bytes = b'\x00' * 960
                    silence_b64 = base64.b64encode(silence_bytes).decode('ascii')
                    await session.openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": silence_b64
                    }))
            except websockets.exceptions.ConnectionClosed:
                print(f"[REALTIME] Keepalive failed - connection closed")
                if not session.is_reconnecting and not session.intentional_disconnect:
                    asyncio.create_task(openai_reconnect(session))
                break
            except Exception as e:
                print(f"[REALTIME] Keepalive error: {e}")

    except asyncio.CancelledError:
        pass
    finally:
        print(f"[REALTIME] Keepalive loop stopped")


async def openai_listener(session: RealtimeSession):
    """
    Background task that listens for messages from OpenAI and forwards to Portal.
    """
    try:
        async for message in session.openai_ws:
            try:
                event = json.loads(message)
                await handle_openai_message(session, event)
            except json.JSONDecodeError:
                print(f"[REALTIME] Invalid JSON from OpenAI: {message[:100]}")
            except Exception as e:
                print(f"[REALTIME] Error handling OpenAI message: {e}")
                # P1b: never dangle a tool call — if this event was a function
                # call, answer it with an error payload + response.create so
                # the model recovers instead of waiting forever (dead air).
                await send_openai_style_tool_error(
                    session.openai_ws, session.portal_ws, event, e)
    except websockets.exceptions.ConnectionClosed as e:
        print(f"[REALTIME] OpenAI connection closed: {e}")
        if not session.intentional_disconnect and not session.is_reconnecting:
            print(f"[REALTIME] Unexpected disconnect - triggering reconnect")
            asyncio.create_task(openai_reconnect(session))
        else:
            session.status = "disconnected"
            await _safe_ws_send(session.portal_ws, {
                "type": "disconnected",
                "data": "OpenAI connection closed"
            })
    except Exception as e:
        print(f"[REALTIME] OpenAI listener error: {e}")
        session.status = "error"

# =============================================================================
# WebSocket Endpoint
# =============================================================================

@app.websocket("/ws/realtime/{session_id}")
async def realtime_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for the OpenAI Realtime API bridge.

    Bridges Portal <-> Orchestrator <-> OpenAI Realtime API.

    Message flow:
    1. Portal connects, sends 'connect' with operator
    2. Orchestrator connects to OpenAI Realtime API
    3. Orchestrator configures session with tools and context
    4. Bidirectional message passing begins
    5. Tool calls are executed locally and results sent to OpenAI
    """
    print(f"[REALTIME] WebSocket connection request for session: {session_id}")
    await websocket.accept()
    print(f"[REALTIME] WebSocket accepted for session: {session_id}")

    # Android URL-query path (audit M4 + I1): Android passes config via query
    # string instead of a JSON connect message. We use websocket.query_params.get()
    # INSIDE the handler (NOT FastAPI Query() signature injection on a WebSocket
    # route — that pattern is unverified in this codebase). These values become
    # defaults for the connect handler; if the web client also sends a JSON
    # connect message, those values win (last write wins — interactive UI).
    url_operator = websocket.query_params.get("operator")
    url_voice = websocket.query_params.get("voice")
    url_model = websocket.query_params.get("model")
    url_vad_type = websocket.query_params.get("vad_type")
    url_vad_eagerness = websocket.query_params.get("vad_eagerness")
    url_agent = websocket.query_params.get("agent")
    _idle_str = websocket.query_params.get("idle_timeout_ms")
    if _idle_str and _idle_str.strip().isdigit():
        url_idle_timeout_ms: Optional[int] = int(_idle_str.strip())
    else:
        url_idle_timeout_ms = None
        if _idle_str:
            print(f"[REALTIME] WARNING: idle_timeout_ms {_idle_str!r} not a positive integer; ignoring")
    _interrupt_str = websocket.query_params.get("interrupt_response")
    url_interrupt_response: Optional[bool] = (
        _interrupt_str.lower() == "true" if _interrupt_str is not None else None
    )
    _create_str = websocket.query_params.get("create_response")
    url_create_response: Optional[bool] = (
        _create_str.lower() == "true" if _create_str is not None else None
    )
    url_noise_reduction = websocket.query_params.get("noise_reduction")
    url_transcription_delay = websocket.query_params.get("transcription_delay")

    # Check dependencies
    if not WEBSOCKETS_AVAILABLE:
        await _safe_ws_send(websocket, {
            "type": "error",
            "data": "Server missing 'websockets' library. Install with: pip install websockets"
        })
        await websocket.close()
        return

    if not OPENAI_API_KEY:
        await _safe_ws_send(websocket, {
            "type": "error",
            "data": "OPENAI_API_KEY not configured on server"
        })
        await websocket.close()
        return

    # Create or get session
    session = REALTIME_SESSIONS.get(session_id)
    if not session:
        session = RealtimeSession(
            session_id=session_id,
            created_at=now_utc_iso()
        )
        REALTIME_SESSIONS[session_id] = session

    session.portal_ws = websocket
    session.last_activity = now_utc_iso()

    openai_task = None
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
                print(f"[REALTIME] Client idle timeout (120s): {session_id}")
                await _safe_ws_send(websocket, {"type": "error", "data": "Idle timeout"})
                break

            # Per-message error isolation — bad JSON doesn't kill session
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as e:
                print(f"[REALTIME] Bad message from client: {e}")
                continue

            msg_type = data.get("type", "")

            if msg_type == "connect":
                # Initial connection - establish OpenAI WebSocket.
                # Merge rule: JSON connect message wins over Android URL-query
                # fallbacks (web is interactive — last write wins).
                # Voice-agent preset (P4): ?agent=<id> or "agent" in the JSON
                # connect message. Precedence: explicit params > preset fields
                # > defaults. Unknown/mismatched preset -> loud client warning,
                # session continues without it (fresh-box degradation).
                agent_id = data.get("agent", url_agent)
                preset = resolve_preset(agent_id, provider="realtime") if agent_id else None
                if agent_id and preset is None:
                    await _safe_ws_send(websocket, {
                        "type": "warning",
                        "data": f"Voice agent preset {agent_id!r} not found for provider 'realtime' — continuing without preset"
                    })
                merged = merge_connect_params({
                    "model": data.get("model", url_model),
                    "voice": data.get("voice", url_voice),
                    "greeting": data.get("greeting", ""),
                    "instructions": data.get("role", ""),
                }, preset)
                operator = data.get("operator", url_operator or "")
                voice = merged["voice"] or "ash"          # route default unchanged
                greeting = merged["greeting"] or ""
                role = merged["instructions"] or ""       # preset instructions ride the custom_role branch
                tool_group_override = merged["tool_group_override"]
                # T2 new fields — model goes to connect_to_openai (URL query),
                # vad/timeout/response fields go to configure_openai_session (session.update).
                model = merged["model"]
                vad_type = data.get("vad_type", url_vad_type)
                vad_eagerness = data.get("vad_eagerness", url_vad_eagerness)
                idle_timeout_ms = data.get("idle_timeout_ms", url_idle_timeout_ms)
                interrupt_response = data.get("interrupt_response", url_interrupt_response)
                create_response = data.get("create_response", url_create_response)
                noise_reduction = data.get("noise_reduction", url_noise_reduction)
                transcription_delay = data.get("transcription_delay", url_transcription_delay)
                session.operator = operator

                await _safe_ws_send(websocket, {
                    "type": "status",
                    "data": "Connecting to OpenAI..."
                })

                # Connect to OpenAI (model bound at upstream WS URL — audit C3)
                if await connect_to_openai(session, model=model):
                    # Configure session with tools, context, voice + VAD/turn settings
                    await configure_openai_session(
                        session,
                        operator,
                        voice,
                        custom_role=role,
                        tool_group_override=tool_group_override,
                        vad_type=vad_type,
                        vad_eagerness=vad_eagerness,
                        idle_timeout_ms=idle_timeout_ms,
                        interrupt_response=interrupt_response,
                        create_response=create_response,
                        noise_reduction=noise_reduction,
                        transcription_delay=transcription_delay,
                    )
                    print(f"[REALTIME] Voice selected: {voice}; model={model or OPENAI_REALTIME_MODEL}; vad_type={vad_type or 'server_vad'}")

                    # Emit provenance to the client once per session start so
                    # the Android/Portal UI can show which snapshots were used
                    # to seed the system message. Task 10 wires the parser.
                    if session.provenance:
                        await _safe_ws_send(websocket, {
                            "type": "provenance",
                            "data": session.provenance
                        })

                    # Start OpenAI listener task and keepalive. The listener task
                    # lives on the SESSION (session.listener_task): openai_reconnect
                    # respawns it, so this local would go stale after the first
                    # reconnect and leak the live task at teardown.
                    openai_task = asyncio.create_task(openai_listener(session))
                    session.listener_task = openai_task
                    keepalive_task = asyncio.create_task(openai_keepalive_loop(session))

                    # If greeting provided (outbound call), inject it so the AI speaks first
                    if greeting:
                        print(f"[REALTIME] Injecting outbound greeting: {greeting[:80]}...")
                        try:
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
                        except Exception as e:
                            print(f"[REALTIME] Greeting injection failed: {e}")

                    await _safe_ws_send(websocket, {
                        "type": "connected",
                        "data": {
                            "session_id": session_id,
                            "operator": operator,
                            "model": model or OPENAI_REALTIME_MODEL
                        }
                    })
                else:
                    await _safe_ws_send(websocket, {
                        "type": "error",
                        "data": "Failed to connect to OpenAI Realtime API"
                    })

            elif msg_type == "disconnect":
                # Graceful disconnect
                session.intentional_disconnect = True
                break

            elif msg_type == "ping":
                # Keep-alive
                await _safe_ws_send(websocket, {"type": "pong"})

            else:
                # Isolate handler errors — don't kill session on one bad forward
                try:
                    await handle_portal_message(session, data)
                except Exception as handler_err:
                    print(f"[REALTIME] Handler error (non-fatal): {handler_err}")

    except WebSocketDisconnect:
        print(f"[REALTIME] Portal WebSocket disconnected: {session_id}")
        session.portal_ws = None

    except Exception as e:
        print(f"[REALTIME] WebSocket error: {e}")
        await _safe_ws_send(websocket, {
            "type": "error",
            "data": str(e)
        })

    finally:
        # P1b (terminal guard): mark the session terminal FIRST — before the
        # save await and before cancelling tasks — so any in-flight, detached
        # openai_reconnect (bare create_task, not tracked here) bails after its
        # backoff sleep instead of re-dialing and flipping status back to
        # "connected", which would resurrect a clientless session the reaper can
        # never evict (it only reaps status=="disconnected").
        session.intentional_disconnect = True

        # Save session to BlackBox before cleanup
        if session.conversation:
            await save_session_to_blackbox(session)

        # Cleanup — cancel the CURRENT listener; after a reconnect this is a
        # different task than the locally-captured openai_task, so cancelling
        # only the local would leak the live listener.
        listener = session.listener_task or openai_task
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

        if session.openai_ws:
            try:
                await session.openai_ws.close()
            except:
                pass
            session.openai_ws = None

        session.portal_ws = None
        session.status = "disconnected"
        session.last_activity = now_utc_iso()  # start reaper grace clock (live_session_reaper)
        release_payload(session)               # free audio/transcript buffers; conversation already saved
        print(f"[REALTIME] Session {session_id} cleaned up")

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
