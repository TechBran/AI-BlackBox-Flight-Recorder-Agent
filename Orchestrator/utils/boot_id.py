"""Identity of this Orchestrator process, served by GET /health as boot_id.

Audit 2026-10-09 rank 43 (A13-ui-ops-01). After Install, Rollback or Restart
the Portal and Android polled /health and took the first 200 as "back
online", but the restart fires 2 s after the update's complete event, so the
OLD process answered and the panel refreshed against a box about to go down
for 60-90 s. The clients now read boot_id before the action and wait for it to
CHANGE.

The value is the kernel's boot id, this pid and this process's start time
(clock ticks since boot, /proc/self/stat field 22): unique per process start,
equal for every request a process answers, and new after any restart or
reboot. Where /proc is missing a random id per process does the same job.
Stdlib only.
"""
from __future__ import annotations

import os
import uuid

_KERNEL_BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
_SELF_STAT_FILE = "/proc/self/stat"


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _process_start_ticks() -> str:
    """Field 22 of /proc/self/stat. The command name (field 2) may contain
    spaces and parentheses, so count from the last ')'."""
    stat = _read(_SELF_STAT_FILE)
    fields = stat.rpartition(")")[2].split()
    # fields[0] is field 3 (state), so field 22 is fields[19].
    return fields[19] if len(fields) > 19 else ""


def _compute() -> str:
    kernel = _read(_KERNEL_BOOT_ID_FILE)
    start = _process_start_ticks()
    if kernel and start:
        return f"{kernel}:{os.getpid()}:{start}"
    return uuid.uuid4().hex


BOOT_ID = _compute()
