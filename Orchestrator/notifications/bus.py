"""MN.2 — the ``notify()`` bus.

The single, deterministic entry point for the whole subsystem. ANY backend event
calls ``await notify(operator, title, body, category)`` and the bus:

  1. Computes the targets = the operator's SUBSCRIBERS that are CURRENTLY ONLINE on
     the tailnet (``reachable_subscribers``). Reachability is derived from the
     SUBSCRIPTION ROW's ``tailnet_name`` joined against the live online tailnet node
     set — NOT from the Gemma attestation registry. This is what lets a model-free
     (notification-only) phone — which never attests — actually receive delivery.
  2. Fans the POST out to every target concurrently with a SHORT per-device
     timeout — one slow/dead device never stalls the others or the caller.

Notifications are TRANSIENT, deliver-to-device-only. The bus deliberately does
NOT record events into the snapshot volume: the original "durable inbox" design
minted a snapshot per notify() (reason="NOTIFY"), and with task-completion
notifications firing constantly that flooded the ledger (5,277 NOTIFY snapshots
in one day's archives) and crowded real content out of the recent-snapshot
context-retrieval window — while NOTHING ever read the inbox back (no endpoint,
no UI consumed it). Removed 2026-07-11 per Brandon. Do NOT reintroduce a mint
here; if a durable inbox is ever actually needed, build it as its own store,
not as ledger snapshots.

Two design contracts:
  * ``notify()`` MUST NEVER raise and never block past the per-device timeout.
  * Cross-operator payloads are METADATA-ONLY: a device subscribed via the 'all'
    sentinel to an operator that is NOT the one whose subscription explicitly
    names this event's operator receives title/category/notif_id but NOT the full
    body. The owner (a subscription that explicitly lists this operator) gets the
    full body. See ``_payload_for_device``.

The Android ``/notify`` receiver is a later chunk, so the POST will fail against a
real phone until then — that's fine: a failed POST is counted ``unreachable`` and
the event still records. Tests mock both ``reachable_subscribers`` and the POST.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

import aiohttp

from Orchestrator.notifications.reachability import reachable_subscribers
from Orchestrator.notifications.subscriptions import SubscriptionStore

logger = logging.getLogger(__name__)

# Per-device POST timeout. Deliberately short: notify() is fire-and-forget, so a
# dead device must fail fast rather than hold the caller. Mirrors the
# control_phone listener port knob.
PER_DEVICE_TIMEOUT_SECS = 5.0
REMOTE_NOTIFY_PORT = 8765


@dataclass
class NotifyResult:
    """The outcome of a ``notify()`` call.

    ``delivered`` / ``unreachable`` are device_ids. ``recorded`` is retained for
    wire-compat on /notifications/send but is now ALWAYS False — notifications are
    transient, deliver-to-device-only, never minted into the snapshot volume (see
    the module docstring for why the "durable inbox" mint was removed).
    """

    notif_id: str
    delivered: List[str] = field(default_factory=list)
    unreachable: List[str] = field(default_factory=list)
    recorded: bool = False


def _notify_port() -> int:
    """Device listener port — [control_phone] port, default REMOTE_NOTIFY_PORT.

    Reuses the existing control_phone listener knob: the on-device HTTP server is
    the same one that serves /task, so /notify lives behind the same port.
    """
    try:
        from Orchestrator.config import CFG

        return CFG.getint("control_phone", "port", fallback=REMOTE_NOTIFY_PORT)
    except Exception:
        return REMOTE_NOTIFY_PORT


def _device_base_url(device: dict) -> str:
    """Build a device's listener base URL from its reachable_devices node."""
    node = device.get("node") or {}
    host = node.get("dns_name") or node.get("ip") or ""
    return f"http://{host}:{_notify_port()}"


def _make_notif_id(dedup_key: Optional[str]) -> str:
    """Derive a notif_id: stable from a dedup_key, else a fresh uuid.

    A dedup_key produces a deterministic id (same key → same id) so a caller that
    re-emits the same logical event reuses one identity. Absent a key, a uuid4 is
    fine — this is runtime, not a workflow script that must be reproducible.
    """
    if dedup_key:
        return f"ntf-{uuid.uuid5(uuid.NAMESPACE_URL, dedup_key).hex[:16]}"
    return f"ntf-{uuid.uuid4().hex[:16]}"


def _payload_for_device(
    device: dict,
    operator: str,
    title: str,
    body: str,
    category: str,
    notif_id: str,
) -> dict:
    """Build the POST payload, enforcing metadata-only across operators.

    The full body is included ONLY when the device's subscription EXPLICITLY names
    this event's operator (an owner relationship). A device that is a target purely
    via the 'all' sentinel — i.e. its row does NOT list this operator — receives
    title/category/notif_id but an empty body. We look the row up rather than trust
    reachable_devices' ``operator`` (which is the device's attested owner, a
    different axis from its subscription).
    """
    payload = {
        "title": title,
        "category": category,
        "operator": operator,
        "notif_id": notif_id,
        "body": "",
    }
    try:
        row = SubscriptionStore().get(device.get("device_id"))
    except Exception:
        row = None
    explicit = bool(row and operator in (row.get("operators") or []))
    if explicit:
        payload["body"] = body
    return payload


async def _post_to_device(device: dict, payload: dict) -> dict:
    """POST the notification to a device's /notify listener. Test seam.

    A SHORT per-device timeout caps the wait so one dead device cannot stall the
    gather. Raises on any transport/HTTP error — the caller counts that as
    unreachable.
    """
    base_url = _device_base_url(device)
    timeout = aiohttp.ClientTimeout(total=PER_DEVICE_TIMEOUT_SECS)
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base_url}/notify", json=payload, timeout=timeout) as resp:
            resp.raise_for_status()
            try:
                return await resp.json()
            except Exception:
                return {"ok": True}


async def notify(
    operator: str,
    title: str,
    body: str,
    category: str = "general",
    *,
    dedup_key: Optional[str] = None,
) -> NotifyResult:
    """Route to ``operator``'s subscribers that are currently online + record.

    Targets come from ``reachable_subscribers`` — the subscription row's
    ``tailnet_name`` joined against the live online tailnet node set (NOT the Gemma
    attestation registry), so a model-free phone is reachable. NEVER raises; never
    blocks past the per-device timeout. Returns a NotifyResult.
    """
    notif_id = _make_notif_id(dedup_key)

    # Compute targets = the operator's subscribers that are currently ONLINE on the
    # tailnet, resolved from each subscription row's tailnet_name (NOT the Gemma
    # attestation registry — see reachable_subscribers). Degrades to [] on any
    # failure (Tailscale down / corrupt store), so this never raises.
    try:
        targets = reachable_subscribers(operator)
    except Exception as e:
        logger.warning("[NOTIFY] reachable_subscribers failed for op=%s: %s", operator, e)
        targets = []

    total = len(targets)

    delivered: List[str] = []
    unreachable: List[str] = []

    if targets:
        payloads = [
            _payload_for_device(d, operator, title, body, category, notif_id)
            for d in targets
        ]
        results = await asyncio.gather(
            *(_dispatch(d, p) for d, p in zip(targets, payloads)),
            return_exceptions=True,
        )
        for device, outcome in zip(targets, results):
            device_id = device.get("device_id")
            if isinstance(outcome, Exception):
                unreachable.append(device_id)
            else:
                delivered.append(device_id)

    # Deliver-only: deliberately NO snapshot record (see module docstring — the
    # old always-mint "durable inbox" flooded the ledger and context retrieval).
    logger.info(
        "[NOTIFY] op=%s cat=%s delivered=%d/%d id=%s",
        operator,
        category,
        len(delivered),
        total,
        notif_id,
    )

    return NotifyResult(
        notif_id=notif_id,
        delivered=delivered,
        unreachable=unreachable,
        recorded=False,
    )


async def _dispatch(device: dict, payload: dict):
    """Per-device POST wrapped so its own timeout fires even if the seam ignores it.

    asyncio.gather(return_exceptions=True) already isolates one failure from the
    others; this extra wait_for guarantees a hung seam still resolves within the
    per-device budget so notify() never blocks the caller past it. Looks up
    ``_post_to_device`` at call time so the test seam (monkeypatch) is honoured.
    """
    return await asyncio.wait_for(_post_to_device(device, payload), PER_DEVICE_TIMEOUT_SECS)
