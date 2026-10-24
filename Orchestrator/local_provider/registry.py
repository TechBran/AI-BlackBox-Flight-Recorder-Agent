"""
Local Provider Registry — operator-bound on-device model attestation.

Records which operator's phone has which on-device Gemma model
installed + verified. This binding drives whether the `local` provider
is offered to that operator. Deliberately separate from the ADB mesh
device registry (Orchestrator/device_registry/) — different concept.

Usage:
    from Orchestrator.local_provider import get_local_registry
    reg = get_local_registry()
    reg.attest(operator="Alice", device_id="pixel-9", model_slug="gemma-4-e4b",
               version="1.0", sha256="abc", delegate="gpu", autonomy_mode="permission")
    reg.status(operator="Alice")  # -> {"available": True, "models": [...]}
"""
import copy
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from Orchestrator.utils import atomic_json

STORE_FILE = Path(__file__).parent / "local_devices.json"


class LocalProviderRegistry:
    """Operator-bound registry of attested on-device models."""

    def __init__(self):
        # Reads the module global at instantiation so tests can monkeypatch it.
        self._file: Path = STORE_FILE
        # operator -> {device_id -> record}
        self._store: Dict[str, Dict[str, dict]] = {}
        self._lock = threading.RLock()
        self._load_from_file()

    def _load_from_file(self):
        """Load attestations from the JSON store if it exists. A store that
        does not parse is quarantined (local_devices.json.corrupt-<ts>) and
        the registry starts empty, instead of every /local/* route 500ing
        (audit O19-local-stack-12)."""
        self._store = atomic_json.read_json(self._file, {}, valid=lambda d: isinstance(d, dict))
        print(f"[LOCAL PROVIDER] Loaded attestations for {len(self._store)} operators")

    def _save_to_file(self):
        """Persist attestations to the JSON store (atomic, owner-only)."""
        with self._lock:
            atomic_json.write_json(self._file, self._store)

    def _commit(self, edit: Callable[[Dict[str, Dict[str, dict]]], object]):
        """Apply `edit` to a copy of the store, persist it, then adopt it:
        a save that fails leaves both the file and the live store as they
        were. Returns what `edit` returned."""
        with self._lock:
            store = copy.deepcopy(self._store)
            result = edit(store)
            atomic_json.write_json(self._file, store)
            self._store = store
            return result

    def attest(self, operator: str, device_id: str, model_slug: str,
               version: str, sha256: str, delegate: str,
               autonomy_mode: str, tailnet_name: Optional[str] = None) -> dict:
        """Upsert an attestation record for an operator's device, persist it.

        ``tailnet_name`` (optional) is the device's Tailscale node name — the join
        key used to marry this registry to ``tailscale status`` when reaching the
        device for remote control. Omitting it is backward-compatible.
        """
        record = {
            "device_id": device_id,
            "model_slug": model_slug,
            "version": version,
            "sha256": sha256,
            "delegate": delegate,
            "autonomy_mode": autonomy_mode,
            "tailnet_name": tailnet_name,
            "verified_at": time.time(),
        }
        self._commit(lambda store: store.setdefault(operator, {}).__setitem__(device_id, record))
        return record

    def status(self, operator: str) -> dict:
        """Return availability + attested models for an operator."""
        models = list(self._store.get(operator, {}).values())
        return {"available": bool(models), "models": models}

    def set_autonomy(self, operator: str, device_id: str, mode: str) -> Optional[dict]:
        """Update a record's autonomy_mode, persist."""
        if not self._store.get(operator, {}).get(device_id):
            return None

        def edit(store):
            record = store[operator][device_id]
            record["autonomy_mode"] = mode
            return record

        return self._commit(edit)

    def remove(self, operator: str, device_id: str) -> bool:
        """Delete a record (and the operator key if now empty), persist."""
        devices = self._store.get(operator)
        if not devices or device_id not in devices:
            return False

        def edit(store):
            del store[operator][device_id]
            if not store[operator]:
                del store[operator]

        self._commit(edit)
        return True

    def all_records(self) -> List[Tuple[str, dict]]:
        """Return every (operator, record) pair across all operators.

        Read accessor for tailnet mesh joins (control_phone device resolution),
        so sibling modules need not reach into the private store.
        """
        return [(op, rec) for op, devs in self._store.items()
                for rec in devs.values()]


# ── Singleton ──
_registry: Optional[LocalProviderRegistry] = None

def get_local_registry() -> LocalProviderRegistry:
    global _registry
    if _registry is None:
        _registry = LocalProviderRegistry()
    return _registry
