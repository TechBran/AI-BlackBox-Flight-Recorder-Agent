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

Status is strictly read-only: it must never create store directories or files
as a side effect (probing is cheap and safe to poll).
"""
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from Orchestrator import config
from Orchestrator.embeddings.migrate import (
    get_job_status,
    request_cancel,
    start_migration,
)
from Orchestrator.embeddings.providers import get_provider
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import (
    META_FILE,
    get_active_slug,
    get_store,
    list_stores,
)
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


def _model_preflight(entry: dict) -> tuple[bool, list[str]]:
    """ready/blockers for one registry entry, preflight-style."""
    provider = entry["provider"]
    if provider in _CLOUD_KEY_PREFLIGHT:
        attr, remediation = _CLOUD_KEY_PREFLIGHT[provider]
        if getattr(config, attr, ""):
            return True, []
        return False, [remediation]
    # Ollama stub — Task 10 replaces with installed/running/pulled/RAM checks.
    return False, ["Ollama integration arrives in a later update"]


@router.get("/status")
def embeddings_status():
    # Plain def: nothing here awaits, and FastAPI runs sync routes in the
    # threadpool, so the cold-start index parse never stalls the event loop.
    from Orchestrator.fossils import load_snapshot_index  # lazy: avoid import cycle

    base = Path(config.EMBEDDINGS_STORES_DIR)
    index_ids = set(load_snapshot_index().keys())

    stores = []
    for meta in list_stores(base):
        stores.append({
            "slug": meta["slug"],
            "dims": meta["dims"],
            "count": meta["count"],
            "missing": _safe_missing(meta["slug"], meta["dims"], base, index_ids),
            "last_updated": meta["last_updated"],
        })

    models = []
    for slug, entry in EMBEDDING_MODELS.items():
        store_exists = (base / slug / META_FILE).is_file()
        ready, blockers = _model_preflight(entry)
        models.append({
            "slug": slug,
            "label": entry["label"],
            "dims": entry["dims"],
            "ram_gb": entry["ram_gb"],
            "cost_per_1m_tokens": entry["cost_per_1m_tokens"],
            "privacy": entry["privacy"],
            "quality_note": entry["quality_note"],
            "store_exists": store_exists,
            "missing": (
                _safe_missing(slug, entry["dims"], base, index_ids)
                if store_exists else None
            ),
            "ready": ready,
            "blockers": blockers,
        })

    return {
        "active": get_active_slug(base_dir=base),
        "health": _read_health(base),
        "job": get_job_status(),  # live migration job state; None when idle
        "stores": stores,
        "models": models,
        # Task 10 fills real installed/running/models detection from the daemon.
        "ollama": {"installed": False, "running": False, "models": []},
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


@router.post("/migrate")
async def embeddings_migrate(req: MigrateRequest):
    """Start a diff-and-fill migration to the target model.

    404 unknown slug, 409 when a job is already running (one job at a time),
    otherwise the freshly-claimed job dict (state == "running").
    """
    if req.target not in EMBEDDING_MODELS:
        raise HTTPException(
            status_code=404, detail=f"Unknown embedding model slug: {req.target!r}"
        )
    try:
        return await start_migration(req.target)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/migrate/cancel")
async def embeddings_migrate_cancel():
    """Cooperatively cancel the running migration; false when nothing runs."""
    return {"cancelled": request_cancel()}


@router.post("/health/check")
async def embeddings_health_check():
    """Run the watcher health check now and return the fresh health dict.

    Same body as the daily scheduled run (probe + catalog + gap-heal /
    auto-migrate side effects included); health.json is rewritten before
    this returns, so a following GET /embeddings/status reflects it.
    """
    return await run_health_check()
