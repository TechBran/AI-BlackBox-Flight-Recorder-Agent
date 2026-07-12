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

import asyncio
import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from Orchestrator.xai_phone import provisioning
from Orchestrator.xai_phone.signature import verify_signature

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


# =============================================================================
# Signed telephony webhook (the ONLY publicly exposed path — see
# scripts/xai_phone_funnel.sh; everything else on :9091 stays tailnet-only)
# =============================================================================

def _spawn_attach(call_id: str, event: dict) -> None:
    """Fire-and-forget call attach. Module-level so tests monkeypatch it;
    late import keeps the route importable without the grok stack."""
    from Orchestrator.xai_phone.call_bridge import attach_call
    asyncio.create_task(attach_call(call_id, event))


@router.post("/voice/incoming")
async def xai_voice_incoming(request: Request):
    """xAI telephony webhook (Standard Webhooks HMAC scheme).

    Verification order is deliberate: raw body read -> signature check
    (constant-time, ±5min tolerance, replay-guarded) -> ONLY then JSON parse.
    Unsigned/stale/replayed requests get a generic 401 (reason logged
    server-side, never echoed). A webhook must be answered fast — the call
    attach runs as a background task.
    """
    body = await request.body()

    secret = provisioning.get_signing_secret()
    if not secret:
        # Fail closed, but distinguishable from a bad signature so a funnel
        # preflight against an unprovisioned box is diagnosable.
        raise HTTPException(status_code=503, detail="xAI phone line not provisioned")

    # Defense-in-depth (P5.1 caller advisory): ANY verify error => reject 401.
    # The RAW body bytes are what we verify AND what we later parse (json.loads
    # below) — never re-read the request as JSON, or the signature would cover
    # different bytes than we act on.
    try:
        ok, reason = verify_signature(secret, dict(request.headers), body)
    except Exception as exc:  # noqa: BLE001 — auth path must never crash-open
        ok, reason = False, f"verify raised {exc.__class__.__name__}"
    if not ok:
        logger.warning("[XAI-PHONE] rejected webhook: %s", reason)
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    event_type = str(event.get("type", ""))
    if event_type != "realtime.call.incoming":
        logger.info("[XAI-PHONE] ignoring webhook event type %r", event_type)
        return {"ok": True, "handled": False}

    call_id = str(event.get("call_id") or (event.get("data") or {}).get("call_id") or "")
    if not call_id:
        raise HTTPException(status_code=400, detail="missing call_id")

    _spawn_attach(call_id, event)
    return {"ok": True, "handled": True, "session_id": f"phone-xai-{call_id}"}
