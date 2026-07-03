"""Reranker ops surface (M11/WI-4, audit A9).

GET /rerank/status — [retrieval] rerank_enabled flag, resolved [rerank]
provider config, and the one-time latency-preflight result. ADDITIVE contract
in the /embeddings/status style, for the M10-style wizard/Portal/Android ops
cards to consume when reranker placement activates post-GPU. Read-only and
side-effect-free on the fresh-box/null-provider default (pure config reads);
with a configured provider it triggers the same once-per-process preflight
retrieve() would — safe to poll (the probe result is cached).
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
