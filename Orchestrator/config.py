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
    from Orchestrator.behavioral_core import DEFAULT_PERSONA_CHAT
except ImportError:
    from behavioral_core import DEFAULT_PERSONA_CHAT

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


def current_default() -> str:
    """The live default operator. Reflects admin add/remove_operator updates to
    USERS_DEFAULT without a restart (top-level `from config import USERS_DEFAULT`
    captures a stale value; call this instead for request-time default resolution)."""
    return USERS_DEFAULT


INCLUDE_OTHERS = CFG.getboolean("context","include_other_operators",fallback=False)

# Generative Model Config
# Nano Banana Pro - Gemini 3 Pro Image Preview for high-quality image generation
GOOGLE_IMAGEN_MODEL = CFG.get("models", "google_imagen", fallback="gemini-3-pro-image-preview")
GOOGLE_VEO_MODEL    = CFG.get("models", "google_veo", fallback="veo-model-placeholder")
GOOGLE_TTS_SYNTHESIZE_URL = CFG.get("models", "google_tts_synthesize", fallback="https://texttospeech.googleapis.com/v1/text:synthesize")
GOOGLE_TTS_VOICES_URL     = CFG.get("models", "google_tts_voices", fallback="https://texttospeech.googleapis.com/v1/voices")

# ── Computer Use (CU) — production pass 2026-06-10 ──────────────────────────
# Single source of truth for CU defaults. Replaces the literals that used to
# live in browser/config.py, gemini_cu/config.py, and two chat_routes sites.
CU_MODEL_DEFAULT        = CFG.get("computer_use", "model_default", fallback="claude-opus-4-6").strip()
CU_GEMINI_MODEL_DEFAULT = CFG.get("computer_use", "gemini_model_default",
                                  fallback="gemini-2.5-computer-use-preview-10-2025").strip()
# Frontier-driven device control (M2 — the cloud "brain" that drives a phone's M1
# hands over Tailscale). Provider + model are config-knobbed, never hardcoded, so the
# real Gemini mobile model (or M7's Claude/OpenAI) drops in without a code change.
# NOTE (M2 substitution): the plan targets Gemini 3.5 Flash environment='mobile', but
# that environment is absent from the installed google-genai SDK and the only CU model
# reachable with the box's key is gemini-2.5-computer-use-preview-10-2025 — so the model
# default is CU_GEMINI_MODEL_DEFAULT (the available Gemini CU model). Override in
# config.ini [computer_use] frontier_model when the mobile model becomes reachable.
CU_FRONTIER_PROVIDER    = CFG.get("computer_use", "frontier_provider", fallback="gemini").strip()
CU_FRONTIER_MODEL       = CFG.get("computer_use", "frontier_model",
                                  fallback=CU_GEMINI_MODEL_DEFAULT).strip()
# M7 provider-agnostic device control: per-provider default frontier model, so
# control_device can drive a phone with Claude / OpenAI (DIY-on-Android via the a11y
# bridge) or Gemini. config.py holds the CHOICE; provider capability facts stay in
# CU_MODEL_FILTERS. Anthropic default reuses the CU model default (a Claude computer-use
# model); OpenAI default is the gpt-5.x `computer` tool model.
CU_FRONTIER_ANTHROPIC_MODEL = CFG.get("computer_use", "frontier_anthropic_model",
                                      fallback=CU_MODEL_DEFAULT).strip()
CU_FRONTIER_OPENAI_MODEL    = CFG.get("computer_use", "frontier_openai_model",
                                      fallback="gpt-5.5").strip()
CU_NATIVE_MODE          = CFG.getboolean("computer_use", "native_mode", fallback=True)
CU_CHROME_PATH          = CFG.get("computer_use", "chrome_path", fallback="/opt/google/chrome/chrome").strip()
CU_MAX_ITERATIONS       = CFG.getint("computer_use", "max_iterations", fallback=150)
# Session budget for one CU run. MUST cover the drivers' 30-min wall-clock cap
# (MAX_WALL_CLOCK=1800 fires first with a clean error event; the outer
# tasks.py wait_for(SESSION_TIMEOUT+30) is only the backstop). The old 300s
# default strangled healthy runs mid-task ("Browser session timed out" at
# step 39/150 after ~5 min) while the step/wall-clock budgets said keep going.
CU_SESSION_TIMEOUT      = CFG.getint("computer_use", "session_timeout_s", fallback=1800)

# CU-CAPABILITY gates: which model ids from each vendor's live catalog can
# drive the computer tool. These answer "can this model CLASS do CU at all?" —
# never "which version." Regex anchored at start (re.match). Data, not code —
# when a vendor ships a new CU-capable family, extend the pattern here.
#
# The three vendors deliberately use DIFFERENT shapes because their capability
# facts differ — a gate must encode what is KNOWN, and fail loud (not silent)
# on what is not:
#   anthropic: opus/sonnet/fable/mythos at major version >= 4
#              (computer_20251124 tool; haiku excluded — no CU support). The
#              version tail is fully open (`[4-9]|\d{2,}`): Opus 4.8 and a
#              future Opus 5 match with zero edits. Gates on CLASS, not version.
#   google:    any Gemini id containing "computer-use" — the capability lives
#              in the family name, not the version (-pro / flash carry no
#              computer tool, so a plain gemini-*-pro is correctly excluded).
#   openai:    CU capability lives in the built-in `computer` tool. Two facts,
#              two clauses:
#                (1) KNOWN minor floor within major 5: gpt-5.5 introduced the
#                    tool and every later 5.x minor carries it, but 5.1–5.4
#                    demonstrably lack it -> `gpt-5\.([5-9]|\d{2,})`.
#                (2) UNKNOWN future majors (gpt-6, gpt-7, ...): whether they
#                    carry the tool is unknowable today. We assume YES
#                    (`gpt-([6-9]|\d{2,})(\.\d+)?`, minor optional). Guessing
#                    "excluded" would reproduce the exact silent-gap bug this
#                    gate exists to fix (a capability that needs a regex edit
#                    months later); guessing "included" means that IF a future
#                    major lacks the tool the API call fails LOUDLY at runtime
#                    and surfaces our structured retryable error, which the
#                    calling model can act on. A loud runtime failure beats a
#                    silent gate. (The 5.x floor is a KNOWN fact, not a guess,
#                    so it stays pinned.)
#              Shared tail `($|-(?!pro($|-)))`: dated/named snapshots pass
#              (gpt-5.6-sol, gpt-5.5-2026-04-23, gpt-6-2027-01-01) while the
#              exact `-pro` segment is excluded (undocumented for computer use)
#              — boundary-anchored, so gpt-5.5-professional still MATCHES.
#              computer-use-preview kept for orgs still on the deprecated,
#              access-gated preview model.
CU_MODEL_FILTERS = {
    "anthropic": r"claude-(opus|sonnet|fable|mythos)-([4-9]|\d{2,})",
    "google":    r"gemini-.*computer-use",
    "openai":    r"(computer-use-preview|gpt-(5\.([5-9]|\d{2,})|([6-9]|\d{2,})(\.\d+)?)($|-(?!pro($|-))))",
}

# ── Embeddings — pluggable snapshot-embedding layer (2026-06-11) ─────────────
# Runtime knobs only. Model data (slugs, provider model ids, dims, costs)
# lives ONLY in Orchestrator/embeddings/registry.py — never hardcode an
# embedding-model literal anywhere else.
EMBEDDINGS_ACTIVE_DEFAULT = CFG.get("embeddings", "active", fallback="gemini-embedding-001").strip()
EMBEDDINGS_STORES_DIR     = str(Path("Manifest") / "embeddings")  # per-model binary vector stores
OLLAMA_BASE_URL           = CFG.get("embeddings", "ollama_url", fallback="http://localhost:11434").strip()
# Auto-migration recent-end gap guard (watcher broken-path target selection):
# a fallback store that is frozen at an old date - missing the newest
# snapshots - must NOT be auto-activated, or a broken active key would silently
# lose recent memory from search. Reject a candidate store if it is missing
# more than EMBEDDINGS_RECENT_GAP_MAX total index ids, OR if it is missing any
# of the newest EMBEDDINGS_RECENT_GAP_TAIL snapshots (by counter). Prefer
# "stay broken with a loud banner" over activating a stale store.
EMBEDDINGS_RECENT_GAP_MAX  = CFG.getint("embeddings", "recent_gap_max", fallback=25)
EMBEDDINGS_RECENT_GAP_TAIL = CFG.getint("embeddings", "recent_gap_tail", fallback=50)
# Fast provider-down health signal (search.py query-embed path): after this
# many CONSECUTIVE query-embed failures, flip health.json to "degraded"
# immediately rather than waiting up to 24h for the watcher's next pass.
EMBEDDINGS_QUERY_FAIL_THRESHOLD = CFG.getint("embeddings", "query_fail_threshold", fallback=3)


CURRENT_OPERATOR = USERS_DEFAULT   # updated on each /chat


def set_current_operator(op: str) -> None:
    """Canonical writer for the most-recent-active-operator fallback (updated per turn).
    Writers MUST call this (updates the config module global) instead of rebinding a
    per-module CURRENT_OPERATOR copy, so all readers see it live."""
    global CURRENT_OPERATOR
    CURRENT_OPERATOR = op


def current_operator() -> str:
    """Live read of the most-recent-active-operator fallback."""
    return CURRENT_OPERATOR

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

# Static core of the system prompt (tool/artifact/memory guidance — no tool descriptions).
# Natural-language spec: respond in plain prose to the user. No JSON envelope is
# required — the orchestrator persists the reply directly and composes snapshots
# server-side. (The {ui_reply, snapshot_perspective} envelope was removed in the
# Phase 1+2 pure-production cutover; do NOT re-introduce a final-reply JSON schema.)
OUTPUT_SPEC_CORE = (
    "Respond in natural language directly to the user. Give the complete, detailed, "
    "user-facing answer in plain prose. If delivering a plan, include ALL steps. "
    "Do NOT wrap your response in a JSON object and do NOT emit snapshot format or "
    "markers — the orchestrator handles snapshots and memory automatically.\n\n"
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
    "You can create downloadable files (text, PDF, CSV, DOCX) by including artifact blocks in your reply:\n\n"
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
    "TOOL USAGE RULES:\n"
    "- Tools are OPTIONAL - only use when the user explicitly requests an action (generating media, sending messages, etc.).\n"
    "- When the user requests media generation, call the appropriate tool directly. Do NOT just write about generating media.\n"
    "- When generating ARTIFACT files (text, PDF, CSV, DOCX), do NOT call media generation tools unless the user explicitly asks for both.\n"
    "- MULTIPLE TOOL CALLS: You CAN call tools multiple times in a single response.\n"
    "- Use search_snapshots when users ask about past conversations, historical context, or reference previous work.\n\n"
    "CRITICAL: To use a tool, you MUST call it. Simply describing what you would do is NOT sufficient.\n\n"
    "FINAL CHECK: Did you call the appropriate tool if the user requested an action?"
)

# Legacy static OUTPUT_SPEC — hardcoded tool descriptions (used when TOOLVAULT_ENABLED=false)
OUTPUT_SPEC_TOOLS_STATIC = (
    "MULTIMODAL GENERATION (TOOL-BASED):\n"
    "You have access to the following tools for generating media. Use them by calling the tool function directly:\n\n"
    "IMAGE GENERATION TOOLS (pick a provider):\n"
    '  Tools: gemini_image / openai_image / grok_image\n'
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
    '  Tool: lyria_music\n'
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

# Assemble the default OUTPUT_SPEC with the lean default persona prepended so
# tone guidance is established before the format/tool spec. (Per-operator
# personas are resolved at request time via behavioral_core.get_persona; this
# module-level constant uses the lean default.)
OUTPUT_SPEC = (
    DEFAULT_PERSONA_CHAT
    + "\n\n"
    + OUTPUT_SPEC_CORE.replace("{TOOL_INSTRUCTIONS}", OUTPUT_SPEC_TOOLS_STATIC)
)


def build_output_spec(tool_instructions: str = "") -> str:
    """Build the system prompt with dynamic or static tool instructions.

    The lean default persona is prepended in both branches so tone guidance
    stays consistent across ToolVault-on and ToolVault-off configurations.
    (Per-operator personas are resolved at request time via get_persona.)

    When TOOLVAULT_ENABLED and tool_instructions provided:
      Uses the dynamically generated instructions from the vault.
    Otherwise:
      Uses the static hardcoded tool descriptions (legacy behavior).
    """
    if tool_instructions:
        return (
            DEFAULT_PERSONA_CHAT
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

# Reranker cloud-provider keys (tiered reranker, M4). Declared here for
# completeness/documentation — a reader greps ONE place for the reranker key
# envs. IMPORTANT: rerank.get_settings() does NOT use these constants. They are
# FROZEN at import; the reranker selector (POST /rerank/select) mirrors a
# newly-pasted key into os.environ and must take effect with NO restart, so the
# key is resolved via a FRESH os.getenv(key_env) at call time instead (the
# frozen-vs-fresh distinction — see rerank.get_settings "key_present").
VOYAGE_API_KEY    = os.getenv("VOYAGE_API_KEY", "")
COHERE_API_KEY    = os.getenv("COHERE_API_KEY", "")
VERTEX_PROJECT_ID = os.getenv("VERTEX_PROJECT_ID", "")

# Pairing defaults (used by /pair/claim and /pair/qr/{token} response payload).
# Customers register their own operators in onboarding step T2.7.1; "Brandon" is the system seed.
DEFAULT_OPERATOR = os.getenv("DEFAULT_OPERATOR", "Brandon")
DEFAULT_ORIGIN = os.getenv("DEFAULT_ORIGIN", "http://localhost:9091")

# Canonical Orchestrator listen port. The app serves on this port (uvicorn in
# Orchestrator/app.py); in-process clients that loop back to the local app
# (e.g. the cron executor) derive http://localhost:<port> from this rather
# than hardcoding 9091, so a fresh box that runs on a different port stays
# self-consistent. Resolution order: ORCHESTRATOR_PORT env -> config.ini
# [server] port -> 9091 default.
ORCHESTRATOR_PORT = int(
    os.getenv("ORCHESTRATOR_PORT")
    or CFG.getint("server", "port", fallback=9091)
)

# Tailnet hostname (set by onboarding T2.3.1 after Tailscale validation succeeds).
# When present, used to construct the canonical https://<hostname> origin for QR
# pairing payloads — the local browser may load the Portal at localhost:9091, but
# a remote phone scanning the QR can ONLY reach the BlackBox via the tailnet
# Magic DNS name (or LAN IP). Empty string when customer skipped Tailscale.
BLACKBOX_TAILNET_HOSTNAME = os.getenv("BLACKBOX_TAILNET_HOSTNAME", "").strip()

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

# OpenAI Realtime API (gpt-realtime voice conversations)
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")  # Newest GA (2026-07-06), P0 WS-probe-verified 2026-07-11
REALTIME_CONTEXT_MAX_CHARS = 50000    # ~20K tokens budget for initial context
REALTIME_SNAPSHOT_CHARS_EACH = 8000   # Max chars per snapshot in context
REALTIME_AUDIO_SAMPLE_RATE = 24000    # PCM16 audio at 24kHz

# OpenAI Realtime model catalog — empirically WS-connection-tested 2026-05-19
# (GA endpoint) and re-probed 2026-07-11 for the 2.1 generation
# (see diagnostics/voice_probes/results/). Findings:
#   - gpt-realtime-2.1 / gpt-realtime-2.1-mini (GA 2026-07-06) are the newest;
#     same price as gen-2, better ASR/interruptions/noise, WS-probe-verified.
#   - gpt-realtime-2 kept (superseded flagship, still GA).
#   - gpt-realtime-mini-2025-12-15 pin kept — NOT affected by the 2026-07-23
#     shutdown of the 2025-10-06 mini snapshots.
#   - gpt-realtime-2025-08-28: was REJECTED at the WS endpoint (close 4000) in
#     May 2026, but the 2026-07-11 re-probe ACCEPTED it (close 4000 did not
#     reproduce) — restored to the catalog.
# Routes filter category=="chat" when serving the dropdown; specialized variants
# (translate, transcribe) are exposed via env-var override only.
OPENAI_REALTIME_MODELS: List[Dict] = [
    # Conversational variants (UI dropdown) — all WS-connection-verified on GA endpoint
    {"id": "gpt-realtime-2.1", "name": "GPT Realtime 2.1 (Newest GA)", "default": True, "category": "chat"},
    {"id": "gpt-realtime-2.1-mini", "name": "GPT Realtime 2.1 Mini (cheap, newest)", "category": "chat"},
    {"id": "gpt-realtime-2", "name": "GPT Realtime 2", "category": "chat"},
    {"id": "gpt-realtime", "name": "GPT Realtime (GA alias)", "category": "chat"},
    {"id": "gpt-realtime-1.5", "name": "GPT Realtime 1.5 (pinned)", "category": "chat"},
    {"id": "gpt-realtime-mini", "name": "GPT Realtime Mini (cheap, alias)", "category": "chat"},
    {"id": "gpt-realtime-mini-2025-12-15", "name": "GPT Realtime Mini (Dec 2025 pin)", "category": "chat"},
    {"id": "gpt-realtime-2025-08-28", "name": "GPT Realtime (Aug 2025 pin)", "category": "chat"},
    # Specialized variants (NOT in main dropdown; audit I4)
    {"id": "gpt-realtime-translate", "name": "GPT Realtime Translate", "category": "translate"},
    {"id": "gpt-realtime-whisper", "name": "GPT Realtime Whisper (STT-only)", "category": "transcribe"},
]

# OpenAI Realtime voices (10 GA voices as of 2026-05-19, verified live).
# Single source of truth — imported by realtime_routes.py /realtime/status.
OPENAI_REALTIME_VOICES: List[str] = [
    "alloy", "ash", "ballad", "coral", "echo",
    "sage", "shimmer", "verse", "marin", "cedar",
]
OPENAI_REALTIME_DEFAULT_VOICE: str = "ash"

# OpenAI Realtime allowlists for server-side validation of client-supplied params.
OPENAI_REALTIME_VAD_TYPES = ("server_vad", "semantic_vad")
OPENAI_REALTIME_VAD_EAGERNESS = ("low", "medium", "high", "auto")
# GA session.audio.input.noise_reduction = {"type": near_field|far_field} | null.
# "off" is our sentinel for an explicit null (disable provider default).
OPENAI_REALTIME_NOISE_REDUCTION_TYPES = ("near_field", "far_field", "off")
# audio.input.transcription.delay for gpt-realtime-whisper (per-minute STT):
# latency/accuracy trade-off knob, per developers.openai.com realtime-transcription.
OPENAI_REALTIME_TRANSCRIPTION_DELAYS = ("minimal", "low", "medium", "high", "xhigh")

# Google Gemini Live API (Gemini 2.5 voice conversations)
GEMINI_LIVE_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")  # THE recommended Live model (research 2026-07-11); deliberate GA-rule exception, matches Android default. Env override wins.
GEMINI_LIVE_INPUT_SAMPLE_RATE = 16000   # PCM16 audio at 16kHz (Gemini input standard)
GEMINI_LIVE_OUTPUT_SAMPLE_RATE = 24000  # PCM16 audio at 24kHz (Gemini output)

# Gemini Live model catalog (verified via genai.list_models() 2026-05-19).
GEMINI_LIVE_MODELS: List[Dict] = [
    {"id": "gemini-3.1-flash-live-preview", "name": "Gemini 3.1 Flash Live (Preview, thinkingLevel)", "default": True},
    {"id": "gemini-2.5-flash-native-audio-latest", "name": "Gemini 2.5 Flash Live (Latest — deprecated line)"},
    {"id": "gemini-2.5-flash-native-audio-preview-12-2025", "name": "Gemini 2.5 Flash Live (Dec 2025 pin — deprecated)"},
]

# Gemini Live voices - complete 30-entry catalog.
# Source: https://ai.google.dev/gemini-api/docs/speech-generation "Voice options"
# table, fetched 2026-05-19.
GEMINI_LIVE_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda",
    "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus",
    "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima",
    "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]

# Character descriptors for Gemini Live voices (1:1 mapping with GEMINI_LIVE_VOICES).
GEMINI_LIVE_VOICE_DESCRIPTORS: Dict[str, str] = {
    "Zephyr": "Bright",          "Puck": "Upbeat",            "Charon": "Informative",
    "Kore": "Firm",              "Fenrir": "Excitable",       "Leda": "Youthful",
    "Orus": "Firm",              "Aoede": "Breezy",           "Callirrhoe": "Easy-going",
    "Autonoe": "Bright",         "Enceladus": "Breathy",      "Iapetus": "Clear",
    "Umbriel": "Easy-going",     "Algieba": "Smooth",         "Despina": "Smooth",
    "Erinome": "Clear",          "Algenib": "Gravelly",       "Rasalgethi": "Informative",
    "Laomedeia": "Upbeat",       "Achernar": "Soft",          "Alnilam": "Firm",
    "Schedar": "Even",           "Gacrux": "Mature",          "Pulcherrima": "Forward",
    "Achird": "Friendly",        "Zubenelgenubi": "Casual",   "Vindemiatrix": "Gentle",
    "Sadachbia": "Lively",       "Sadaltager": "Knowledgeable", "Sulafat": "Warm",
}

# -- TTS voice catalog (single source of truth for the TTS voice PICKER) ------
# Served by GET /tts/catalog; consumed by the web Portal + Android Settings.
# DISTINCT from GEMINI_LIVE_VOICES above (the live Voice Agent, /gemini-live/voices)
# -- different feature. Do not merge the two.

# Gemini TTS descriptions (Flash + Pro share these -- defined ONCE). Names come
# from GEMINI_LIVE_VOICES (the 30-name catalog) so names live in one place.
GEMINI_TTS_VOICE_DESCRIPTIONS: Dict[str, str] = {
    "Zephyr": "Bright, cheerful", "Puck": "Playful, mischievous", "Charon": "Calm, informative",
    "Kore": "Clear, versatile", "Fenrir": "Bold, confident", "Leda": "Warm, youthful",
    "Orus": "Deep, firm", "Aoede": "Breezy, conversational", "Callirrhoe": "Smooth, flowing",
    "Autonoe": "Gentle, measured", "Enceladus": "Rich, resonant", "Iapetus": "Deep, steady",
    "Umbriel": "Soft, mysterious", "Algieba": "Warm, articulate", "Despina": "Light, energetic",
    "Erinome": "Serene, melodic", "Algenib": "Crisp, precise", "Rasalgethi": "Grand, theatrical",
    "Laomedeia": "Graceful, elegant", "Achernar": "Bright, radiant", "Alnilam": "Strong, commanding",
    "Schedar": "Regal, distinguished", "Gacrux": "Earthy, grounded", "Pulcherrima": "Beautiful, refined",
    "Achird": "Friendly, approachable", "Zubenelgenubi": "Balanced, neutral", "Vindemiatrix": "Mature, wise",
    "Sadachbia": "Lucky, optimistic", "Sadaltager": "Hopeful, bright", "Sulafat": "Lyrical, musical",
}

# OpenAI TTS HD voices (11): (id, name, description).
OPENAI_TTS_VOICES = [
    ("alloy", "Alloy", "Neutral, balanced"), ("ash", "Ash", "Clear, direct"),
    ("ballad", "Ballad", "Warm, gentle"), ("coral", "Coral", "Friendly, conversational"),
    ("echo", "Echo", "Smooth, authoritative"), ("fable", "Fable", "Expressive, British"),
    ("nova", "Nova", "Energetic, confident"), ("onyx", "Onyx", "Deep, authoritative"),
    ("sage", "Sage", "Thoughtful, measured"), ("shimmer", "Shimmer", "Soft, ethereal"),
    ("verse", "Verse", "Poetic, dramatic"),
]

def build_tts_catalog() -> list:
    """Grouped TTS voice catalog -- the single source of truth for the picker.
    Returns [{id,label,voices:[{id,name,description}]}]. Gemini ids are
    'gemini-flash:<Name>' / 'gemini-pro:<Name>'; OpenAI 'openai:<id>'."""
    def gemini_group(provider: str, label: str) -> dict:
        return {"id": provider, "label": label, "voices": [
            {"id": f"{provider}:{n}", "name": n, "description": GEMINI_TTS_VOICE_DESCRIPTIONS[n]}
            for n in GEMINI_LIVE_VOICES
        ]}
    return [
        {"id": "openai", "label": "OpenAI TTS HD", "voices": [
            {"id": f"openai:{vid}", "name": nm, "description": ds}
            for vid, nm, ds in OPENAI_TTS_VOICES
        ]},
        gemini_group("gemini-flash", "Gemini Flash TTS"),
        gemini_group("gemini-pro", "Gemini Pro TTS"),
    ]

# ElevenLabs TTS quality-first defaults (env-overridable). Brandon's directive:
# default to the flagship model + highest output quality the plan allows; cheaper
# tiers are EXPLICIT, never silent. On a tier-gate 4xx the synth path retries ONCE
# at mp3_44100_128 and PRINTS a visible downgrade notice (see elevenlabs/tts.py).
ELEVENLABS_TTS_MODEL_DEFAULT = os.getenv("ELEVENLABS_TTS_MODEL_DEFAULT", "eleven_v3")
ELEVENLABS_TTS_FORMAT_DEFAULT = os.getenv("ELEVENLABS_TTS_FORMAT_DEFAULT", "mp3_44100_192")
# ElevenLabs TTS streaming: abort a generation only after this many seconds with
# NO audio bytes received (a true stall) — NOT a total wall-clock cap. As long as
# ElevenLabs keeps streaming audio, generation continues regardless of length.
ELEVENLABS_TTS_STREAM_IDLE_S = int(os.getenv("ELEVENLABS_TTS_STREAM_IDLE_S", "30"))
# Generous backstop on the whole MCP /tts tool call (the server self-bounds via the
# idle timeout above; this only prevents an indefinite client hang). Not the binding
# constraint for legitimate long generations.
TTS_TOOL_BACKSTOP_S = int(os.getenv("TTS_TOOL_BACKSTOP_S", "900"))

# ElevenLabs Music (POST /v1/music). Songs run long (up to 5 min) so 128 is the
# sensible default — it avoids the 192-tier-gate downgrade round-trip and the
# extra bytes buy nothing audible over a multi-minute track.
ELEVENLABS_MUSIC_FORMAT_DEFAULT = os.getenv("ELEVENLABS_MUSIC_FORMAT_DEFAULT", "mp3_44100_128")

GEMINI_LIVE_DEFAULT_VOICE = "Orus"      # Default voice for phone

# Gemini Live allowlists for server-side validation of client-supplied params.
GEMINI_LIVE_VAD_SENSITIVITIES = ("LOW", "MEDIUM", "HIGH")  # start/end-of-speech sensitivity enum
GEMINI_LIVE_THINKING_LEVELS = ("minimal", "low", "medium", "high")  # google-genai SDK 1.64.0 ThinkingConfig enum (lowercase)

# Model ids that support generationConfig.thinkingConfig.thinkingLevel.
# Only emit thinkingLevel for members of this set — emitting on non-thinking
# models would either be silently ignored or trigger upstream API errors.
# Per google-genai SDK 1.64.0 + 2026-05-19 model catalog research.
GEMINI_LIVE_THINKING_CAPABLE_MODELS: frozenset = frozenset({
    "gemini-3.1-flash-live-preview",
})

# xAI Grok Voice Agent API (Grok real-time voice conversations)
GROK_LIVE_URL = "wss://api.x.ai/v1/realtime"
GROK_LIVE_MODEL = os.getenv("GROK_LIVE_MODEL", "grok-voice-latest")  # alias -> newest (currently grok-voice-think-fast-1.0)
# Grok voice model catalog — P0 WS-probe-verified 2026-07-11 (see
# diagnostics/voice_probes/results/). grok-voice-fast-1.0 is deprecated
# upstream; the legacy "grok-voice-agent" string was a cosmetic label, never
# a real model id — the code previously sent NO model at all.
GROK_LIVE_MODELS: List[Dict] = [
    {"id": "grok-voice-latest", "name": "Grok Voice (Latest alias)", "default": True},
    {"id": "grok-voice-think-fast-1.0", "name": "Grok Voice Think Fast 1.0 (flagship pin)"},
]
# reasoning.effort exists ONLY on the newest voice generation (think-fast).
# Emitting it on other models risks an upstream reject — capability-gate like
# GEMINI_LIVE_THINKING_CAPABLE_MODELS.
GROK_LIVE_REASONING_EFFORTS = ("high", "none")
GROK_LIVE_REASONING_CAPABLE_MODELS: frozenset = frozenset({
    "grok-voice-latest",            # alias currently resolves to think-fast-1.0
    "grok-voice-think-fast-1.0",
})
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
# TG-side AMI (SMS send/receive + GSM status). Per-gateway creds arrive in Phase 2;
# these are the singleton fallback. NEVER hardcode the secret.
ASTERISK_AMI_HOST = os.getenv("ASTERISK_AMI_HOST", "")
ASTERISK_AMI_PORT = int(os.getenv("ASTERISK_AMI_PORT", "5038"))
ASTERISK_AMI_USER = os.getenv("ASTERISK_AMI_USER", "")
ASTERISK_AMI_SECRET = os.getenv("ASTERISK_AMI_SECRET", "")
# Encrypts gateway credentials (http.password, ami.secret) at rest. Stable random string.
TELEPHONY_SECRET_KEY = os.getenv("TELEPHONY_SECRET_KEY", "")

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
ANTHROPIC_MODEL_DEFAULT = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

# Extended thinking config per Claude model.
# Opus 4.8 (added 2026-05-28): newest Opus; mirrors 4.7's constraints
# (adaptive thinking, summarized-display, no temperature/top_p/top_k)
# until Anthropic publishes anything that contradicts. If the API
# rejects any of these on first call we'll relax.
# Opus 4.7: adaptive thinking only (budget_tokens returns 400). 1M context is native.
# display="summarized" streams readable thinking text (default "omitted" = empty blocks — would silently break thinking UI).
# effort="xhigh" is Opus 4.7's recommended level for agentic/coding work; Sonnet 4.6 maxes at "high".
# Haiku 4.5 is deliberately omitted — it doesn't support effort or adaptive thinking.
ANTHROPIC_THINKING_MODELS = {
    "claude-fable-5",   # Mythos-class: thinking always on; explicit {type: "adaptive"} accepted
    "claude-mythos-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
}
ANTHROPIC_EFFORT_MAP = {
    "claude-fable-5": "xhigh",    # recommended for agentic/coding on the Claude 5 tier
    "claude-mythos-5": "xhigh",
    "claude-opus-4-8": "xhigh",   # mirror 4.7 — newest Opus tier
    "claude-opus-4-7": "xhigh",   # Opus 4.7-only tier between "high" and "max"
    "claude-opus-4-6": "high",
    "claude-sonnet-4-6": "high",  # Sonnet caps at "high" — xhigh/max are Opus-tier only
}
# Opus 4.7 removed `temperature`, `top_p`, `top_k` — sending any returns 400.
# Opus 4.8 and the Claude 5 tier (Fable/Mythos) carry the same constraint.
ANTHROPIC_NO_SAMPLING_MODELS = {
    "claude-fable-5", "claude-mythos-5", "claude-opus-4-8", "claude-opus-4-7",
}
# Opus 4.7+ omit thinking text by default — set display="summarized" to get visible thinking.
# Fable/Mythos 5 likewise default to "omitted" (summaries only; raw CoT never returned).
# Other models stream thinking text as-is without the flag.
ANTHROPIC_THINKING_DISPLAY_MODELS = {
    "claude-fable-5", "claude-mythos-5", "claude-opus-4-8", "claude-opus-4-7",
}
GEMINI_MODEL_DEFAULT    = os.getenv("GOOGLE_GEMINI_MODEL", "gemini-3.1-pro-preview")
XAI_MODEL_DEFAULT       = os.getenv("XAI_MODEL", "grok-4.3")  # Bumped 2026-05-18: prior default grok-4-1-fast-reasoning is on xAI's May 2026 deprecation list (auto-redirected to grok-4.3 server-side)
DEFAULT_PROVIDER        = (os.getenv("DEFAULT_PROVIDER") or "google").strip().lower()
STT_MODEL       = os.getenv("STT_MODEL","whisper-1").strip()

# --- STT provider/model registry (swap a string to upgrade the model) ---
STT_PROVIDER       = os.getenv("STT_PROVIDER", "").strip().lower()   # "" = auto (whichever cred present)
STT_OPENAI_STREAM  = os.getenv("STT_OPENAI_STREAM", "gpt-realtime-whisper").strip()
STT_OPENAI_FILE    = os.getenv("STT_OPENAI_FILE",   "gpt-4o-transcribe").strip()
STT_OPENAI_DELAY   = os.getenv("STT_OPENAI_DELAY",  "low").strip()
STT_GOOGLE_MODEL   = os.getenv("STT_GOOGLE_MODEL",  "chirp_2").strip()
STT_GOOGLE_REGION  = os.getenv("STT_GOOGLE_REGION", "us-central1").strip()
ELEVENLABS_STT_STREAM_MODEL = os.getenv("ELEVENLABS_STT_STREAM_MODEL", "scribe_v2_realtime")
ELEVENLABS_STT_FILE_MODEL = os.getenv("ELEVENLABS_STT_FILE_MODEL", "scribe_v2")
STT_OPENAI_AVAILABLE = bool(OPENAI_API_KEY)
STT_GOOGLE_AVAILABLE = bool(GOOGLE_APPLICATION_CREDENTIALS and os.path.exists(GOOGLE_APPLICATION_CREDENTIALS))


# Anchors and ID regex
SNAP_RE = re.compile(r"SNAP-(\d{8})-(\d+)$")
# Bare SNAP-ID matcher used by fossils.extract_snap_ids' marker-less fallback.
# Lives here alongside START_RX/END_RX/SNAP_RE so every snapshot regex shares one
# home; it was previously defined ONLY in tasks.py, which fossils.py does not
# import — a latent NameError in the fallback branch (dormant because real
# snapshot blocks always carry a START marker).
SNAP_ID_RX = re.compile(r"(SNAP-\d{8}-\d+)")
END_RX = re.compile(
    r'^\s*===\s*END SNAPSHOT\s*[—-]\s*(?P<snap>SNAP-\d{8}-\d+)\s*[—-]\s*UTC\s*(?P<utc>.+?)\s*===\s*$',
    re.M
)
START_RX = re.compile(
    r'^\s*===\s*START SNAPSHOT\s*[—-]\s*UTC\s*.+?\s*[—-]\s*(?P<snap>SNAP-\d{8}-\d+)\s*.*?===\s*$',
    re.M
)


