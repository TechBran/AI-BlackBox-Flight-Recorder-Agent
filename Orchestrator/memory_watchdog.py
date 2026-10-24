"""Logged RSS watchdog for the orchestrator process. It warns; it never acts.

blackbox.service used to recycle itself with uvicorn's --limit-max-requests
100000: past that many requests uvicorn exits and systemd starts it again,
which on a busy box was an unplanned outage every few days (in-flight chat,
voice, CU and WebSocket sessions dropped, then the 60-90 s index rebuild),
leak or no leak (audit X06-ops-deploy-01). The unit now bounds memory instead:
MemoryHigh= makes the kernel throttle and reclaim, MemoryMax= is the hard cap
(an OOM kill, which Restart=always recovers). This loop reads the process's
resident memory every CHECK_INTERVAL_S and logs a warning when it reaches
WARN_FRACTION of the lowest of those limits, so a leak shows up in the journal
as a climb well before either one bites. Off systemd (a dev run, cgroup v1)
it measures against physical RAM.

Reads /proc and /sys only; stdlib plus psutil (already in requirements.in).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("blackbox.memory_watchdog")

CHECK_INTERVAL_S = 120.0
# Warn at this fraction of the budget (the lowest of memory.high / memory.max).
WARN_FRACTION = 0.8
# While it stays above, repeat the warning this often, or sooner when it has
# grown by CLIMB_STEP of the budget since the last one.
REPEAT_WARNING_S = 3600.0
CLIMB_STEP = 0.05
# Below WARN_FRACTION - RECOVER_MARGIN it counts as recovered.
RECOVER_MARGIN = 0.05

_LIMIT_FILES = ("memory.high", "memory.max")


def own_cgroup_dir(proc_cgroup: Path | str = "/proc/self/cgroup",
                   root: Path | str = "/sys/fs/cgroup") -> Optional[Path]:
    """This process's cgroup v2 directory, or None (cgroup v1, no /proc)."""
    try:
        lines = Path(proc_cgroup).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        # The unified hierarchy's entry is "0::<path>".
        if line.startswith("0::"):
            rel = line[3:].strip().lstrip("/")
            return Path(root) / rel if rel else Path(root)
    return None


def _read_limit(path: Path) -> Optional[int]:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def memory_limits(cgroup_dir: Optional[Path]) -> Dict[str, Optional[int]]:
    """memory.high and memory.max of the cgroup in bytes; None when unset."""
    if cgroup_dir is None:
        return {name: None for name in _LIMIT_FILES}
    return {name: _read_limit(Path(cgroup_dir) / name) for name in _LIMIT_FILES}


def budget(limits: Dict[str, Optional[int]], total: int) -> Tuple[str, int]:
    """The (name, bytes) the watchdog measures against: the lowest set limit,
    else physical RAM."""
    set_limits = [(value, name) for name, value in limits.items() if value]
    if set_limits:
        value, name = min(set_limits)
        return name, value
    return "physical RAM", total


def rss_bytes() -> int:
    import psutil
    return psutil.Process().memory_info().rss


def _physical_ram() -> int:
    import psutil
    return psutil.virtual_memory().total


def _gib(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GiB"


class MemoryWatchdog:
    """One check() per interval; returns the warning it logged, else None."""

    def __init__(self, *, limits: Dict[str, Optional[int]],
                 rss: Callable[[], int] = rss_bytes,
                 total: Optional[int] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self._rss = rss
        self._clock = clock
        self.budget_name, self.budget_bytes = budget(
            limits, total if total is not None else _physical_ram())
        self._warned_at: Optional[float] = None
        self._warned_fraction = 0.0

    def _describe_limits(self) -> str:
        parts = [f"{name}={_gib(value) if value else 'unset'}"
                 for name, value in self.limits.items()]
        return ", ".join(parts)

    def check(self) -> Optional[str]:
        try:
            rss = self._rss()
        except Exception as e:  # noqa: BLE001 - a watchdog must never die
            logger.debug("[MEM-WATCHDOG] RSS read failed: %s", e)
            return None
        fraction = rss / self.budget_bytes if self.budget_bytes else 0.0
        now = self._clock()
        if fraction < WARN_FRACTION:
            if self._warned_at is not None and fraction < WARN_FRACTION - RECOVER_MARGIN:
                logger.info("[MEM-WATCHDOG] RSS %s is back under %d%% of %s (%s)",
                            _gib(rss), round(WARN_FRACTION * 100),
                            self.budget_name, _gib(self.budget_bytes))
                self._warned_at = None
                self._warned_fraction = 0.0
            return None
        due = (self._warned_at is None
               or now - self._warned_at >= REPEAT_WARNING_S
               or fraction - self._warned_fraction >= CLIMB_STEP)
        if not due:
            return None
        message = (
            f"[MEM-WATCHDOG] RSS {_gib(rss)} is {round(fraction * 100)}% of "
            f"{self.budget_name} ({_gib(self.budget_bytes)}; {self._describe_limits()}). "
            "The kernel throttles the service at memory.high and kills it at "
            "memory.max (systemd then restarts it); a steady climb here is a leak.")
        logger.warning("%s", message)
        self._warned_at = now
        self._warned_fraction = fraction
        return message


def default_watchdog() -> MemoryWatchdog:
    """A watchdog over this process and the limits of its own cgroup."""
    return MemoryWatchdog(limits=memory_limits(own_cgroup_dir()))


async def _watch(dog, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        dog.check()


def start_memory_watchdog(dog=None, interval: float = CHECK_INTERVAL_S) -> "asyncio.Task":
    """Start the loop on the running event loop and return its task."""
    if dog is None:
        dog = default_watchdog()
        logger.info("[MEM-WATCHDOG] watching RSS against %s (%s); warns at %d%%",
                    dog.budget_name, dog._describe_limits(), round(WARN_FRACTION * 100))
    return asyncio.get_running_loop().create_task(_watch(dog, interval),
                                                  name="memory-watchdog")
