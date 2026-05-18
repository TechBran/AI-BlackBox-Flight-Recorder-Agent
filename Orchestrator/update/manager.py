"""UpdateManager — flock-based mutex + persistent state machine.

Holds the cross-process serialization point for the update pipeline. Two
distinct concerns:

1. **Mutex (audit M6)**: fcntl.flock on Manifest/update.lock. Survives
   multi-worker uvicorn AND survives crash recovery (OS auto-releases the
   lock on FD close, including on process kill -9). In-memory threading.Lock
   wouldn't survive process death; APScheduler job persistence wouldn't
   catch the "operator clicked Install in two browser tabs" case.

2. **State persistence (audit C3 + M5)**: writes Manifest/update_state.json
   at every phase boundary. On startup, blackbox.service reads this file —
   if it shows a non-terminal phase (apt_install, pip_install, ...), the
   update was interrupted (kill -9, OOM, power loss). UI surfaces a banner
   and offers manual rollback to the pre-update tag.

PHASE state machine:
  None                                          ← no update ever run
  staging      → pre-update tag + venv freezes + worktree validation
  apt_install  → apt-get install via blackbox-apt-install helper (per package)
  pip_install  → Orchestrator/venv/bin/pip install -r requirements.txt
  mcp_install  → MCP/venv/bin/pip install -r MCP/requirements.txt
  systemd_regen → blackbox-write-systemd helper writes (per file)
  reset_hard   → git reset --hard origin/main (atomic file swap)
  restart_pending → SSE complete event flushed; detached restart scheduled
  complete     → terminal success state (kept in state.json for /update/status)
  failed       → terminal failure state with error + rollback info
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Optional


# Type alias for the union of valid phase strings.
Phase = str  # one of the constants below


PHASE_STAGING = "staging"
PHASE_APT_INSTALL = "apt_install"
PHASE_PIP_INSTALL = "pip_install"
PHASE_MCP_INSTALL = "mcp_install"
PHASE_SYSTEMD_REGEN = "systemd_regen"
PHASE_RESET_HARD = "reset_hard"
PHASE_RESTART_PENDING = "restart_pending"
PHASE_COMPLETE = "complete"
PHASE_FAILED = "failed"

TERMINAL_PHASES = (PHASE_COMPLETE, PHASE_FAILED)


class UpdateInProgressError(Exception):
    """Raised when an update is already in progress and a new one is requested."""


class UpdateManager:
    """Singleton-style manager. Instantiate once per process with the
    BLACKBOX_ROOT path; subsequent calls share the same lock file and
    state file."""

    def __init__(self, blackbox_root: Path):
        self.root = Path(blackbox_root)
        self.manifest_dir = self.root / "Manifest"
        self.lock_file = self.manifest_dir / "update.lock"
        self.state_file = self.manifest_dir / "update_state.json"
        # File descriptor of the held lock — None when not held. Each
        # acquire() opens a fresh fd so the manager can be used across
        # multiple update attempts in a single process lifetime.
        self._lock_fd: Optional[int] = None

    # ── Mutex ───────────────────────────────────────────────────────────

    @contextlib.contextmanager
    def acquire_or_raise(self):
        """Context manager. Acquires the flock or raises UpdateInProgressError.

        Usage:
            with mgr.acquire_or_raise():
                ... run update steps ...
            # lock released on exit (even on exception)
        """
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise UpdateInProgressError(
                    "Another update is already in progress. "
                    "Check /update/state for status."
                )
            # Write our PID + start time so an SSH operator inspecting
            # the lock file can identify which process holds it.
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()} since={int(time.time())}\n".encode())
            self._lock_fd = fd
            yield
        finally:
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass
                self._lock_fd = None

    def is_locked(self) -> bool:
        """Non-blocking probe: is some other process holding the lock right now?
        Used by /update/status to surface "in progress" to the UI without
        actually trying to claim the lock."""
        if not self.lock_file.exists():
            return False
        # Try to take a SHARED non-blocking lock — if any other process
        # holds an exclusive lock, this raises BlockingIOError.
        try:
            fd = os.open(str(self.lock_file), os.O_RDONLY)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                # Got it → nobody holds an exclusive lock.
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(fd)

    # ── State machine persistence ───────────────────────────────────────

    def write_state(self, *, task_id: str, phase: Phase,
                    target_sha: str, from_sha: str,
                    pre_update_tag: str,
                    extra: Optional[dict] = None) -> None:
        """Persist the current update phase atomically (temp + rename).

        Called at every phase boundary by the runner. On crash recovery,
        startup reads this file to detect "we died mid-update" and shows
        a recovery banner.
        """
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "task_id": task_id,
            "phase": phase,
            "target_sha": target_sha,
            "from_sha": from_sha,
            "pre_update_tag": pre_update_tag,
            "updated_iso": _now_iso(),
        }
        if extra:
            payload.update(extra)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.state_file)

    def read_state(self) -> Optional[dict]:
        """Return the persisted state dict, or None if no update has ever run.

        Caller checks `["phase"] in TERMINAL_PHASES` to decide whether the
        last update completed or was interrupted.
        """
        if not self.state_file.exists():
            return None
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def is_interrupted(self) -> bool:
        """True iff the last persisted state shows a non-terminal phase
        AND no process currently holds the lock (so we're not just
        observing a running update). Used by startup banner."""
        state = self.read_state()
        if state is None:
            return False
        if state.get("phase") in TERMINAL_PHASES:
            return False
        # Non-terminal phase recorded but lock not held → interrupted.
        return not self.is_locked()


def _now_iso() -> str:
    """UTC timestamp in ISO 8601 with second precision."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
