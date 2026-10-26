"""The logged RSS watchdog that replaced uvicorn's --limit-max-requests recycle.

The unit used to restart the service every 100000 requests whether or not
anything leaked (audit X06-ops-deploy-01). It now caps memory with MemoryHigh=
and MemoryMax=, and memory_watchdog only says in the journal, ahead of either,
that the process's resident memory is climbing toward them. It never acts.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from Orchestrator import memory_watchdog as mw

GIB = 1024 ** 3


def _cgroup_tree(tmp_path, proc_line, files):
    proc = tmp_path / "proc_self_cgroup"
    proc.write_text(proc_line)
    root = tmp_path / "sys_fs_cgroup"
    unit = root / "system.slice" / "blackbox.service"
    unit.mkdir(parents=True)
    for name, value in files.items():
        (unit / name).write_text(value)
    return proc, root, unit


def test_own_cgroup_dir_reads_the_unified_hierarchy(tmp_path):
    proc, root, unit = _cgroup_tree(tmp_path, "0::/system.slice/blackbox.service\n", {})
    assert mw.own_cgroup_dir(proc_cgroup=proc, root=root) == unit


@pytest.mark.parametrize("content", [
    "4:memory:/system.slice/blackbox.service\n1:name=systemd:/system.slice/blackbox.service\n",
    "",
])
def test_own_cgroup_dir_is_none_without_a_v2_entry(tmp_path, content):
    proc, root, _ = _cgroup_tree(tmp_path, content, {})
    assert mw.own_cgroup_dir(proc_cgroup=proc, root=root) is None


def test_own_cgroup_dir_is_none_when_proc_is_unreadable(tmp_path):
    assert mw.own_cgroup_dir(proc_cgroup=tmp_path / "absent", root=tmp_path) is None


def test_memory_limits_reads_high_and_max(tmp_path):
    _, _, unit = _cgroup_tree(tmp_path, "0::/system.slice/blackbox.service\n",
                              {"memory.high": f"{7 * GIB}\n", "memory.max": "max\n"})
    assert mw.memory_limits(unit) == {"memory.high": 7 * GIB, "memory.max": None}


def test_memory_limits_without_a_cgroup_or_files(tmp_path):
    assert mw.memory_limits(None) == {"memory.high": None, "memory.max": None}
    assert mw.memory_limits(tmp_path) == {"memory.high": None, "memory.max": None}
    (tmp_path / "memory.high").write_text("garbage\n")
    assert mw.memory_limits(tmp_path)["memory.high"] is None


def test_budget_is_the_lowest_limit_else_physical_ram():
    assert mw.budget({"memory.high": 7 * GIB, "memory.max": 11 * GIB}, total=30 * GIB) \
        == ("memory.high", 7 * GIB)
    assert mw.budget({"memory.high": None, "memory.max": 11 * GIB}, total=30 * GIB) \
        == ("memory.max", 11 * GIB)
    # A dev run outside systemd (or a box on cgroup v1) still gets a reference.
    assert mw.budget({"memory.high": None, "memory.max": None}, total=30 * GIB) \
        == ("physical RAM", 30 * GIB)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _watchdog(rss, clock, limits=None):
    return mw.MemoryWatchdog(
        limits=limits or {"memory.high": 10 * GIB, "memory.max": 12 * GIB},
        rss=lambda: rss[0], total=30 * GIB, clock=clock)


def test_no_warning_below_the_threshold(caplog):
    rss, clock = [7 * GIB], _Clock()
    dog = _watchdog(rss, clock)
    with caplog.at_level(logging.WARNING, logger="blackbox.memory_watchdog"):
        assert dog.check() is None
    assert caplog.records == []


def test_warns_once_on_crossing_then_rate_limits(caplog):
    rss, clock = [int(8.5 * GIB)], _Clock()
    dog = _watchdog(rss, clock)
    with caplog.at_level(logging.WARNING, logger="blackbox.memory_watchdog"):
        first = dog.check()
        assert first is not None
        assert "85%" in first and "memory.high" in first and "memory.max" in first
        # Same level a minute later: no second line.
        clock.now += 60
        assert dog.check() is None
        # Still there after the repeat interval: say it again.
        clock.now += mw.REPEAT_WARNING_S
        assert dog.check() is not None
    assert [r.levelno for r in caplog.records] == [logging.WARNING, logging.WARNING]


def test_warns_again_when_it_keeps_climbing(caplog):
    rss, clock = [int(8.0 * GIB)], _Clock()
    dog = _watchdog(rss, clock)
    assert dog.check() is not None
    clock.now += 60
    rss[0] = int(8.3 * GIB)          # +3 points: not yet
    assert dog.check() is None
    rss[0] = int(8.6 * GIB)          # +6 points since the last warning
    assert dog.check() is not None


def test_recovery_resets_the_warning(caplog):
    rss, clock = [int(8.5 * GIB)], _Clock()
    dog = _watchdog(rss, clock)
    assert dog.check() is not None
    rss[0] = 6 * GIB
    with caplog.at_level(logging.INFO, logger="blackbox.memory_watchdog"):
        assert dog.check() is None
    assert any("back under" in r.getMessage() for r in caplog.records)
    clock.now += 60
    rss[0] = int(8.5 * GIB)
    assert dog.check() is not None


def test_check_never_raises_when_rss_cannot_be_read(caplog):
    def broken():
        raise OSError("no /proc")
    dog = mw.MemoryWatchdog(limits={"memory.high": None, "memory.max": None},
                            rss=broken, total=30 * GIB, clock=_Clock())
    assert dog.check() is None


def test_the_loop_checks_on_its_interval_and_cancels_cleanly():
    calls = []

    class _Dog:
        def check(self):
            calls.append(1)

    async def scenario():
        task = mw.start_memory_watchdog(dog=_Dog(), interval=0.01)
        await asyncio.sleep(0.06)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(calls) >= 2


def test_the_watchdog_is_started_by_a_gated_boot_hook():
    from Orchestrator import startup
    from Orchestrator.utils.testing import BOOT_HOOK_ATTR
    hook = getattr(startup, "startup_memory_watchdog", None)
    assert hook is not None, "nothing starts the memory watchdog"
    from Orchestrator.lifespan import lifespan_of
    registered = [h for h in lifespan_of(startup.app).hooks("startup")
                  if getattr(h, "__wrapped__", None) is hook]
    assert registered and getattr(registered[0], BOOT_HOOK_ATTR, False)


# ── the unit's own usage, which is what the kernel enforces the limits on ────
# memory.high and memory.max apply to the whole cgroup: the orchestrator plus
# the CLI agents KillMode=process keeps in the unit, the CU browsers and
# displays, the audio subprocess and page cache. RSS alone can sit far below
# while the unit is throttled or OOM-killed.

def _unit_files(tmp_path, current, stat, events):
    unit = tmp_path / "unit"
    unit.mkdir(exist_ok=True)
    (unit / "memory.current").write_text(f"{current}\n")
    if stat is not None:
        (unit / "memory.stat").write_text(stat)
    if events is not None:
        (unit / "memory.events").write_text(events)
    return unit


def test_unit_memory_reads_current_working_set_and_events(tmp_path):
    unit = _unit_files(
        tmp_path, 9 * GIB,
        f"anon {5 * GIB}\nfile {4 * GIB}\nactive_file {GIB}\ninactive_file {3 * GIB}\n",
        "low 0\nhigh 7\nmax 1\noom 1\noom_kill 1\noom_group_kill 0\n")
    usage = mw.unit_memory(unit)
    assert usage["current"] == 9 * GIB
    # The kubelet's working set: memory.current less the inactive page cache
    # the kernel drops first.
    assert usage["working_set"] == 6 * GIB
    assert usage["events"] == {"low": 0, "high": 7, "max": 1, "oom": 1,
                               "oom_kill": 1, "oom_group_kill": 0}


def test_unit_memory_without_stat_or_events_or_a_cgroup(tmp_path):
    unit = _unit_files(tmp_path, 2 * GIB, None, None)
    assert mw.unit_memory(unit) == {"current": 2 * GIB, "working_set": 2 * GIB, "events": {}}
    assert mw.unit_memory(None) is None
    assert mw.unit_memory(tmp_path / "absent") is None


def _unit_watchdog(rss, unit, clock):
    return mw.MemoryWatchdog(
        limits={"memory.high": 10 * GIB, "memory.max": 12 * GIB},
        rss=lambda: rss[0], unit=lambda: unit[0], total=30 * GIB, clock=clock)


def test_warns_on_the_units_working_set_while_rss_is_low(caplog):
    rss = [2 * GIB]
    unit = [{"current": 9 * GIB, "working_set": int(8.5 * GIB), "events": {"high": 3}}]
    dog = _unit_watchdog(rss, unit, _Clock())
    with caplog.at_level(logging.WARNING, logger="blackbox.memory_watchdog"):
        message = dog.check()
    assert message is not None
    assert "85%" in message and "working set" in message and "memory.current" in message
    assert "RSS 2.0 GiB" in message


def test_the_unit_reading_is_optional(caplog):
    rss = [int(8.5 * GIB)]
    dog = _unit_watchdog(rss, [None], _Clock())
    message = dog.check()
    assert message is not None and "RSS 8.5 GiB" in message


def test_an_oom_kill_in_the_unit_is_logged_once(caplog):
    rss = [GIB]
    unit = [{"current": 3 * GIB, "working_set": 3 * GIB, "events": {"oom_kill": 2}}]
    clock = _Clock()
    dog = _unit_watchdog(rss, unit, clock)
    with caplog.at_level(logging.WARNING, logger="blackbox.memory_watchdog"):
        # Counts from before this process started (the cgroup outlives a
        # restart while KillMode=process children run) are only a baseline.
        assert dog.check() is None
        unit[0] = {"current": 3 * GIB, "working_set": 3 * GIB, "events": {"oom_kill": 3}}
        clock.now += 120
        dog.check()
        clock.now += 120
        dog.check()
    oom = [r.getMessage() for r in caplog.records if "OOM" in r.getMessage()]
    assert len(oom) == 1
    assert "1 process" in oom[0] and "oom_kill=3" in oom[0]


def test_default_watchdog_reads_its_own_cgroup(tmp_path, monkeypatch):
    unit = _unit_files(tmp_path, 4 * GIB, f"inactive_file {GIB}\n", "oom_kill 0\n")
    (unit / "memory.high").write_text(f"{10 * GIB}\n")
    (unit / "memory.max").write_text("max\n")
    monkeypatch.setattr(mw, "own_cgroup_dir", lambda: unit)
    dog = mw.default_watchdog()
    assert dog.budget_name == "memory.high"
    assert dog._unit()["working_set"] == 3 * GIB


def test_default_watchdog_ignores_a_cgroup_without_limits(tmp_path, monkeypatch):
    """A dev run's cgroup is the terminal's scope: its usage is not ours."""
    unit = _unit_files(tmp_path, 20 * GIB, None, "oom_kill 0\n")
    monkeypatch.setattr(mw, "own_cgroup_dir", lambda: unit)
    dog = mw.default_watchdog()
    assert dog.budget_name == "physical RAM"
    assert dog._unit() is None
