"""Onboarding state — first-run detection and step-completion tracking.

Public API:
    OnboardingState() — direct constructor (still works, but prefer get_state()).
    get_state()       — singleton accessor; HTTP handlers should use this.

Concurrency: all mutating methods take a module-level threading.Lock and re-read
state from disk before mutating, so concurrent requests don't lose progress.
Persistence: _save() is atomic via .tmp + os.replace, so a crash mid-write
won't leave a corrupt JSON file that silently rewinds the user to step 1.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Literal

from Orchestrator.utils.paths import resolve

logger = logging.getLogger(__name__)

STATE_FILE = resolve(".onboarding_state.json")
COMPLETE_SENTINEL = resolve(".onboarding_complete")

StepName = Literal[
    "welcome",
    "tailscale",
    "api_keys",
    "phone",
    "optional_integrations",
    "pair_phone",
    "operator",
    "done",
]

ALL_STEPS: list[StepName] = [
    "welcome", "tailscale", "api_keys",
    "phone", "optional_integrations",
    "pair_phone", "operator", "done",
]

_DEFAULTS: dict = {
    "started_at": None,  # set at first-call time inside _load
    "completed_steps": [],
    "skipped_steps": [],
    "current_step": "welcome",
    "validated_at": {},  # pre-emptive for Phase 1.4 manage-mode validation timestamps
}

_lock = threading.Lock()
_instance: "OnboardingState | None" = None


def get_state() -> "OnboardingState":
    """Singleton accessor — use this in routes instead of OnboardingState()."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:  # double-checked locking
                _instance = OnboardingState()
    return _instance


class OnboardingState:
    """Persistent onboarding progress state.

    Stored as JSON in {BLACKBOX_ROOT}/.onboarding_state.json.
    Marker file {BLACKBOX_ROOT}/.onboarding_complete signals 'done' to other code.
    """

    def __init__(self) -> None:
        self._data: dict = self._load()

    def _load(self) -> dict:
        defaults = {**_DEFAULTS, "started_at": time.time()}
        if STATE_FILE.exists():
            try:
                loaded = json.loads(STATE_FILE.read_text())
                # Merge loaded values into defaults — missing keys keep defaults
                return {**defaults, **loaded}
            except Exception as e:
                logger.warning(
                    "onboarding_state_corrupt — falling back to defaults: %s", e
                )
        return defaults

    def _save(self) -> None:
        """Atomically write state to disk via .tmp + os.replace."""
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, STATE_FILE)

    def is_complete(self) -> bool:
        return COMPLETE_SENTINEL.exists()

    def mark_step_complete(self, step: StepName) -> None:
        if step not in ALL_STEPS:
            raise ValueError(f"unknown step name: {step!r}; valid: {ALL_STEPS}")
        with _lock:
            self._data = self._load()
            if step not in self._data["completed_steps"]:
                self._data["completed_steps"].append(step)
            if step in self._data["skipped_steps"]:
                self._data["skipped_steps"].remove(step)
            self._save()
            logger.info("onboarding step complete: %s", step)

    def mark_step_skipped(self, step: StepName) -> None:
        if step not in ALL_STEPS:
            raise ValueError(f"unknown step name: {step!r}; valid: {ALL_STEPS}")
        with _lock:
            self._data = self._load()
            if step not in self._data["skipped_steps"]:
                self._data["skipped_steps"].append(step)
            if step in self._data["completed_steps"]:
                self._data["completed_steps"].remove(step)
            self._save()
            logger.info("onboarding step skipped: %s", step)

    def set_current(self, step: StepName) -> None:
        if step not in ALL_STEPS:
            raise ValueError(f"unknown step name: {step!r}; valid: {ALL_STEPS}")
        with _lock:
            self._data = self._load()
            self._data["current_step"] = step
            self._save()

    def mark_complete(self) -> None:
        """Final marker — wizard is done. Sentinel is the authoritative source."""
        with _lock:
            self._data = self._load()
            ts = int(time.time())
            COMPLETE_SENTINEL.write_text(f"completed_at={ts}\n")
            self._data["completed_at"] = ts
            self._save()
            logger.info("onboarding completed at %d", ts)

    def reset(self) -> None:
        """Clear onboarding state (for re-runs)."""
        with _lock:
            if STATE_FILE.exists():
                STATE_FILE.unlink()
            if COMPLETE_SENTINEL.exists():
                COMPLETE_SENTINEL.unlink()
            self._data = self._load()
            logger.warning("onboarding state reset")

    def snapshot(self) -> dict:
        """Return state dict for /onboarding/state response. Returns shallow copies of lists."""
        return {
            "is_complete": self.is_complete(),
            "completed_steps": list(self._data["completed_steps"]),
            "skipped_steps": list(self._data["skipped_steps"]),
            "current_step": self._data["current_step"],
            "all_steps": list(ALL_STEPS),
        }
