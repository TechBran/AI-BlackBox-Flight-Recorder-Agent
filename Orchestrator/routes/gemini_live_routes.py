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
from typing import Optional, Dict

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

from fastapi import WebSocket

# Local imports
from Orchestrator.checkpoint import app
from Orchestrator.config import (
    GOOGLE_API_KEY,
    gemini_live_url,
    GEMINI_LIVE_MODEL,
    GEMINI_LIVE_MODELS,
    GEMINI_LIVE_TRANSLATE_MODEL,
    GEMINI_LIVE_VOICES,
    GEMINI_LIVE_VOICE_DESCRIPTORS,
    GEMINI_LIVE_VAD_SENSITIVITIES,
    GEMINI_LIVE_THINKING_LEVELS,
    GEMINI_LIVE_THINKING_CAPABLE_MODELS,
    GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS,
    GEMINI_LIVE_INPUT_SAMPLE_RATE,
    GEMINI_LIVE_OUTPUT_SAMPLE_RATE,
    GEMINI_LIVE_DEFAULT_VOICE,
    REALTIME_CONTEXT_MAX_CHARS,
    orchestrator_url,
)
from Orchestrator.models import GeminiLiveSession, GEMINI_LIVE_SESSIONS
from Orchestrator.volume import now_utc_iso
from Orchestrator.context_builder import build_fossil_context
from Orchestrator.secret_redaction import redact
from Orchestrator.utils.async_helpers import run_blocking
from Orchestrator.whisper_filter import is_whisper_hallucination
from Orchestrator.tools.tool_registry import get_gemini_live_tools
from Orchestrator.voice_agents.registry import resolve_preset, merge_connect_params
from Orchestrator.behavioral_core import get_persona, VOICE_DELIVERY_NOTE
from Orchestrator.routes.voice_prompts import build_voice_instructions
from Orchestrator.routes.voice_translate import (
    resolve_translate_params,
    build_translate_instructions,
)
from Orchestrator.routes.voice_ws_shared import (
    adopt_upstream,
    cancel_voice_tools,
    close_portal_ws,
    safe_send,
    save_voice_transcript,
    send_gemini_tool_error,
    spawn_reconnect,
    superseded,
)
from Orchestrator.voice import tools as voice_tools
from Orchestrator.voice.bridge import LiveVoiceBridge, VoiceAdapter
from Orchestrator.voice.transcript import save_session_transcript

LOG_PREFIX = "[GEMINI-LIVE]"

_safe_ws_send = safe_send


async def _close_portal_ws(session, reason: str = "gemini terminal disconnect"):
    """Terminal disconnect: close the client WS (voice_ws_shared.close_portal_ws)."""
    await close_portal_ws(session, reason)


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
    """The search_snapshots tool (Orchestrator/voice/tools.py)."""
    return await voice_tools.execute_search_snapshots(session, arguments, log_prefix=LOG_PREFIX)

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
        # The API key rides in the x-goog-api-key handshake header (as the
        # google-genai SDK does), never the URL: exception text and websockets'
        # own logging carry the URL.
        # v1alpha endpoint is REQUIRED when affective dialog / proactive audio were
        # requested for this session (flags validated + persisted by the WS handler
        # via resolve_affective_flags BEFORE this call); all other sessions stay on
        # v1beta. Reading session state (not request params) keeps the P1a
        # reconnect path on the identical endpoint.
        api_version = "v1alpha" if (session.affective_dialog or session.proactive_audio) else "v1beta"
        url = gemini_live_url(api_version)

        print(f"[GEMINI-LIVE] Connecting to Gemini Live API ({api_version})...")
        # The attach this dial is for: a re-attach during the dial owns the
        # session by the time it returns, and its upstream must stay in place.
        generation = getattr(session, "generation", 0)
        # Add explicit ping settings to prevent connection drops
        ws = await websockets.connect(
            url,
            additional_headers={"x-goog-api-key": GOOGLE_API_KEY},
            open_timeout=10,       # 10s max to establish connection (prevents indefinite hang)
            ping_interval=20,      # Send ping every 20 seconds
            ping_timeout=30,       # Wait 30 seconds for pong response
            close_timeout=10,      # Wait 10 seconds for close handshake
        )
        if not await adopt_upstream(session, "gemini_ws", ws, generation, "[GEMINI-LIVE]"):
            return False
        session.status = "connected"
        session.last_activity = now_utc_iso()
        print(f"[GEMINI-LIVE] Connected to Gemini for session {session.session_id}")
        return True

    except Exception as e:
        print(f"[GEMINI-LIVE] Connection failed: {type(e).__name__}: {redact(str(e))}")
        session.status = "error"
        return False


def _parse_ws_bool(value) -> bool:
    """Allowlist-parse a boolean that arrives as JSON bool or URL-query string.

    Accepts bool, "true"/"1"/"yes", "false"/"0"/"no"/""/None. Anything else
    (attacker-shaped strings, dicts) logs a warning and resolves False —
    mirrors the warn-and-fallback convention of the other Gemini allowlists.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no", ""):
            return False
    print(f"[GEMINI-LIVE] WARNING: unrecognized boolean flag {value!r}; treating as False")
    return False


def resolve_affective_flags(model, affective_raw, proactive_raw):
    """Validate affective-dialog / proactive-audio request flags against the model.

    Returns (affective: bool, proactive: bool, error: Optional[str]).
    `error` is a client-facing message when either flag is requested on a model
    outside GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS (3.1 rejects these v1alpha-only
    setup fields); both flags resolve False in that case. Model resolution
    mirrors configure_gemini_session: unknown/None -> GEMINI_LIVE_MODEL default.
    """
    affective = _parse_ws_bool(affective_raw)
    proactive = _parse_ws_bool(proactive_raw)
    if not (affective or proactive):
        return False, False, None
    _allowed_model_ids = {m["id"] for m in GEMINI_LIVE_MODELS}
    resolved_model = model if model in _allowed_model_ids else GEMINI_LIVE_MODEL
    if resolved_model not in GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS:
        return False, False, (
            f"Affective dialog / proactive audio are not supported by {resolved_model}. "
            f"They require a Gemini 2.5 native-audio model "
            f"({', '.join(sorted(GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS))} — deprecated line, "
            "v1alpha endpoint only). Turn the toggles off or switch model."
        )
    return affective, proactive, None


async def configure_gemini_session(
    session: GeminiLiveSession,
    operator: str,
    voice: str,
    custom_role: Optional[str] = None,
    phone_mode: Optional[bool] = None,
    model: Optional[str] = None,
    vad_sensitivity_start: Optional[str] = None,
    vad_sensitivity_end: Optional[str] = None,
    thinking_level: Optional[str] = None,
    tool_group_override: Optional[str] = None,
    mode: Optional[str] = None,
    target_language: Optional[str] = None,
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
        mode: Optional session mode — "translate" builds a minimal tool-free
            translation session on GEMINI_LIVE_TRANSLATE_MODEL (P6a).
        target_language: BCP-47 target for translate mode; malformed/missing
            values fall back to "en" with a logged warning.

        All new kwargs default to None to preserve phone bridge call sites at
        phone/bridge.py:1286, 1370 (audit C2 — those sites only pass positional
        + phone_mode=True; Optional=None defaults keep them unchanged).
    """
    if not session.gemini_ws:
        return

    # P1.4 — fall back to session-persisted values. None means "not specified
    # by this caller": gemini_reconnect passes only (session, operator, voice),
    # so before this fallback every reconnect silently reverted the session to
    # the default model with default VAD/thinking and dropped custom_role/
    # phone_mode (recon finding #5). Explicit values (including "" / False)
    # still win over the persisted ones — phone bridge call sites unchanged.
    if model is None:
        model = session.model
    if vad_sensitivity_start is None:
        vad_sensitivity_start = session.vad_sensitivity_start
    if vad_sensitivity_end is None:
        vad_sensitivity_end = session.vad_sensitivity_end
    if thinking_level is None:
        thinking_level = session.thinking_level
    if custom_role is None:
        custom_role = session.custom_role
    if phone_mode is None:
        phone_mode = session.phone_mode

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

    # ── Translation mode (P6a): minimal setup — NO persona/context/tools ──
    # Dedicated model, tool-free, tiny prompt (design doc workstream 5).
    # Gated on the P0 probe (diagnostics/voice_probes/results/*-translate.json,
    # combined P0.5 file — gemini entries).
    # Ignores any client model pick — the translate model always wins.
    is_translate, resolved_target_language = resolve_translate_params(
        mode, target_language, log_prefix="[GEMINI-LIVE]")
    if is_translate:
        session.provenance = {}  # no snapshot retrieval in translate mode
        setup_message = {
            "setup": {
                "model": f"models/{GEMINI_LIVE_TRANSLATE_MODEL}",
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {"voiceName": voice}
                        }
                    },
                },
                "systemInstruction": {
                    "parts": [{"text": build_translate_instructions(resolved_target_language)}]
                },
                # NO tools / thinkingConfig / contextWindowCompression —
                # translation sessions are short-lived tool-free pipes.
            }
        }
        await session.gemini_ws.send(json.dumps(setup_message))
        session.context_injected = True
        session.voice = voice
        print(f"[GEMINI-LIVE] TRANSLATE session configured "
              f"(target={resolved_target_language}, "
              f"model={GEMINI_LIVE_TRANSLATE_MODEL}, voice={voice})")
        return

    # Build system instructions with operator-specific context.
    # `operator` is request-scoped — comes from the WS connect handshake
    # message (`data.get("operator", "")`) at the bottom of this file.
    # At session open we have no user_text yet, so keyword/semantic will
    # be empty; recent + checkpoint still populate.
    context, provenance = await run_blocking(build_context_for_operator, operator, user_text="")  # retrieval: off the loop
    # Stash provenance on the session so the cleanup/save path and the
    # client-facing WS emission below can reference it.
    session.provenance = provenance
    # One builder for the three voice providers (voice_prompts); a custom_role
    # (outbound call) replaces the default persona prompt.
    system_instructions = build_voice_instructions(
        "gemini", operator, context, custom_role,
        persona=lambda: get_persona(operator, "voice") + "\n\n" + VOICE_DELIVERY_NOTE)

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
        # P1.3 — read the tool group FRESH per session-configure so
        # /toolvault/reload (and schema fixes) reach live voice without a
        # restart. The registry's mtime cache makes this cheap. grok/openai
        # routes get the same un-freeze in the hardening phase.
        # P4: a voice-agent preset can swap the tool group at configure time.
        "tools": get_gemini_live_tools(tool_group_override or "gemini_live"),
        "contextWindowCompression": {
            "slidingWindow": {}
        },
        # P1.8 — native transcription for both directions: Google transcribes
        # in-session, so the post-hoc Whisper /stt/json hop is fallback-only
        # (removes the STT quota dependency from the voice path).
        "inputAudioTranscription": {},
        "outputAudioTranscription": {}
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

    # Affective dialog + proactive audio (v1alpha, 2.5-native-audio family ONLY).
    # Flags were validated + persisted onto the session by the WS connect handler
    # (resolve_affective_flags) BEFORE connect_to_gemini chose the v1alpha URL; a
    # reconnect reconfigure re-reads them here so the rebuilt session is identical.
    # The capability re-check is defense in depth — 3.1 rejects these setup fields
    # and would close the WS, so never emit them for non-capable models.
    if session.affective_dialog or session.proactive_audio:
        if resolved_model in GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS:
            if session.affective_dialog:
                setup_config["enableAffectiveDialog"] = True
            if session.proactive_audio:
                setup_config["proactivity"] = {"proactiveAudio": True}
            print(f"[GEMINI-LIVE] affective_dialog={session.affective_dialog} "
                  f"proactive_audio={session.proactive_audio} enabled for {resolved_model} (v1alpha)")
        else:
            print(f"[GEMINI-LIVE] affective/proactive flags ignored — model "
                  f"{resolved_model!r} not in GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS")

    setup_message = {"setup": setup_config}

    await session.gemini_ws.send(json.dumps(setup_message))
    session.context_injected = True
    session.voice = voice
    # P1.4 — persist the validated config so the reconnect path reconfigures
    # with exactly what this session was opened with.
    session.model = resolved_model
    session.vad_sensitivity_start = vad_sensitivity_start
    session.vad_sensitivity_end = vad_sensitivity_end
    session.thinking_level = thinking_level
    session.custom_role = custom_role
    session.phone_mode = phone_mode
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
                orchestrator_url("/stt/json"),
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


def _flush_open_turn(session: GeminiLiveSession) -> None:
    """Move a turn still in the transcript buffers into the conversation.

    The live path flushes on turnComplete, which Gemini sends only once the
    reply has finished PLAYING. A session that ends after generationComplete
    and before turnComplete (an End tap or a hang-up during the goodbye), or
    whose upstream is replaced mid-reply (re-attach, reconnect: that socket
    will never send the turnComplete), would otherwise lose the last exchange
    from the saved transcript; release_payload() discards the buffers. User
    before assistant, as on turnComplete.
    """
    user_text = (session.input_transcript_buffer or "").strip()
    if user_text:
        session.conversation.append({
            "role": "user",
            "content": user_text,
            "timestamp": now_utc_iso(),
            "source": "voice"
        })
    session.input_transcript_buffer = ""
    reply = (session.transcript_buffer or "").strip()
    if reply:
        session.conversation.append({
            "role": "assistant",
            "content": reply,
            "timestamp": now_utc_iso()
        })
    session.transcript_buffer = ""


async def save_session_to_blackbox(session: GeminiLiveSession):
    """
    Save the Gemini Live session conversation to BlackBox.
    Called on disconnect/cleanup to ensure all messages are captured,
    including a turn whose turnComplete never arrived (_flush_open_turn).
    """
    _flush_open_turn(session)
    await save_session_transcript(
        session,
        title="Gemini Live Voice Session",
        fields=[("Voice", session.voice), ("Timestamp", now_utc_iso())],
        ai_label="AI",
        user_message=f"[Voice Session Transcript] Gemini Live voice session {session.session_id}",
        model_label="gemini-live-voice",
        log_prefix=LOG_PREFIX,
        persist=save_voice_transcript,
    )


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
        if session.native_transcription_active:
            # P1.8 — native transcription owns the user transcript: skip the
            # Whisper hop entirely and drop the buffered audio.
            session.user_audio_buffer = []
        else:
            # FALLBACK ONLY: post-hoc Whisper via /stt/json.
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
        # A listener just READ from the current Gemini socket — the connection
        # is genuinely healthy. Reset the consecutive-failure counter HERE, not
        # in gemini_reconnect: "setup sent" is not health — a rejected setup
        # (e.g. WS close 1007) closes the socket right after, and resetting on
        # send made max_reconnects unreachable (infinite mute-reconnect cycle).
        session.reconnect_count = 0
        if session.portal_ws:
            await _safe_ws_send(session.portal_ws, {
                "type": "setup_complete",
                "data": event.get("setupComplete", {})
            })
        return

    # Check for serverContent (audio/text response)
    if "serverContent" in event:
        server_content = event["serverContent"]

        # Native transcription (P1.8): BidiGenerateContentTranscription objects
        # with a .text field — the REAL field names (the old code sniffed
        # inputTranscript/userTranscript/etc., none of which exist, and would
        # have crashed slicing a dict — recon finding #14).
        input_tx = server_content.get("inputTranscription")
        if isinstance(input_tx, dict) and input_tx.get("text"):
            session.native_transcription_active = True
            session.input_transcript_buffer += input_tx["text"]
            session.user_audio_buffer = []  # native owns the transcript — drop the Whisper buffer
            await _safe_ws_send(session.portal_ws, {
                "type": "user_transcript_delta",
                "data": input_tx["text"]
            })

        output_tx = server_content.get("outputTranscription")
        if isinstance(output_tx, dict) and output_tx.get("text"):
            session.native_transcription_active = True
            # Feed the same buffer part.text used to fill — native-audio models
            # are NOT guaranteed to emit part.text (recon: empty assistant
            # lines in saved transcripts).
            session.transcript_buffer += output_tx["text"]
            await _safe_ws_send(session.portal_ws, {
                "type": "transcript_delta",
                "data": output_tx["text"]
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
                    if not session.is_speaking and session.user_audio_buffer and not session.native_transcription_active:
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

        # generationComplete and turnComplete are SEPARATE frames: the model
        # finishes generating first, and turnComplete follows once playback of
        # the reply is done (or after `interrupted`). Handling both as "turn
        # over" flushed the transcript and sent response_complete on the early
        # frame, so the outputTranscription that arrives in between became a
        # second assistant entry and the client got a second response_complete
        # (audit O09-live-voice-providers-07). generationComplete only means
        # the model stopped producing audio.
        if generation_complete:
            session.is_speaking = False

        # One assistant entry and one response_complete per turn.
        if turn_complete:
            session.is_speaking = False

            # P1.8 — flush the native user transcript FIRST so the ledger
            # conversation stays user → assistant ordered.
            if session.input_transcript_buffer.strip():
                user_text = session.input_transcript_buffer.strip()
                session.conversation.append({
                    "role": "user",
                    "content": user_text,
                    "timestamp": now_utc_iso(),
                    "source": "voice"
                })
                await _safe_ws_send(session.portal_ws, {
                    "type": "user_transcript",
                    "data": user_text
                })
                session.input_transcript_buffer = ""

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
        # Each call runs OFF this listener so it keeps draining frames while the
        # tool works: inline, a long tool held the socket unread (audit
        # O09-live-voice-providers-05). The answer goes back on the socket the
        # call came from.
        await voice_tools.dispatch_gemini_calls(
            session, event, log_prefix=LOG_PREFIX,
            execute=lambda name, args: _execute_gemini_tool(session, name, args))
        return

    # Check for toolCallCancellation
    if "toolCallCancellation" in event:
        cancellation = event["toolCallCancellation"]
        cancelled_ids = cancellation.get("ids", [])
        print(f"[GEMINI-LIVE] Tool calls cancelled: {cancelled_ids}")
        # The model no longer wants these answers (typically the user spoke
        # over it): stop the tools instead of running them to completion.
        # Requested, not awaited: the listener goes on reading while a tool
        # unwinds, and a cancellation of the listener itself stays its own.
        await cancel_voice_tools(session, set(cancelled_ids), wait=False)
        return

    # Check for sessionResumptionUpdate (store handle for reconnection)
    if "sessionResumptionUpdate" in event:
        new_handle = event["sessionResumptionUpdate"].get("newHandle", "")
        if new_handle:
            session.resumption_handle = new_handle
            print(f"[GEMINI-LIVE] Session resumption handle updated")
        return

    # Check for goAway (server will drop this connection at timeLeft — schedule
    # a graceful reconnect BEFORE the deadline instead of waiting for the cut)
    if "goAway" in event:
        goaway = event.get("goAway") or {}
        delay = _goaway_delay_seconds(goaway)
        print(f"[GEMINI-LIVE] Server sending goAway (timeLeft={goaway.get('timeLeft')!r}) - graceful reconnect in {delay:.1f}s")
        generation = session.generation
        if session.external_reconnect:
            # The owner's own loop re-dials when the listener ends (phone
            # bridge): close this socket before Google's deadline instead of
            # dialing a second upstream for the same session.
            upstream = session.gemini_ws

            async def _goaway_close():
                if delay > 0:
                    await asyncio.sleep(delay)
                if upstream is not None and session.gemini_ws is upstream:
                    try:
                        await upstream.close()
                    except Exception:
                        pass
            spawn_reconnect(session, _goaway_close())
        elif not session.intentional_disconnect and not session.is_reconnecting:
            async def _goaway_reconnect():
                if delay > 0:
                    await asyncio.sleep(delay)
                if (not session.intentional_disconnect and not session.is_reconnecting
                        and not superseded(session, generation)):
                    await gemini_reconnect(session)
            spawn_reconnect(session, _goaway_reconnect())
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


async def _execute_gemini_tool(session: GeminiLiveSession, name: str, args: Dict) -> str:
    """Run ONE tool call of the session and return its result text (the shared
    voice tool ladder, Orchestrator/voice/tools.py)."""
    return await voice_tools.execute_voice_tool(session, name, args, log_prefix=LOG_PREFIX,
                                                created_by="gemini-live")

# =============================================================================
# Reconnection and Keepalive
# =============================================================================

GOAWAY_RECONNECT_MARGIN_SEC = 2.0  # reconnect this many seconds BEFORE Google's stated deadline


def _goaway_delay_seconds(goaway: dict) -> float:
    """Seconds to wait before the graceful pre-deadline reconnect.

    ``goAway.timeLeft`` is a protobuf Duration, JSON-encoded as a string like
    "10s" / "9.5s" (a {"seconds": N, "nanos": M} dict is tolerated for safety).
    Returns max(0, timeLeft - margin); 0 (reconnect immediately — the pre-P1.6
    behavior) when the field is missing or unparseable. Never raises.
    """
    time_left = (goaway or {}).get("timeLeft")
    seconds = 0.0
    try:
        if isinstance(time_left, str) and time_left.endswith("s"):
            seconds = float(time_left[:-1])
        elif isinstance(time_left, dict):
            seconds = float(time_left.get("seconds", 0)) + float(time_left.get("nanos", 0)) / 1e9
    except (TypeError, ValueError):
        seconds = 0.0
    return max(0.0, seconds - GOAWAY_RECONNECT_MARGIN_SEC)


# 20 ms of PCM16 silence at the 16 kHz input rate (320 samples = 640 bytes).
_SILENCE_20MS_B64 = base64.b64encode(b'\x00' * 640).decode('ascii')


class GeminiLiveAdapter(VoiceAdapter):
    """Gemini Live's half of the shared LiveVoiceBridge: BidiGenerateContent
    setup, realtime_input / clientContent frames, serverContent / toolCall
    events, session resumption on reconnect."""

    name = "Gemini"
    log_prefix = LOG_PREFIX
    ws_attr = "gemini_ws"
    key_name = "GOOGLE_API_KEY"
    connecting_status = "Connecting to Gemini Live..."
    connect_failed = "Failed to connect to Gemini Live API"
    forward_close_frames = True
    quiet_reconnecting_close = True
    terminal_close = True

    def sessions(self):
        return GEMINI_LIVE_SESSIONS

    def new_session(self, session_id):
        return GeminiLiveSession(session_id=session_id, created_at=now_utc_iso())

    def websockets_available(self):
        return WEBSOCKETS_AVAILABLE

    def api_key(self):
        return GOOGLE_API_KEY

    def url_params(self, websocket):
        # Field naming: vad_sensitivity_start / vad_sensitivity_end across all
        # sides (JSON message, URL query, Python kwarg, Android client).
        q = websocket.query_params
        return {key: q.get(key) for key in (
            "operator", "voice", "model", "vad_sensitivity_start", "vad_sensitivity_end",
            "thinking_level", "affective", "proactive", "mode", "target_language", "agent")}

    async def parse_connect(self, session, websocket, data, url):
        # Voice-agent preset (P4) — precedence: explicit > preset > defaults.
        agent_id = data.get("agent", url["agent"])
        preset = resolve_preset(agent_id, provider="gemini-live") if agent_id else None
        if agent_id and preset is None:
            await safe_send(websocket, {
                "type": "warning",
                "data": f"Voice agent preset {agent_id!r} not found for provider 'gemini-live' — continuing without preset"
            })
        merged = merge_connect_params({
            "model": data.get("model", url["model"]),
            "voice": data.get("voice", url["voice"]),
            "greeting": data.get("greeting", ""),
            "instructions": data.get("role", ""),
        }, preset)
        operator = data.get("operator", url["operator"] or "")
        model = merged["model"]
        mode = data.get("mode", url["mode"])
        target_language = data.get("target_language", url["target_language"])
        is_translate, _ = resolve_translate_params(mode, target_language, log_prefix=LOG_PREFIX)
        # Persisted for the reconnect path (P6a).
        session.mode = mode if is_translate else ""
        session.target_language = target_language or "" if is_translate else ""
        session.operator = operator

        # P6 — affective dialog + proactive audio. Requested on a model that
        # rejects the fields (e.g. 3.1): a clear client error, no connect.
        affective, proactive, affective_error = resolve_affective_flags(
            model, data.get("affective", url["affective"]), data.get("proactive", url["proactive"]))
        if affective_error:
            await safe_send(websocket, {"type": "error", "data": affective_error})
            return None
        # Persisted BEFORE connect_to_gemini: the flags select the v1alpha
        # endpoint there, and the reconnect path re-reads them.
        session.affective_dialog = affective
        session.proactive_audio = proactive

        return {
            "operator": operator,
            "voice": merged["voice"] or GEMINI_LIVE_DEFAULT_VOICE,
            "greeting": merged["greeting"] or "",
            "role": merged["instructions"] or "",
            "model": model,
            "tool_group_override": merged["tool_group_override"],
            # Validated against the allowlists by configure_gemini_session.
            "vad_sensitivity_start": data.get("vad_sensitivity_start", url["vad_sensitivity_start"]),
            "vad_sensitivity_end": data.get("vad_sensitivity_end", url["vad_sensitivity_end"]),
            "thinking_level": data.get("thinking_level", url["thinking_level"]),
            "mode": mode,
            "target_language": target_language,
        }

    async def dial(self, session, params):
        return await connect_to_gemini(session)

    async def configure(self, session, params):
        # A role (outbound call) becomes the custom system context.
        await configure_gemini_session(
            session,
            params["operator"],
            params["voice"],
            custom_role=params["role"],
            model=params["model"],
            vad_sensitivity_start=params["vad_sensitivity_start"],
            vad_sensitivity_end=params["vad_sensitivity_end"],
            thinking_level=params["thinking_level"],
            mode=params["mode"],
            target_language=params["target_language"],
            tool_group_override=params["tool_group_override"],
        )

    async def inject_greeting(self, session, greeting):
        prompt = f"The user just answered the phone. Greet them and deliver this message: {greeting}"
        await session.gemini_ws.send(json.dumps({
            "clientContent": {
                "turns": [{"role": "user", "parts": [{"text": prompt}]}],
                "turnComplete": True
            }
        }))

    def connected_data(self, session, session_id, params):
        return {
            "session_id": session_id,
            "operator": params["operator"],
            "model": GEMINI_LIVE_TRANSLATE_MODEL if session.mode == "translate" else (params["model"] or GEMINI_LIVE_MODEL),
            "voice": params["voice"],
            "affective_dialog": session.affective_dialog,
            "proactive_audio": session.proactive_audio
        }

    async def handle_client(self, session, data):
        await handle_portal_message(session, data)

    async def handle_event(self, session, event):
        await handle_gemini_message(session, event)

    async def answer_failed_event(self, session, event, exc):
        # Answer every functionCall id this event carried that the dispatch
        # loop did not already answer.
        await send_gemini_tool_error(session.gemini_ws, session.portal_ws, event, exc,
                                     getattr(session, "answered_tool_call_ids", None))

    def keepalive_frame(self, session):
        return {
            "realtime_input": {
                "audio": {
                    "mimeType": f"audio/pcm;rate={GEMINI_LIVE_INPUT_SAMPLE_RATE}",
                    "data": _SILENCE_20MS_B64
                }
            }
        }

    def flush(self, session):
        _flush_open_turn(session)

    async def redial(self, session):
        # The session-persisted affective flags pick the same endpoint version.
        return await connect_to_gemini(session)

    async def reconfigure(self, session):
        # configure_gemini_session falls back to the session-persisted
        # model/VAD/thinking/custom_role/phone_mode (P1.4), and the stored
        # resumption handle resumes the conversation.
        await configure_gemini_session(
            session,
            session.operator,
            session.voice,
            mode=session.mode or None,
            target_language=session.target_language or None,
        )
        return True

    async def save(self, session):
        await save_session_to_blackbox(session)

    def reconnect(self, session):
        return gemini_reconnect(session)

    def listener(self, session):
        return gemini_listener(session)

    def keepalive(self, session):
        return gemini_keepalive_loop(session)


GEMINI_LIVE_BRIDGE = LiveVoiceBridge(GeminiLiveAdapter())


async def gemini_reconnect(session: GeminiLiveSession):
    """Reconnect to Gemini Live transparently (resumption handle, backoff)."""
    await GEMINI_LIVE_BRIDGE.reconnect(session)


async def gemini_keepalive_loop(session: GeminiLiveSession):
    """Keepalive silence so Gemini's idle timeout never ends a quiet session."""
    await GEMINI_LIVE_BRIDGE.keepalive_loop(session)


async def gemini_listener(session: GeminiLiveSession):
    """Read Gemini's frames and forward them to the client (also the phone
    bridge's listener, through its PhoneWebSocketAdapter)."""
    await GEMINI_LIVE_BRIDGE.listener(session)


# =============================================================================
# WebSocket Endpoint
# =============================================================================

@app.websocket("/ws/gemini-live/{session_id}")
async def gemini_live_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for Gemini Live API bridge.

    Bridges Portal <-> Orchestrator <-> Gemini Live API through the shared
    LiveVoiceBridge (Orchestrator/voice/bridge.py):
    1. Portal connects, sends 'connect' with operator and voice
    2. Orchestrator connects to Gemini Live API
    3. Orchestrator configures session with tools and context
    4. Bidirectional message passing begins
    5. Tool calls are executed locally and results sent to Gemini
    """
    await GEMINI_LIVE_BRIDGE.serve(websocket, session_id)


async def _end_portal_session(session: GeminiLiveSession, websocket, generation: int,
                              keepalive_task) -> None:
    """Teardown when this handler's client WS ends (voice_ws_shared.end_portal_session).

    Only the CURRENT owner tears the session down, and it stops touching the
    session the moment a re-attach claims it, even mid-teardown: before the
    generation stamp, an old handler whose finally ran after the re-attach
    closed the new upstream, and until the re-check after each await, one
    whose transcript save outlasted the re-attach left it deaf and reapable
    (O09-live-voice-providers-01).
    """
    await GEMINI_LIVE_BRIDGE.end_portal_session(session, websocket, generation, keepalive_task)

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
