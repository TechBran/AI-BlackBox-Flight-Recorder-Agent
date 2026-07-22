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


def record_download_state(model_id: str, state: str = "downloaded", **extra) -> None:
    """Atomically merge {model_id: {"state": state, **extra}} into
    DOWNLOAD_STATE_PATH (the download-state contract's WRITER). mkdir -p the
    parent, tmp write + os.replace, fail-soft on OSError (logged, never raised):
    a bookkeeping-file write failure must never fail the download itself — the
    weights are on disk regardless, and _member_gguf_present already reports the
    truth. Called at every terminal-success point of a download so future
    downloads DO record state (the pre-existing bug: this file was never written
    in production, so model_downloaded fell back to False for every member)."""
    try:
        current = read_download_state()
        current[model_id] = {"state": state, **extra}
        DOWNLOAD_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DOWNLOAD_STATE_PATH.with_name(DOWNLOAD_STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(current), encoding="utf-8")
        os.replace(tmp, DOWNLOAD_STATE_PATH)
    except OSError as exc:
        logger.warning("local_stack: could not record download state for %s (%s)",
                       model_id, exc)


# Members whose weights are NOT a direct HF-CDN download (absent from
# localstack_downloads.DOWNLOAD_MANIFEST) but STILL land as a single named GGUF
# on disk we can presence-check: the reranker is self-converted from a pinned
# llama.cpp build (§7/§14), not fetched, so it has no manifest entry — map it to
# its on-disk filename here so an installed reranker reads as downloaded.
_SELFCONVERT_GGUF = {"rerank-qwen3-8b": "Qwen3-Reranker-8B-Q8_0.gguf"}


def _member_gguf_present(model_id: str) -> bool:
    """True iff `model_id`'s single on-disk GGUF exists and is non-empty — the
    ON-DISK TRUTH the download-state bookkeeping file only approximates. Members
    whose weights are a single GGUF resolve their filename from the download
    manifest's "file" dest (embeddings) or _SELFCONVERT_GGUF (the self-converted
    reranker); multi-file (qwen-tts hf_snapshot) / auto-pulled (speaches) members
    return False here and fall back to the state file. Never raises."""
    # lazy import to avoid a cycle (localstack_downloads imports only stdlib+httpx)
    from Orchestrator.localstack_downloads import MODELS_DIR, DOWNLOAD_MANIFEST
    entry = DOWNLOAD_MANIFEST.get(model_id)
    name = entry["dest"] if entry and entry.get("kind") == "file" else _SELFCONVERT_GGUF.get(model_id)
    if not name:
        return False  # multi-file (qwen-tts) / auto-pulled (speaches) -> fall back to state file
    try:
        p = MODELS_DIR / name
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def model_downloaded(model_id: str) -> bool:
    """True iff `model_id`'s weights are present — ON-DISK PRESENCE FIRST, the
    download-state bookkeeping file only as a fallback.

    A GGUF physically on disk (_member_gguf_present) counts as downloaded even
    when Manifest/local_models/downloads.json was never written (the pre-existing
    production bug: that file is never written, so a state-file-only check
    reported the ACTIVE, serving qwen3-embedding-8b-local as "not downloaded").
    Multi-file / auto-pulled members with no presence-checkable single GGUF fall
    back to the state-file contract (a recorded terminal success "downloaded" or
    "done"). Fail-soft: absent/corrupt state + no on-disk GGUF => False.

    Consumed by the localstack embeddings/rerank preflight (M3 Task 3.5 / M4);
    those tests monkeypatch it, but a real installed+healthy box calls THIS."""
    if _member_gguf_present(model_id):
        return True
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
# batch uses full large-v3 for quality. These now double as the FAIL-OPEN
# fallback the getters return when the hardware probe can't determine a fit — a
# probe error, or a GPU whose VRAM is unverifiable (an lspci-discovered card):
# the flagship 16 GB pairing, matching hardware.derive_tier's "unverifiable VRAM
# => HIGH" stance. Wizard-overridable via the fresh-read sidecar (below).
ONBOX_STT_STREAM_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"
ONBOX_STT_BATCH_MODEL = "Systran/faster-whisper-large-v3"

# ── GPU-fit "best Whisper" table (M-B Task B1) ────────────────────────────────
# "Best whisper that fits the GPU," auto-selected from the hardware probe's VRAM
# — the STT analogue of rerank.py's RERANK_MODELS + hardware.probe().vram_mb
# tiering. Ordered high→low; the FIRST tier whose min_vram_mb <= probed VRAM
# wins; the final entry (min_vram_mb == 0) is the CPU / small-GPU floor. Within a
# tier's budget streaming favors low latency (the turbo / smaller build) and
# batch favors quality (the larger build). All ids are faster-whisper CT2 repos
# from the same family the Speaches member already serves. Literal discipline:
# whisper repo ids live ONLY here (mirrors the RERANK_MODELS "one home" rule).
WHISPER_FIT = (
    {   # 16 GB+ GPU (RTX 2000 Ada gate) — the flagship pairing == today's constants
        "min_vram_mb": 16000,
        "stream": "deepdml/faster-whisper-large-v3-turbo-ct2",
        "batch":  "Systran/faster-whisper-large-v3",
        "compute_type": "float16",
        "tier": "gpu-16gb",
    },
    {   # 8 GB GPU — turbo stream (int8) stays; batch drops to int8 medium
        "min_vram_mb": 8000,
        "stream": "deepdml/faster-whisper-large-v3-turbo-ct2",
        "batch":  "Systran/faster-whisper-medium",
        "compute_type": "int8",
        "tier": "gpu-8gb",
    },
    {   # CPU / <8 GB — int8 small stream + base batch (the floor)
        "min_vram_mb": 0,
        "stream": "Systran/faster-whisper-small",
        "batch":  "Systran/faster-whisper-base",
        "compute_type": "int8",
        "tier": "cpu",
    },
)

# Fresh-read sidecar (mirrors rerank.json's discipline, custom_servers.py E8
# lesson): a FUTURE wizard writes the resolved {"stream": id, "batch": id} here
# and the getters pick it up with NO restart. Absent today — the selection is
# derived purely from the probe. Module attr so tests repoint it.
WHISPER_FIT_SIDECAR_PATH = resolve("Manifest", "local_models", "whisper_fit.json")


def read_whisper_fit_sidecar() -> dict:
    """{"stream": id, "batch": id} from WHISPER_FIT_SIDECAR_PATH; {} fail-soft on
    absent/corrupt/wrong-shape (mirrors read_download_state). FRESH read every
    call — a wizard-written override applies with no restart, no cache."""
    try:
        data = json.loads(WHISPER_FIT_SIDECAR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_whisper_fit() -> "dict | None":
    """Best-fit WHISPER_FIT tier for THIS box from the hardware probe's VRAM, or
    None when the probe can't determine a fit (a probe error, or a GPU whose VRAM
    is unverifiable — an lspci-discovered card) so the getters fall back to
    today's flagship constants. A CPU-only box (no GPU) resolves the int8 floor
    tier — never the GPU flagship. Never raises; the probe itself is fail-soft
    and 60 s-cached (hardware.probe)."""
    try:
        from Orchestrator import hardware  # stdlib-only import; cycle-proof
        hw = hardware.probe()
    except Exception:
        return None
    if not hw.get("gpu"):
        # CPU-only — the int8 floor tier (best that fits with no GPU).
        return WHISPER_FIT[-1]
    vram = hw.get("vram_mb")
    if not isinstance(vram, int):
        # GPU present but VRAM unverifiable (lspci) — defer to the flagship
        # constants (matches derive_tier's "unverifiable VRAM => HIGH").
        return None
    for tier in WHISPER_FIT:
        if vram >= tier["min_vram_mb"]:
            return tier
    return None


def _resolve_stt_id(kind: str) -> str:
    """Resolve the on-box whisper `kind` ("stream" | "batch") id, FRESH + LIVE:
      1. the fresh-read sidecar (future wizard override, per-kind),
      2. the hardware-probe best-fit WHISPER_FIT tier,
      3. today's flagship constant (probe failed / VRAM unverifiable).
    Never raises."""
    const = ONBOX_STT_STREAM_MODEL if kind == "stream" else ONBOX_STT_BATCH_MODEL
    side = read_whisper_fit_sidecar().get(kind)
    if isinstance(side, str) and side.strip():
        return side.strip()
    fit = resolve_whisper_fit()
    if isinstance(fit, dict) and isinstance(fit.get(kind), str):
        return fit[kind]
    return const


def stt_stream_model() -> str:
    """The on-box streaming (realtime) whisper repo id, GPU-fit + sidecar-aware.
    Only ever called on the on-box STT path (gated by onbox_stt_available()); when
    the stack is off no live consumer invokes it, so cloud STT is unaffected."""
    return _resolve_stt_id("stream")


def stt_batch_model() -> str:
    """The on-box file/batch whisper repo id, GPU-fit + sidecar-aware. Only ever
    called on the on-box STT path (gated by onbox_stt_available())."""
    return _resolve_stt_id("batch")


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


# ─────────────────────────────────────────────────────────────────────────────
# M5 — D12 Orchestrator-level voice/retrieval serialization primitive.
#
# A direct-to-port on-box voice stream (Design-B STT WS to :9099, or streaming
# Qwen TTS) is INVISIBLE to llama-swap's in-flight drain counter, so llama-swap
# would evict the audio group mid-utterance to serve a retrieval-group request.
# We serialize at the Orchestrator instead: while ANY on-box voice stream is
# open, retrieval-group dispatch (localstack embeddings/rerank -> :9098) WAITS
# behind it, sequencing retrieval into the STT-finalize -> retrieve -> TTS-speak
# gap of a voice turn (§6).
#
# Deliberately poll-based on a plain int, NOT an asyncio.Event: voice_session()
# runs on the FastAPI loop while auto-mint embeds run via search._run_async
# (which may drive a coroutine on a DIFFERENT loop/thread). A module-level
# asyncio.Event would raise "bound to a different event loop"; a GIL-atomic int
# read is loop- and thread-agnostic. Single active user (personal box), so this
# is cooperative sequencing, not a hard mutex.
# ─────────────────────────────────────────────────────────────────────────────
import asyncio as _asyncio
import time as _time
from contextlib import asynccontextmanager as _asynccontextmanager
from contextlib import contextmanager as _contextmanager

_voice_depth = 0

# Bounded wait a retrieval caller tolerates before giving up on the gate. The
# auto-mint embed path converts a timeout into a vector-less mint; a query embed
# falls back to keyword search; rerank falls through to un-reranked retrieval.
RETRIEVAL_GATE_TIMEOUT_S = 8.0
_GATE_POLL_S = 0.05


def is_voice_active() -> bool:
    """True while >=1 on-box voice stream (STT bridge or streaming TTS) is open."""
    return _voice_depth > 0


@_asynccontextmanager
async def voice_session():
    """Hold for the FULL duration of an on-box voice stream. While held,
    retrieval_gate() callers wait. Re-entrant via a depth counter (a duplex voice
    turn may briefly overlap listen+speak)."""
    global _voice_depth
    _voice_depth += 1
    try:
        yield
    finally:
        _voice_depth -= 1
        if _voice_depth < 0:
            _voice_depth = 0


@_asynccontextmanager
async def retrieval_gate(*, timeout: float | None = RETRIEVAL_GATE_TIMEOUT_S):
    """Await until no on-box voice stream is open, then yield. Wrap every
    localstack retrieval-group dispatch (:9098 embeddings/rerank) in this.

    timeout=None  -> wait indefinitely.
    timeout=<s>   -> raise asyncio.TimeoutError once the ceiling passes, so the
                     caller degrades (vector-less mint / keyword fallback /
                     un-reranked) rather than deadlock behind a long voice call.
    """
    deadline = None if timeout is None else _time.monotonic() + timeout
    while is_voice_active():
        if deadline is not None and _time.monotonic() >= deadline:
            raise _asyncio.TimeoutError(
                "on-box voice session held the retrieval group past the gate timeout")
        await _asyncio.sleep(_GATE_POLL_S)
    yield


@_contextmanager
def retrieval_gate_sync(*, timeout: float | None = RETRIEVAL_GATE_TIMEOUT_S):
    """Blocking sibling of retrieval_gate() for SYNC retrieval-group dispatch.

    The production reranker entry — Orchestrator/rerank.py score() — is a plain
    synchronous function that runs in the FastAPI threadpool over a blocking
    requests.post, so it cannot `await` the async gate. This blocks that worker
    thread instead, polling the SAME GIL-atomic _voice_depth with time.sleep
    (loop- and thread-agnostic, exactly the reason the counter is a plain int and
    not an asyncio.Event). Sleeping here parks only the retrieval worker thread,
    never the FastAPI event loop where voice_session() runs.

    timeout=None  -> wait indefinitely.
    timeout=<s>   -> raise asyncio.TimeoutError once the ceiling passes, so the
                     sync caller degrades (un-reranked) rather than block a worker
                     thread behind a long voice call.
    """
    deadline = None if timeout is None else _time.monotonic() + timeout
    while is_voice_active():
        if deadline is not None and _time.monotonic() >= deadline:
            raise _asyncio.TimeoutError(
                "on-box voice session held the retrieval group past the gate timeout")
        _time.sleep(_GATE_POLL_S)
    yield
