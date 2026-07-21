"""On-box local model stack — the single Orchestrator-side resolver (M1).

llama-swap (blackbox-models.service, :9098) fronts the on-box STT / TTS /
embeddings / reranker members. This module is the ONE source of truth every
consumer (STT/TTS resolvers, the localstack embeddings & rerank providers, the
wizard, GET /local-models/status) calls to answer: is the on-box stack
installed? reachable? is this capability SEEDED to resolve on-box? where do I
reach it?

Fresh-read discipline (custom_servers.py E8 lesson): the per-capability enable
flags live in config.ini's [local_models] section and are RE-READ from disk on
every call, so a wizard flip takes effect with NO restart. No import-time
config snapshot is trusted for routing.

Anti-flap invariant (design §4/§6, correction [30]): is_healthy() keys on
install + config + process-liveness of the llama-swap FRONT DOOR — never on
live per-member VRAM residency. A normal audio<->retrieval group swap takes the
demanded group's members transiently down; llama-swap's request queue absorbs
that, so a mid-swap request WAITS rather than routing to cloud. Routing
decisions are config/install state, not turn-to-turn health flapping.

HTTP is mocked in tests via the module `_transport` seam (httpx.MockTransport),
exactly like Orchestrator/embeddings/ollama_io.py.
"""
from __future__ import annotations

import configparser
import json
import logging

import httpx

from Orchestrator.utils.paths import resolve  # honors BLACKBOX_ROOT first

logger = logging.getLogger(__name__)

# ── canonical names (design "CANONICAL NAMES"; keep in lock-step across the box)
DEFAULT_BASE_URL = "http://127.0.0.1:9098/v1"   # llama-swap front door + /v1
CAPABILITIES = ("stt", "tts", "embeddings", "rerank")
SECTION = "local_models"

# config.ini is a per-box, gitignored file (config.py reads it CWD-relative at
# import; resolve() honors BLACKBOX_ROOT first). Module attr so tests repoint it.
CONFIG_PATH = resolve("config.ini")


# ── config fresh-read ─────────────────────────────────────────────────────────

def _read_config() -> configparser.ConfigParser:
    """Parse config.ini FRESH (never the import-time config.CFG snapshot).
    Fail-soft: a missing/corrupt/unreadable file yields an empty parser, so
    every getter falls back to its default."""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(str(CONFIG_PATH))
    except (configparser.Error, OSError) as exc:
        logger.warning("local_stack: unreadable config.ini at %s (%s)", CONFIG_PATH, exc)
    return cfg


def master_enabled() -> bool:
    """[local_models] enabled — the installer/wizard flips this true when the
    stack is installed and its service should run. The 'installed' signal."""
    return _read_config().getboolean(SECTION, "enabled", fallback=False)


def base_url() -> str:
    """[local_models] base_url — the llama-swap /v1 front door. A trailing
    slash is normalized off so consumers can concatenate paths safely."""
    val = _read_config().get(SECTION, "base_url", fallback=DEFAULT_BASE_URL).strip().rstrip("/")
    return val or DEFAULT_BASE_URL


def base_url_root() -> str:
    """Front-door ROOT (no /v1) for llama-swap admin endpoints (/health,
    /running, /upstream/*)."""
    root = base_url().rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


def enabled(cap: str) -> bool:
    """True iff `cap` is SEEDED to resolve on-box: master [local_models] enabled
    AND the per-capability flag. Fresh read — a wizard flip applies with no
    restart. An unknown capability is always False. This is the persisted
    wizard-time DEFAULT (D2); it does NOT override an explicit credentialed user
    pick — each capability's own resolver checks that BEFORE calling here."""
    if cap not in CAPABILITIES:
        return False
    cfg = _read_config()
    if not cfg.getboolean(SECTION, "enabled", fallback=False):
        return False
    return cfg.getboolean(SECTION, cap, fallback=False)


def is_installed() -> bool:
    """The on-box stack is installed + configured (master [local_models]
    enabled). Cheap, no HTTP — install/config state only."""
    return master_enabled()
