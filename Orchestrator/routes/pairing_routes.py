"""Pairing routes — QR-based device pairing for AI BlackBox.

POST /pair/start   — Mint a one-time pairing token (TTL 5min).
POST /pair/claim   — Redeem a token (called by the claiming device).
GET  /pair/status  — Check if a token has been claimed (for poll-style UX).
GET  /pair/qr/{token} — Render PNG QR code for the token (server-side).
"""
from __future__ import annotations

import io
import secrets
import time
from typing import Optional

import qrcode
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/pair", tags=["pairing"])

PAIR_TOKEN_TTL_SECS = 300

# Token store: token -> {created_at, claimed_at, claimed_by}
# In-memory; tokens are short-lived. Restart-tolerant via TTL.
_pair_tokens: dict[str, dict] = {}


class PairStartResponse(BaseModel):
    type: str = "pair"
    token: str
    exp: int


class PairClaimRequest(BaseModel):
    token: str
    device_name: str
    device_kind: str  # "android", "desktop", "ios", etc.


class PairClaimResponse(BaseModel):
    success: bool
    operator: Optional[str] = None
    origin: Optional[str] = None


class PairStatusResponse(BaseModel):
    exists: bool
    claimed: bool
    claimed_by: Optional[str] = None
    expires_in: int


def _purge_expired() -> None:
    now = time.time()
    expired = [t for t, m in _pair_tokens.items() if now - m["created_at"] > PAIR_TOKEN_TTL_SECS]
    for t in expired:
        del _pair_tokens[t]


@router.post("/start", response_model=PairStartResponse)
def pair_start() -> PairStartResponse:
    _purge_expired()
    token = secrets.token_urlsafe(16)
    now = time.time()
    _pair_tokens[token] = {
        "created_at": now,
        "claimed_at": None,
        "claimed_by": None,
    }
    return PairStartResponse(token=token, exp=int(now + PAIR_TOKEN_TTL_SECS))


@router.post("/claim", response_model=PairClaimResponse)
def pair_claim(req: PairClaimRequest) -> PairClaimResponse:
    _purge_expired()
    meta = _pair_tokens.get(req.token)
    if not meta:
        raise HTTPException(status_code=404, detail="token unknown or expired")
    if meta["claimed_at"]:
        raise HTTPException(status_code=409, detail="token already claimed")
    meta["claimed_at"] = time.time()
    meta["claimed_by"] = req.device_name
    # Pull operator + origin from /health-style config (kept minimal here)
    from Orchestrator.config import DEFAULT_OPERATOR, DEFAULT_ORIGIN  # added in 0.4.2 (this task)
    return PairClaimResponse(success=True, operator=DEFAULT_OPERATOR, origin=DEFAULT_ORIGIN)


@router.get("/status", response_model=PairStatusResponse)
def pair_status(token: str) -> PairStatusResponse:
    _purge_expired()
    meta = _pair_tokens.get(token)
    if not meta:
        return PairStatusResponse(exists=False, claimed=False, expires_in=0)
    expires_in = max(0, int(PAIR_TOKEN_TTL_SECS - (time.time() - meta["created_at"])))
    return PairStatusResponse(
        exists=True,
        claimed=meta["claimed_at"] is not None,
        claimed_by=meta["claimed_by"],
        expires_in=expires_in,
    )


@router.get("/qr/{token}")
def pair_qr(token: str):
    """Render PNG QR for a pairing token. Replaces external api.qrserver.com."""
    _purge_expired()
    meta = _pair_tokens.get(token)
    if not meta:
        raise HTTPException(status_code=404, detail="token unknown or expired")
    from Orchestrator.config import DEFAULT_OPERATOR, DEFAULT_ORIGIN
    payload = (
        '{"type":"pair","token":"' + token + '","exp":' + str(int(meta["created_at"] + PAIR_TOKEN_TTL_SECS))
        + ',"origin":"' + DEFAULT_ORIGIN + '","operator":"' + DEFAULT_OPERATOR + '"}'
    )
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")
