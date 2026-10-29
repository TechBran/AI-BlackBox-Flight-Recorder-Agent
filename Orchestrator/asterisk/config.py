#!/usr/bin/env python3
"""
config.py - Asterisk PBX / Yeastar TG200 Configuration

All config vars are also mirrored in the main Orchestrator/config.py
for consistency with the existing pattern. This file provides the
asterisk-specific defaults and parsing.
"""

import json
import os

# ---------------------------------------------------------------------------
# Asterisk ARI (REST Interface)
# ---------------------------------------------------------------------------
ASTERISK_ENABLED = os.getenv("ASTERISK_ENABLED", "false").lower() == "true"
ASTERISK_ARI_URL = os.getenv("ASTERISK_ARI_URL", "http://127.0.0.1:8088")
ASTERISK_ARI_USER = os.getenv("ASTERISK_ARI_USER", "blackbox")
ASTERISK_ARI_PASSWORD = os.getenv("ASTERISK_ARI_PASSWORD", "")
ASTERISK_ARI_APP = os.getenv("ASTERISK_ARI_APP", "blackbox")

# Derived WebSocket URL for ARI events
_ari_base = ASTERISK_ARI_URL.replace("http://", "ws://").replace("https://", "wss://")
ASTERISK_ARI_WS_URL = os.getenv(
    "ASTERISK_ARI_WS_URL",
    f"{_ari_base}/ari/events?app={ASTERISK_ARI_APP}&subscribeAll=true"
)

# ---------------------------------------------------------------------------
# AudioSocket (TCP audio bridge between Asterisk and Orchestrator)
# ---------------------------------------------------------------------------
ASTERISK_AUDIOSOCKET_HOST = os.getenv("ASTERISK_AUDIOSOCKET_HOST", "127.0.0.1")
ASTERISK_AUDIOSOCKET_PORT = int(os.getenv("ASTERISK_AUDIOSOCKET_PORT", "9092"))

# Audio format: Asterisk AudioSocket() app sends slin 8kHz (forced by app_audiosocket.c)
# TG200 negotiates G.711 u-law — Asterisk transcodes to slin before AudioSocket
ASTERISK_SAMPLE_RATE = 8000    # 8kHz — TG200 only supports G.711 u-law
ASTERISK_FRAME_SIZE_MS = 20    # 20ms frames = 320 bytes per frame at 8kHz

# ---------------------------------------------------------------------------
# TG200 Gateway Defaults
# ---------------------------------------------------------------------------
# Overrides only: outbound calls take their trunk and caller id from the
# gateway registry (gateway_manager.resolve_outbound_route).
TG200_TRUNK_NAME = os.getenv("TG200_TRUNK_NAME", "")
TG200_PHONE_NUMBER = os.getenv("TG200_PHONE_NUMBER", "")
TG200_DEFAULT_IP = os.getenv("TG200_DEFAULT_IP", "192.168.5.150")
TG200_SIP_PORT = int(os.getenv("TG200_SIP_PORT", "5060"))
TG200_HTTP_PORT = int(os.getenv("TG200_HTTP_PORT", "80"))

# TG200 web GUI credentials (for SMS API and status checks)
TG200_HTTP_USER = os.getenv("TG200_HTTP_USER", "admin")
TG200_HTTP_PASSWORD = os.getenv("TG200_HTTP_PASSWORD", "password")

# ---------------------------------------------------------------------------
# Multi-Trunk Scalability
# ---------------------------------------------------------------------------
# JSON array of trunk configs for multiple gateways:
# [{"name":"tg200-1","ip":"192.168.5.150","phone":"+15555550110","capacity":2}, ...]
_trunks_json = os.getenv("ASTERISK_TRUNKS", "[]")
try:
    ASTERISK_TRUNKS = json.loads(_trunks_json)
except (json.JSONDecodeError, TypeError):
    ASTERISK_TRUNKS = []

# ---------------------------------------------------------------------------
# Gateway persistence file
# ---------------------------------------------------------------------------
GATEWAYS_FILE = os.path.join(os.path.dirname(__file__), "gateways.json")

# ---------------------------------------------------------------------------
# Asterisk dialplan contexts
# ---------------------------------------------------------------------------
ASTERISK_INBOUND_CONTEXT = "from-tg200"
