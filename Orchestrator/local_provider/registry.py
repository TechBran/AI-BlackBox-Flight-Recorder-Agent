"""
Local Provider Registry — operator-bound on-device model attestation.

Records which operator's phone has which on-device Gemma model
installed + verified. This binding drives whether the `local` provider
is offered to that operator. Deliberately separate from the ADB mesh
device registry (Orchestrator/device_registry/) — different concept.

Usage:
    from Orchestrator.local_provider import get_local_registry
    reg = get_local_registry()
    reg.attest(operator="Brandon", device_id="pixel-9", model_slug="gemma-4-e4b",
               version="1.0", sha256="abc", delegate="gpu", autonomy_mode="permission")
    reg.status(operator="Brandon")  # -> {"available": True, "models": [...]}
"""
import json
import time
from pathlib import Path
from typing import Dict, Optional

STORE_FILE = Path(__file__).parent / "local_devices.json"


class LocalProviderRegistry:
    """Operator-bound registry of attested on-device models."""

    def __init__(self):
        # Reference the module-level STORE_FILE at call time so tests that
        # monkeypatch the attribute take effect for fresh instances.
        import Orchestrator.local_provider.registry as _self_module
        self._file: Path = _self_module.STORE_FILE
        # operator -> {device_id -> record}
        self._store: Dict[str, Dict[str, dict]] = {}
        self._load_from_file()

    def _load_from_file(self):
        """Load attestations from the JSON store if it exists."""
        if self._file.exists():
            with open(self._file) as f:
                self._store = json.load(f)
        print(f"[LOCAL PROVIDER] Loaded attestations for {len(self._store)} operators")

    def _save_to_file(self):
        """Persist attestations to the JSON store."""
        with open(self._file, "w") as f:
            json.dump(self._store, f, indent=2)

    def attest(self, operator: str, device_id: str, model_slug: str,
               version: str, sha256: str, delegate: str,
               autonomy_mode: str) -> dict:
        """Upsert an attestation record for an operator's device, persist it."""
        record = {
            "device_id": device_id,
            "model_slug": model_slug,
            "version": version,
            "sha256": sha256,
            "delegate": delegate,
            "autonomy_mode": autonomy_mode,
            "verified_at": time.time(),
        }
        self._store.setdefault(operator, {})[device_id] = record
        self._save_to_file()
        return record

    def status(self, operator: str) -> dict:
        """Return availability + attested models for an operator."""
        models = list(self._store.get(operator, {}).values())
        return {"available": bool(models), "models": models}

    def set_autonomy(self, operator: str, device_id: str, mode: str) -> Optional[dict]:
        """Update a record's autonomy_mode, persist."""
        record = self._store.get(operator, {}).get(device_id)
        if not record:
            return None
        record["autonomy_mode"] = mode
        self._save_to_file()
        return record

    def remove(self, operator: str, device_id: str) -> bool:
        """Delete a record (and the operator key if now empty), persist."""
        devices = self._store.get(operator)
        if not devices or device_id not in devices:
            return False
        del devices[device_id]
        if not devices:
            del self._store[operator]
        self._save_to_file()
        return True


# ── Singleton ──
_registry: Optional[LocalProviderRegistry] = None

def get_local_registry() -> LocalProviderRegistry:
    global _registry
    if _registry is None:
        _registry = LocalProviderRegistry()
    return _registry
