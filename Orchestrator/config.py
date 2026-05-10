#!/usr/bin/env python3
"""
config.py - Extracted from Orchestrator/app.py

This module was automatically extracted using byte-offset manifest refactoring.
Original location: Lines 1-286

Extraction date: 2025-12-10T21:00:21.374044+00:00
Original SHA-256: 7e83b6097a446045e21c3630bd725ff1b9d59b8f905a9d074141e36cd906ea7b
"""

# Standard library imports
import asyncio
import base64
import dataclasses
import hashlib
import io
import json
import math
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
import typing
import uuid
import wave

# External library imports
import collections
import configparser
import dotenv
import enum
import fastapi
import fcntl
import google
import httpx
import platform
import psutil
import pty
import pydantic
import requests
import select
import signal
import socket
import struct
import termios

import os
import json
import time
import re
import hashlib
import threading
import configparser
import math
import signal
import sys
import asyncio
import subprocess
import pty
import select
import fcntl
import struct
import termios
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from collections import defaultdict
from dataclasses import dataclass, field, asdict
import uuid
import base64
import io                 # <-- NEW: For in-memory file handling
import wave               # <-- NEW: For writing WAV files
# import audioop          # <-- REMOVED: Not in Python 3.13
import sqlite3
from enum import Enum

# Behavioral layer — personality, tone, anti-sycophancy.
# See behavioral_core.py for the full prompt text and rationale.
# Dual import form: fully-qualified when loaded as Orchestrator.config,
# bare fallback when loaded standalone (some test paths do this).
try:
    from Orchestrator.behavioral_core import BEHAVIORAL_CORE_CHAT
except ImportError:
    from behavioral_core import BEHAVIORAL_CORE_CHAT

from fastapi import FastAPI, HTTPException, UploadFile, File, Body, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware # IMPORTED FOR ANDROID/TAILSCALE FIX
from pydantic import BaseModel
from dotenv import load_dotenv
import requests
import google.generativeai as genai
import httpx  # For async HTTP proxy
import psutil  # For system monitoring metrics
import platform
import socket
# Google Cloud Auth (for service account authentication with Cloud TTS API)
try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleAuthRequest
    GOOGLE_AUTH_AVAILABLE = True
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False
    print("[INIT] google-auth not installed - Cloud TTS GA models unavailable")

# -----------------------------------------------------------------------------
# Setup & config
# -----------------------------------------------------------------------------
load_dotenv()
CFG = configparser.ConfigParser()
if not CFG.read("config.ini"):
    raise SystemExit("config.ini not found. Run uvicorn from the project root.")

GM_PATH   = Path(CFG["paths"]["gm"])
GM_HASH   = Path(CFG["paths"]["gm_hash"])
VOL_PATH  = Path(CFG["paths"]["volume"])
ARC_DIR   = Path(CFG["paths"]["archive"])
MANIFEST  = Path(CFG["paths"]["manifest"])
VOL_PATH = Path("Volumes/SNAPSHOT_VOLUME.txt")  # Main immutable volume
SNAPSHOT_INDEX = Path("Manifest/snapshot_index.json")  # Byte offset index for fast retrieval
OPERATOR_STATE_FILE = Path("Manifest/operator_state.json")  # Persistent operator state
OPERATOR_PREFS_FILE = Path("Manifest/operator_preferences.json")  # Cross-device preferences (voice, etc.)
APPS_REGISTRY_FILE = Path("Manifest/apps_registry.json")  # Persistent apps registry
UPLOADS_DIR = Path("Portal/uploads") # For generated and uploaded media
ARTIFACTS_DIR = Path("Portal/artifacts")  # For generated downloadable artifacts
ARTIFACT_RETENTION_DAYS = 4  # Auto-cleanup after 4 days
ARTIFACT_MAX_SIZE_MB = 50  # Maximum artifact size

AUDIO_ENGINE  = CFG.get("audio","engine",fallback="auto").strip().lower()
TTS_MODEL     = CFG.get("audio","model", fallback="tts-1").strip()
TTS_VOICE     = CFG.get("audio","voice", fallback="alloy").strip()
TTS_FORMAT    = CFG.get("audio","format",fallback="mp3").strip()
TTS_TIMEOUT   = CFG.getint("audio","timeout_ms",fallback=120000)
USERS_LIST     = [u.strip() for u in CFG.get("users","list",fallback="Brandon").split(",") if u.strip()]
USERS_DEFAULT  = CFG.get("users","default",fallback=(USERS_LIST[0] if USERS_LIST else "Operator")).strip()
INCLUDE_OTHERS = CFG.getboolean("context","include_other_operators",fallback=False)

# Generative Model Config
# Nano Banana Pro - Gemini 3 Pro Image Preview for high-quality image generation
GOOGLE_IMAGEN_MODEL = CFG.get("models", "google_imagen", fallback="gemini-3-pro-image-preview")
GOOGLE_VEO_MODEL    = CFG.get("models", "google_veo", fallback="veo-model-placeholder")
GOOGLE_TTS_SYNTHESIZE_URL = CFG.get("models", "google_tts_synthesize", fallback="https://texttospeech.googleapis.com/v1/text:synthesize")
GOOGLE_TTS_VOICES_URL     = CFG.get("models", "google_tts_voices", fallback="https://texttospeech.googleapis.com/v1/voices")


CURRENT_OPERATOR = USERS_DEFAULT   # updated on each /chat

# Auto-mint policy
AUTO_ENABLE      = CFG.getboolean("auto_mint", "enable", fallback=True)
TURNS_THRESHOLD  = CFG.getint("auto_mint", "turns_threshold", fallback=10)
TOKENS_THRESHOLD = CFG.getint("auto_mint", "tokens_threshold", fallback=12000)
ON_YELLOW        = CFG.get("auto_mint", "on_yellow", fallback="auto")   # auto|confirm|ignore
ON_RED           = CFG.get("auto_mint", "on_red", fallback="auto")      # auto always
DEBOUNCE_MS      = CFG.getint("auto_mint", "debounce_ms", fallback=3000)

# Checkpoint policy
CHECKPOINT_TURNS_TO_COMPRESS     = CFG.getint("checkpoint", "turns_to_compress", fallback=50)
CHECKPOINT_AUTO_CREATE_INTERVAL  = CFG.getint("checkpoint", "auto_create_interval", fallback=50)
CHECKPOINT_MIN_SNAPSHOTS         = CFG.getint("checkpoint", "min_snapshots_required", fallback=5)

# Prompt budgeting (for DriftLight display only)
CTX_MAX          = CFG.getint("budget", "context_tokens_max", fallback=128000)
RECENT_TURNS_TOK = CFG.getint("budget", "recent_turns_tokens", fallback=12000)
REPLY_BUF_TOK    = CFG.getint("budget", "reply_buffer_tokens", fallback=6000)

# Static core of the system prompt (format rules, artifacts, rules — no tool descriptions)
OUTPUT_SPEC_CORE = (
    "This response is processed by an automated system and MUST strictly follow the specified JSON format. Any deviation will result in failure.\n\n"
    "RESPONSE FORMAT:\n"
    '{"ui_reply": string, "snapshot_perspective": string}\n\n'
    "KEY DEFINITIONS:\n"
    '- `ui_reply`: The complete, detailed, user-facing answer. If delivering a plan, include ALL steps here. This is what the user sees in the chat UI.\n'
    '- `snapshot_perspective`: Your internal reasoning summary (1-15 lines) explaining how you arrived at the answer. '
    'If a plan exists, include the full plan here as well (do not truncate). '
    'ALWAYS end with a "Keywords:" line containing 5-7 lowercase, kebab-case terms (entities, configs, actions, IDs) plus 1-2 common aliases. '
    'Example: "Keywords: dgx-spark, llama-405b, training-pipeline, lora, fine-tuning, model-weights, snapshot-protocol"\n\n'
    "{TOOL_INSTRUCTIONS}\n\n"
    "BLACKBOX MEMORY SYSTEM (Snapshots):\n"
    "  The context you receive includes recent snapshots from the BlackBox - an immutable conversation ledger\n"
    "  that serves as your external memory. Snapshots contain past conversations, decisions, and context.\n\n"
    "  WHAT SNAPSHOTS ARE:\n"
    "  - Snapshots are saved conversation summaries with semantic embeddings for search\n"
    "  - They contain past work, decisions, preferences, and historical context\n"
    "  - The context package you receive already includes recent snapshots for immediate context\n"
    "  - For deeper historical searches, use the search_snapshots tool\n\n"
    "INLINE IMAGE DISPLAY:\n"
    '  You can reference existing images in your response using markdown image syntax: ![alt text](url)\n'
    '  The system will detect and render these images inline with your text.\n\n'
    "ARTIFACT/FILE GENERATION:\n"
    "You can create downloadable files (text, PDF, CSV, DOCX) by including artifact blocks in your `ui_reply`:\n\n"
    '  Format: [ARTIFACT:filename.ext:type]\\ncontent\\n[/ARTIFACT]\n'
    '  Types: text (for .txt, .md, .json, .py, .js, etc.), pdf, csv, docx\n\n'
    '  TEXT FILE EXAMPLE:\n'
    '  [ARTIFACT:notes.txt:text]\n'
    '  These are my notes on the topic...\n'
    '  [/ARTIFACT]\n\n'
    '  PDF EXAMPLE (supports markdown headers, bullets, bold, italic):\n'
    '  [ARTIFACT:report.pdf:pdf]\n'
    '  # Project Report\n'
    '  ## Summary\n'
    '  This report covers **important findings**.\n'
    '  - Key point one\n'
    '  - Key point two\n'
    '  [/ARTIFACT]\n\n'
    '  CSV EXAMPLE:\n'
    '  [ARTIFACT:data.csv:csv]\n'
    '  Name,Age,City\n'
    '  Alice,30,New York\n'
    '  Bob,25,Los Angeles\n'
    '  [/ARTIFACT]\n\n'
    '  DOCX EXAMPLE (Word document, supports markdown formatting):\n'
    '  [ARTIFACT:document.docx:docx]\n'
    '  # Meeting Notes\n'
    '  ## Attendees\n'
    '  - John Smith\n'
    '  - Jane Doe\n'
    '  ## Action Items\n'
    '  1. Review proposal\n'
    '  2. Schedule follow-up\n'
    '  [/ARTIFACT]\n\n'
    '  Note: Artifacts are automatically processed and replaced with download buttons in the UI.\n\n'
    "RULES:\n"
    "1. Your entire output MUST be a single, raw JSON object. Start with `{` and end with `}`. NO extra text, headers, or code fences.\n"
    "2. BOTH `ui_reply` and `snapshot_perspective` keys are MANDATORY in every response.\n"
    "3. The `snapshot_perspective` value MUST NOT be empty. It must contain reasoning + keywords.\n"
    "4. All string values must be standard JSON strings. Do not nest JSON-encoded strings within values.\n"
    "5. The `snapshot_perspective` MUST end with a 'Keywords:' line. Do not skip this.\n"
    "6. Tools are OPTIONAL - only use when the user explicitly requests an action (generating media, sending messages, etc.).\n"
    "7. When user requests media generation, call the appropriate tool directly. Do NOT just write about generating media.\n"
    "8. When generating ARTIFACT files (text, PDF, CSV, DOCX), do NOT call media generation tools unless the user explicitly asks for both.\n"
    "9. MULTIPLE TOOL CALLS: You CAN call tools multiple times in a single response.\n"
    "10. NEVER generate snapshot format or markers. The orchestrator handles snapshots - you ONLY output JSON.\n"
    "11. Use search_snapshots when users ask about past conversations, historical context, or reference previous work.\n\n"
    "CRITICAL: To use a tool, you MUST call it. Simply describing what you would do is NOT sufficient.\n\n"
    "FINAL CHECK: Is the entire response a single JSON object with no extra text? Do both required keys exist? Does snapshot_perspective end with Keywords? Did you call the appropriate tool if the user requested an action?"
)

# Legacy static OUTPUT_SPEC — hardcoded tool descriptions (used when TOOLVAULT_ENABLED=false)
OUTPUT_SPEC_TOOLS_STATIC = (
    "MULTIMODAL GENERATION (TOOL-BASED):\n"
    "You have access to the following tools for generating media. Use them by calling the tool function directly:\n\n"
    "IMAGE GENERATION TOOL:\n"
    '  Tool: generate_image\n'
    '  Parameters:\n'
    '    - prompt (required): Detailed description of the image to generate\n'
    '    - aspectRatio (optional): "16:9", "9:16", "1:1", "4:3", "3:4" (default: "16:9")\n'
    '    - resolution (optional): "1K", "2K", "4K" (default: "1K")\n'
    '    - numberOfImages (optional): 1-4 (default: 1)\n'
    '  Note: Images appear automatically when ready (typically 10-30 seconds).\n\n'
    "VIDEO GENERATION TOOL:\n"
    '  Tool: generate_video\n'
    '  Parameters:\n'
    '    - prompt (required): Detailed description of the video to generate\n'
    '    - aspectRatio (optional): "16:9" or "9:16" (default: "16:9")\n'
    '    - duration (optional): 5 or 8 seconds (default: 8)\n'
    '    - resolution (optional): "720p" or "1080p" (default: "720p")\n'
    '    - negativePrompt (optional): Things to avoid in the video\n'
    '  Note: Videos take 5-20 minutes to generate.\n\n'
    "MUSIC GENERATION TOOL (Google Lyria):\n"
    '  Tool: generate_music\n'
    '  Parameters:\n'
    '    - prompt (required): Description using ONLY instruments, tempo, and texture\n'
    '    - negativePrompt (optional): Things to exclude\n'
    '    - sampleCount (optional): Number of variations (1-4, default: 1)\n'
    '  CRITICAL: Lyria REJECTS genre/style/artist names. Translate to instrument/tempo/texture.\n\n'
    "SNAPSHOT SEARCH TOOL:\n"
    '  Tool: search_snapshots\n'
    '  Parameters:\n'
    '    - query (required): Natural language or keyword search\n\n'
    "CONTACT BOOK TOOLS:\n"
    "  Tool: search_contacts — search by name, phone, tag\n"
    "  Tool: save_contact — save new contacts (name, notes, tags required)\n"
    "  Before making calls or sending texts, always search_contacts first.\n"
)

# Assemble the default OUTPUT_SPEC with behavioral core prepended so tone and
# anti-sycophancy guidance is established before the JSON format spec.
OUTPUT_SPEC = (
    BEHAVIORAL_CORE_CHAT
    + "\n\n"
    + OUTPUT_SPEC_CORE.replace("{TOOL_INSTRUCTIONS}", OUTPUT_SPEC_TOOLS_STATIC)
)


def build_output_spec(tool_instructions: str = "") -> str:
    """Build the system prompt with dynamic or static tool instructions.

    Behavioral core is prepended in both branches so tone guidance stays
    consistent across ToolVault-on and ToolVault-off configurations.

    When TOOLVAULT_ENABLED and tool_instructions provided:
      Uses the dynamically generated instructions from the vault.
    Otherwise:
      Uses the static hardcoded tool descriptions (legacy behavior).
    """
    if tool_instructions:
        return (
            BEHAVIORAL_CORE_CHAT
            + "\n\n"
            + OUTPUT_SPEC_CORE.replace("{TOOL_INSTRUCTIONS}", tool_instructions)
        )
    return OUTPUT_SPEC# Ensure directories exist
for p in [GM_PATH.parent, VOL_PATH.parent, ARC_DIR, MANIFEST.parent, UPLOADS_DIR, ARTIFACTS_DIR]:
    p.mkdir(parents=True, exist_ok=True)

# Initialize manifest if missing
if not MANIFEST.exists():
    MANIFEST.write_text(json.dumps({
        "latest_path": str(VOL_PATH.as_posix()),
        "latest_sha256": "",
        "latest_utc": "",
        "archive": []
    }, indent=2), encoding="utf-8")

# API Keys
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY","")
GOOGLE_API_KEY    = os.getenv("GOOGLE_API_KEY","")
# Gemini API key — historically read directly via os.getenv in gemini_agent_routes.
# Falls back to GOOGLE_API_KEY (they are interchangeable for the Gemini SDK).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", GOOGLE_API_KEY)
XAI_API_KEY       = os.getenv("XAI_API_KEY", "")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")

# Pairing defaults (used by /pair/claim and /pair/qr/{token} response payload).
# Customers register their own operators in onboarding step T2.7.1; "Brandon" is the system seed.
DEFAULT_OPERATOR = os.getenv("DEFAULT_OPERATOR", "Brandon")
DEFAULT_ORIGIN = os.getenv("DEFAULT_ORIGIN", "http://localhost:9091")

# Configure Gemini for embeddings
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

# API URLs
OPENAI_URL     = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL  = "https://api.anthropic.com/v1/messages"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
XAI_URL        = "https://api.x.ai/v1/chat/completions"
PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
CLOUD_TTS_URL  = "https://texttospeech.googleapis.com/v1beta1/text:synthesize"  # Cloud TTS API (GA models - v1beta1 for Gemini)
LYRIA_MUSIC_URL = "https://us-central1-aiplatform.googleapis.com/v1/projects/{project_id}/locations/us-central1/publishers/google/models/lyria-002:predict"
OPENAI_STT_URL  = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_TTS_URL  = "https://api.openai.com/v1/audio/speech"

# OpenAI Realtime API (GPT-4o voice conversations)
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
OPENAI_REALTIME_MODEL = "gpt-realtime"  # GA model - best quality, 20% cheaper than previews
REALTIME_CONTEXT_MAX_CHARS = 50000    # ~20K tokens budget for initial context
REALTIME_SNAPSHOT_CHARS_EACH = 8000   # Max chars per snapshot in context
REALTIME_AUDIO_SAMPLE_RATE = 24000    # PCM16 audio at 24kHz

# Google Gemini Live API (Gemini 2.5 voice conversations)
GEMINI_LIVE_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
GEMINI_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"  # Native audio model for Live API
GEMINI_LIVE_INPUT_SAMPLE_RATE = 16000   # PCM16 audio at 16kHz (Gemini input standard)
GEMINI_LIVE_OUTPUT_SAMPLE_RATE = 24000  # PCM16 audio at 24kHz (Gemini output)
GEMINI_LIVE_VOICES = ["Charon", "Puck", "Kore", "Aoede", "Fenrir", "Orus"]  # Available voices
GEMINI_LIVE_DEFAULT_VOICE = "Orus"      # Default voice for phone

# xAI Grok Voice Agent API (Grok real-time voice conversations)
GROK_LIVE_URL = "wss://api.x.ai/v1/realtime"
GROK_LIVE_VOICES = ["Ara", "Rex", "Sal", "Eve", "Leo"]  # Available voices
GROK_LIVE_DEFAULT_VOICE = "Rex"         # Default voice for phone
GROK_LIVE_SAMPLE_RATE = 24000           # PCM16 audio at 24kHz (same as OpenAI Realtime)

# =============================================================================
# Phone Integration (3CX + Drachtio + FreeSwitch)
# =============================================================================

# Phone Feature Toggle
PHONE_ENABLED = os.getenv("PHONE_ENABLED", "false").lower() == "true"

# 3CX Cloud PBX Settings
PBX_3CX_URL = os.getenv("PBX_3CX_URL", "")           # e.g., "yourcompany.3cx.us"
PBX_3CX_EXTENSION = os.getenv("PBX_3CX_EXTENSION", "")
PBX_3CX_PASSWORD = os.getenv("PBX_3CX_PASSWORD", "")
PBX_3CX_DID = os.getenv("PBX_3CX_DID", "")           # DID phone number
PBX_OUTBOUND_CALLER_ID = os.getenv("PBX_OUTBOUND_CALLER_ID", "")

# Drachtio SIP Server (for SIP signaling)
DRACHTIO_HOST = os.getenv("DRACHTIO_HOST", "localhost")
DRACHTIO_PORT = int(os.getenv("DRACHTIO_PORT", "9022"))
DRACHTIO_SECRET = os.getenv("DRACHTIO_SECRET", "cymru")

# FreeSwitch Media Server (for audio handling)
FREESWITCH_HOST = os.getenv("FREESWITCH_HOST", "localhost")
FREESWITCH_ESL_PORT = int(os.getenv("FREESWITCH_ESL_PORT", "8021"))
FREESWITCH_ESL_PASSWORD = os.getenv("FREESWITCH_ESL_PASSWORD", "ClueCon")
FREESWITCH_RTP_START = int(os.getenv("FREESWITCH_RTP_START", "16384"))
FREESWITCH_RTP_END = int(os.getenv("FREESWITCH_RTP_END", "32768"))

# Phone Audio Settings
PHONE_SAMPLE_RATE = 8000                 # G.711 standard (8kHz)
PHONE_AUDIO_FORMAT = "ulaw"              # G.711 mu-law
PHONE_FRAME_SIZE_MS = 20                 # 20ms frames (160 samples at 8kHz)

# IVR Settings
IVR_TIMEOUT_MS = int(os.getenv("IVR_TIMEOUT_MS", "5000"))        # 5 second timeout for DTMF
IVR_MAX_RETRIES = int(os.getenv("IVR_MAX_RETRIES", "3"))         # 3 retry attempts
IVR_DEFAULT_BACKEND = os.getenv("IVR_DEFAULT_BACKEND", "openai_realtime")  # Default on timeout
IVR_INTER_DIGIT_TIMEOUT_MS = int(os.getenv("IVR_INTER_DIGIT_TIMEOUT_MS", "3000"))  # Between digits

# PIN Security (gates inbound calls to prevent spam/token burn)
PHONE_PIN_ENABLED = os.getenv("PHONE_PIN_ENABLED", "true").lower() == "true"
PHONE_PIN_CODE = os.getenv("PHONE_PIN_CODE", "6157")              # Default PIN: 6157
PHONE_PIN_MAX_ATTEMPTS = int(os.getenv("PHONE_PIN_MAX_ATTEMPTS", "3"))  # Max wrong attempts

# Phone Session Settings
PHONE_SESSION_TIMEOUT_S = int(os.getenv("PHONE_SESSION_TIMEOUT_S", "3600"))  # 1 hour max call
PHONE_IDLE_TIMEOUT_S = int(os.getenv("PHONE_IDLE_TIMEOUT_S", "300"))          # 5 min idle timeout
PHONE_CLI_SESSION_TIMEOUT_MIN = int(os.getenv("PHONE_CLI_SESSION_TIMEOUT_MIN", "15"))  # 15 min CLI session persistence

# Twilio Integration (alternative to FreeSwitch for phone-AI bridging)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")  # e.g., "+17164512527"
# Public base URL for Twilio webhooks (must be HTTPS accessible from internet)
# For local development, use ngrok: "wss://your-ngrok-url.ngrok.io"
# For production: "wss://your-domain.com"
TWILIO_WEBHOOK_BASE_URL = os.getenv("TWILIO_WEBHOOK_BASE_URL", "wss://localhost:9091")

# Sovereign SIM - Cellular Modem Integration (SIMCom SIM8260G-M2)
CELLULAR_ENABLED = os.getenv("CELLULAR_ENABLED", "false").lower() == "true"
CELLULAR_AT_PORT = os.getenv("CELLULAR_AT_PORT", "/dev/ttyUSB2")
CELLULAR_AUDIO_PORT = os.getenv("CELLULAR_AUDIO_PORT", "/dev/ttyUSB4")
CELLULAR_PHONE_NUMBER = os.getenv("CELLULAR_PHONE_NUMBER", "")
TELEPHONY_PROVIDER = os.getenv("TELEPHONY_PROVIDER", "twilio")  # "twilio" | "cellular" | "asterisk" | "auto"

# Cellular Internet Failover (SIM8260G-M2 as data-only modem via ModemManager/NetworkManager)
CELLULAR_INTERNET_ENABLED = os.getenv("CELLULAR_INTERNET_ENABLED", "false").lower() == "true"
CELLULAR_INTERNET_CONNECTION = os.getenv("CELLULAR_INTERNET_CONNECTION", "5G-Internet")
CELLULAR_INTERNET_AUTO_RECONNECT = os.getenv("CELLULAR_INTERNET_AUTO_RECONNECT", "true").lower() == "true"

# Asterisk PBX Integration (Yeastar TG200 GSM-to-SIP Gateway)
ASTERISK_ENABLED = os.getenv("ASTERISK_ENABLED", "false").lower() == "true"
ASTERISK_ARI_URL = os.getenv("ASTERISK_ARI_URL", "http://127.0.0.1:8088")
ASTERISK_ARI_USER = os.getenv("ASTERISK_ARI_USER", "blackbox")
ASTERISK_ARI_PASSWORD = os.getenv("ASTERISK_ARI_PASSWORD", "")
ASTERISK_AUDIOSOCKET_PORT = int(os.getenv("ASTERISK_AUDIOSOCKET_PORT", "9092"))
TG200_PHONE_NUMBER = os.getenv("TG200_PHONE_NUMBER", "")

# UGV Beast on-device ER agent (new in 2026-04-18 deployment).
UGV_ER_URL: str = os.getenv("UGV_ER_URL", "http://ugv-beast:8082")
UGV_ER_TIMEOUT_S: int = int(os.getenv("UGV_ER_TIMEOUT_S", "10"))

# ToolVault — Dynamic tool injection
TOOLVAULT_ENABLED = os.getenv("TOOLVAULT_ENABLED", "false").lower() == "true"

# Google OAuth 2.0 (Gmail integration)
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")

# Google Cloud Service Account (for GA Gemini TTS)
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
USE_CLOUD_TTS = bool(GOOGLE_APPLICATION_CREDENTIALS and os.path.exists(GOOGLE_APPLICATION_CREDENTIALS))

# Default Models
OPENAI_MODEL_DEFAULT    = os.getenv("OPENAI_MODEL", "gpt-5.1")
ANTHROPIC_MODEL_DEFAULT = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")

# Extended thinking config per Claude model.
# Opus 4.7: adaptive thinking only (budget_tokens returns 400). 1M context is native.
# display="summarized" streams readable thinking text (default "omitted" = empty blocks — would silently break thinking UI).
# effort="xhigh" is Opus 4.7's recommended level for agentic/coding work; Sonnet 4.6 maxes at "high".
# Haiku 4.5 is deliberately omitted — it doesn't support effort or adaptive thinking.
ANTHROPIC_THINKING_MODELS = {"claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6"}
ANTHROPIC_EFFORT_MAP = {
    "claude-opus-4-7": "xhigh",   # Opus 4.7-only tier between "high" and "max"
    "claude-opus-4-6": "high",
    "claude-sonnet-4-6": "high",  # Sonnet caps at "high" — xhigh/max are Opus-tier only
}
# Opus 4.7 removed `temperature`, `top_p`, `top_k` — sending any returns 400.
ANTHROPIC_NO_SAMPLING_MODELS = {"claude-opus-4-7"}
# Opus 4.7 omits thinking text by default — set display="summarized" to get visible thinking.
# Other models stream thinking text as-is without the flag.
ANTHROPIC_THINKING_DISPLAY_MODELS = {"claude-opus-4-7"}
GEMINI_MODEL_DEFAULT    = os.getenv("GOOGLE_GEMINI_MODEL", "gemini-3.1-pro-preview")
XAI_MODEL_DEFAULT       = os.getenv("XAI_MODEL", "grok-4-1-fast-reasoning")
DEFAULT_PROVIDER        = (os.getenv("DEFAULT_PROVIDER") or "google").strip().lower()
STT_MODEL       = os.getenv("STT_MODEL","whisper-1").strip()


# Anchors and ID regex
SNAP_RE = re.compile(r"SNAP-(\d{8})-(\d+)$")
END_RX = re.compile(
    r'^\s*===\s*END SNAPSHOT\s*[—-]\s*(?P<snap>SNAP-\d{8}-\d+)\s*[—-]\s*UTC\s*(?P<utc>.+?)\s*===\s*$',
    re.M
)
START_RX = re.compile(
    r'^\s*===\s*START SNAPSHOT\s*[—-]\s*UTC\s*.+?\s*[—-]\s*(?P<snap>SNAP-\d{8}-\d+)\s*.*?===\s*$',
    re.M
)


