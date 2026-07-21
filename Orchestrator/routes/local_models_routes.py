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
from fastapi import APIRouter, Response

from Orchestrator import hardware, local_stack

router = APIRouter(prefix="/local-models", tags=["local-models"])


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
        models.append({
            "model": m["model"],
            "capability": m["capability"],
            "group": m["group"],
            "label": m["label"],
            "running": run is not None,
            "state": run["state"] if run else None,
            "download": dl if isinstance(dl, dict) else {"state": "pending"},
        })

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
