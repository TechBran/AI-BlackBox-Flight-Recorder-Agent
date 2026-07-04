"""Reranker ops surface (M11/WI-4, audit A9).

GET /rerank/status — [retrieval] rerank_enabled flag, resolved [rerank]
provider config, the one-time latency-preflight result, and (M13, additive)
`gpu` + `service_reachable` for the onboarding wizard's reranker block.
ADDITIVE contract in the /embeddings/status style, for the M10-style
wizard/Portal/Android ops cards. Read-only; on the fresh-box/null-provider
default the only I/O is the TTL-cached ~1s-capped reachability probe of the
default provider URL (+ the 60s-cached hardware probe); with a configured
provider it also triggers the same once-per-process preflight retrieve()
would — safe to poll (every probe result is cached).

POST /rerank/select — the wizard/Portal write surface (M8): persist a reranker
selection (provider+model+enabled, optional api_key) so a choice takes effect
with NO restart or config.ini edit. Loopback-only by design — same trust model
as POST /embeddings/placement, /onboarding/credentials, and config reveal: it
writes a secret to .env, so it must never be exposed off-box.
"""
import os

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from Orchestrator import hardware, rerank
from Orchestrator.embeddings import store
from Orchestrator.onboarding import secrets_writer

router = APIRouter(prefix="/rerank", tags=["rerank"])


@router.get("/status")
def rerank_status(response: Response):
    # no-store: enabled/preflight state flips on config + GPU install; a
    # cached "disabled" card after activation would mislead the operator.
    response.headers["Cache-Control"] = "no-store"
    return rerank.status()


class RerankSelectRequest(BaseModel):
    provider: str                 # KNOWN_PROVIDERS (vllm/cpu/voyage/…)
    model: str                    # a RERANK_MODELS slug
    enabled: bool                 # turn the rerank stage on/off (sidecar `enabled`)
    api_key: str | None = None    # optional pasted key → .env + os.environ mirror


@router.post("/select")
def rerank_select(req: RerankSelectRequest, response: Response):
    """Persist a reranker selection so the wizard/Portal can drive it live (M8).

    Validation ladder (all 4xx, never 500 on bad input):
      • provider ∈ KNOWN_PROVIDERS ......... else 400
      • model ∈ RERANK_MODELS .............. else 404 (mirrors /embeddings/placement)
      • the model's provider == request.provider ... else 400 (no cross-provider pick)
      • the model's tiers include THIS box's hardware tier ... else 400

    When api_key is given AND the model declares a key_env, the key is written to
    .env (secrets_writer.update_env) AND mirrored into os.environ (live, no
    restart — the credentials-route pattern). The key is NEVER logged or echoed:
    the response is the fresh /rerank/status, which carries key_present (a bool),
    never the key value. Writes the rerank.json sidecar and resets the preflight
    caches so the new provider/model re-probes on the next retrieve().
    """
    response.headers["Cache-Control"] = "no-store"

    provider = req.provider.strip().lower()
    if provider not in rerank.KNOWN_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(f"Unknown reranker provider {req.provider!r}; must be one of "
                    f"{sorted(rerank.KNOWN_PROVIDERS)}"),
        )

    model = req.model.strip()
    entry = rerank.RERANK_MODELS.get(model)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown reranker model: {model!r}"
        )

    if entry.get("provider") != provider:
        raise HTTPException(
            status_code=400,
            detail=(f"model {model!r} is served by provider "
                    f"{entry.get('provider')!r}, not {provider!r}"),
        )

    tier = hardware.probe().get("tier")
    tiers = entry.get("tiers", [])
    if tier not in tiers:
        raise HTTPException(
            status_code=400,
            detail=(f"model {model!r} requires tier ∈ {tiers}; this box is "
                    f"{tier}"),
        )

    # Live key write — only when a key was actually pasted AND the model declares
    # a key_env (cloud bearer providers). update_env validates the value; a
    # malformed key (newline/null) is a bad request, not a 500. NEVER logged.
    key_env = entry.get("key_env")
    if req.api_key and key_env:
        try:
            secrets_writer.update_env({key_env: req.api_key})
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid api_key: {e}")
        # Mirror into the running process env so the FRESH os.getenv reads in
        # rerank.get_settings()/score() see it with no restart (credentials
        # route pattern, credentials_routes.py:143).
        os.environ[key_env] = req.api_key

    store.set_rerank_selection(
        {"enabled": req.enabled, "provider": provider, "model": model}
    )
    # New provider/model must re-probe; also clears the CPU model cache (M5).
    rerank.reset_preflight()
    # status() reflects the selection (provider/model/enabled/available/
    # key_present/reachable/preflight) — key_present is a bool, never the key.
    return rerank.status()
