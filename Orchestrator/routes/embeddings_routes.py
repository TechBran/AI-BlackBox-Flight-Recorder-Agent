"""Embeddings management endpoints (Tasks 7-8).

GET  /embeddings/status         — full module state: active model, watcher
                                  health, migration job, on-disk stores,
                                  registry models with preflight readiness,
                                  ollama daemon state. The JSON shape is a
                                  BINDING contract consumed by the onboarding
                                  wizard step, the Portal updates card and the
                                  Android updates card (Tasks 13-15).
POST /embeddings/validate       — probe-embed one short string with a model's
                                  provider before the wizard commits to it.
POST /embeddings/migrate        — start the diff-and-fill migration job
                                  (404 unknown slug, 409 if one is running).
POST /embeddings/migrate/cancel — cooperative cancel of the running job.
POST /embeddings/health/check   — run the Task 9 watcher health check now
                                  (manual trigger for ops/tests/the wizard).
POST /embeddings/ollama/pull    — pull a local model's weights from the Ollama
                                  library (registry slug in, 409 if a pull is
                                  already streaming; progress surfaces in
                                  status as `ollama.pull`).
POST /embeddings/placement      — set/clear a LOCAL model's device placement
                                  ("gpu"/"cpu"/null=auto, WI-9); applies on
                                  the next embed call, no restart.

Status is strictly read-only: it must never create store directories or files
as a side effect (probing is cheap and safe to poll).
"""
import asyncio
import json
import math
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from Orchestrator import config, hardware
from Orchestrator.embeddings import ollama_io
from Orchestrator.embeddings.migrate import (
    get_job_status,
    request_cancel,
    start_migration,
    start_rebuild,
    start_reembed,
)
from Orchestrator.embeddings.providers import get_provider
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import (
    META_FILE,
    PLACEMENTS,
    get_active_slug,
    get_keep_alive,
    get_placement,
    get_store,
    list_stores,
    set_keep_alive,
    set_placement,
)
from Orchestrator.embeddings.store import is_warm as store_is_warm
from Orchestrator.embeddings.watcher import run_health_check

VALIDATE_TIMEOUT_S = 15.0  # wizard-click probe cap; see review note in /validate

router = APIRouter(prefix="/embeddings", tags=["embeddings"])

HEALTH_FILE = "health.json"  # written by the Task 9 watcher; we only read it
_DEFAULT_HEALTH = {
    "state": "ok", "detail": "", "successor": None, "successor_slug": None,
}

# Cloud-provider preflight: config attribute that must be truthy + the
# customer-facing remediation string shown when it isn't (/cu/preflight style).
_CLOUD_KEY_PREFLIGHT = {
    "gemini": ("GOOGLE_API_KEY", "Add a Google API key in onboarding → API Keys"),
    "openai": ("OPENAI_API_KEY", "Add an OpenAI API key in onboarding → API Keys"),
}


def _read_health(base: Path) -> dict:
    """Watcher health state from {base}/health.json; absent/corrupt → ok."""
    try:
        raw = json.loads((base / HEALTH_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return dict(_DEFAULT_HEALTH)
    if not isinstance(raw, dict):
        return dict(_DEFAULT_HEALTH)
    return {
        "state": raw.get("state", "ok"),
        "detail": raw.get("detail", ""),
        "successor": raw.get("successor"),             # display-only string
        "successor_slug": raw.get("successor_slug"),   # registry slug or None
    }


def _safe_missing(slug: str, dims: int, base: Path, index_ids: set) -> int | None:
    """len(index ids - store ids) for an EXISTING store dir; None if the store
    cannot be opened (corrupt meta, dims drift) — status must never 500 over
    one bad store directory."""
    try:
        store = get_store(slug, dims=dims, base_dir=base)
        return len(index_ids - store.ids())
    except Exception as e:
        print(f"[EMBEDDINGS] status: cannot open store {slug!r}: {e}")
        return None


def _ollama_state() -> dict:
    """The status `ollama` block, probed exactly ONCE per status call (it also
    feeds every local model's preflight). The daemon GETs are sync — the
    status route is a plain `def` running in the threadpool, so no event-loop
    bridge is needed (decision documented in ollama_io's module docstring)."""
    running = ollama_io.daemon_version() is not None
    return {
        "installed": ollama_io.binary_installed(),
        "running": running,
        "models": ollama_io.local_models() if running else [],
        "pull": ollama_io.pull_status(),
    }


# VRAM headroom the GPU recommendation demands beyond the model's resident
# estimate (registry ram_gb ≈ quantized weights + ctx buffers): KV growth,
# display server, a co-resident reranker's slice (WI-4). Below this margin the
# CPU path is recommended — never blocked (audit WI-9: CPU is never a dead end).
GPU_HEADROOM_MB = 1024


def _recommended_placement(entry: dict, hw: dict) -> str:
    """"gpu" when the probed GPU fits entry's resident estimate with >=1GB
    headroom, else "cpu". GPU present but VRAM unknown (lspci-only probe)
    reads as doesn't-fit: an unverifiable fit is recommended against, but the
    user can still pin "gpu" explicitly via the placement toggle."""
    if not hw.get("gpu") or not hw.get("vram_mb"):
        return "cpu"
    needed_mb = int(entry["ram_gb"] * 1024) + GPU_HEADROOM_MB
    return "gpu" if hw["vram_mb"] >= needed_mb else "cpu"


# Advisory-only CPU re-embed rate: seconds/snapshot ≈ a local model's RAM
# footprint (registry ram_gb, which proxies parameter count) × this factor.
# Deriving from ram_gb — never a slug literal (guard test) — means the heavy
# 6 GB model estimates ~6x the light 1 GB one for the same corpus, so a no-GPU
# user sees that the "max quality" local model is the slow one. A coarse "~"
# ballpark, NOT a benchmark; a "don't kick off an hours-long re-embed
# unknowingly" nudge. It NEVER gates `ready` and never enters blockers[]
# (audit WI-9: CPU is never a dead end).
_CPU_SECONDS_PER_GB_SNAPSHOT = 0.35


def _fmt_cpu_duration(total_seconds: float) -> str:
    """A clearly-approximate '~N min' / '~N.N hr' duration string."""
    minutes = max(1, math.ceil(total_seconds / 60.0))
    if minutes < 90:
        return f"~{minutes} min"
    return f"~{total_seconds / 3600.0:.1f} hr"


def _cpu_warning(entry: dict, hw: dict, snapshot_count: int) -> "str | None":
    """ADVISORY re-embed-slowness note for a LOCAL model on a no-GPU box.

    Advisory ONLY — never a blocker, never affects `ready` (audit WI-9: CPU is
    never a dead end; a no-GPU user can still select the heavy local model when
    RAM fits, they just see this). ram_preflight (ollama_io) stays the hard
    fit-check for genuinely-insufficient RAM; this is the softer slowness signal
    on top.

    None for cloud models (ram_gb 0 — fine everywhere) and for any box with a
    GPU (it embeds fine). Otherwise a corpus-scaled, clearly-approximate estimate
    plus tier-tuned steering: LOW (no GPU + limited RAM) steers to a cloud model;
    MID (>=32 GB RAM) notes local is viable, just slow.
    """
    if entry["provider"] != "ollama":  # cloud model — no CPU concern
        return None
    if hw.get("gpu"):                  # a GPU box embeds fine
        return None
    rate = entry["ram_gb"] * _CPU_SECONDS_PER_GB_SNAPSHOT
    est = _fmt_cpu_duration(snapshot_count * rate)
    msg = (
        f"~{snapshot_count:,} snapshots would re-embed on CPU ({est}) if you "
        f"switch to this local model; a cloud model switches instantly."
    )
    tier = hw.get("tier")
    if tier == "LOW":
        msg += (
            " No GPU and limited RAM on this box — a cloud embedding model "
            "(Gemini/OpenAI) is recommended, and it's fast on any box."
        )
    elif tier == "MID":
        msg += (
            " This box has the RAM to run it locally; expect a slow one-time "
            "re-embed on CPU."
        )
    return msg


def _model_preflight(
    entry: dict, ollama: dict, hw: dict
) -> tuple[bool, list[str], "str | None"]:
    """(ready, blockers, recommended_placement) for one registry entry,
    preflight-style.

    Ollama blockers, in order — install/start are mutually exclusive (first
    failing wins between them), everything else applicable is appended so the
    wizard can show the full punch list:
    1. binary missing AND daemon unreachable → install one-liner
    2. daemon unreachable (binary present)   → start one-liner
       (model-pulled state is unknowable while the daemon is down, so no
       speculative pull blocker stacks on top of install/start)
    3. model not pulled → wizard shows its Pull button on this blocker;
       ram_gb doubles as the download-size estimate (a quantized model file
       ≈ its RAM footprint; the registry has no separate size field)
    4. free RAM short → ollama_io.ram_preflight remediation string

    recommended_placement (WI-9) is ADVISORY only — hardware never adds a
    blocker (no GPU / short VRAM ⇒ the CPU path is offered, exactly today's
    behavior). None for cloud models (placement is a local concept).
    """
    provider = entry["provider"]
    if provider in _CLOUD_KEY_PREFLIGHT:
        attr, remediation = _CLOUD_KEY_PREFLIGHT[provider]
        if getattr(config, attr, ""):
            return True, [], None
        return False, [remediation], None

    blockers: list[str] = []
    if not ollama["running"]:
        if ollama["installed"]:
            blockers.append("Start it: sudo systemctl start ollama")
        else:
            blockers.append(
                "Install Ollama: curl -fsSL https://ollama.com/install.sh | sh"
            )
    elif entry["model_id"] not in ollama["models"]:
        blockers.append(
            f"Pull the model from the setup wizard "
            f"(≈{entry['ram_gb']:g} GB download)"
        )
    ram_blocker = ollama_io.ram_preflight(entry["ram_gb"])
    if ram_blocker is not None:
        blockers.append(ram_blocker)
    return (not blockers), blockers, _recommended_placement(entry, hw)


@router.get("/status")
def embeddings_status(response: Response):
    # no-store: health/successor state flips on migrate + model registration, and
    # a WebView heuristically caching this response keeps drawing a stale
    # "upgrade available" banner after the upgrade already completed. The dict
    # return is unaffected — FastAPI merges this injected Response's headers.
    response.headers["Cache-Control"] = "no-store"
    # Plain def: nothing here awaits, and FastAPI runs sync routes in the
    # threadpool, so the cold-start index parse never stalls the event loop.
    from Orchestrator.fossils import load_snapshot_index  # lazy: avoid import cycle

    base = Path(config.EMBEDDINGS_STORES_DIR)
    index_ids = set(load_snapshot_index().keys())
    snapshot_count = len(index_ids)  # corpus size for the M9 cpu_warning estimate
    ollama_state = _ollama_state()
    hw = hardware.probe()  # 60s TTL cache — safe under the wizard's 2s poll

    stores = []
    for meta in list_stores(base):
        stores.append({
            "slug": meta["slug"],
            "dims": meta["dims"],
            # count stays SNAPSHOT currency on every schema (binding contract,
            # audit A11); schema/rows are ADDITIVE (M6e): schema 1 = one row
            # per snapshot (rows == count), schema 2 = chunked (rows >= count).
            "count": meta["count"],
            "schema": meta["schema"],
            "rows": meta["rows"],
            "missing": _safe_missing(meta["slug"], meta["dims"], base, index_ids),
            "last_updated": meta["last_updated"],
        })
    stores_by_slug = {s["slug"]: s for s in stores}

    models = []
    for slug, entry in EMBEDDING_MODELS.items():
        store_exists = (base / slug / META_FILE).is_file()
        ready, blockers, recommended = _model_preflight(entry, ollama_state, hw)
        # keep_alive + placement toggles are local-only (Ollama); null for cloud
        is_local = entry["provider"] == "ollama"
        keep_alive = get_keep_alive(slug, base_dir=base) if is_local else None
        # ADDITIVE (M6e): the model card mirrors its store's schema/rows
        # (same currency as the stores[] entries); null when no readable
        # store exists — matching the `missing` null convention.
        smeta = stores_by_slug.get(slug)
        schema = smeta["schema"] if smeta else None
        models.append({
            "slug": slug,
            "label": entry["label"],
            "dims": entry["dims"],
            "ram_gb": entry["ram_gb"],
            "cost_per_1m_tokens": entry["cost_per_1m_tokens"],
            "privacy": entry["privacy"],
            "quality_note": entry["quality_note"],
            "store_exists": store_exists,
            "schema": schema,
            # ADDITIVE (re-embed UI): human label for the store's embedding
            # strategy, derived from the SAME schema reported above. schema 2 =
            # chunked, schema 1 = whole_document, no readable store = none.
            "strategy": (
                "chunked" if schema == 2
                else ("whole_document" if schema == 1 else "none")
            ),
            "rows": smeta["rows"] if smeta else None,
            "missing": (
                _safe_missing(slug, entry["dims"], base, index_ids)
                if store_exists else None
            ),
            "ready": ready,
            "blockers": blockers,
            "keep_alive": keep_alive,
            "warm": store_is_warm(keep_alive) if is_local else None,
            # ADDITIVE (WI-9/M10): device placement. placement = the persisted
            # per-box override ("gpu"/"cpu", null = auto — Ollama decides);
            # recommended_placement = the hardware-probe advisory. Both null
            # for cloud models, mirroring keep_alive.
            "placement": get_placement(slug, base_dir=base) if is_local else None,
            "recommended_placement": recommended,
            # ADDITIVE (reranker-tiering M9): advisory CPU re-embed-slowness
            # note for a LOCAL model on a no-GPU box + LOW-tier cloud steering.
            # Never a blocker, never affects `ready` (CPU is never a dead end);
            # null for cloud models and any GPU box.
            "cpu_warning": _cpu_warning(entry, hw, snapshot_count),
        })

    return {
        "active": get_active_slug(base_dir=base),
        "health": _read_health(base),
        "job": get_job_status(),  # live migration job state; None when idle
        "stores": stores,
        "models": models,
        "ollama": ollama_state,
        # ADDITIVE (WI-9/M10): the host hardware probe, verbatim —
        # {gpu, gpu_name, vram_mb, ram_mb, source}.
        "hardware": hw,
    }


class ValidateRequest(BaseModel):
    slug: str


@router.post("/validate")
async def embeddings_validate(req: ValidateRequest):
    """Probe-embed one short string with the slug's provider.

    Provider failure (bad key, daemon down, network) is an EXPECTED outcome:
    always HTTP 200 with ok=false, never a 500. Only an unknown slug is a 404.
    """
    if req.slug not in EMBEDDING_MODELS:
        raise HTTPException(
            status_code=404, detail=f"Unknown embedding model slug: {req.slug!r}"
        )
    try:
        provider = get_provider(req.slug)
        # Hard 15s cap: a cold/wedged local daemon otherwise holds the wizard's
        # "use this model" click for the provider's full retry envelope (~8 min).
        vectors = await asyncio.wait_for(
            provider.embed(["probe"], "document"), timeout=VALIDATE_TIMEOUT_S
        )
        return {"ok": True, "dims": len(vectors[0])}
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": (
                f"Validation timed out after {VALIDATE_TIMEOUT_S:.0f}s - provider "
                "unreachable or model still loading; try again"
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


class MigrateRequest(BaseModel):
    target: str
    rebuild: bool = False  # M6d: true = BUILD-ONLY chunk-store candidate


@router.post("/migrate")
async def embeddings_migrate(req: MigrateRequest):
    """Start a diff-and-fill migration to the target model.

    404 unknown slug, 409 when a job is already running (one job at a time),
    otherwise the freshly-claimed job dict (state == "running").

    rebuild=true (additive, M6d) starts the BUILD-ONLY chunk rebuild instead:
    a schema-2 candidate under {stores}/_build/{target}, NO cutover — the
    service keeps serving (and minting into) the active store while it runs;
    activation is the separate explicit M6f dir-swap. Plain calls (no
    rebuild field) behave exactly as before.
    """
    if req.target not in EMBEDDING_MODELS:
        raise HTTPException(
            status_code=404, detail=f"Unknown embedding model slug: {req.target!r}"
        )
    try:
        if req.rebuild:
            return await start_rebuild(req.target)
        return await start_migration(req.target)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


class ReembedRequest(BaseModel):
    target: str


@router.post("/reembed")
async def embeddings_reembed(req: ReembedRequest):
    """Full re-embed of target's store under the current chunk strategy, then
    activate it in-service (candidate build -> atomic dir-swap -> live). 404
    unknown slug, 409 when a job is already running; else the running job dict."""
    if req.target not in EMBEDDING_MODELS:
        raise HTTPException(status_code=404,
                            detail=f"Unknown embedding model slug: {req.target!r}")
    try:
        return await start_reembed(req.target)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/migrate/cancel")
async def embeddings_migrate_cancel():
    """Cooperatively cancel the running migration; false when nothing runs."""
    return {"cancelled": request_cancel()}


class OllamaPullRequest(BaseModel):
    model: str  # REGISTRY SLUG — same currency as validate/migrate


@router.post("/ollama/pull")
async def embeddings_ollama_pull(req: OllamaPullRequest):
    """Pull a local model's weights from the Ollama library.

    The body carries a REGISTRY SLUG (consistency with every other endpoint);
    the raw ollama model id is resolved here. 404 unknown slug, 400 for a
    non-ollama slug (nothing to pull), 409 when a pull is already streaming,
    otherwise the freshly-claimed pull state — progress is then polled via
    GET /embeddings/status (`ollama.pull`).
    """
    entry = EMBEDDING_MODELS.get(req.model)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown embedding model slug: {req.model!r}"
        )
    if entry["provider"] != "ollama":
        raise HTTPException(
            status_code=400,
            detail=f"{req.model!r} is not an Ollama model; nothing to pull",
        )
    try:
        return await ollama_io.start_pull(entry["model_id"])
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/health/check")
async def embeddings_health_check():
    """Run the watcher health check now and return the fresh health dict.

    Same body as the daily scheduled run (probe + catalog + gap-heal /
    auto-migrate side effects included); health.json is rewritten before
    this returns, so a following GET /embeddings/status reflects it.
    """
    return await run_health_check()


# Strong ref so a fire-and-forget warm-up task isn't GC'd mid-load (the
# weak-task-ref scar shared with migrate/watcher/ollama pull).
_WARMUP_TASK = None


def _log_warmup_outcome(task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"[EMBEDDINGS] keep_alive warm-up embed failed (non-fatal): {exc}")


async def _warmup_model(slug: str):
    # One tiny embed to force Ollama to load the model now, so 'warm' is true
    # immediately rather than on the next mint. Best-effort: a failure here
    # never affects the toggle (the keep_alive override is already written).
    try:
        await get_provider(slug).embed(["warmup"], "document")
    except Exception as e:  # noqa: BLE001 — best-effort, logged not raised
        print(f"[EMBEDDINGS] keep_alive warm-up for {slug!r} failed (non-fatal): {e}")


class KeepAliveRequest(BaseModel):
    slug: str   # REGISTRY SLUG (local/Ollama model)
    warm: bool  # True = pin resident in RAM; False = unload when idle


@router.post("/keep_alive")
async def embeddings_keep_alive(req: KeepAliveRequest):
    """Set a LOCAL model's keep_alive policy (the wizard's warm/cold toggle).

    warm=True pins the model in RAM for instant embeds (costs ram_gb); warm=False
    frees the RAM and reloads on demand. 404 unknown slug, 400 for a cloud model
    (no keep_alive). On warm=True a best-effort background warm-up embed loads
    the model now; the override itself takes effect on the next embed regardless.
    """
    entry = EMBEDDING_MODELS.get(req.slug)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown embedding model slug: {req.slug!r}"
        )
    if entry["provider"] != "ollama":
        raise HTTPException(
            status_code=400,
            detail=f"{req.slug!r} is a cloud model; keep_alive is Ollama-only",
        )
    value = set_keep_alive(req.slug, req.warm)
    if req.warm:
        global _WARMUP_TASK
        _WARMUP_TASK = asyncio.create_task(_warmup_model(req.slug))
        _WARMUP_TASK.add_done_callback(_log_warmup_outcome)
    return {"slug": req.slug, "warm": req.warm, "keep_alive": value}


class PlacementRequest(BaseModel):
    slug: str                     # REGISTRY SLUG (local/Ollama model)
    placement: str | None = None  # "gpu" | "cpu" | null = auto (Ollama decides)


@router.post("/placement")
async def embeddings_placement(req: PlacementRequest):
    """Set (or clear, placement=null) a LOCAL model's device placement (WI-9).

    "cpu" pins the model off the GPU (the provider sends options.num_gpu: 0);
    "gpu"/null leave offload to Ollama (num_gpu omitted). Takes effect on the
    model's NEXT embed call, no restart: the provider reads placement.json
    fresh per call, and Ollama reloads an already-loaded model when its
    options change. 404 unknown slug, 400 for a cloud model or a value outside
    {"gpu", "cpu", null}.
    """
    entry = EMBEDDING_MODELS.get(req.slug)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown embedding model slug: {req.slug!r}"
        )
    if entry["provider"] != "ollama":
        raise HTTPException(
            status_code=400,
            detail=f"{req.slug!r} is a cloud model; placement is Ollama-only",
        )
    if req.placement is not None and req.placement not in PLACEMENTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"placement must be one of {list(PLACEMENTS)} or null (auto), "
                f"got {req.placement!r}"
            ),
        )
    value = set_placement(req.slug, req.placement)
    return {"slug": req.slug, "placement": value}
