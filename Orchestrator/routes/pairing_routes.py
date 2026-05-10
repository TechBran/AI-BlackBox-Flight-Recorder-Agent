"""Pairing routes — QR-based device pairing for AI BlackBox.

POST /pair/start   — Mint a one-time pairing token (TTL 5min).
POST /pair/claim   — Redeem a token (called by the claiming device).
GET  /pair/status  — Check if a token has been claimed (for poll-style UX).
GET  /pair/qr/{token} — Render PNG QR code for the token (server-side).
"""
from __future__ import annotations

import io
import json
import logging
import secrets
import time
from typing import Optional

import qrcode
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from Orchestrator.config import DEFAULT_OPERATOR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pair", tags=["pairing"])

PAIR_TOKEN_TTL_SECS = 300

# Token store: token -> {created_at, claimed_at, claimed_by, device_kind}
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
    device_kind: Optional[str] = None
    expires_in: int


def _purge_expired() -> None:
    now = time.time()
    expired = [
        t for t, m in list(_pair_tokens.items())  # snapshot to avoid concurrent-mutation race
        if now - m["created_at"] > PAIR_TOKEN_TTL_SECS
    ]
    for t in expired:
        _pair_tokens.pop(t, None)  # pop with default to handle concurrent removal


@router.post("/start", response_model=PairStartResponse)
def pair_start() -> PairStartResponse:
    _purge_expired()
    token = secrets.token_urlsafe(16)
    now = time.time()
    _pair_tokens[token] = {
        "created_at": now,
        "claimed_at": None,
        "claimed_by": None,
        "device_kind": None,
    }
    logger.info("pair_token_minted token_id=%s exp=%d", token[:6], int(now + PAIR_TOKEN_TTL_SECS))
    return PairStartResponse(token=token, exp=int(now + PAIR_TOKEN_TTL_SECS))


@router.post("/claim", response_model=PairClaimResponse)
def pair_claim(req: PairClaimRequest, request: Request) -> PairClaimResponse:
    _purge_expired()
    meta = _pair_tokens.get(req.token)
    if not meta:
        raise HTTPException(status_code=404, detail="token unknown or expired")
    if meta["claimed_at"]:  # noqa: race-acceptable for human-paced QR flow
        logger.warning(
            "pair_claim_rejected_already_claimed token_id=%s by=%s",
            req.token[:6], req.device_name,
        )
        raise HTTPException(status_code=409, detail="token already claimed")
    meta["claimed_at"] = time.time()
    meta["claimed_by"] = req.device_name
    meta["device_kind"] = req.device_kind
    origin = str(request.base_url).rstrip('/')
    logger.info(
        "pair_claimed token_id=%s by=%s kind=%s",
        req.token[:6], req.device_name, req.device_kind,
    )
    return PairClaimResponse(success=True, operator=DEFAULT_OPERATOR, origin=origin)


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
        claimed_by=meta.get("claimed_by"),
        device_kind=meta.get("device_kind"),
        expires_in=expires_in,
    )


@router.get("/qr/{token}")
def pair_qr(token: str, request: Request):
    """Render PNG QR for a pairing token. Replaces external api.qrserver.com.

    Origin is derived from request.base_url so the QR encodes the URL the
    Portal was loaded from (Tailscale Magic DNS / LAN / localhost) — phone
    scanning the QR can reach the same URL via its own network path.
    """
    _purge_expired()
    meta = _pair_tokens.get(token)
    if not meta:
        raise HTTPException(status_code=404, detail="token unknown or expired")
    origin = str(request.base_url).rstrip('/')
    payload = json.dumps({
        "type": "pair",
        "token": token,
        "exp": int(meta["created_at"] + PAIR_TOKEN_TTL_SECS),
        "origin": origin,
        "operator": DEFAULT_OPERATOR,
    })
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")
