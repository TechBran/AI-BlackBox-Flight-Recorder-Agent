"""On-box local model stack status endpoint (M1).

GET /local-models/status — aggregates llama-swap /health + /running, the host
hardware tier, disk headroom, per-model download state, and the per-capability
on-box routing decision. The JSON shape is an ADDITIVE binding contract (mirrors
GET /embeddings/status conventions) consumed by the local_models wizard step and
the Updates panels (status-only). Read-only: never mutates state.

Later capability milestones enrich routing[cap] ADDITIVELY (explicit-user-pick +
cloud-fallback target); M1 reports the on-box view. Plain `def` — the httpx
probes are sync and FastAPI runs sync routes in the threadpool (embeddings/status
precedent), so the front-door probes never stall the event loop.
"""
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from Orchestrator import hardware, local_stack
from Orchestrator import localstack_downloads as _dl

router = APIRouter(prefix="/local-models", tags=["local-models"])


def _artifact_row(key: str) -> dict:
    """One per-artifact download child (A4) for the audio members. `downloadable`
    is False while `repo_pending_g3` (placeholder repo ids pinned on MS02 at
    G3/G4) so the wizard renders a disabled button with a reason instead of a
    live 404 button; the raw flag is surfaced too. `downloaded` is the canonical
    model_downloaded() truth (state-file for these multi-file artifacts)."""
    entry = _dl.DOWNLOAD_MANIFEST[key]
    pending = bool(entry.get("repo_pending_g3"))
    return {
        "key": key,
        "label": entry.get("label", key),
        "downloadable": (key in _dl.DOWNLOAD_MANIFEST) and not pending,
        "downloaded": local_stack.model_downloaded(key),
        "size_gb": entry.get("approx_gb"),
        "repo_pending_g3": pending,
    }


def _routing_decision(cap: str, healthy: bool) -> dict:
    """On-box routing view for one capability. `seeded` = the wizard-time D2
    default (local_stack.enabled). decision: "on-box" (seeded + reachable),
    "unhealthy" (seeded but the stack is down -> per-capability degradation),
    or "off" (not seeded -> an explicit pick / cloud owns it)."""
    seeded = local_stack.enabled(cap)
    if not seeded:
        decision = "off"
    elif healthy:
        decision = "on-box"
    else:
        decision = "unhealthy"
    return {"enabled": seeded, "healthy": healthy, "decision": decision}


@router.get("/status")
def local_models_status(response: Response):
    # no-store: routing/health/download state flips on wizard activation and
    # service up/down; a WebView caching this would draw a stale panel.
    response.headers["Cache-Control"] = "no-store"

    installed = local_stack.is_installed()
    health = local_stack.llama_swap_health()
    healthy = installed and health["reachable"]
    running = local_stack.running_members()
    running_by_id = {r["model"]: r for r in (running or [])}
    downloads = local_stack.read_download_state()
    hw = hardware.probe()

    free_mb = hardware.disk_free_mb()
    disk = {
        "free_mb": free_mb,
        "required_mb": local_stack.DISK_GATE_MB,
        "ok": (free_mb >= local_stack.DISK_GATE_MB) if free_mb is not None else None,
    }

    models = []
    for m in local_stack.MEMBERS:
        run = running_by_id.get(m["model"])
        dl = downloads.get(m["model"])
        row = {
            "model": m["model"],
            "capability": m["capability"],
            "group": m["group"],
            "label": m["label"],
            "running": run is not None,
            "state": run["state"] if run else None,
            "download": dl if isinstance(dl, dict) else {"state": "pending"},
            # True only when POST /local-models/download can fetch this member —
            # i.e. its id is a DOWNLOAD_MANIFEST key. rerank-qwen3-8b (self-
            # converted at install) is deliberately NOT in the manifest, so the
            # wizard must not offer a member-level Download button. (Per-artifact
            # audio children are enumerated in `artifacts` below.)
            "downloadable": m["model"] in _dl.DOWNLOAD_MANIFEST,
        }
        # Per-artifact children (A4): the 3 Qwen variants under qwen-tts + whisper
        # under speaches, each its own download button in the wizard audio
        # section. INERT WHEN OFF: only populated when the stack is installed
        # (no [local_models] section -> is_installed() False -> empty list), so a
        # dev box with the stack off emits no artifact rows.
        child_keys = _dl.MEMBER_ARTIFACTS.get(m["model"])
        if child_keys is not None:
            row["artifacts"] = (
                [_artifact_row(k) for k in child_keys] if installed else []
            )
        models.append(row)

    routing = {cap: _routing_decision(cap, healthy) for cap in local_stack.CAPABILITIES}

    return {
        "installed": installed,
        "enabled": local_stack.master_enabled(),
        "healthy": healthy,
        "base_url": local_stack.base_url(),
        "hardware": hw,               # verbatim probe() shape, incl. tier
        "disk": disk,
        "llama_swap": {
            "reachable": health["reachable"],
            "health_status": health["status_code"],
            "running": running,       # null when the proxy is unreachable
        },
        "models": models,
        "routing": routing,
    }


class LocalModelDownloadRequest(BaseModel):
    artifact: str  # key into localstack_downloads.DOWNLOAD_MANIFEST


@router.post("/download")
async def local_models_download(req: LocalModelDownloadRequest):
    """Stream an on-box model weight download from the HF CDN as NDJSON
    progress lines. 404 unknown artifact, 507 when <40GB free, 409 when a
    download is already running, else a streaming NDJSON body (poll
    GET /local-models/status for the same state out-of-band). Cloned from
    POST /embeddings/ollama/pull's singleton pattern."""
    if req.artifact not in _dl.DOWNLOAD_MANIFEST:
        raise HTTPException(status_code=404, detail=f"Unknown artifact: {req.artifact!r}")
    # The ONE shared M1 probe (Task 1.2), in MB; gate against the same 40 GB
    # threshold the status endpoint reports (MIN_FREE_GB * 1024 MB).
    free_mb = hardware.disk_free_mb()
    if free_mb is not None and free_mb < _dl.MIN_FREE_GB * 1024:
        raise HTTPException(
            status_code=507,
            detail=(f"Need >= {_dl.MIN_FREE_GB:g} GB free to download model weights; "
                    f"only {free_mb / 1024:.0f} GB available. Free up disk and retry."),
        )
    try:
        stream = _dl.start_download(req.artifact)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return StreamingResponse(stream, media_type="application/x-ndjson")
