"""xAI sovereign phone line — HTTP surface.

Routes:
    GET  /xai/phone/status      line status (?preflight=true adds a webhook
                                reachability probe); never leaks the secret
    POST /xai/phone/provision   idempotent provisioning (409 unless force)
    POST /xai/voice/incoming    signed telephony webhook (added in Task P5.6)

Uses the newer APIRouter convention (sms_routes.py precedent), included from
Orchestrator/app.py.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException

from Orchestrator.xai_phone import provisioning

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/xai", tags=["xai-phone"])


async def _unsigned_post(url: str) -> int:
    """Bare unsigned POST; returns the HTTP status. Module-level so tests
    monkeypatch it (provisioning._api_post precedent)."""
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        resp = await client.post(url, content=b"{}")
        return resp.status_code


async def _preflight_webhook(webhook_url: str | None) -> dict:
    """Is the public webhook URL reachable AND enforcing signatures?
    An unsigned POST must come back 401 — that proves both at once."""
    if not webhook_url:
        return {"ok": False, "detail": "no webhook_url provisioned"}
    try:
        status_code = await _unsigned_post(webhook_url)
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"unreachable: {exc.__class__.__name__}: {exc}"}
    return {
        "ok": status_code == 401,
        "status_code": status_code,
        "detail": "unsigned POST rejected with 401 (reachable + enforcing)"
        if status_code == 401
        else f"expected 401 for unsigned POST, got {status_code} — check funnel target/path",
    }


@router.get("/phone/status")
async def xai_phone_status(preflight: bool = False):
    status = provisioning.get_status()
    if preflight:
        status["webhook_preflight"] = await _preflight_webhook(status.get("webhook_url"))
    return status


@router.post("/phone/provision")
async def xai_phone_provision(payload: dict):
    name = str(payload.get("name") or "").strip()
    webhook_url = str(payload.get("webhook_url") or "").strip()
    force = bool(payload.get("force", False))
    if not name or not webhook_url:
        raise HTTPException(status_code=400, detail="name and webhook_url are required")
    if not webhook_url.startswith("https://"):
        raise HTTPException(status_code=400,
                            detail="webhook_url must be a public https:// URL (Tailscale Funnel — scripts/xai_phone_funnel.sh)")
    try:
        return await provisioning.provision_number(name, webhook_url, force=force)
    except provisioning.AlreadyProvisionedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:  # XAI_API_KEY missing
        raise HTTPException(status_code=503, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502,
                            detail=f"xAI API error {exc.response.status_code}: {exc.response.text[:200]}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"xAI API unreachable: {exc}")
