"""Notification subsystem — a wholesale, deterministic, per-operator bus.

Any backend event can call ``notify(operator, title, body, category)``; it routes
to the operator's SUBSCRIBED, currently-REACHABLE devices over the existing
Tailscale transport, and ALWAYS records the event as a searchable snapshot (the
durable inbox) even when zero devices are reachable.

Locked design:
  * Fresh box is OPT-IN — a device subscribed to nothing by default.
  * Cross-operator payloads are METADATA-ONLY (no full body for an 'all'
    recipient that isn't the event's own operator).
  * Subscription is USERS_LIST-gated.

Modules:
  * ``subscriptions`` — the durable, atomic per-device subscription store (MN.1).
  * ``bus`` — the ``notify()`` fan-out + always-record bus (MN.2).
"""

from .subscriptions import SubscriptionStore
from .bus import notify, NotifyResult

__all__ = ["SubscriptionStore", "notify", "NotifyResult"]
