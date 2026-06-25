"""MN.1 — per-device operator-subscription store.

``SubscriptionStore`` is the durable record of which devices want notifications
for which operators. It is the OPT-IN gate of the notification subsystem: a fresh
box has no file, so no device is subscribed to anything until it self-registers.

Storage: a single JSON object at ``Manifest/device_notification_subs.json``
keyed by ``device_id``::

    {
      "<device_id>": {
        "all": bool,            # subscribed to EVERY operator (the "all" sentinel)
        "operators": [..],      # explicit per-operator subscriptions
        "tailnet_name": str,    # join key for mesh.reachable_devices (optional)
        "device_kind": str,     # e.g. "android" (optional)
        "display_name": str,    # human label (optional)
        "updated_at": str       # ISO-8601 UTC of the last upsert
      }
    }

Durability mirrors the pairing-routes idiom: atomic write (tmp sibling →
``os.replace``) and corrupt-tolerant read (missing/corrupt/wrong-type file →
empty ``{}``, never an exception). The membership predicate is PURE::

    subscribed = sub["all"] or operator in sub["operators"]
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from Orchestrator.utils.paths import resolve

logger = logging.getLogger(__name__)

# Default store location. Overridable per-instance (tests pass a tmp_path).
DEFAULT_SUBS_FILE = resolve("Manifest", "device_notification_subs.json")


def _now_iso() -> str:
    """ISO-8601 UTC timestamp (seconds resolution, trailing 'Z')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SubscriptionStore:
    """Atomic, corrupt-tolerant per-device subscription store (opt-in)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else DEFAULT_SUBS_FILE

    # ----------------------------------------------------------------- I/O ---
    def _load(self) -> Dict[str, dict]:
        """Read the store from disk.

        Returns ``{}`` if the file is missing, unreadable, malformed, or not a
        top-level dict — a corrupt store must never brick notification routing.
        """
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "device_notification_subs.json read failed: %s — treating as empty", e
            )
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "device_notification_subs.json malformed (expected dict, got %s) — "
                "treating as empty",
                type(data).__name__,
            )
            return {}
        # Defensive: drop any non-dict rows.
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _save(self, data: Dict[str, dict]) -> None:
        """Atomically write the store: tmp sibling → ``os.replace``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)

    # ------------------------------------------------------------- mutators ---
    def upsert(
        self,
        device_id: str,
        *,
        all: bool,
        operators: List[str],
        tailnet_name: Optional[str] = None,
        device_kind: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> dict:
        """Create-or-replace this device's subscription row. Returns the row."""
        data = self._load()
        row = {
            "all": bool(all),
            "operators": list(operators or []),
            "tailnet_name": tailnet_name,
            "device_kind": device_kind,
            "display_name": display_name,
            "updated_at": _now_iso(),
        }
        data[device_id] = row
        self._save(data)
        return row

    def delete(self, device_id: str) -> None:
        """Remove a device's subscription. A no-op if it does not exist."""
        data = self._load()
        if device_id in data:
            del data[device_id]
            self._save(data)

    # ------------------------------------------------------------- readers ---
    def get(self, device_id: str) -> Optional[dict]:
        """The device's subscription row, or ``None`` if it has none."""
        return self._load().get(device_id)

    def all_subscriptions(self) -> Dict[str, dict]:
        """The full device→subscription map (admin view)."""
        return self._load()

    # ----------------------------------------------------------- predicates ---
    @staticmethod
    def _row_subscribed(row: dict, operator: str) -> bool:
        """PURE membership test: subscribed = all OR operator in operators."""
        if not isinstance(row, dict):
            return False
        return bool(row.get("all")) or operator in (row.get("operators") or [])

    def is_subscribed(self, device_id: str, operator: str) -> bool:
        """True if ``device_id`` is subscribed to ``operator`` (opt-in: absent → False)."""
        row = self._load().get(device_id)
        if row is None:
            return False
        return self._row_subscribed(row, operator)

    def subscribers_for(self, operator: str) -> List[str]:
        """device_ids subscribed to ``operator`` OR to 'all' (opt-in)."""
        return [
            device_id
            for device_id, row in self._load().items()
            if self._row_subscribed(row, operator)
        ]
