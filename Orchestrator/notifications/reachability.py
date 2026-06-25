"""MN.2 — notification-reachability resolver.

The notification bus' delivery targets are decoupled from the Gemma attestation
registry: a notification-only (model-free) phone never calls
``POST /local/device/attest`` (the Android app fires that ONLY when a model is
installed), so it never appears in ``mesh.reachable_devices`` — which iterates the
attestation registry. Gating delivery on that registry would mean a model-free
phone receives NOTHING.

So for notifications the SUBSCRIPTION ROW is the source of truth for both "who
wants notifications" and "where to reach them": the row already carries the
device's ``tailnet_name`` (the tailnet IPv4/hostname the phone self-reports at
subscribe time). A subscriber is a deliverable target iff that ``tailnet_name`` is
CURRENTLY ONLINE on the tailnet — resolved the SAME way ``mesh.reachable_devices``
resolves online-ness (parse ``tailscale status`` → online nodes → ``_name_matches``),
NOT by requiring an attestation row.

A Gemma-attested device that also subscribed resolves identically: its
subscription row carries a ``tailnet_name`` too. No hard dependency on the
attestation registry remains for delivery.

``mesh.reachable_devices`` is left UNCHANGED — ``control_phone`` still uses it to
turn "the originating operator" into "a reachable, model-bearing device".
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import List, Optional

from Orchestrator.local_provider import mesh
from Orchestrator.notifications.subscriptions import SubscriptionStore

logger = logging.getLogger(__name__)


def reachable_subscribers(
    operator: str,
    *,
    store: Optional[SubscriptionStore] = None,
    status_json: Optional[str] = None,
) -> List[dict]:
    """The operator's subscribers that are CURRENTLY ONLINE on the tailnet.

    Returns ``[{device_id, tailnet_name, node}]`` — one entry per subscriber whose
    ``tailnet_name`` matches an online tailnet node. ``node`` is the matched
    ``mesh.Node`` as a dict (``dns_name``/``ip`` → the POST address). Subscribers
    that are offline, or whose row has no ``tailnet_name`` (unjoinable), are simply
    omitted — they are NOT targets (the event still records in the durable inbox).

    Reuses mesh's online-resolution end-to-end:
      * ``mesh.parse_tailscale_status`` over ``mesh._run_tailscale_status`` (the
        mockable shell-out seam) — exactly how ``reachable_devices`` lists nodes;
      * ``mesh._name_matches`` to join a stored name/IP to a live node.

    ``store`` / ``status_json`` are test seams (None → the default store / the live
    tailscale call). Never raises: a tailscale failure yields no online nodes, and
    a corrupt subscription store yields no subscribers — both degrade to ``[]``.
    """
    sub_store = store if store is not None else SubscriptionStore()

    # Online tailnet node set — resolved IDENTICALLY to mesh.reachable_devices.
    raw = status_json if status_json is not None else mesh._run_tailscale_status()
    online_nodes = [n for n in mesh.parse_tailscale_status(raw) if n.online]

    targets: List[dict] = []
    for device_id in sub_store.subscribers_for(operator):
        row = sub_store.get(device_id)
        tname = (row or {}).get("tailnet_name")
        if not tname:
            # No join key — model-free or not, we cannot locate it. Not a target.
            continue
        match = next((n for n in online_nodes if mesh._name_matches(tname, n)), None)
        if match is None:
            continue  # offline / not on the tailnet right now
        targets.append({
            "device_id": device_id,
            "tailnet_name": tname,
            "node": asdict(match),
        })
    return targets
