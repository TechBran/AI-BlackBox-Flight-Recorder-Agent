"""In-call control for the xAI sovereign line: transfer (SIP REFER) + hangup.

    POST https://api.x.ai/v1/realtime/calls/{call_id}/refer  {"target_uri": ...}
    POST https://api.x.ai/v1/realtime/calls/{call_id}/hangup

Scoped BY DESIGN to an active xAI call: callers may omit call_id, in which
case the single active phone-xai-* session supplies it; with zero (or 2+)
active calls and no explicit call_id the operation fails gracefully. This is
the session-context check that keeps the tools inert outside a live call.
Twilio calls are a separate line and are NOT controllable here.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from Orchestrator.config import XAI_API_KEY
from Orchestrator.models import GROK_LIVE_SESSIONS
from Orchestrator.xai_phone.provisioning import XAI_API_BASE

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ("connecting", "connected", "responding")


def active_call_ids() -> list[str]:
    """call_ids of live xAI SIP call sessions (phone-xai-* and not torn down)."""
    return [
        s.call_id
        for s in GROK_LIVE_SESSIONS.values()
        if getattr(s, "call_id", "")
        and s.session_id.startswith("phone-xai-")
        and s.status in _ACTIVE_STATUSES
    ]


def _resolve_call_id(call_id: Optional[str]) -> tuple[Optional[str], str]:
    active = active_call_ids()
    if call_id:
        if call_id not in active:
            return None, f"call_id {call_id!r} is not an active xAI call (active: {active or 'none'})"
        return call_id, ""
    if not active:
        return None, ("No active xAI phone call — transfer_call/hangup_call only work "
                      "inside a live xAI phone-line session")
    if len(active) > 1:
        return None, f"Multiple active calls ({', '.join(active)}) — pass call_id explicitly"
    return active[0], ""


async def _call_post(call_id: str, action: str, payload: Optional[dict] = None) -> tuple[bool, str]:
    """POST a call-control action. Module-level so tests monkeypatch it."""
    if not XAI_API_KEY:
        return False, "XAI_API_KEY not configured"
    url = f"{XAI_API_BASE}/v1/realtime/calls/{call_id}/{action}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                json=payload if payload is not None else {},
                headers={"Authorization": f"Bearer {XAI_API_KEY}"},
            )
        if resp.status_code >= 400:
            return False, f"xAI {action} failed: HTTP {resp.status_code} {resp.text[:200]}"
        return True, f"{action} accepted for call {call_id}"
    except httpx.HTTPError as exc:
        return False, f"xAI {action} request error: {exc}"


async def transfer_call(target_uri: str, call_id: Optional[str] = None) -> tuple[bool, str]:
    if not target_uri:
        return False, "target_uri is required (e.g. tel:+15550100 or sip:agent@example.com)"
    resolved, err = _resolve_call_id(call_id)
    if not resolved:
        return False, err
    return await _call_post(resolved, "refer", {"target_uri": target_uri})


async def hangup_call(call_id: Optional[str] = None) -> tuple[bool, str]:
    resolved, err = _resolve_call_id(call_id)
    if not resolved:
        return False, err
    return await _call_post(resolved, "hangup")
