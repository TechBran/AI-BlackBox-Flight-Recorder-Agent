"""Logged memory watchdog for blackbox.service. It warns; it never acts.

blackbox.service used to recycle itself with uvicorn's --limit-max-requests
100000: past that many requests uvicorn exits and systemd starts it again,
which on a busy box was an unplanned outage every few days (in-flight chat,
voice, CU and WebSocket sessions dropped, then the 60-90 s index rebuild),
leak or no leak (audit X06-ops-deploy-01). The unit now bounds memory instead:
MemoryHigh= makes the kernel throttle and reclaim, MemoryMax= is the hard cap
(an OOM kill, which Restart=always recovers). This loop reads, every
CHECK_INTERVAL_S, the process's resident memory and the unit's working set,
and logs a warning when the larger reaches WARN_FRACTION of the lowest of
those limits, so a leak shows up in the journal as a climb well before either
one bites. The kernel enforces the limits on the whole cgroup, not on this
process: memory.current also counts the CLI agents KillMode=process keeps in
the unit, the CU browsers and displays, the audio subprocess and page cache.
The working set is memory.current less inactive_file, the page cache the
kernel drops first (the kubelet measures a container the same way). It also
logs each OOM kill the kernel records for the unit (memory.events). Off
systemd (a dev run, cgroup v1) it measures RSS against physical RAM.

Reads /proc and /sys only; stdlib plus psutil (already in requirements.in).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

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


def _read_keyed(path: Path) -> Dict[str, int]:
    """A cgroup "key value" file (memory.stat, memory.events) as ints; {} if unreadable."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {}
    values = {}
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1])
    return values


def unit_memory(cgroup_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    """The cgroup's memory.current, its working set (memory.current less
    inactive_file) and its memory.events counters; None without a readable
    memory.current (no cgroup v2)."""
    if cgroup_dir is None:
        return None
    cgroup_dir = Path(cgroup_dir)
    current = _read_limit(cgroup_dir / "memory.current")
    if current is None:
        return None
    inactive_file = _read_keyed(cgroup_dir / "memory.stat").get("inactive_file", 0)
    return {"current": current,
            "working_set": max(0, current - inactive_file),
            "events": _read_keyed(cgroup_dir / "memory.events")}


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
                 unit: Callable[[], Optional[Dict[str, Any]]] = lambda: None,
                 total: Optional[int] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self._rss = rss
        self._unit = unit
        self._clock = clock
        self.budget_name, self.budget_bytes = budget(
            limits, total if total is not None else _physical_ram())
        self._warned_at: Optional[float] = None
        self._warned_fraction = 0.0
        self._oom_kills: Optional[int] = None

    def _describe_limits(self) -> str:
        parts = [f"{name}={_gib(value) if value else 'unset'}"
                 for name, value in self.limits.items()]
        return ", ".join(parts)

    def _describe_usage(self, rss: Optional[int], unit: Optional[Dict[str, Any]]) -> str:
        parts = []
        if rss is not None:
            parts.append(f"Process RSS {_gib(rss)}")
        if unit:
            high = (unit.get("events") or {}).get("high")
            throttled = f"; memory.events high={high}" if high is not None else ""
            parts.append(
                f"unit working set {_gib(unit['working_set'])} (memory.current "
                f"{_gib(unit['current'])}, which also counts the CLI agents, the CU "
                f"browsers, the audio subprocess and page cache{throttled})")
        return "; ".join(parts) + "."

    def _note_oom_kills(self, events: Dict[str, int]) -> None:
        kills = events.get("oom_kill")
        if kills is None:
            return
        # The first reading is a baseline: the cgroup outlives a restart while
        # KillMode=process children still run in it.
        if self._oom_kills is not None and kills > self._oom_kills:
            logger.warning(
                "[MEM-WATCHDOG] the kernel OOM-killed %d process(es) in the unit "
                "since the last check (memory.events oom_kill=%d)",
                kills - self._oom_kills, kills)
        self._oom_kills = kills

    def check(self) -> Optional[str]:
        try:
            rss = self._rss()
        except Exception as e:  # noqa: BLE001 - a watchdog must never die
            logger.debug("[MEM-WATCHDOG] RSS read failed: %s", e)
            rss = None
        try:
            unit = self._unit()
        except Exception as e:  # noqa: BLE001 - a watchdog must never die
            logger.debug("[MEM-WATCHDOG] cgroup read failed: %s", e)
            unit = None
        if unit:
            self._note_oom_kills(unit.get("events") or {})
        readings = []
        if rss is not None:
            readings.append((rss, "RSS"))
        if unit:
            readings.append((unit["working_set"], "unit working set"))
        if not readings:
            return None
        used, label = max(readings)
        fraction = used / self.budget_bytes if self.budget_bytes else 0.0
        now = self._clock()
        if fraction < WARN_FRACTION:
            if self._warned_at is not None and fraction < WARN_FRACTION - RECOVER_MARGIN:
                logger.info("[MEM-WATCHDOG] %s %s is back under %d%% of %s (%s)",
                            label, _gib(used), round(WARN_FRACTION * 100),
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
            f"[MEM-WATCHDOG] {label} {_gib(used)} is {round(fraction * 100)}% of "
            f"{self.budget_name} ({_gib(self.budget_bytes)}; {self._describe_limits()}). "
            f"{self._describe_usage(rss, unit)} "
            "The kernel throttles the service at memory.high and kills it at "
            "memory.max (systemd then restarts it); a steady climb here is a leak.")
        logger.warning("%s", message)
        self._warned_at = now
        self._warned_fraction = fraction
        return message


def default_watchdog() -> MemoryWatchdog:
    """A watchdog over this process and the limits of its own cgroup, and over
    that cgroup's usage when it has a limit (a dev run's cgroup is a terminal
    scope that holds whatever else the terminal runs)."""
    cgroup = own_cgroup_dir()
    limits = memory_limits(cgroup)
    if not any(limits.values()):
        return MemoryWatchdog(limits=limits)
    return MemoryWatchdog(limits=limits, unit=lambda: unit_memory(cgroup))


async def _watch(dog, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        dog.check()


def start_memory_watchdog(dog=None, interval: float = CHECK_INTERVAL_S) -> "asyncio.Task":
    """Start the loop on the running event loop and return its task."""
    if dog is None:
        dog = default_watchdog()
        logger.info("[MEM-WATCHDOG] watching RSS and the unit's working set against "
                    "%s (%s); warns at %d%%",
                    dog.budget_name, dog._describe_limits(), round(WARN_FRACTION * 100))
    return asyncio.get_running_loop().create_task(_watch(dog, interval),
                                                  name="memory-watchdog")
