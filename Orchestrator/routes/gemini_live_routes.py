#!/usr/bin/env python3
"""
gemini_live_routes.py - Gemini Live API WebSocket Bridge

This module provides a WebSocket bridge between the Portal frontend and
Google's Gemini Live API, enabling real-time voice conversations with
semantic search capabilities over the BlackBox snapshot volume.

Architecture:
    Portal (Browser) <--WebSocket--> Orchestrator <--WebSocket--> Gemini Live API

Features:
- Bidirectional audio/text streaming
- Tool calling (search_snapshots for semantic search)
- Automatic context injection (checkpoint + recent snapshots)
- Session management with voice selection
- 16kHz input / 24kHz output audio format
"""

# Standard library imports
import asyncio
import json
import base64
import time
from typing import Optional, Dict, Any

# External library imports
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("[GEMINI-LIVE] websockets library not installed - run: pip install websockets")

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    print("[GEMINI-LIVE] aiohttp library not installed - run: pip install aiohttp")

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

# Local imports
from Orchestrator.checkpoint import app
from Orchestrator.config import (
    GOOGLE_API_KEY,
    GEMINI_LIVE_URL,
    GEMINI_LIVE_MODEL,
    GEMINI_LIVE_MODELS,
    GEMINI_LIVE_VOICES,
    GEMINI_LIVE_VOICE_DESCRIPTORS,
    GEMINI_LIVE_VAD_SENSITIVITIES,
    GEMINI_LIVE_THINKING_LEVELS,
    GEMINI_LIVE_THINKING_CAPABLE_MODELS,
    GEMINI_LIVE_INPUT_SAMPLE_RATE,
    GEMINI_LIVE_OUTPUT_SAMPLE_RATE,
    GEMINI_LIVE_DEFAULT_VOICE,
    REALTIME_CONTEXT_MAX_CHARS,
    REALTIME_SNAPSHOT_CHARS_EACH,
    GEMINI_MODEL_DEFAULT,
    VOL_PATH
)
from Orchestrator.models import GeminiLiveSession, GEMINI_LIVE_SESSIONS, TaskType
from Orchestrator.volume import now_utc_iso, read_text_safe
from Orchestrator.fossils import (
    hybrid_retrieve,
    get_recent_fossils_for_operator,
    get_recent_checkpoints_for_operator
)
from Orchestrator.context_builder import build_fossil_context
from Orchestrator.web_tools import perform_web_search, perform_web_fetch
from Orchestrator.tasks import create_task
from Orchestrator.whisper_filter import is_whisper_hallucination
from Orchestrator.tools.tool_registry import get_gemini_live_tools
from Orchestrator.behavioral_core import BEHAVIORAL_CORE_VOICE


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
# Tool Definitions for Gemini Live
# =============================================================================

GEMINI_LIVE_TOOLS = get_gemini_live_tools("gemini_live")


# =============================================================================
# Context Injection
# =============================================================================

def build_context_for_operator(operator: str, user_text: str = "") -> tuple[str, dict]:
    """
    Build initial context for a Gemini Live session.

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
        user_text, operator, log_prefix="[GEMINI-LIVE]"
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

async def execute_search_snapshots(session: GeminiLiveSession, arguments: Dict) -> str:
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
            # Truncate each result
            if len(snap_text) > 3000:
                snap_text = snap_text[:3000] + "\n... [truncated]"
            output_parts.append(f"--- Result {i} ---\n{snap_text}")

        return "\n\n".join(output_parts)

    except Exception as e:
        print(f"[GEMINI-LIVE] Search error: {e}")
        return f"Search failed: {str(e)}"

# =============================================================================
# Gemini Live API Connection
# =============================================================================

async def connect_to_gemini(session: GeminiLiveSession) -> bool:
    """
    Establish WebSocket connection to Gemini Live API.

    Returns True if connection successful, False otherwise.
    """
    if not WEBSOCKETS_AVAILABLE:
        print("[GEMINI-LIVE] Cannot connect - websockets library not installed")
        return False

    if not GOOGLE_API_KEY:
        print("[GEMINI-LIVE] Cannot connect - GOOGLE_API_KEY not set")
        return False

    try:
        # Gemini Live uses API key in URL query parameter
        url = f"{GEMINI_LIVE_URL}?key={GOOGLE_API_KEY}"

        print(f"[GEMINI-LIVE] Connecting to Gemini Live API...")
        # Add explicit ping settings to prevent connection drops
        session.gemini_ws = await websockets.connect(
            url,
            open_timeout=10,       # 10s max to establish connection (prevents indefinite hang)
            ping_interval=20,      # Send ping every 20 seconds
            ping_timeout=30,       # Wait 30 seconds for pong response
            close_timeout=10,      # Wait 10 seconds for close handshake
        )
        session.status = "connected"
        session.last_activity = now_utc_iso()
        print(f"[GEMINI-LIVE] Connected to Gemini for session {session.session_id}")
        return True

    except Exception as e:
        print(f"[GEMINI-LIVE] Connection failed: {e}")
        session.status = "error"
        return False

async def configure_gemini_session(
    session: GeminiLiveSession,
    operator: str,
    voice: str,
    custom_role: str = "",
    phone_mode: bool = False,
    model: Optional[str] = None,
    vad_sensitivity_start: Optional[str] = None,
    vad_sensitivity_end: Optional[str] = None,
    thinking_level: Optional[str] = None,
):
    """
    Configure the Gemini Live session with tools and settings.
    Sends BidiGenerateContentSetup message.

    Args:
        session: GeminiLiveSession object
        operator: Operator name for context
        voice: Voice to use
        custom_role: Optional custom system prompt/persona for outbound calls
        phone_mode: If True, enable server-side VAD with phone-tuned parameters (LOW sensitivity, 1000ms silence)
        model: Optional model id override (e.g. "gemini-3.1-flash-live-preview").
            Validated against GEMINI_LIVE_MODELS — invalid values fall back to
            GEMINI_LIVE_MODEL default with a logged warning. The model field is
            part of the BidiGenerateContentSetup payload (NOT URL-bound like
            OpenAI Realtime).
        vad_sensitivity_start: Optional start-of-speech sensitivity — one of
            "LOW" | "MEDIUM" | "HIGH" (UPPERCASE per Gemini API enum). Translated
            internally to the START_SENSITIVITY_* enum value. Invalid → log
            warning + ignore (server applies its own default).
        vad_sensitivity_end: Optional end-of-speech sensitivity — same shape.
        thinking_level: Optional reasoning budget hint — one of "minimal" | "low"
            | "medium" | "high" (LOWERCASE per google-genai SDK 1.64.0
            ThinkingConfig enum — do NOT uppercase). Only emitted when
            resolved_model is in GEMINI_LIVE_THINKING_CAPABLE_MODELS (config.py);
            ignored otherwise so non-thinking sessions don't trip server-side rejects.

        All new kwargs default to None to preserve phone bridge call sites at
        phone/bridge.py:1286, 1370 (audit C2 — those sites only pass positional
        + phone_mode=True; Optional=None defaults keep them unchanged).
    """
    if not session.gemini_ws:
        return

    # Allowlist validation of client-supplied fields.
    _allowed_model_ids = {m["id"] for m in GEMINI_LIVE_MODELS}
    if model is not None and model not in _allowed_model_ids:
        print(f"[GEMINI-LIVE] WARNING: model {model!r} not in GEMINI_LIVE_MODELS allowlist; falling back to default {GEMINI_LIVE_MODEL!r}")
        model = None
    resolved_model = model or GEMINI_LIVE_MODEL

    if vad_sensitivity_start is not None and vad_sensitivity_start not in GEMINI_LIVE_VAD_SENSITIVITIES:
        print(f"[GEMINI-LIVE] WARNING: vad_sensitivity_start {vad_sensitivity_start!r} not in {GEMINI_LIVE_VAD_SENSITIVITIES}; ignoring")
        vad_sensitivity_start = None
    if vad_sensitivity_end is not None and vad_sensitivity_end not in GEMINI_LIVE_VAD_SENSITIVITIES:
        print(f"[GEMINI-LIVE] WARNING: vad_sensitivity_end {vad_sensitivity_end!r} not in {GEMINI_LIVE_VAD_SENSITIVITIES}; ignoring")
        vad_sensitivity_end = None

    if thinking_level is not None and thinking_level not in GEMINI_LIVE_THINKING_LEVELS:
        print(f"[GEMINI-LIVE] WARNING: thinking_level {thinking_level!r} not in {GEMINI_LIVE_THINKING_LEVELS} (lowercase); ignoring")
        thinking_level = None

    # Build system instructions with operator-specific context.
    # `operator` is request-scoped — comes from the WS connect handshake
    # message (`data.get("operator", "")`) at the bottom of this file.
    # At session open we have no user_text yet, so keyword/semantic will
    # be empty; recent + checkpoint still populate.
    context, provenance = build_context_for_operator(operator, user_text="")
    # Stash provenance on the session so the cleanup/save path and the
    # client-facing WS emission below can reference it.
    session.provenance = provenance
    is_system_operator = (operator == "system")

    # If custom_role is provided, use it as the primary system instruction
    if custom_role:
        system_instructions = f"""{custom_role}

TEMPORAL AWARENESS — FIRST ACTION:
Your VERY FIRST action must be to call get_current_time to anchor yourself in the present. Do this before any other tool calls or responses.

ESSENTIAL TOOLS:
You have access to search_snapshots and get_recent_snapshots for memory/context.
You can also generate images, videos, music, and send SMS or make phone calls.
You have search_contacts and save_contact for the contact book.
You can create, edit, and search scheduled cron jobs for automated tasks and reminders.
Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.

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
            context_section = f"CONTEXT:\n{context if context else 'No recent context loaded yet. Use get_recent_snapshots immediately!'}"
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

        system_instructions = f"""{BEHAVIORAL_CORE_VOICE}

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
   - Call generate_image with reference_images parameter:
     generate_image(prompt="new image description", reference_images=["/ui/uploads/ref1.png", "/ui/uploads/ref2.png"])
   - Can include up to 10 reference images

4. TEXT-TO-VIDEO/IMAGE (from scratch):
   - Just use generate_video(prompt="...") or generate_image(prompt="...") without URL parameters

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

COMMUNICATION TOOLS:
You can reach out to people via phone or text:

1. SEND SMS (text message):
   - send_sms(phone_number="+15551234567", message="Hello!")
   - Use when asked to "text someone" or "send a message"
   - Messages are limited to 1600 characters

2. MAKE PHONE CALL (interactive AI call):
   - make_phone_call(phone_number="+15551234567", greeting="Hello, I'm calling from BlackBox")
   - Initiates a call where the AI assistant talks to the recipient
   - Use when asked to "call someone" or "call them back"

3. MAKE VOICE CALL (one-way message delivery):
   - make_voice_call(phone_number="+15551234567", message="Your message here", voice="onyx")
   - Calls and delivers a pre-recorded TTS message
   - Use when asked to leave a voice message

Phone numbers can be in E.164 format (+15551234567) or 10-digit US format (5551234567).

CONTACT BOOK:
You have search_contacts and save_contact for the contact book.
Before making calls or sending texts, always search_contacts first to find the person's number. When a user mentions someone new with contact info, save them to the contact book.

SCHEDULED TASKS (CRON JOBS):
You can create, edit, and search scheduled cron jobs for automated tasks and reminders.

SESSION START - CRITICAL:
IMMEDIATELY use get_recent_snapshots(count=3) at the START of EVERY session to catch up on recent context.
This is essential because:
- You may be continuing work started by another model or agent
- The snapshots contain the most recent conversations, decisions, and context
- {"For this outbound call: CHECK SNAPSHOTS for who you're calling, what task to complete, order details, etc." if is_system_operator else "For outbound calls: task details, order info, names, addresses may be in the snapshots"}
- Context handoff between models happens through snapshots - USE THEM!

Do this BEFORE responding to the user - check what happened recently so you're caught up."""

    # Build BidiGenerateContentSetup message
    setup_config = {
        "model": f"models/{resolved_model}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": voice
                    }
                }
            }
        },
        "systemInstruction": {
            "parts": [{"text": system_instructions}]
        },
        "tools": GEMINI_LIVE_TOOLS,
        "contextWindowCompression": {
            "slidingWindow": {}
        }
    }

    # Add session resumption if we have a handle from a previous connection
    if session.resumption_handle:
        setup_config["sessionResumption"] = {"handle": session.resumption_handle}

    # For phone mode, enable server-side VAD with phone-tuned parameters
    # Matching the OpenAI pattern: stream audio directly, let server handle turn detection
    if phone_mode:
        setup_config["realtimeInputConfig"] = {
            "automaticActivityDetection": {
                "disabled": False,
                "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                "prefixPaddingMs": 300,
                "silenceDurationMs": 1000
            },
            "activityHandling": "START_OF_ACTIVITY_INTERRUPTS"
        }
        print(f"[GEMINI-LIVE] Phone mode: server-side VAD enabled (LOW sensitivity, 1000ms silence)")

    # Client-supplied VAD sensitivity overrides (web UI / Android).
    # Merge into existing realtimeInputConfig.automaticActivityDetection if
    # phone_mode already set one; otherwise create the dict. Skip the field
    # entirely if the corresponding param is None — let server defaults apply.
    if vad_sensitivity_start is not None or vad_sensitivity_end is not None:
        rt_cfg = setup_config.setdefault("realtimeInputConfig", {})
        aad = rt_cfg.setdefault("automaticActivityDetection", {"disabled": False})
        if vad_sensitivity_start is not None:
            # LOW/MEDIUM/HIGH → START_SENSITIVITY_LOW/MEDIUM/HIGH (Gemini API enum)
            aad["startOfSpeechSensitivity"] = f"START_SENSITIVITY_{vad_sensitivity_start}"
        if vad_sensitivity_end is not None:
            aad["endOfSpeechSensitivity"] = f"END_SENSITIVITY_{vad_sensitivity_end}"
        print(f"[GEMINI-LIVE] VAD sensitivity override: start={vad_sensitivity_start}, end={vad_sensitivity_end}")

    # thinkingLevel is ONLY valid for models in GEMINI_LIVE_THINKING_CAPABLE_MODELS
    # (currently just gemini-3.1-flash-live-preview). Emitting it on 2.5 would
    # either be silently ignored or trip an upstream reject — gate strictly on
    # the capability set so renames/new models in config.py stay in sync. Path is
    # setup.generationConfig.thinkingConfig.thinkingLevel (under generationConfig,
    # NOT modelConfig — verified against google-genai SDK 1.64.0 types.py:
    # ThinkingConfig). Value is LOWERCASE (minimal/low/medium/high) per same SDK enum.
    if thinking_level is not None and resolved_model in GEMINI_LIVE_THINKING_CAPABLE_MODELS:
        setup_config["generationConfig"]["thinkingConfig"] = {
            "thinkingLevel": thinking_level
        }
        print(f"[GEMINI-LIVE] thinkingLevel={thinking_level} enabled for {resolved_model}")
    elif thinking_level is not None:
        print(f"[GEMINI-LIVE] thinking_level={thinking_level!r} ignored — model {resolved_model!r} does not support thinkingConfig (not in GEMINI_LIVE_THINKING_CAPABLE_MODELS)")

    setup_message = {"setup": setup_config}

    await session.gemini_ws.send(json.dumps(setup_message))
    session.context_injected = True
    session.voice = voice
    print(f"[GEMINI-LIVE] Session configured for operator {operator} with voice {voice}; model={resolved_model}")

# =============================================================================
# Message Handlers
# =============================================================================

async def transcribe_user_audio(session: GeminiLiveSession) -> Optional[str]:
    """
    Transcribe buffered user audio using Whisper via /stt/json endpoint.
    Returns the transcript text or None if transcription failed.
    """
    if not session.user_audio_buffer:
        return None

    if not AIOHTTP_AVAILABLE:
        print("[GEMINI-LIVE] Cannot transcribe - aiohttp not installed")
        session.user_audio_buffer = []
        return None

    # Combine all base64 PCM16 chunks
    try:
        combined_binary = b"".join(base64.b64decode(chunk) for chunk in session.user_audio_buffer)
        combined_base64 = base64.b64encode(combined_binary).decode('utf-8')

        # Clear buffer
        session.user_audio_buffer = []

        if len(combined_binary) < 100:
            print(f"[GEMINI-LIVE] Audio too short to transcribe ({len(combined_binary)} bytes)")
            return None

        print(f"[GEMINI-LIVE] Transcribing {len(combined_binary)} bytes of user audio with Whisper")

        # Call internal STT endpoint
        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(
                "http://localhost:9091/stt/json",
                json={
                    "audio": combined_base64,
                    "sample_rate": GEMINI_LIVE_INPUT_SAMPLE_RATE,
                    "format": "pcm16"
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    transcript = result.get("text", "").strip()
                    if transcript and not is_whisper_hallucination(transcript):
                        print(f"[GEMINI-LIVE] Whisper transcript: {transcript[:100]}...")
                        return transcript
                    elif transcript:
                        print(f"[GEMINI-LIVE] Filtered Whisper hallucination: '{transcript[:80]}'")
                    else:
                        print("[GEMINI-LIVE] Whisper returned empty transcript")
                else:
                    error = await resp.text()
                    print(f"[GEMINI-LIVE] Whisper error: {resp.status} - {error[:200]}")

    except Exception as e:
        print(f"[GEMINI-LIVE] Transcription error: {e}")
        session.user_audio_buffer = []  # Clear buffer on error

    return None


async def save_session_to_blackbox(session: GeminiLiveSession):
    """
    Save the Gemini Live session conversation to BlackBox.
    Called on disconnect/cleanup to ensure all messages are captured.
    """
    if not session.conversation:
        print(f"[GEMINI-LIVE] No conversation to save for session {session.session_id}")
        return

    if not session.operator:
        print(f"[GEMINI-LIVE] No operator set, cannot save session {session.session_id}")
        return

    # Sort conversation by timestamp to ensure correct order
    # (Whisper transcription may complete after AI response due to race condition)
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

    session_summary = f"""=== Gemini Live Voice Session ===
Session ID: {session.session_id}
Voice: {session.voice}
Timestamp: {now_utc_iso()}
Messages: {len(session.conversation)}

--- Transcript ---
{transcript}
--- End Session ---"""

    print(f"[GEMINI-LIVE] Saving session {session.session_id} with {len(session.conversation)} messages to BlackBox")

    try:
        # Use aiohttp to call /chat endpoint
        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(
                "http://localhost:9091/chat",
                json={
                    "operator": session.operator,
                    "messages": [{
                        "role": "user",
                        "content": f"[Voice Session Transcript]\n{session_summary}"
                    }],
                    "provider": "google",
                    "model": GEMINI_MODEL_DEFAULT,
                    "streaming": False,
                    "auto_checkpoint": False
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    print(f"[GEMINI-LIVE] Session {session.session_id} saved to BlackBox")
                else:
                    error = await resp.text()
                    print(f"[GEMINI-LIVE] Failed to save session: {resp.status} - {error[:200]}")

    except Exception as e:
        print(f"[GEMINI-LIVE] Error saving session to BlackBox: {e}")

    # Clear conversation after saving
    session.conversation = []


async def handle_portal_message(session: GeminiLiveSession, data: Dict):
    """
    Handle messages from Portal and forward to Gemini.

    Message types from Portal:
    - audio_input: Base64 PCM16 audio chunk (16kHz)
    - audio_commit: End of audio input, triggers response
    - text_input: Text message
    - interrupt: Cancel current response
    - video_frame: Base64 JPEG frame for multimodal vision (screen sharing)
    """
    msg_type = data.get("type", "")

    if msg_type == "audio_input":
        # Forward audio to Gemini using BidiGenerateContentRealtimeInput
        audio_data = data.get("data", "")
        if session.gemini_ws and audio_data:
            realtime_input = {
                "realtime_input": {
                    "audio": {
                        "mimeType": f"audio/pcm;rate={GEMINI_LIVE_INPUT_SAMPLE_RATE}",
                        "data": audio_data  # Base64 PCM16 at 16kHz
                    }
                }
            }
            await session.gemini_ws.send(json.dumps(realtime_input))

            # Buffer audio for Whisper transcription (Gemini doesn't provide user transcription)
            session.user_audio_buffer.append(audio_data)

            session.is_recording = True
            session.last_activity = now_utc_iso()

    elif msg_type == "audio_commit":
        # Gemini uses automatic activity detection by default
        # No need to send explicit activityEnd - just stop sending audio
        # Gemini will detect the pause and start responding
        session.is_recording = False
        # Capture timestamp NOW (when user stopped speaking) - not after Whisper completes
        # This ensures correct ordering even if Whisper takes longer than Gemini response
        commit_timestamp = now_utc_iso()
        session.last_activity = commit_timestamp
        print(f"[GEMINI-LIVE] Audio commit - {len(session.user_audio_buffer)} chunks buffered")

        # Transcribe user audio with Whisper and send user_transcript event
        # This runs async so it doesn't block Gemini's response.
        # NOTE: Gemini Live user transcript is post-hoc Whisper file STT (no
        # interim deltas), so it is final-only by design — no user_transcript_delta
        # here (unlike the GPT Realtime + Grok live agents which emit interims).
        transcript = await transcribe_user_audio(session)
        if transcript:
            # Add to session conversation for BlackBox snapshot
            # Use commit_timestamp (when user stopped speaking) for correct chronological order
            session.conversation.append({
                "role": "user",
                "content": transcript,
                "timestamp": commit_timestamp,
                "source": "voice"
            })
            # Also notify Portal (may or may not arrive before disconnect)
            if session.portal_ws:
                if await _safe_ws_send(session.portal_ws, {
                    "type": "user_transcript",
                    "data": transcript
                }):
                    print(f"[GEMINI-LIVE] Sent user_transcript to Portal")
                else:
                    print(f"[GEMINI-LIVE] Could not send user_transcript (ws closed?)")

    elif msg_type == "text_input":
        # Send text message using BidiGenerateContentClientContent
        text = data.get("text", "")
        if session.gemini_ws and text:
            # Add to session conversation
            session.conversation.append({
                "role": "user",
                "content": text,
                "timestamp": now_utc_iso(),
                "source": "text"
            })
            client_content = {
                "clientContent": {
                    "turns": [{
                        "role": "user",
                        "parts": [{"text": text}]
                    }],
                    "turnComplete": True
                }
            }
            await session.gemini_ws.send(json.dumps(client_content))
            session.last_activity = now_utc_iso()

    elif msg_type == "interrupt":
        # Gemini doesn't have explicit cancel - just stop processing
        # The model handles interruption via activity detection
        session.is_speaking = False

    elif msg_type == "video_frame":
        # Forward video frame to Gemini for multimodal vision (screen sharing)
        # Gemini Live API processes video at 1 FPS alongside audio
        if session.gemini_ws:
            realtime_input = {
                "realtime_input": {
                    "video": {
                        "mimeType": "image/jpeg",
                        "data": data.get("data", "")  # Base64 JPEG frame
                    }
                }
            }
            await session.gemini_ws.send(json.dumps(realtime_input))
            session.last_activity = now_utc_iso()

async def handle_gemini_message(session: GeminiLiveSession, event: Dict):
    """
    Handle messages from Gemini and forward to Portal.

    Key event types from Gemini:
    - setupComplete: Session is ready
    - serverContent: Audio/text response with modelTurn
    - toolCall: Function execution request
    - toolCallCancellation: Cancel pending tool calls
    """

    session.last_ai_message_time = time.time()

    # Check for setupComplete
    if "setupComplete" in event:
        print(f"[GEMINI-LIVE] Session setup complete")
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "setup_complete",
                "data": event.get("setupComplete", {})
            })
        return

    # Check for serverContent (audio/text response)
    if "serverContent" in event:
        server_content = event["serverContent"]

        # Log all keys in serverContent to discover user transcription field
        sc_keys = list(server_content.keys())
        if sc_keys and sc_keys != ['modelTurn'] and sc_keys != ['turnComplete']:
            print(f"[GEMINI-LIVE] serverContent keys: {sc_keys}")

        # Handle input audio transcription (user's speech) - check various possible field names
        user_transcript = None
        for field in ["inputTranscript", "inputAudioTranscript", "userTranscript", "transcript"]:
            if field in server_content:
                user_transcript = server_content[field]
                print(f"[GEMINI-LIVE] Found user transcript in '{field}': {user_transcript[:100] if user_transcript else 'empty'}...")
                break

        if user_transcript and session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "user_transcript",
                "data": user_transcript
            })

        # Check if turn is complete
        turn_complete = server_content.get("turnComplete", False)
        generation_complete = server_content.get("generationComplete", False)

        # Extract model turn parts
        model_turn = server_content.get("modelTurn", {})
        parts = model_turn.get("parts", [])

        for part in parts:
            # Handle audio response
            if "inlineData" in part:
                inline_data = part["inlineData"]
                mime_type = inline_data.get("mimeType", "")
                audio_data = inline_data.get("data", "")

                if "audio" in mime_type and audio_data:
                    # Log audio format for debugging (every 10th chunk)
                    if not hasattr(session, '_audio_chunk_count'):
                        session._audio_chunk_count = 0
                    session._audio_chunk_count += 1
                    if session._audio_chunk_count % 10 == 1:
                        import base64
                        audio_bytes = base64.b64decode(audio_data)
                        print(f"[GEMINI-LIVE] Audio chunk #{session._audio_chunk_count}: mime={mime_type}, size={len(audio_bytes)} bytes")

                    # CONTINUOUS LISTENING FIX: When AI starts speaking, transcribe user audio
                    # This ensures each user turn is captured as a separate message
                    if not session.is_speaking and session.user_audio_buffer:
                        # AI just started responding - user's turn is complete
                        # Capture timestamp NOW (when user's turn ended)
                        user_turn_timestamp = now_utc_iso()
                        print(f"[GEMINI-LIVE] AI started speaking - transcribing {len(session.user_audio_buffer)} user audio chunks")

                        # Check if this is a phone session (PhoneWebSocketAdapter has .bridge attribute)
                        # Phone sessions need NON-BLOCKING transcription to avoid audio pipeline delays
                        is_phone_session = hasattr(session.portal_ws, 'bridge')

                        if is_phone_session:
                            # PHONE: Non-blocking transcription - don't delay audio delivery
                            async def transcribe_phone_background(sess, timestamp):
                                try:
                                    transcript = await transcribe_user_audio(sess)
                                    if transcript:
                                        sess.conversation.append({
                                            "role": "user",
                                            "content": transcript,
                                            "timestamp": timestamp,
                                            "source": "voice"
                                        })
                                        print(f"[GEMINI-LIVE] Phone user said: {transcript[:100]}...")
                                except Exception as e:
                                    print(f"[GEMINI-LIVE] Phone transcription error: {e}")
                            asyncio.create_task(transcribe_phone_background(session, user_turn_timestamp))
                        else:
                            # PORTAL: Blocking transcription for correct message ordering
                            user_transcript = await transcribe_user_audio(session)
                            if user_transcript:
                                # Add user message BEFORE AI response
                                session.conversation.append({
                                    "role": "user",
                                    "content": user_transcript,
                                    "timestamp": user_turn_timestamp,
                                    "source": "voice"
                                })
                                # Notify Portal
                                if session.portal_ws:
                                    await _safe_ws_send(session.portal_ws, {
                                        "type": "user_transcript",
                                        "data": user_transcript
                                    })

                    # Forward audio chunk to Portal
                    if session.portal_ws:
                        await _safe_ws_send(session.portal_ws, {
                            "type": "audio_delta",
                            "data": audio_data
                        })
                        session.is_speaking = True

            # Handle text response (for transcription or text-only mode)
            if "text" in part:
                text = part["text"]
                session.transcript_buffer += text
                if session.portal_ws:
                    await _safe_ws_send(session.portal_ws, {
                        "type": "transcript_delta",
                        "data": text
                    })

        # Handle turn/generation complete
        if turn_complete or generation_complete:
            session.is_speaking = False

            # Add AI response to conversation for BlackBox snapshot
            if session.transcript_buffer.strip():
                session.conversation.append({
                    "role": "assistant",
                    "content": session.transcript_buffer.strip(),
                    "timestamp": now_utc_iso()
                })

            # Send response_complete with a flag indicating if this was a tool-enhanced response
            if session.portal_ws:
                await _safe_ws_send(session.portal_ws, {
                    "type": "response_complete",
                    "data": {
                        "transcript": session.transcript_buffer,
                        "had_tool_call": session.pending_tool_call
                    }
                })

            # Reset state for next turn
            session.transcript_buffer = ""
            session.pending_tool_call = False

        return

    # Check for toolCall
    if "toolCall" in event:
        tool_call = event["toolCall"]
        function_calls = tool_call.get("functionCalls", [])

        # Mark that this response involved a tool call
        # The portal will use this to potentially discard the previous response
        session.pending_tool_call = True
        print(f"[GEMINI-LIVE] Tool call detected")

        for fc in function_calls:
            call_id = fc.get("id", "")
            name = fc.get("name", "")
            args = fc.get("args", {})

            print(f"[GEMINI-LIVE] Tool call: {name} with args: {args}")

            # Notify Portal that tool is being called
            if session.portal_ws:
                await _safe_ws_send(session.portal_ws, {
                    "type": "tool_call",
                    "data": {"name": name, "arguments": args}
                })

            # Execute the tool
            if name == "search_snapshots":
                result = await execute_search_snapshots(session, args)
            elif name == "web_search":
                query = args.get("query", "")
                recency = args.get("search_recency_filter", "month")
                print(f"[GEMINI-LIVE] Executing web search: {query}")
                result = perform_web_search(query, search_recency_filter=recency)
                print(f"[GEMINI-LIVE] Web search result length: {len(result)} chars")
            elif name == "web_fetch":
                url = args.get("url", "")
                max_chars = args.get("max_chars", 80000)
                print(f"[GEMINI-LIVE] Executing web fetch: {url}")
                result = perform_web_fetch(url, max_chars)
                print(f"[GEMINI-LIVE] Web fetch result length: {len(result)} chars")
            elif name == "generate_image":
                prompt = args.get("prompt", "")
                aspect_ratio = args.get("aspectRatio", "16:9")
                resolution = args.get("resolution", "1K")
                num_images = args.get("numberOfImages", 1)
                reference_images = args.get("reference_images", [])  # For image-to-image

                mode = "image-to-image" if reference_images else "text-to-image"
                print(f"[GEMINI-LIVE] Executing image generation ({mode}): {prompt[:100]}... ({num_images} @ {resolution})")

                # Create image generation task
                image_options = {
                    "aspectRatio": aspect_ratio,
                    "resolution": resolution,
                    "numberOfImages": num_images,
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

                print(f"[GEMINI-LIVE] Image generation task created: {task.task_id}")
            elif name == "generate_video":
                prompt = args.get("prompt", "")
                aspect_ratio = args.get("aspectRatio", "16:9")
                duration = args.get("duration", 8)
                resolution = args.get("resolution", "720p")
                negative_prompt = args.get("negativePrompt", "")
                image_url = args.get("image_url")  # For image-to-video
                video_url = args.get("video_url")  # For video extension

                mode = "text-to-video"
                if image_url:
                    mode = "image-to-video"
                elif video_url:
                    mode = "video-extension"

                print(f"[GEMINI-LIVE] Executing video generation ({mode}): {prompt[:100]}... ({duration}s @ {resolution})")

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

                print(f"[GEMINI-LIVE] Video generation task created: {task.task_id}")
            elif name == "generate_music":
                prompt = args.get("prompt", "")
                negative_prompt = args.get("negativePrompt", "")
                sample_count = args.get("sampleCount", 1)
                print(f"[GEMINI-LIVE] Executing music generation: {prompt[:100]}... ({sample_count} variation(s))")

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
                if session.portal_ws:
                    await _safe_ws_send(session.portal_ws, {
                        "type": "music_task",
                        "data": {"task_id": task.task_id, "prompt": prompt, "sample_count": sample_count}
                    })

                print(f"[GEMINI-LIVE] Music generation task created: {task.task_id}")
            elif name == "get_media":
                from Orchestrator.routes.chat_routes import execute_get_media
                url = args.get("url")
                task_id_param = args.get("task_id")
                print(f"[GEMINI-LIVE] Executing get_media: url={url}, task_id={task_id_param}")
                media_result = execute_get_media(url=url, task_id=task_id_param)
                result = json.dumps(media_result, indent=2)
            elif name == "list_media":
                from Orchestrator.routes.chat_routes import execute_list_media
                media_type = args.get("media_type")
                limit = args.get("limit", 20)
                print(f"[GEMINI-LIVE] Executing list_media: type={media_type}, limit={limit}")
                list_result = execute_list_media(media_type=media_type, limit=limit)
                result = json.dumps(list_result, indent=2)
            elif name == "search_media":
                from Orchestrator.routes.chat_routes import execute_search_media
                query = args.get("query", "")
                media_type = args.get("media_type")
                limit = args.get("limit", 10)
                print(f"[GEMINI-LIVE] Executing search_media: query='{query}', type={media_type}")
                search_result = execute_search_media(query=query, media_type=media_type, limit=limit)
                result = json.dumps(search_result, indent=2)
            elif name == "send_sms":
                # Send SMS using unified tool executor
                from Orchestrator.tools import BlackBoxToolExecutor
                phone_number = args.get("phone_number", "")
                message = args.get("message", "")
                print(f"[GEMINI-LIVE] Executing send_sms to {phone_number}: {message[:50]}...")
                executor = BlackBoxToolExecutor(operator=session.operator or "system")
                tool_result = await executor.execute("send_sms", {"phone_number": phone_number, "message": message})
                result = tool_result.rich_result()
                print(f"[GEMINI-LIVE] SMS result: {result}")
            elif name == "make_phone_call":
                # Initiate outbound phone call
                import aiohttp
                phone_number = args.get("phone_number", "")
                greeting = args.get("greeting", "")
                role = args.get("role", "")
                backend = args.get("backend", "openai_realtime")
                print(f"[GEMINI-LIVE] Executing make_phone_call to {phone_number}")
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
                print(f"[GEMINI-LIVE] Call result: {result}")
            elif name == "make_voice_call":
                # Call with pre-generated TTS message
                from Orchestrator.tools import BlackBoxToolExecutor
                phone_number = args.get("phone_number", "")
                message = args.get("message", "")
                voice = args.get("voice", "onyx")
                print(f"[GEMINI-LIVE] Executing make_voice_call to {phone_number}: {message[:50]}...")
                executor = BlackBoxToolExecutor(operator=session.operator or "system")
                tool_result = await executor.execute("make_voice_call", {
                    "phone_number": phone_number,
                    "message": message,
                    "voice": voice
                })
                result = tool_result.rich_result()
                print(f"[GEMINI-LIVE] Voice call result: {result}")
            elif name == "get_recent_snapshots":
                count = min(args.get("count", 3), 5)
                operator = session.operator or "system"

                # System operator sees ALL snapshots (for outbound calls/context handoff)
                # Regular operators only see their own snapshots (no context bleed)
                see_all = (operator == "system")

                print(f"[GEMINI-LIVE] Getting {count} recent snapshots for {operator} (see_all={see_all})")
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

                                # Truncate each snapshot
                                if len(snap_text) > 3000:
                                    snap_text = snap_text[:3000] + "\n... [truncated]"
                                result_parts.append(f"--- {snap_id} (operator: {operator_name}) ---\n{snap_text}")

                            result = "\n\n".join(result_parts)
                            print(f"[GEMINI-LIVE] Retrieved {len(recent)} recent snapshots")
                    else:
                        result = "No snapshots found in index."
                except Exception as e:
                    print(f"[GEMINI-LIVE] Error getting recent snapshots: {e}")
                    result = f"Error retrieving recent snapshots: {str(e)}"
            elif name == "search_contacts":
                from Orchestrator.contacts import search_contacts
                query = args.get("query", "")
                operator = session.operator or "system"
                print(f"[GEMINI-LIVE] Executing search_contacts: query='{query}', operator={operator}")
                contacts = search_contacts(query, operator)
                if contacts:
                    result = json.dumps(contacts, indent=2)
                else:
                    result = f"No contacts found matching '{query}'."
                print(f"[GEMINI-LIVE] search_contacts result: {len(contacts)} contacts found")
            elif name == "save_contact":
                from Orchestrator.contacts import upsert_contact
                operator = session.operator or "system"
                contact_name = args.get("name", "")
                notes = args.get("notes", "")
                tags = args.get("tags", [])
                phone = args.get("phone")
                email = args.get("email")
                relationship = args.get("relationship")
                print(f"[GEMINI-LIVE] Executing save_contact: name='{contact_name}', operator={operator}")
                saved = upsert_contact(
                    name=contact_name, notes=notes, tags=tags,
                    operator=operator, created_by="gemini-live",
                    phone=phone, email=email, relationship=relationship
                )
                result = f"Contact saved: {json.dumps(saved, indent=2)}"
                print(f"[GEMINI-LIVE] save_contact result: saved '{contact_name}'")
            elif name == "create_cron_job":
                from Orchestrator.tools import BlackBoxToolExecutor
                operator = session.operator or "system"
                executor = BlackBoxToolExecutor(operator=operator)
                cron_result = await executor.execute("create_cron_job", args)
                result = cron_result.rich_result()
                print(f"[GEMINI-LIVE] create_cron_job result: {result}")
            elif name == "edit_cron_job":
                from Orchestrator.tools import BlackBoxToolExecutor
                operator = session.operator or "system"
                executor = BlackBoxToolExecutor(operator=operator)
                cron_result = await executor.execute("edit_cron_job", args)
                result = cron_result.rich_result()
                print(f"[GEMINI-LIVE] edit_cron_job result: {result}")
            elif name == "search_cron_jobs":
                from Orchestrator.tools import BlackBoxToolExecutor
                operator = session.operator or "system"
                executor = BlackBoxToolExecutor(operator=operator)
                cron_result = await executor.execute("search_cron_jobs", args)
                result = cron_result.rich_result()
                print(f"[GEMINI-LIVE] search_cron_jobs result: {result}")
            elif name in ("use_computer", "list_devices", "control_android_device"):
                from Orchestrator.tools import BlackBoxToolExecutor
                operator = session.operator or "system"
                executor = BlackBoxToolExecutor(operator=operator)
                tool_result = await executor.execute(name, args)
                result = tool_result.result
                print(f"[GEMINI-LIVE] {name}: {result[:100]}")
            elif name == "get_current_time":
                from datetime import datetime
                now = datetime.now()
                result = f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}"
                print(f"[GEMINI-LIVE] get_current_time: {result}")
            else:
                result = f"Unknown tool: {name}"

            # Send result back to Gemini using BidiGenerateContentToolResponse
            if session.gemini_ws:
                tool_response = {
                    "toolResponse": {
                        "functionResponses": [{
                            "id": call_id,
                            "name": name,
                            "response": {"result": result}
                        }]
                    }
                }
                await session.gemini_ws.send(json.dumps(tool_response))
                print(f"[GEMINI-LIVE] Sent tool response for {name}: {len(result)} chars")

            # Notify Portal
            if session.portal_ws:
                await _safe_ws_send(session.portal_ws, {
                    "type": "tool_result",
                    "data": {"name": name, "result_length": len(result)}
                })

        return

    # Check for toolCallCancellation
    if "toolCallCancellation" in event:
        cancellation = event["toolCallCancellation"]
        cancelled_ids = cancellation.get("ids", [])
        print(f"[GEMINI-LIVE] Tool calls cancelled: {cancelled_ids}")
        return

    # Check for sessionResumptionUpdate (store handle for reconnection)
    if "sessionResumptionUpdate" in event:
        new_handle = event["sessionResumptionUpdate"].get("newHandle", "")
        if new_handle:
            session.resumption_handle = new_handle
            print(f"[GEMINI-LIVE] Session resumption handle updated")
        return

    # Check for goAway (server disconnecting - reconnect transparently)
    if "goAway" in event:
        print(f"[GEMINI-LIVE] Server sending goAway - triggering transparent reconnect")
        if not session.intentional_disconnect and not session.is_reconnecting:
            asyncio.create_task(gemini_reconnect(session))
        return

    # Check for error in response
    if "error" in event:
        error = event.get("error", {})
        error_msg = error.get("message", "Unknown error")
        print(f"[GEMINI-LIVE] Gemini error: {error_msg}")
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "error",
                "data": error_msg
            })
        return

# =============================================================================
# Reconnection and Keepalive
# =============================================================================

async def gemini_reconnect(session: GeminiLiveSession):
    """
    Reconnect to Gemini Live API transparently.
    Uses session resumption handle if available, exponential backoff.
    """
    if session.is_reconnecting:
        print(f"[GEMINI-LIVE] Already reconnecting, skipping")
        return

    if session.reconnect_count >= session.max_reconnects:
        print(f"[GEMINI-LIVE] Max reconnects ({session.max_reconnects}) reached, giving up")
        session.status = "disconnected"
        await _safe_ws_send(session.portal_ws, {
            "type": "disconnected",
            "data": "Connection lost after multiple reconnection attempts"
        })
        # Save conversation before giving up
        await save_session_to_blackbox(session)
        return

    session.is_reconnecting = True
    session.reconnect_count += 1
    attempt = session.reconnect_count

    # Exponential backoff: 0.5s, 1s, 2s, 4s, 5s max (fast recovery for voice calls)
    delay = min(0.5 * (2 ** (attempt - 1)), 5)
    print(f"[GEMINI-LIVE] Reconnecting (attempt {attempt}/{session.max_reconnects}) in {delay}s...")

    # Notify Portal
    await _safe_ws_send(session.portal_ws, {
        "type": "reconnecting",
        "data": {"attempt": attempt, "max": session.max_reconnects, "delay": delay}
    })

    await asyncio.sleep(delay)

    try:
        # Close old connection
        if session.gemini_ws:
            try:
                await session.gemini_ws.close()
            except Exception:
                pass
            session.gemini_ws = None

        # Reconnect
        if await connect_to_gemini(session):
            # Reconfigure with resumption handle
            await configure_gemini_session(session, session.operator, session.voice)

            # Re-emit provenance after reconfigure so client UI stays in sync with the
            # newly-rebuilt system context (see Task 3 code review).
            if session.provenance:
                await _safe_ws_send(session.portal_ws, {
                    "type": "provenance",
                    "data": session.provenance
                })

            # Reset state
            session.reconnect_count = 0
            session.is_reconnecting = False
            session.last_ai_message_time = time.time()
            session.status = "connected"

            print(f"[GEMINI-LIVE] Reconnected successfully on attempt {attempt}")

            # Notify Portal
            await _safe_ws_send(session.portal_ws, {
                "type": "reconnected",
                "data": {"attempt": attempt}
            })
        else:
            print(f"[GEMINI-LIVE] Reconnect attempt {attempt} failed")
            session.is_reconnecting = False
            # Try again
            asyncio.create_task(gemini_reconnect(session))

    except Exception as e:
        print(f"[GEMINI-LIVE] Reconnect error: {e}")
        session.is_reconnecting = False
        asyncio.create_task(gemini_reconnect(session))


async def gemini_keepalive_loop(session: GeminiLiveSession):
    """
    Send periodic keepalive silence to prevent Gemini idle timeout.
    Also monitors for stale connections (no AI message for 60s).
    """
    keepalive_interval = 15  # seconds
    stale_timeout = 60  # seconds without AI message = stale (standardized across all backends)

    session.last_ai_message_time = time.time()
    print(f"[GEMINI-LIVE] Keepalive loop started (interval={keepalive_interval}s, stale={stale_timeout}s)")

    try:
        while True:
            await asyncio.sleep(keepalive_interval)

            # Check if session is still active
            if not session.gemini_ws or session.intentional_disconnect:
                break

            # Check for stale connection
            time_since_message = time.time() - session.last_ai_message_time
            if time_since_message > stale_timeout:
                print(f"[GEMINI-LIVE] STALE CONNECTION: No AI message for {time_since_message:.0f}s")
                if not session.is_reconnecting and not session.intentional_disconnect:
                    session.last_ai_message_time = time.time()  # Reset to prevent rapid retrigger
                    asyncio.create_task(gemini_reconnect(session))
                continue

            # Send keepalive: 20ms of silence as PCM16@16kHz
            try:
                if session.gemini_ws:
                    # 20ms at 16kHz = 320 samples, PCM16 = 640 bytes of zeros
                    silence_bytes = b'\x00' * 640
                    silence_b64 = base64.b64encode(silence_bytes).decode('ascii')
                    await session.gemini_ws.send(json.dumps({
                        "realtime_input": {
                            "audio": {
                                "mimeType": f"audio/pcm;rate={GEMINI_LIVE_INPUT_SAMPLE_RATE}",
                                "data": silence_b64
                            }
                        }
                    }))
            except websockets.exceptions.ConnectionClosed:
                print(f"[GEMINI-LIVE] Keepalive failed - connection closed")
                if not session.is_reconnecting and not session.intentional_disconnect:
                    asyncio.create_task(gemini_reconnect(session))
                break
            except Exception as e:
                print(f"[GEMINI-LIVE] Keepalive error: {e}")

    except asyncio.CancelledError:
        pass
    finally:
        print(f"[GEMINI-LIVE] Keepalive loop stopped")

# =============================================================================
# WebSocket Bridge Tasks
# =============================================================================

async def gemini_listener(session: GeminiLiveSession):
    """
    Background task that listens for messages from Gemini and forwards to Portal.
    """
    try:
        async for message in session.gemini_ws:
            try:
                event = json.loads(message)
                await handle_gemini_message(session, event)
            except json.JSONDecodeError:
                print(f"[GEMINI-LIVE] Invalid JSON from Gemini: {message[:100]}")
            except Exception as e:
                print(f"[GEMINI-LIVE] Error handling Gemini message: {e}")
    except websockets.exceptions.ConnectionClosed as e:
        print(f"[GEMINI-LIVE] Gemini connection closed: {e}")
        if not session.intentional_disconnect and not session.is_reconnecting:
            print(f"[GEMINI-LIVE] Unexpected disconnect - triggering reconnect")
            asyncio.create_task(gemini_reconnect(session))
        else:
            session.status = "disconnected"
            await _safe_ws_send(session.portal_ws, {
                "type": "disconnected",
                "data": "Gemini connection closed"
            })
    except Exception as e:
        print(f"[GEMINI-LIVE] Gemini listener error: {e}")
        session.status = "error"

# =============================================================================
# WebSocket Endpoint
# =============================================================================

@app.websocket("/ws/gemini-live/{session_id}")
async def gemini_live_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for Gemini Live API bridge.

    Bridges Portal <-> Orchestrator <-> Gemini Live API.

    Message flow:
    1. Portal connects, sends 'connect' with operator and voice
    2. Orchestrator connects to Gemini Live API
    3. Orchestrator configures session with tools and context
    4. Bidirectional message passing begins
    5. Tool calls are executed locally and results sent to Gemini
    """
    print(f"[GEMINI-LIVE] WebSocket connection request for session: {session_id}")
    await websocket.accept()
    print(f"[GEMINI-LIVE] WebSocket accepted for session: {session_id}")

    # Android URL-query path (audit M4 + I1): Android passes config via query
    # string instead of a JSON connect message. We use websocket.query_params.get()
    # INSIDE the handler (NOT FastAPI Query() signature injection on a WebSocket
    # route — that pattern is unverified in this codebase). These values become
    # defaults for the connect handler; if the web client also sends a JSON
    # connect message, those values win (JSON wins, URL fills missing — mirrors
    # T2 precedence at realtime_routes.py:1377-1391).
    #
    # Field naming: vad_sensitivity_start / vad_sensitivity_end across all sides
    # (JSON message, URL query, Python kwarg, Android client T11) for
    # consistency with the function signature.
    url_operator = websocket.query_params.get("operator")
    url_voice = websocket.query_params.get("voice")
    url_model = websocket.query_params.get("model")
    url_vad_sensitivity_start = websocket.query_params.get("vad_sensitivity_start")
    url_vad_sensitivity_end = websocket.query_params.get("vad_sensitivity_end")
    url_thinking_level = websocket.query_params.get("thinking_level")

    # Check dependencies
    if not WEBSOCKETS_AVAILABLE:
        await _safe_ws_send(websocket, {
            "type": "error",
            "data": "Server missing 'websockets' library. Install with: pip install websockets"
        })
        await websocket.close()
        return

    if not GOOGLE_API_KEY:
        await _safe_ws_send(websocket, {
            "type": "error",
            "data": "GOOGLE_API_KEY not configured on server"
        })
        await websocket.close()
        return

    # Create or get session
    session = GEMINI_LIVE_SESSIONS.get(session_id)
    if not session:
        session = GeminiLiveSession(
            session_id=session_id,
            created_at=now_utc_iso()
        )
        GEMINI_LIVE_SESSIONS[session_id] = session

    session.portal_ws = websocket
    session.last_activity = now_utc_iso()

    gemini_task = None
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
                print(f"[GEMINI-LIVE] Client idle timeout (120s): {session_id}")
                await _safe_ws_send(websocket, {"type": "error", "data": "Idle timeout"})
                break

            # Per-message error isolation
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as e:
                print(f"[GEMINI-LIVE] Bad message from client: {e}")
                continue

            msg_type = data.get("type", "")

            if msg_type == "connect":
                # Initial connection - establish Gemini WebSocket.
                # Merge rule: JSON connect message wins over Android URL-query
                # fallbacks (web is interactive — JSON wins, URL fills missing).
                # Mirrors T2 precedence at realtime_routes.py:1377-1391.
                operator = data.get("operator", url_operator or "")
                voice = data.get("voice", url_voice or GEMINI_LIVE_DEFAULT_VOICE)
                greeting = data.get("greeting", "")
                role = data.get("role", "")
                # T3 new fields — passed through to configure_gemini_session,
                # which validates against allowlists and emits payload extensions.
                model = data.get("model", url_model)
                vad_sensitivity_start = data.get("vad_sensitivity_start", url_vad_sensitivity_start)
                vad_sensitivity_end = data.get("vad_sensitivity_end", url_vad_sensitivity_end)
                thinking_level = data.get("thinking_level", url_thinking_level)
                session.operator = operator

                await _safe_ws_send(websocket, {
                    "type": "status",
                    "data": "Connecting to Gemini Live..."
                })

                # Connect to Gemini
                if await connect_to_gemini(session):
                    # Configure session with tools, context, voice + model/VAD/thinking
                    # If role provided (outbound call), use it as custom context
                    await configure_gemini_session(
                        session,
                        operator,
                        voice,
                        custom_role=role,
                        model=model,
                        vad_sensitivity_start=vad_sensitivity_start,
                        vad_sensitivity_end=vad_sensitivity_end,
                        thinking_level=thinking_level,
                    )

                    # Emit provenance to the client once per session start so
                    # the Android/Portal UI can show which snapshots were used
                    # to seed the system message. Task 10 wires the parser.
                    if session.provenance:
                        await _safe_ws_send(websocket, {
                            "type": "provenance",
                            "data": session.provenance
                        })

                    # If greeting provided (outbound call), inject it as initial turn
                    # so the model speaks first when the callee answers
                    if greeting:
                        print(f"[GEMINI-LIVE] Injecting outbound greeting: {greeting[:80]}...")
                        try:
                            prompt = f"The user just answered the phone. Greet them and deliver this message: {greeting}"
                            await session.gemini_ws.send(json.dumps({
                                "clientContent": {
                                    "turns": [{"role": "user", "parts": [{"text": prompt}]}],
                                    "turnComplete": True
                                }
                            }))
                        except Exception as e:
                            print(f"[GEMINI-LIVE] Greeting injection failed: {e}")

                    # Start Gemini listener task and keepalive
                    gemini_task = asyncio.create_task(gemini_listener(session))
                    keepalive_task = asyncio.create_task(gemini_keepalive_loop(session))

                    await _safe_ws_send(websocket, {
                        "type": "connected",
                        "data": {
                            "session_id": session_id,
                            "operator": operator,
                            "model": model or GEMINI_LIVE_MODEL,
                            "voice": voice
                        }
                    })
                else:
                    await _safe_ws_send(websocket, {
                        "type": "error",
                        "data": "Failed to connect to Gemini Live API"
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
                    await handle_portal_message(session, data)
                except Exception as handler_err:
                    print(f"[GEMINI-LIVE] Handler error (non-fatal): {handler_err}")

    except WebSocketDisconnect:
        print(f"[GEMINI-LIVE] Portal WebSocket disconnected: {session_id}")
        session.portal_ws = None  # Clear portal ws but keep model connection for grace period

    except Exception as e:
        print(f"[GEMINI-LIVE] WebSocket error: {e}")
        await _safe_ws_send(websocket, {
            "type": "error",
            "data": str(e)
        })

    finally:
        # Cleanup
        if gemini_task:
            gemini_task.cancel()
            try:
                await gemini_task
            except asyncio.CancelledError:
                pass

        if keepalive_task:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass

        if session.gemini_ws:
            try:
                await session.gemini_ws.close()
            except:
                pass
            session.gemini_ws = None

        # Save session to BlackBox before clearing (backend handles this now)
        if session.conversation:
            await save_session_to_blackbox(session)

        session.portal_ws = None
        session.status = "disconnected"
        print(f"[GEMINI-LIVE] Session {session_id} cleaned up")

# =============================================================================
# HTTP Endpoints
# =============================================================================

@app.get("/gemini-live/status")
async def gemini_live_status():
    """Get status of Gemini Live API integration.

    Emits the locked status-shape from plan v2 Architecture section:
    - enabled: API key configured?
    - model_default / models[]: dropdown catalog. Gemini has no chat/transcribe
      split (unlike OpenAI Realtime), so no category filter is applied.
    - voice_default / voices[]: flat string array of 30 voice names.
    - voice_descriptors: Gemini-only dict mapping voice name → 1-word character
      descriptor (e.g. "Zephyr" → "Bright"). OpenAI voices have no descriptors,
      so this field is Gemini-status-specific by plan design.
    """
    return {
        "available": WEBSOCKETS_AVAILABLE and bool(GOOGLE_API_KEY),
        "websockets_installed": WEBSOCKETS_AVAILABLE,
        "api_key_configured": bool(GOOGLE_API_KEY),
        "enabled": bool(GOOGLE_API_KEY),
        # Legacy single-value fields — preserved for backcompat with any caller
        # that hasn't migrated to model_default / voice_default yet.
        "model": GEMINI_LIVE_MODEL,
        "default_voice": GEMINI_LIVE_DEFAULT_VOICE,
        "input_sample_rate": GEMINI_LIVE_INPUT_SAMPLE_RATE,
        "output_sample_rate": GEMINI_LIVE_OUTPUT_SAMPLE_RATE,
        # Locked v2 catalog shape:
        "model_default": GEMINI_LIVE_MODEL,
        "models": list(GEMINI_LIVE_MODELS),
        "voice_default": GEMINI_LIVE_DEFAULT_VOICE,
        "voices": list(GEMINI_LIVE_VOICES),
        "voice_descriptors": dict(GEMINI_LIVE_VOICE_DESCRIPTORS),
        "active_sessions": len([s for s in GEMINI_LIVE_SESSIONS.values() if s.status == "connected"]),
    }

@app.get("/gemini-live/sessions")
async def list_gemini_live_sessions():
    """List active Gemini Live sessions."""
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "operator": s.operator,
                "status": s.status,
                "voice": s.voice,
                "created_at": s.created_at,
                "last_activity": s.last_activity
            }
            for s in GEMINI_LIVE_SESSIONS.values()
        ]
    }

@app.get("/gemini-live/voices")
async def list_gemini_live_voices():
    """List available Gemini Live voices."""
    from Orchestrator.config import GEMINI_LIVE_VOICES
    return {
        "voices": GEMINI_LIVE_VOICES,
        "default": GEMINI_LIVE_DEFAULT_VOICE
    }
