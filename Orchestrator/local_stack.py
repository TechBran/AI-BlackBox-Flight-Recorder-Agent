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
import os
from pathlib import Path
from urllib.parse import quote as _quote

import httpx
import yaml

from Orchestrator.utils.paths import resolve  # honors BLACKBOX_ROOT first

logger = logging.getLogger(__name__)

# ── canonical names (design "CANONICAL NAMES"; keep in lock-step across the box)
DEFAULT_BASE_URL = "http://127.0.0.1:9098/v1"   # llama-swap front door + /v1
CAPABILITIES = ("stt", "tts", "embeddings", "rerank")
SECTION = "local_models"

# The four llama-swap members (ids MUST match the config.yaml template, §8).
# capability/group drive the status rollup + the per-capability routing block.
MEMBERS = (
    {"model": "embed-qwen3-8b",    "capability": "embeddings", "group": "retrieval",
     "label": "Qwen3-Embedding-8B (Q8_0)"},
    {"model": "rerank-qwen3-8b",   "capability": "rerank",     "group": "retrieval",
     "label": "Qwen3-Reranker-8B (Q8_0)"},
    {"model": "speaches",          "capability": "stt",        "group": "audio",
     "label": "Speaches (faster-whisper)"},
    {"model": "qwen-tts",          "capability": "tts",        "group": "audio",
     "label": "Qwen3-TTS (On-Box)"},
)

# Full GPU-tier weight set is ~34GB (D13's 8B Q8_0 rerank adds ~6.8GB over the old
# 0.6B; design §14); still fits the 40GB gate (tighter headroom). Gate at 40GB free.
DISK_GATE_MB = 40 * 1024

# Download-state contract: the later download milestone's POST /local-models/
# download writes {"<member>": {"state": str, ...}} here; read fail-soft (absent
# file => every member reports "pending"). Module attr so tests repoint it.
DOWNLOAD_STATE_PATH = resolve("Manifest", "local_models", "downloads.json")

# Fail-fast loopback probes (ollama_io GET_TIMEOUT precedent).
GET_TIMEOUT = httpx.Timeout(2.0, connect=2.0)
# httpx.MockTransport injected by tests; None => real network.
_transport: "httpx.BaseTransport | None" = None

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


# ── llama-swap process-liveness (NOT per-member VRAM residency) ───────────────

def llama_swap_health(timeout: "httpx.Timeout | None" = None) -> dict:
    """Probe the llama-swap FRONT DOOR /health. Returns
    {"reachable": bool, "status_code": int|None}. Fail-soft (never raises).
    The front-door /health is up whenever the proxy PROCESS is up, independent
    of which group is resident — so it does not flap on group swaps (this is the
    proxy-level endpoint, distinct from each member's own checkEndpoint)."""
    root = base_url_root()
    try:
        with httpx.Client(timeout=timeout or GET_TIMEOUT, transport=_transport) as client:
            resp = client.get(f"{root}/health")
            return {"reachable": resp.status_code == 200, "status_code": resp.status_code}
    except Exception:
        return {"reachable": False, "status_code": None}


def is_healthy(timeout: "httpx.Timeout | None" = None) -> bool:
    """Installed AND the llama-swap front door is reachable. Keys on install +
    config + process-liveness of llama-swap ITSELF — never live per-member VRAM
    residency (correction [30]). The on-box availability signal for routing.
    Short-circuits with NO probe when not installed."""
    if not is_installed():
        return False
    return llama_swap_health(timeout)["reachable"]


def should_route_onbox(cap: str) -> bool:
    """The shared on-box availability signal for each capability's resolver:
    `cap` is seeded on-box (D2) AND the stack is reachable now. The resolver
    MUST honor an explicit credentialed user pick BEFORE calling this."""
    return enabled(cap) and is_healthy()


def running_members(timeout: "httpx.Timeout | None" = None) -> "list[dict] | None":
    """llama-swap /running -> [{"model": str, "state": str}]. None when the
    proxy is UNREACHABLE (distinct from [] = up but nothing resident). Tolerant
    of both {"running": [...]} and a bare list; drops items without a str
    model; defaults a missing state to "ready"."""
    root = base_url_root()
    try:
        with httpx.Client(timeout=timeout or GET_TIMEOUT, transport=_transport) as client:
            resp = client.get(f"{root}/running")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    items = data.get("running") if isinstance(data, dict) else data
    out = []
    for it in (items or []):
        if isinstance(it, dict) and isinstance(it.get("model"), str):
            out.append({"model": it["model"], "state": str(it.get("state", "ready"))})
    return out


# ── download-state contract (writer = the later download milestone) ───────────

def read_download_state() -> dict:
    """{member_id: {"state": str, ...}} from DOWNLOAD_STATE_PATH; {} fail-soft
    on absent/corrupt/wrong-shape."""
    try:
        data = json.loads(DOWNLOAD_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def model_downloaded(model_id: str) -> bool:
    """True iff `model_id`'s weights are present, per the download-state contract
    (read_download_state / DOWNLOAD_STATE_PATH, written by the download milestone).
    A member counts as downloaded when its recorded state is a terminal success
    ("downloaded" or "done"). Fail-soft: an absent/corrupt state file or an
    unlisted member => False. This is the on-disk-presence signal the localstack
    embeddings/rerank preflight (M3 Task 3.5 / M4) consumes; those tests
    monkeypatch it, but a real installed+healthy box calls THIS implementation, so
    it MUST exist here (missing it => AttributeError -> HTTP 500 on
    GET /embeddings/status)."""
    entry = read_download_state().get(model_id)
    return isinstance(entry, dict) and entry.get("state") in ("downloaded", "done")


# Keep-warm maps to a llama-swap member ttl (§6): 0 = immune to the 10-min idle
# TTL (still yields to a cross-group swap); 600 = the template default (cold).
TTL_WARM = 0
TTL_COLD = 600


def llama_swap_config_path() -> "Path | None":
    """Path to the live llama-swap config.yaml the installer (M2, Step 2f) wrote
    to ~/.blackbox/localstack/llama-swap-config.yaml (the installer's CONFIG_DEST,
    Task 2.5) — or None when that file is absent (stack not installed). Derived
    from the fixed install path rather than a config.ini key: the installer
    writes the config there unconditionally, so nothing needs to declare/write a
    [local_models] config_path key, and keep-warm resolves the REAL generated
    config in production (a getattr(config, "LOCAL_MODELS_CONFIG_PATH", ...) would
    always be None → get/set_member_ttl dead on-box). blackbox.service runs
    ProtectHome=no, so ~ is the real user's home. Distinct from CONFIG_PATH above
    (config.ini) — this resolves the llama-swap config.yaml."""
    p = Path(os.path.expanduser("~/.blackbox/localstack/llama-swap-config.yaml"))
    return p if p.exists() else None


def get_member_ttl(member: str) -> "int | None":
    """The ttl (seconds) configured for a llama-swap member, or None when the
    config is absent/unreadable or the member is missing. 0 == kept warm."""
    path = llama_swap_config_path()
    if path is None:
        return None
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(cfg, dict):
        return None
    model = (cfg.get("models") or {}).get(member)
    if not isinstance(model, dict) or "ttl" not in model:
        return None
    try:
        return int(model["ttl"])
    except (TypeError, ValueError):
        return None


def set_member_ttl(member: str, ttl: int) -> None:
    """Surgically set one member's ttl and atomically rewrite the config.

    WARNING (§6): the service runs with --watch-config, which auto-restarts the
    WHOLE proxy on any edit (unloads every member — there is no in-place reload,
    llama-swap #160/#547). Batch config writes; one keep-warm toggle is one
    write and one brief full-stack reload. Raises if the stack isn't installed or
    the config is unreadable/corrupt (RuntimeError) or the member isn't in the
    config (ValueError)."""
    path = llama_swap_config_path()
    if path is None:
        raise RuntimeError("local stack not installed — no llama-swap config to edit")
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"llama-swap config unreadable at {path}: {exc}") from exc
    models = (cfg or {}).get("models") or {}
    if member not in models or not isinstance(models[member], dict):
        raise ValueError(f"llama-swap config has no member {member!r}")
    models[member]["ttl"] = int(ttl)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ─────────────────────────────────────────────────────────────────────────────
# M5 — on-box STT model ids + Design-B direct-WS Speaches locator.
# (base_url()/is_healthy()/enabled() come from the M1 module.)
# ─────────────────────────────────────────────────────────────────────────────

# llama-swap pins the Speaches member to this STATIC loopback port in the
# generated config (installer/templates/llama-swap-config.yaml.template) so the
# Design-B streaming STT bridge can open a DIRECT WebSocket to it — llama-swap
# WebSocket proxying is a known-missing feature (mostlygeek/llama-swap#754).
SPEACHES_STATIC_PORT = 9099

# On-box whisper model ids served by the Speaches member (§5.3 / §8 template).
# Streaming defaults to the turbo ct2 build (parity with today's gemma-box path);
# batch uses full large-v3 for quality. Wizard-overridable later (Q3/Q6).
ONBOX_STT_STREAM_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"
ONBOX_STT_BATCH_MODEL = "Systran/faster-whisper-large-v3"


def stt_stream_model() -> str:
    return ONBOX_STT_STREAM_MODEL


def stt_batch_model() -> str:
    return ONBOX_STT_BATCH_MODEL


def speaches_warm_url() -> str:
    """llama-swap /upstream passthrough that LOADS the audio group and proxies
    Speaches /health — GET it until 200 to warm the group before a direct-WS
    stream (Design B). Going through :9098 is what triggers the load/evict.
    Reuses base_url_root() (the M1 front-door-root helper) for /v1 stripping."""
    return f"{base_url_root()}/upstream/speaches/health"


def speaches_realtime_ws_url(model: str, *, intent: str = "transcription") -> str:
    """DIRECT ws:// URL to the pinned Speaches member's /v1/realtime endpoint
    (Design B — bypasses the llama-swap proxy, which cannot proxy WebSockets)."""
    return (f"ws://127.0.0.1:{SPEACHES_STATIC_PORT}/v1/realtime"
            f"?model={_quote(model, safe='')}&intent={_quote(intent, safe='')}")
