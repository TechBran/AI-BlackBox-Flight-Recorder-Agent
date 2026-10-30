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
try:
    from Orchestrator.utils.paths import blackbox_root as _blackbox_root
except ImportError:
    from utils.paths import blackbox_root as _blackbox_root

from fastapi import FastAPI, HTTPException, UploadFile, File, Body, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware # IMPORTED FOR ANDROID/TAILSCALE FIX
from pydantic import BaseModel
from dotenv import load_dotenv
import requests
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

# Runtime databases live in a 0700 data/ dir, NEVER inside Portal/: /ui is a
# StaticFiles mount of Portal/, so the old Portal/tasks.db default made every
# operator's prompts, results and error strings downloadable at /ui/tasks.db
# (audit NEW-core-1 / X03-security-backend-07). The ONE definition of the task
# DB path — models and the relocate CLI import it; monitoring and admin_routes
# ask models.active_tasks_db_path(), the file the task_db singleton opened.
# migrations/tasks_db_location.py moves a legacy Portal/tasks.db (+ -wal/-shm)
# here before the first open, leaves Portal/tasks.db as a compat symlink to it
# (old code — a still-running service, a Rollback — then shares the one DB) and
# keeps the dir 0700 / the files 0600.
BLACKBOX_DATA_DIR = _blackbox_root() / "data"
TASKS_DB_PATH = BLACKBOX_DATA_DIR / "tasks.db"
LEGACY_TASKS_DB_PATH = _blackbox_root() / "Portal" / "tasks.db"  # pre-2026-10 location

# [debug] serve_dev_pages: Portal/dev/ (the FX Lab gallery, the approved
# onboarding mocks) is served at /ui/dev/ only when this is true.
DEBUG_SERVE_DEV_PAGES = CFG.getboolean("debug", "serve_dev_pages", fallback=False)

# [devices] require_pairing: refuse to call a device that has no pairing secret on
# record (Orchestrator/device_registry/pairing.py). Default false during the
# pairing rollout: apps older than the pairing release hold no secret and accept
# unauthenticated calls, so refusing them would only cut notifications and device
# control until every device runs the new app. A device that HAS paired always
# gets its bearer either way. Set true once every device runs the new app.
DEVICES_REQUIRE_PAIRING = CFG.getboolean("devices", "require_pairing", fallback=False)


# [wellness] (health tracking, design M7): read FRESH from config.ini on every
# call, so the kill switch takes effect on the next request with no restart.
# enabled defaults to true (decision D19: inert until someone enrolls); a
# config.ini that cannot be read or parsed reads as OFF (fail closed).
# simulator (default false) lets scripts/wellness_sim_phone.py stand in for a
# phone on a loopback address; never set it on a box people use.
def _wellness_section():
    from Orchestrator.utils.config_ini import ConfigIniError, read_config_ini
    try:
        return read_config_ini()
    except ConfigIniError:
        return None


def wellness_enabled() -> bool:
    cfg = _wellness_section()
    if cfg is None:
        return False
    try:
        return cfg.getboolean("wellness", "enabled", fallback=True)
    except ValueError:
        return False


def wellness_simulator() -> bool:
    cfg = _wellness_section()
    try:
        return bool(cfg is not None and cfg.getboolean("wellness", "simulator", fallback=False))
    except ValueError:
        return False


def wellness_simulator_port() -> int:
    cfg = _wellness_section()
    try:
        port = cfg.getint("wellness", "simulator_port", fallback=8765) if cfg else 8765
    except ValueError:
        return 8765
    return port if 1 <= port <= 65535 else 8765


AUDIO_ENGINE  = CFG.get("audio","engine",fallback="auto").strip().lower()
# Quality-first fallback (matches config.ini.template): the box default for every
# client now that the Portal and Android no longer pin their own OpenAI model.
TTS_MODEL     = CFG.get("audio","model", fallback="tts-1-hd").strip()
TTS_VOICE     = CFG.get("audio","voice", fallback="alloy").strip()
TTS_FORMAT    = CFG.get("audio","format",fallback="mp3").strip()
TTS_TIMEOUT   = CFG.getint("audio","timeout_ms",fallback=120000)
# Fallback = config.ini.template's seed operator (a fresh box runs on that).
USERS_LIST     = [u.strip() for u in CFG.get("users","list",fallback="Default").split(",") if u.strip()]
USERS_DEFAULT  = CFG.get("users","default",fallback=(USERS_LIST[0] if USERS_LIST else "Operator")).strip()

# ── Flight Recorder operator (design 2026-07-23) ────────────────────────────
# The permanent, undeletable overseer that ships on every box. Reserved names
# are refused by /operator/add (case-insensitive) and /operator/{name} DELETE.
# Explicitly NOT "system": that magic string carries notification/task
# suppression side effects an overseer must not inherit.
FLIGHT_RECORDER_OPERATOR = "Flight Recorder"
RESERVED_OPERATORS = {FLIGHT_RECORDER_OPERATOR, "system"}


def reads_all_operators(op) -> bool:
    """Operators whose retrieval scope is the WHOLE ledger (read-only widening).

    Reproduces the historical conditionals of the retrieval.py semantic gate
    and fossils._decode_scored_snapshots (BOTH already widened empty/None AND
    "system") and adds ONLY the Flight Recorder. NOT used by
    get_recent_fossils_for_operator, whose historical gate widened solely
    "system" — that site inlines its own check (review 2026-07-23). Write
    paths stamp the requesting operator into every mint, so widened reads
    never produce writes under another operator's name.
    """
    return not op or op == "system" or op == FLIGHT_RECORDER_OPERATOR


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
def _resolve_chrome_path() -> str:
    """Resolve the CU browser binary portably (M6 finding, 2026-07-23: MS02 has
    chromium but no Google Chrome — the hardcoded /opt path failed every virtual
    CU session there with 'Chrome not found'). Order: an explicitly-configured
    EXISTING path wins; else the first present of the well-known binaries; else
    the old default (so the error message still names a concrete path)."""
    import shutil as _sh
    configured = CFG.get("computer_use", "chrome_path", fallback="").strip()
    if configured and os.path.exists(configured):
        return configured
    for cand in ("/opt/google/chrome/chrome",):
        if os.path.exists(cand):
            return cand
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = _sh.which(name)
        if p:
            return p
    return configured or "/opt/google/chrome/chrome"


CU_CHROME_PATH          = _resolve_chrome_path()
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


# ── [local_models] — on-box local model stack (M1, design 2026-07-20) ────────
# Master enable + base_url snapshot at import for discoverability alongside
# [computer_use]/[embeddings]. The LIVE source of truth for routing is
# Orchestrator/local_stack.py, which RE-READS config.ini fresh per request so a
# wizard flip applies with NO restart (custom_servers.py E8 lesson). The
# per-capability enable flags (local_models.stt / .tts / .embeddings / .rerank)
# are read fresh by local_stack.enabled(cap); they default false — nothing
# routes on-box until the wizard flips each ("Nothing activates implicitly on
# install", design §8).
LOCAL_MODELS_ENABLED  = CFG.getboolean("local_models", "enabled", fallback=False)
LOCAL_MODELS_BASE_URL = CFG.get("local_models", "base_url",
                                fallback="http://127.0.0.1:9098/v1").strip()


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


def orchestrator_url(path: str = "", *, scheme: str = "http", host: str = "localhost") -> str:
    """Loopback URL of THIS Orchestrator: every in-process caller that talks to
    the local app (SMS replies, /chat/save mints, phone/voice bridges, the cron
    executor, tool executors) builds its URL here, so a box served on another
    ORCHESTRATOR_PORT still reaches itself. Read at call time. The formatting
    lives in the stdlib-only Orchestrator.utils.loopback (the MCP venv imports
    the tool executor without fastapi); this box's port is passed in."""
    from Orchestrator.utils.loopback import orchestrator_url as _loopback_url
    return _loopback_url(path, scheme=scheme, host=host, port=ORCHESTRATOR_PORT)


# Pairing defaults (/pair/claim, /pair/qr/{token} and /health's pairing block).
# DEFAULT_ORIGIN is the operator's override; perimeter.py also trusts it.
DEFAULT_ORIGIN = os.getenv("DEFAULT_ORIGIN") or orchestrator_url()


def _pairing_config_operator() -> str:
    """config.ini [pairing] default_operator (empty when unset)."""
    try:
        return (CFG.get("pairing", "default_operator", fallback="") or "").strip()
    except Exception:
        return ""


def _wizard_default_operator() -> str:
    """DEFAULT_OPERATOR, which the onboarding wizard's operator step writes to
    .env for the first operator it adds. Read fresh, so it applies without a
    restart; os.environ covers a value set outside .env."""
    try:
        from dotenv import dotenv_values
        from Orchestrator.onboarding.secrets_writer import ENV_FILE
        value = dotenv_values(str(ENV_FILE)).get("DEFAULT_OPERATOR")
    except Exception:
        value = None
    return (value or os.getenv("DEFAULT_OPERATOR") or "").strip()


def pairing_operator(requested: Optional[str] = None) -> str:
    """The operator a newly paired device starts as.

    First name that is on the live roster wins: ``requested`` (chosen when the
    token was minted) -> DEFAULT_OPERATOR (the wizard's choice) -> config.ini
    [pairing] default_operator -> current_default(). A name that is not (or no
    longer) on the roster is skipped, so a phone is never paired to a phantom
    operator. The wizard pairs the phone before its operator step runs, so on
    a fresh box this is the roster default until an operator is added.
    """
    for name in (requested, _wizard_default_operator(), _pairing_config_operator()):
        name = (name or "").strip()
        if name and name in USERS_LIST:
            return name
    return current_default()


# Tailnet hostname (set by onboarding T2.3.1 after Tailscale validation succeeds).
# When present, used to construct the canonical https://<hostname> origin for QR
# pairing payloads — the local browser may load the Portal at localhost:9091, but
# a remote phone scanning the QR can ONLY reach the BlackBox via the tailnet
# Magic DNS name (or LAN IP). Empty string when customer skipped Tailscale.
BLACKBOX_TAILNET_HOSTNAME = os.getenv("BLACKBOX_TAILNET_HOSTNAME", "").strip()

# Perimeter guard extras (Orchestrator/security/origin_guard.py, audit rank 13).
# Built in already: IP-literal Hosts, single-label names, .local/.localhost/
# .lan/.home.arpa/.internal/.home/.localdomain/.corp, every *.ts.net name,
# this box's FQDN, the pairing/DEFAULT_ORIGIN and Twilio webhook hosts,
# same-origin requests and the trusted cross-origins (user apps + zellij on
# the same host, loopback, Tauri). List anything else a legitimate caller
# uses — a custom reverse-proxy domain, a webhook tunnel host, an extra dev
# origin — under [security] as comma-separated values, one exact name each
# (no wildcards; unusable entries are logged at startup). Absent = none.
# Read at import: restart to apply.
SECURITY_EXTRA_ALLOWED_HOSTS = [
    h.strip() for h in CFG.get("security", "extra_allowed_hosts", fallback="").split(",") if h.strip()
]
SECURITY_EXTRA_ALLOWED_ORIGINS = [
    o.strip() for o in CFG.get("security", "extra_allowed_origins", fallback="").split(",") if o.strip()
]

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

# Perplexity Sonar model: what perplexity_web_search sends AND what the wizard's
# key probe asks for (Perplexity publishes no model list to discover it from).
# One place to change when Perplexity retires the id. Env PERPLEXITY_SEARCH_MODEL
# wins, then config.ini [web_search] perplexity_model.
def resolve_perplexity_search_model(env=os.environ, cfg=CFG) -> str:
    return ((env.get("PERPLEXITY_SEARCH_MODEL") or "").strip()
            or cfg.get("web_search", "perplexity_model", fallback="").strip()
            or "sonar")


PERPLEXITY_SEARCH_MODEL = resolve_perplexity_search_model()

# OpenAI Realtime API (gpt-realtime voice conversations)
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")  # Newest GA (2026-07-06), P0 WS-probe-verified 2026-07-11
# P6a translation voice mode — dedicated realtime translation model
# ($0.034/min, 16K ctx). Also listed in OPENAI_REALTIME_MODELS with
# category="translate" (hidden from the chat dropdown); MUST stay in that
# allowlist — connect_to_openai validates against it.
OPENAI_REALTIME_TRANSLATE_MODEL = os.getenv(
    "OPENAI_REALTIME_TRANSLATE_MODEL", "gpt-realtime-translate")
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

# Google Gemini Live API (Gemini 2.5/3.1 voice conversations).
# The BidiGenerateContent endpoint exists on two API versions:
#   v1beta  — default; all standard Live sessions
#   v1alpha — required for enableAffectiveDialog + proactivity.proactiveAudio
#             (2.5-native-audio family only; 3.1 rejects these setup fields)
GEMINI_LIVE_URL_TEMPLATE = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.{version}.GenerativeService.BidiGenerateContent"
)


def gemini_live_url(version: str = "v1beta") -> str:
    """Gemini Live WS endpoint for an API version. Allowlist: v1beta | v1alpha."""
    if version not in ("v1beta", "v1alpha"):
        raise ValueError(f"Unsupported Gemini Live API version: {version!r}")
    return GEMINI_LIVE_URL_TEMPLATE.format(version=version)


# Back-compat constant — existing imports (gemini_live_routes, phone bridge) keep working.
GEMINI_LIVE_URL = gemini_live_url()
GEMINI_LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")  # THE recommended Live model (research 2026-07-11); deliberate GA-rule exception, matches Android default. Env override wins.
# P6a translation voice mode — dedicated Gemini Live translation model.
# Single-source recon (google-gemini/gemini-skills), P0-probe-gated
# (diagnostics/voice_probes/results/). Deliberately NOT in GEMINI_LIVE_MODELS
# (kept out of the chat dropdown); the translate branch binds it directly.
GEMINI_LIVE_TRANSLATE_MODEL = os.getenv(
    "GEMINI_LIVE_TRANSLATE_MODEL", "gemini-3.5-live-translate-preview")
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

# Model each Gemini TTS group synthesises with when a caller names a voice but no
# model. GA ids; the AI Studio path maps them to its preview ids
# (tts_routes.call_gemini_tts_preview). /tts/catalog publishes them per group so
# the Portal and Android stop carrying their own copies.
GEMINI_TTS_MODELS: Dict[str, str] = {
    "gemini-flash": "gemini-2.5-flash-tts",
    "gemini-pro": "gemini-2.5-pro-tts",
}
# The AI Studio (API-key) id of each GA model above: call_gemini_tts_preview
# calls these when no service account is configured. Every GEMINI_TTS_MODELS
# value needs an entry; an id in neither table is refused with a 400
# (tts_routes.resolve_gemini_tts_model), never swapped for Pro.
GEMINI_TTS_PREVIEW_IDS: Dict[str, str] = {
    "gemini-2.5-flash-tts": "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-tts": "gemini-2.5-pro-preview-tts",
}
# The Gemini voice a Gemini group speaks with when it becomes the box default and
# [audio] voice names no Gemini voice. A choice, not a provider fact.
GEMINI_TTS_DEFAULT_VOICE = "Charon"
# Order resolve_tts_default() falls through when the [audio] voice's provider has
# no credentials: cloud groups first (OpenAI is the [audio] engine), then
# ElevenLabs, then the on-box and local stacks. Unlisted groups follow in
# catalog order.
TTS_DEFAULT_GROUP_ORDER = ("openai", "gemini-pro", "elevenlabs", "gemini-flash", "qwen", "local")


def _tts_credentials() -> Dict[str, bool]:
    """Which cloud TTS providers THIS PROCESS can synthesise with: {"openai",
    "google"}. The import-time values the synth paths read (tts_routes'
    OPENAI_API_KEY and GOOGLE_API_KEY, call_gemini_tts's USE_CLOUD_TTS), so the
    catalog never advertises, or defaults to, a provider /tts cannot use. Google
    TTS works with GOOGLE_API_KEY (AI Studio) or a service account (Cloud TTS).
    A key saved in the wizard counts after the restart
    /onboarding/restart-status asks for (_tts_saved_keys names it meanwhile)."""
    return {"openai": bool((OPENAI_API_KEY or "").strip()),
            "google": bool((GOOGLE_API_KEY or "").strip())
            or bool(USE_CLOUD_TTS and GOOGLE_AUTH_AVAILABLE)}


def _tts_saved_keys() -> Dict[str, bool]:
    """Which cloud TTS providers have a key in .env on disk (read fresh): a key
    the wizard saved that the running process has not loaded yet. Only used to
    say "restart" instead of "add a key". Never raises."""
    try:
        from dotenv import dotenv_values
        from Orchestrator.onboarding.secrets_writer import ENV_FILE
        env = dotenv_values(str(ENV_FILE))
    except Exception:
        return {"openai": False, "google": False}

    def _val(key: str) -> str:
        return (env.get(key) or "").strip()

    creds = _val("GOOGLE_APPLICATION_CREDENTIALS")
    return {"openai": bool(_val("OPENAI_API_KEY")),
            "google": bool(_val("GOOGLE_API_KEY")) or bool(creds and os.path.exists(creds))}


def _tts_group_status(group: dict, available: bool, reason: str) -> dict:
    group["available"] = available
    if not available:
        group["unavailable_reason"] = reason
    return group


def build_tts_catalog() -> list:
    """Grouped TTS voice catalog -- the single source of truth for the picker.
    Returns [{id,label,voices:[{id,name,description}],available,model}]. Gemini
    ids are 'gemini-flash:<Name>' / 'gemini-pro:<Name>'; OpenAI 'openai:<id>'.
    `available` says whether the running process can synthesise with the group's
    provider (`unavailable_reason` names the missing key, or the restart a saved
    key still needs) and `model` is the model the box synthesises that group with
    when a caller names none. Both are additive: every group is still listed, so
    a picker can show what a key would unlock. A bare [audio] voice the static
    OpenAI list lacks (e.g. a gpt-4o-mini-tts voice) is listed too, so the
    configured default is a voice the picker can show."""
    creds = _tts_credentials()
    saved = _tts_saved_keys() if not all(creds.values()) else {}

    def reason(provider: str, missing: str) -> str:
        if saved.get(provider):
            return "Key saved: restart the BlackBox service to use it"
        return missing

    openai_voices = [{"id": f"openai:{vid}", "name": nm, "description": ds}
                     for vid, nm, ds in OPENAI_TTS_VOICES]
    configured = (TTS_VOICE or "").strip()
    if configured and ":" not in configured \
            and all(v["id"] != f"openai:{configured}" for v in openai_voices):
        openai_voices.append({"id": f"openai:{configured}", "name": configured.capitalize(),
                              "description": "Configured in config.ini [audio]"})

    def gemini_group(provider: str, label: str) -> dict:
        return _tts_group_status({
            "id": provider, "label": label, "voices": [
                {"id": f"{provider}:{n}", "name": n, "description": GEMINI_TTS_VOICE_DESCRIPTIONS[n]}
                for n in GEMINI_LIVE_VOICES
            ],
            "model": GEMINI_TTS_MODELS[provider],
        }, creds["google"], reason("google", "Needs GOOGLE_API_KEY or a Google service-account credential"))
    return [
        _tts_group_status({
            "id": "openai", "label": "OpenAI TTS HD", "voices": openai_voices,
            "model": TTS_MODEL,
        }, creds["openai"], reason("openai", "Needs OPENAI_API_KEY")),
        gemini_group("gemini-flash", "Gemini Flash TTS"),
        gemini_group("gemini-pro", "Gemini Pro TTS"),
    ]


def resolve_tts_default(groups: list) -> dict:
    """The voice the box speaks with when a caller chooses none, over a catalog
    `groups` list (build_tts_catalog() plus whatever dynamic groups are present):

      1. the [audio] voice, when its group is listed and available (a bare name
         is an OpenAI voice, as the /tts OpenAI path has always read it);
      2. else the first available group in TTS_DEFAULT_GROUP_ORDER, speaking its
         preferred voice (the [audio] voice for OpenAI, GEMINI_TTS_DEFAULT_VOICE
         for Gemini, the first listed voice otherwise);
      3. else the [audio] voice, marked unavailable -- no provider can speak.

    Returns {id: 'provider:voice', provider, voice, model, available, source:
    config|fallback|none}. Pure over `groups`, so GET /tts/catalog, POST /tts
    and POST /tts/batch give one answer and every client renders the same one."""
    configured = (TTS_VOICE or "").strip()
    want = configured if (":" in configured or not configured) else f"openai:{configured}"

    def usable(g: dict) -> bool:
        return g.get("available", True) is not False and bool(g.get("voices"))

    def has(g: dict, vid: str) -> bool:
        return any(v.get("id") == vid for v in g.get("voices") or [])

    def entry(g: dict, vid: str, source: str) -> dict:
        return {"id": vid, "provider": g.get("id", ""), "voice": vid.split(":", 1)[-1],
                "model": g.get("model") or "", "available": source != "none",
                "source": source}

    for g in groups:
        if want and has(g, want):
            if usable(g):
                return entry(g, want, "config")
            break
    by_id = {g.get("id"): g for g in groups}
    ordered = [by_id[i] for i in TTS_DEFAULT_GROUP_ORDER if i in by_id]
    ordered += [g for g in groups if g.get("id") not in TTS_DEFAULT_GROUP_ORDER]
    for g in ordered:
        if not usable(g):
            continue
        gid = g.get("id", "")
        preferred = []
        if gid == "openai" and configured and ":" not in configured:
            preferred.append(f"openai:{configured}")
        if gid in GEMINI_TTS_MODELS:
            preferred.append(f"{gid}:{GEMINI_TTS_DEFAULT_VOICE}")
        vid = next((p for p in preferred if has(g, p)), g["voices"][0]["id"])
        return entry(g, vid, "fallback")
    provider = want.split(":", 1)[0] if want else ""
    return entry(by_id.get(provider) or {"id": provider}, want, "none")

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

# Model ids that support setup.enableAffectiveDialog + setup.proactivity.proactiveAudio.
# v1alpha-ONLY features on the 2.5-native-audio family (DEPRECATED line, no shutdown
# date announced). gemini-3.1-flash-live-preview REJECTS these setup fields — never
# emit them for non-members. Per ai.google.dev/gemini-api/docs/live-guide (2026-07-11).
GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS: frozenset = frozenset({
    "gemini-2.5-flash-native-audio-latest",
    "gemini-2.5-flash-native-audio-preview-12-2025",
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
GROK_LIVE_INPUT_SAMPLE_RATE = 16000     # PCM16 mic input — matches Android capture (VoiceScreen.kt); P0 probe-verified xAI accepts 16k
GROK_LIVE_OUTPUT_SAMPLE_RATE = 24000    # PCM16 AI output — matches Portal/Android/phone playback (unchanged)
GROK_LIVE_SAMPLE_RATE = GROK_LIVE_OUTPUT_SAMPLE_RATE  # legacy alias — status back-compat only

# =============================================================================
# Phone Integration (Twilio, Asterisk/TG200)
# =============================================================================

# Phone Feature Toggle
PHONE_ENABLED = os.getenv("PHONE_ENABLED", "false").lower() == "true"

# Phone Audio Settings
PHONE_SAMPLE_RATE = 8000                 # G.711 standard (8kHz)
PHONE_AUDIO_FORMAT = "ulaw"              # G.711 mu-law
PHONE_FRAME_SIZE_MS = 20                 # 20ms frames (160 samples at 8kHz)

# IVR Settings
IVR_TIMEOUT_MS = int(os.getenv("IVR_TIMEOUT_MS", "5000"))        # 5 second timeout for DTMF
IVR_MAX_RETRIES = int(os.getenv("IVR_MAX_RETRIES", "3"))         # 3 retry attempts
IVR_DEFAULT_BACKEND = os.getenv("IVR_DEFAULT_BACKEND", "openai_realtime")  # Default on timeout
IVR_INTER_DIGIT_TIMEOUT_MS = int(os.getenv("IVR_INTER_DIGIT_TIMEOUT_MS", "3000"))  # Between digits

# PIN Security (gates inbound calls to a tool-armed AI). NO default: an empty or
# malformed PIN fails CLOSED (every IVR refuses the call). The telephony
# wizard's Apply step / POST /phone/pin/generate writes a random one into .env.
# Gate logic + the cross-call lockout live in Orchestrator/phone/pin_gate.py.
# Only an explicit false value turns the gate off; anything else (a typo, "1",
# "yes") keeps the PIN required.
PHONE_PIN_ENABLED = os.getenv("PHONE_PIN_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")
PHONE_PIN_CODE = os.getenv("PHONE_PIN_CODE", "")                  # 4-12 digits
PHONE_PIN_MAX_ATTEMPTS = int(os.getenv("PHONE_PIN_MAX_ATTEMPTS", "3"))  # Wrong attempts per call (and per caller before lockout)

# Phone Session Settings
PHONE_SESSION_TIMEOUT_S = int(os.getenv("PHONE_SESSION_TIMEOUT_S", "3600"))  # 1 hour max call
PHONE_IDLE_TIMEOUT_S = int(os.getenv("PHONE_IDLE_TIMEOUT_S", "300"))          # 5 min idle timeout
PHONE_CLI_SESSION_TIMEOUT_MIN = int(os.getenv("PHONE_CLI_SESSION_TIMEOUT_MIN", "15"))  # 15 min CLI session persistence

# Twilio Integration (alternative to FreeSwitch for phone-AI bridging)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")  # e.g., "+15555550100"
# Public base URL for Twilio webhooks (must be HTTPS accessible from internet)
# For local development, use ngrok: "wss://your-ngrok-url.ngrok.io"
# For production: "wss://your-domain.com"
TWILIO_WEBHOOK_BASE_URL = os.getenv("TWILIO_WEBHOOK_BASE_URL") or orchestrator_url(scheme="wss")

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


def _env_flag(name: str, default: bool) -> bool:
    """Boolean env var: unset/empty -> ``default``; false/0/no/off -> False;
    any other value -> True."""
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value not in ("false", "0", "no", "off")


# ToolVault — Dynamic tool injection. ToolVault is the product's tool catalog,
# so it is ON unless TOOLVAULT_ENABLED explicitly switches it off (a fresh box
# has no .env entry for it; the old false default ran the frozen static prompt).
TOOLVAULT_ENABLED = _env_flag("TOOLVAULT_ENABLED", True)

# Google OAuth 2.0 (Gmail integration)
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")

# Google Cloud Service Account (for GA Gemini TTS)
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
USE_CLOUD_TTS = bool(GOOGLE_APPLICATION_CREDENTIALS and os.path.exists(GOOGLE_APPLICATION_CREDENTIALS))

# Default Models
OPENAI_MODEL_DEFAULT    = os.getenv("OPENAI_MODEL", "gpt-5.1")


def _anthropic_model_default() -> str:
    """ANTHROPIC_MODEL, else the current flagship Opus (owner decision 2026-10-17)."""
    return os.getenv("ANTHROPIC_MODEL") or "claude-opus-5-5"


ANTHROPIC_MODEL_DEFAULT = _anthropic_model_default()

# Claude request capabilities, parsed from the model id (anthropic_caps below).
# This replaced three exact-id tables (ANTHROPIC_THINKING_MODELS,
# ANTHROPIC_EFFORT_MAP, ANTHROPIC_THINKING_DISPLAY_MODELS) that rotted the way
# the sampling deny-list did (O01-core-runtime-07): the Portal lists models
# live from /v1/models, so the next point release, or a dated snapshot id, got
# no thinking config and no effort pin until someone appended it.
#
# What the id decides, by claude-<family>-<major>[-<minor>][-<yyyymmdd>]:
# * 4.6 and later think adaptively ({type: "adaptive"}); budget_tokens 400s on
#   4.7+ and 5.x, and Opus 5.5 / Sonnet 5.5 / Fable 5.1 reject
#   {type: "disabled"}, so adaptive is the only setting every one accepts.
#   Older ids (Haiku 4.5, Sonnet 4.5, Opus 4.5 and before) run without
#   thinking, as they always did here, and keep sampling params.
# * 4.7 and later default thinking display to "omitted" (empty thinking
#   blocks, a silently blank thinking panel), so they get display="summarized".
#   4.6 already summarizes by default.
# * Effort is pinned per tier: Opus and the Fable/Mythos class at "xhigh"
#   (the level for agentic and coding work; it arrived with Opus 4.7, so Opus
#   4.6 gets "high"; the API default on Opus 5.5 is only "medium"), Sonnet at
#   "high" (Sonnet 4.6 caps there), Haiku left at the API default so the cheap
#   tier stays cheap. A family this table has never seen thinks and summarizes
#   like the rest of its version but keeps the API's default effort.
# Every chat payload sends adaptive thinking and never forces a tool, so the
# 5.x rejections (disabled thinking, budget_tokens, sampling params, forced
# tool_choice) never go out. 1M context, 128K output on the 4.6+ models;
# context_builder's anthropic window guard budgets for that.
_CLAUDE_ID_RX = re.compile(
    r"claude-(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?(?:-\d{8})?(?:-latest)?"
)
_ANTHROPIC_EFFORT_BY_FAMILY = {
    "opus": "xhigh",
    "fable": "xhigh",
    "mythos": "xhigh",
    "sonnet": "high",
    "haiku": None,
}
# Sampling params (`temperature`, `top_p`, `top_k`) are an ALLOW-list of the legacy
# families that still accept them. Opus 4.7 removed them and every model since
# returns 400 "`temperature` is deprecated for this model" on any non-default
# value. This used to be a deny-list (ANTHROPIC_NO_SAMPLING_MODELS) and it rotted
# twice — claude-fable-5 on 2026-06-11, then the whole 5.x tier (Opus 5.5,
# Sonnet 5.5, ...) on 2026-10-07 — because the Portal lists models live from
# /v1/models, so every new release 400'd until someone appended it. Unknown and
# future models now get no sampling params at all. Prefix match so dated
# snapshot IDs (claude-haiku-4-5-20251001) are covered.
ANTHROPIC_SAMPLING_MODEL_PREFIXES = (
    "claude-opus-4-6", "claude-sonnet-4-6",
    "claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5",
    "claude-opus-4-1", "claude-opus-4-0", "claude-sonnet-4-0",
    "claude-opus-4-2025", "claude-sonnet-4-2025",   # claude-{opus,sonnet}-4-20250514
    "claude-3",
)


def anthropic_accepts_sampling(model: str) -> bool:
    """True only for legacy Claude models that still accept temperature/top_p/top_k."""
    return (model or "").startswith(ANTHROPIC_SAMPLING_MODEL_PREFIXES)


@dataclass(frozen=True)
class AnthropicCaps:
    """What a Messages request may carry for one Claude model."""
    thinking: Optional[dict]   # the request's `thinking` field; None = send none
    effort: Optional[str]      # output_config.effort to pin; None = API default
    sampling: bool             # accepts temperature / top_p / top_k


def anthropic_caps(model: Optional[str]) -> AnthropicCaps:
    """The request capabilities of Claude model ``model`` (see the table above).

    Shared by the stream and non-stream chat paths. An id that does not parse
    as claude-<family>-<major>... (a legacy claude-3-* id, a typo, "") gets no
    thinking config and no effort pin, and sampling only if it is a legacy id.
    """
    model_id = (model or "").strip().lower()
    sampling = anthropic_accepts_sampling(model_id)
    match = _CLAUDE_ID_RX.fullmatch(model_id)
    if match is None:
        return AnthropicCaps(thinking=None, effort=None, sampling=sampling)
    version = (int(match["major"]), int(match["minor"] or 0))
    if version < (4, 6):
        return AnthropicCaps(thinking=None, effort=None, sampling=sampling)
    thinking = {"type": "adaptive"}
    if version >= (4, 7):
        thinking["display"] = "summarized"
    effort = _ANTHROPIC_EFFORT_BY_FAMILY.get(match["family"])
    if effort == "xhigh" and version < (4, 7):
        effort = "high"
    return AnthropicCaps(thinking=thinking, effort=effort, sampling=sampling)


GEMINI_MODEL_DEFAULT    = os.getenv("GOOGLE_GEMINI_MODEL", "gemini-3.1-pro-preview")
XAI_MODEL_DEFAULT       = os.getenv("XAI_MODEL", "grok-4.3")  # Bumped 2026-05-18: prior default grok-4-1-fast-reasoning is on xAI's May 2026 deprecation list (auto-redirected to grok-4.3 server-side)


def _default_provider() -> str:
    """DEFAULT_PROVIDER, else the first chat provider this box holds a key for
    (google, anthropic, openai, xai), else "google". Onboarding never writes
    DEFAULT_PROVIDER, so a BYOK box with only an Anthropic key must not send
    its provider-less turns (SMS without a saved preference, /chat/stream
    without a provider) to a Gemini that has no key. Read at import, like
    the *_API_KEY constants it checks, so a new key applies on restart."""
    explicit = (os.getenv("DEFAULT_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    keyed = (("google", GOOGLE_API_KEY), ("anthropic", ANTHROPIC_API_KEY),
             ("openai", OPENAI_API_KEY), ("xai", XAI_API_KEY))
    return next((provider for provider, key in keyed if key), "google")


DEFAULT_PROVIDER        = _default_provider()

# ── Retired Claude ids -> the newest live model of the same family ──────────
# An id saved before Anthropic retired it (an SMS model preference, a cron job,
# an Android DataStore value) 404s with not_found_error. successor_for() maps it
# at dispatch instead. Owner decision 2026-10-17; the Android app carries the
# identical table, so change both together. Retired: Claude 4.0 (claude-{opus,
# sonnet}-4, -4-0, -4-YYYYMMDD), Opus 4.1 (dated or not) and every claude-3*,
# claude-2* and claude-instant* id. A family-less id (claude-2*, claude-instant*)
# runs what "Auto" runs, ANTHROPIC_MODEL_DEFAULT, as Android rewrites it to Auto.
ANTHROPIC_RETIRED_SUCCESSORS = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}
_ANTHROPIC_RETIRED_ID_RX = re.compile(
    r"claude-(?:"
    r"(?:sonnet|opus)-4(?:-0|-1)?(?:-\d{8})?"
    r"|3(?:[.-][\w.-]*)?"
    r"|2(?:[.-][\w.-]*)?"
    r"|instant(?:-[\w.-]*)?"
    r")"
)


def anthropic_model_retired(model_id: Optional[str]) -> bool:
    """True when ``model_id`` is a Claude id Anthropic has retired (it 404s)."""
    return _ANTHROPIC_RETIRED_ID_RX.fullmatch((model_id or "").strip()) is not None


def _claude_family(model_id: str) -> Optional[str]:
    return next((f for f in ANTHROPIC_RETIRED_SUCCESSORS if f in model_id), None)


def successor_for(model_id: str) -> str:
    """``model_id`` unchanged, unless it is a retired Claude id: then the newest
    live model of its family (ANTHROPIC_RETIRED_SUCCESSORS), or for an id with no
    family, the model "Auto" runs (ANTHROPIC_MODEL_DEFAULT; its own successor if
    that is retired too, the flagship Opus if it has no family either)."""
    if not anthropic_model_retired(model_id):
        return model_id
    family = _claude_family(model_id.strip())
    if family:
        return ANTHROPIC_RETIRED_SUCCESSORS[family]
    if not anthropic_model_retired(ANTHROPIC_MODEL_DEFAULT):
        return ANTHROPIC_MODEL_DEFAULT
    return ANTHROPIC_RETIRED_SUCCESSORS[_claude_family(ANTHROPIC_MODEL_DEFAULT) or "opus"]


# Only the alias every chat dispatch understands: /chat/stream and the worker
# route "claude", "gpt" and the like to the Gemini fallthrough, so resolving
# them to another provider's model would send that model to Gemini.
_PROVIDER_ALIASES = {"gemini": "google"}


def resolve_default_model(provider: Optional[str] = None, model: Optional[str] = None) -> str:
    """The model id to dispatch for ``provider`` when the caller asked for ``model``.

    One resolver for every chat path (both /chat/stream routes, the non-stream
    worker that serves /chat, cron and SMS, the SMS router and /sms/preferences):
      * an empty provider is DEFAULT_PROVIDER;
      * an empty, whitespace or "Auto" model (the Portal's "(Auto - Latest)"
        option sends "") is that provider's configured *_MODEL_DEFAULT (the
        module constant, looked up per call), so the env overrides
        (ANTHROPIC_MODEL, GOOGLE_GEMINI_MODEL, OPENAI_MODEL, XAI_MODEL) apply
        on every path;
      * a concrete model passes through;
      * an anthropic dispatch of a retired Claude id, requested or configured,
        gets successor_for()'s live model instead;
      * provider "custom" is returned untouched: its default is the registry's
        first discovered model, which the custom branches resolve themselves.
    An unknown provider resolves like Gemini (the /chat/stream fallthrough).
    """
    key = (provider or "").strip().lower() or DEFAULT_PROVIDER
    key = _PROVIDER_ALIASES.get(key, key)
    if key == "custom":
        return model or ""
    requested = (model or "").strip()
    if not requested or requested.lower() == "auto":
        requested = {
            "anthropic": ANTHROPIC_MODEL_DEFAULT,
            "openai": OPENAI_MODEL_DEFAULT,
            "xai": XAI_MODEL_DEFAULT,
            "computer-use": CU_MODEL_DEFAULT,
        }.get(key, GEMINI_MODEL_DEFAULT)
    return successor_for(requested) if key == "anthropic" else requested


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


