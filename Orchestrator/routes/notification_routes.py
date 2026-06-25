"""MN.3 — device notification subscription REST API.

The non-Python surface of the notification subsystem. A device self-registers its
subscription (no prior device-registry row needed); an admin can list/inspect/
revoke; and ``/notifications/send`` lets non-Python callers (and tests) drive the
``notify()`` bus directly.

Endpoints (registered onto the shared app via the bare ``@app`` pattern):
  POST   /notifications/subscribe                  upsert (USERS_LIST-gated)
  GET    /notifications/subscriptions?device_id=   one device's sub (404 if none)
  GET    /notifications/subscriptions              admin device→operators map
  DELETE /notifications/subscriptions/{device_id}  unsubscribe
  POST   /notifications/send                        drive notify() → NotifyResult

Subscription is USERS_LIST-gated: every named operator must be a known operator;
the literal "all" sentinel is allowed and maps to ``all=True``. An unknown operator
yields 422 — a fresh box cannot subscribe a device to an operator that doesn't
exist.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import List, Optional

from fastapi import HTTPException, Query
from pydantic import BaseModel

from Orchestrator.checkpoint import app
from Orchestrator.config import USERS_LIST
from Orchestrator.notifications.bus import notify
from Orchestrator.notifications.subscriptions import SubscriptionStore

logger = logging.getLogger(__name__)

# The subscription-to-everything sentinel. A device that lists this (instead of
# named operators) is subscribed to EVERY operator (all=True).
ALL_SENTINEL = "all"


class SubscribeBody(BaseModel):
    device_id: str
    tailnet_name: Optional[str] = None
    device_kind: Optional[str] = None
    display_name: Optional[str] = None
    operators: List[str] = []  # named operators, or ["all"]


class SendBody(BaseModel):
    operator: str
    title: str
    body: str
    category: str = "general"
    dedup_key: Optional[str] = None


def _resolve_operators(operators: List[str]) -> tuple[bool, List[str]]:
    """Validate the requested operators against USERS_LIST.

    Returns ``(all_flag, named_operators)``. The "all" sentinel anywhere in the
    list maps to ``all=True`` with an empty named list. Every other entry must be
    a known operator (USERS_LIST) — an unknown one raises 422.
    """
    requested = [o.strip() for o in (operators or []) if o and o.strip()]
    if ALL_SENTINEL in requested:
        return True, []
    unknown = [o for o in requested if o not in USERS_LIST]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown operator(s): {', '.join(unknown)}. "
            f"Known operators: {', '.join(USERS_LIST)} (or the 'all' sentinel).",
        )
    return False, requested


@app.post("/notifications/subscribe")
async def notifications_subscribe(body: SubscribeBody):
    """Create-or-replace a device's notification subscription (USERS_LIST-gated)."""
    if not body.device_id or not body.device_id.strip():
        raise HTTPException(status_code=422, detail="device_id is required.")

    all_flag, named = _resolve_operators(body.operators)

    row = SubscriptionStore().upsert(
        body.device_id.strip(),
        all=all_flag,
        operators=named,
        tailnet_name=body.tailnet_name,
        device_kind=body.device_kind,
        display_name=body.display_name,
    )
    logger.info(
        "[NOTIFY] subscribe device=%s all=%s operators=%s",
        body.device_id, all_flag, named,
    )
    return {"device_id": body.device_id.strip(), **row}


@app.get("/notifications/subscriptions")
async def notifications_list(device_id: Optional[str] = Query(default=None)):
    """One device's subscription (?device_id=) or the full admin map."""
    store = SubscriptionStore()
    if device_id:
        row = store.get(device_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No subscription for that device.")
        return row
    return store.all_subscriptions()


@app.delete("/notifications/subscriptions/{device_id}")
async def notifications_unsubscribe(device_id: str):
    """Remove a device's subscription (idempotent)."""
    SubscriptionStore().delete(device_id)
    logger.info("[NOTIFY] unsubscribe device=%s", device_id)
    return {"device_id": device_id, "unsubscribed": True}


@app.post("/notifications/send")
async def notifications_send(body: SendBody):
    """Drive the notify() bus for a non-Python caller; return the NotifyResult."""
    result = await notify(
        body.operator,
        body.title,
        body.body,
        category=body.category,
        dedup_key=body.dedup_key,
    )
    return asdict(result)
