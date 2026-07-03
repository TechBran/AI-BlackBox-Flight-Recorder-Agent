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
"""
from fastapi import APIRouter, Response

from Orchestrator import rerank

router = APIRouter(prefix="/rerank", tags=["rerank"])


@router.get("/status")
def rerank_status(response: Response):
    # no-store: enabled/preflight state flips on config + GPU install; a
    # cached "disabled" card after activation would mislead the operator.
    response.headers["Cache-Control"] = "no-store"
    return rerank.status()
