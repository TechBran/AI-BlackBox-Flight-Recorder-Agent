"""The app runs one lifespan: its hooks in a fixed order, and a registry of the
long-lived tasks those hooks start, cancelled at shutdown.

The base code registered 33 @app.on_event hooks (removed in Starlette 1.0),
and the loops they started (CU display reaper, live-session reaper, zellij
reaper, embeddings watcher, migration resume, ...) kept no reference and were
never stopped (audit O01-core-runtime-02, rank 70).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO = pathlib.Path(__file__).resolve().parents[2]


def _lifespan():
    # Imported per test, so the real-app checks below fail for the right reason
    # (no such module) on a tree without it.
    from Orchestrator import lifespan
    return lifespan


def _testing():
    from Orchestrator.utils import testing
    return testing


# ── the mechanism, on a throwaway app ────────────────────────────────────────

def test_hooks_run_in_registration_order(monkeypatch):
    monkeypatch.delenv("BLACKBOX_TESTING", raising=False)
    ls, testing = _lifespan(), _testing()
    app = FastAPI()
    calls = []

    @testing.boot_hook(app, "startup")
    def first():
        calls.append("start-1")

    @ls.lifespan_hook(app, "startup")
    async def second():
        calls.append("start-2")

    @testing.boot_hook(app, "startup")
    async def third():
        calls.append("start-3")

    @testing.boot_hook(app, "shutdown")
    async def stop_one():
        calls.append("stop-1")

    @ls.lifespan_hook(app, "shutdown")
    def stop_two():
        calls.append("stop-2")

    with TestClient(app):
        assert calls == ["start-1", "start-2", "start-3"]
    assert calls == ["start-1", "start-2", "start-3", "stop-1", "stop-2"]
    assert [ls.hook_name(h) for h in ls.lifespan_of(app).hooks("startup")] == [
        f"{__name__}.test_hooks_run_in_registration_order.<locals>.{n}"
        for n in ("first", "second", "third")]


def test_shutdown_cancels_registered_tasks_before_the_shutdown_hooks(monkeypatch):
    monkeypatch.delenv("BLACKBOX_TESTING", raising=False)
    ls = _lifespan()
    app = FastAPI()
    seen = {}

    async def forever():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    @ls.lifespan_hook(app, "startup")
    async def start_loop():
        seen["task"] = app.state.background_tasks.track(
            asyncio.get_running_loop().create_task(forever()), "forever-loop")

    @ls.lifespan_hook(app, "shutdown")
    async def stop():
        # The registry has already cancelled the loop when the hooks run.
        seen["done_at_shutdown_hook"] = seen["task"].done()

    with TestClient(app):
        assert app.state.background_tasks.names() == ["forever-loop"]
        assert not seen["task"].done()
    assert seen["cancelled"] is True
    assert seen["task"].cancelled()
    assert seen["done_at_shutdown_hook"] is True
    assert app.state.background_tasks.names() == []


def test_a_failing_or_hanging_shutdown_hook_does_not_skip_the_rest(monkeypatch):
    monkeypatch.delenv("BLACKBOX_TESTING", raising=False)
    ls = _lifespan()
    monkeypatch.setattr(ls, "HOOK_TIMEOUT_S", 0.05)
    app = FastAPI()
    calls = []

    @ls.lifespan_hook(app, "shutdown")
    async def raises():
        calls.append("raises")
        raise RuntimeError("ARI already gone")

    @ls.lifespan_hook(app, "shutdown")
    async def hangs():
        calls.append("hangs")
        await asyncio.Event().wait()

    @ls.lifespan_hook(app, "shutdown")
    async def last():
        calls.append("last")

    with TestClient(app):
        pass
    assert calls == ["raises", "hangs", "last"]


def test_a_failed_startup_stops_what_it_had_started(monkeypatch):
    monkeypatch.delenv("BLACKBOX_TESTING", raising=False)
    ls = _lifespan()
    app = FastAPI()
    seen = {}

    @ls.lifespan_hook(app, "startup")
    async def starts_a_loop():
        seen["task"] = app.state.background_tasks.track(
            asyncio.get_running_loop().create_task(asyncio.Event().wait()), "loop")

    @ls.lifespan_hook(app, "startup")
    def breaks():
        raise RuntimeError("index unreadable")

    @ls.lifespan_hook(app, "startup")
    def never():
        seen["never"] = True

    @ls.lifespan_hook(app, "shutdown")
    def stop():
        seen["stopped"] = True

    with pytest.raises(RuntimeError, match="index unreadable"):
        with TestClient(app):
            pass
    assert "never" not in seen
    assert seen["stopped"] is True
    assert seen["task"].cancelled()


def test_testing_mode_skips_gated_hooks_and_runs_plain_ones(monkeypatch):
    monkeypatch.setenv("BLACKBOX_TESTING", "1")
    ls, testing = _lifespan(), _testing()
    app = FastAPI()
    calls = []

    @testing.boot_hook(app, "startup")
    async def reaches_out():
        calls.append("gated")

    @ls.lifespan_hook(app, "startup")
    async def in_process():
        calls.append("plain")

    with TestClient(app):
        pass
    assert calls == ["plain"]


def test_the_registry_holds_tasks_until_they_end():
    ls = _lifespan()
    registry = ls.TaskRegistry()

    async def scenario():
        quick = registry.track(asyncio.get_running_loop().create_task(asyncio.sleep(0)), "quick")
        slow = registry.track(asyncio.get_running_loop().create_task(asyncio.sleep(30)), "slow")
        assert registry.names() == ["quick", "slow"]
        await quick
        await asyncio.sleep(0)
        assert registry.names() == ["slow"]
        assert registry.track(None) is None
        with pytest.raises(TypeError):
            registry.track(object())
        assert await registry.cancel_all(timeout=1) == 1
        assert slow.cancelled() and registry.names() == []

    asyncio.run(scenario())


def test_the_registry_leaves_another_loops_tasks_alone():
    ls = _lifespan()
    registry = ls.TaskRegistry()
    other = asyncio.new_event_loop()
    try:
        foreign = other.create_task(asyncio.sleep(30))
        registry.track(foreign, "foreign")

        async def scenario():
            return await registry.cancel_all(timeout=0.1)

        assert asyncio.run(scenario()) == 0
        assert not foreign.cancelled()
        foreign.cancel()
        other.run_until_complete(asyncio.gather(foreign, return_exceptions=True))
    finally:
        other.close()


# ── the real app ─────────────────────────────────────────────────────────────

# Every hook of the production app, in the order the lifespan runs them. The
# order is import order (lifespan.py); the docstrings of startup.py's hooks say
# which neighbours they depend on. Change this list only with the move.
PRODUCTION_STARTUP = [
    "Orchestrator.startup.startup_check_index",
    "Orchestrator.startup._cu_display_reaper",
    "Orchestrator.startup.startup_storage_maintenance",
    "Orchestrator.startup.startup_memory_watchdog",
    "Orchestrator.startup.startup_embeddings_transcode",
    "Orchestrator.startup.startup_strip_retired_seed_contacts",
    "Orchestrator.startup.startup_embeddings_migration_resume",
    "Orchestrator.startup.startup_embeddings_watcher",
    "Orchestrator.startup.startup_notify_bridge",
    "Orchestrator.startup.startup_live_session_reaper",
    "Orchestrator.startup.startup_assert_sudoers_current",
    "Orchestrator.startup.startup_assert_helpers_current",
    "Orchestrator.startup.startup_assert_url_handlers",
    "Orchestrator.startup.startup_toolvault_embedding_sync",
    "Orchestrator.startup.startup_scheduler",
    "Orchestrator.startup.startup_cellular",
    "Orchestrator.startup.startup_cellular_internet",
    "Orchestrator.startup.startup_phone_pin_check",
    "Orchestrator.startup.startup_web_search_floor_check",
    "Orchestrator.startup.startup_local_stack_sweep",
    "Orchestrator.startup.startup_asterisk",
    "Orchestrator.startup.startup_sms",
    "Orchestrator.startup.startup_cli_agent_zellij",
    "Orchestrator.startup.startup_cli_agent_zellij_reaper",
    "Orchestrator.routes.tts_routes._tts_queue_capture_loop",
    "Orchestrator.routes.agent_routes.start_agent_session_reaper",
    "Orchestrator.routes.gemini_agent_routes.start_gemini_session_reaper",
]
PRODUCTION_SHUTDOWN = [
    "Orchestrator.startup.shutdown_sms",
    "Orchestrator.startup.shutdown_asterisk",
    "Orchestrator.startup.shutdown_cellular_internet",
    "Orchestrator.startup.shutdown_cellular",
    "Orchestrator.startup.shutdown_scheduler",
    "Orchestrator.routes.tts_routes._tts_queue_shutdown",
]


def test_the_app_is_built_on_the_one_lifespan():
    from Orchestrator.app import app
    ls = _lifespan()
    assert app.state.lifespan is ls.APP_LIFESPAN
    assert app.state.background_tasks is ls.APP_LIFESPAN.tasks
    assert ls.lifespan_of(app) is ls.APP_LIFESPAN


_LIST_HOOKS = """
import json
from Orchestrator.app import app
from Orchestrator.lifespan import hook_name, lifespan_of
lifespan = lifespan_of(app)
print(json.dumps({event: [hook_name(h) for h in lifespan.hooks(event)]
                  for event in ("startup", "shutdown")}))
"""


def test_production_hook_order(tmp_path):
    """The order uvicorn gets: a fresh process that imports Orchestrator.app
    first, as `uvicorn Orchestrator.app:app` does. (Inside the suite a test may
    import a route module before the app, which registers its hooks earlier.)
    It runs from a scratch directory, where config.py's relative stores land."""
    config = REPO / "config.ini"
    shutil.copy(config if config.is_file() else REPO / "config.ini.template",
                tmp_path / "config.ini")
    env = dict(os.environ, PYTHONPATH=str(REPO), BLACKBOX_TESTING="1")
    run = subprocess.run([sys.executable, "-c", _LIST_HOOKS], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    hooks = json.loads(run.stdout.strip().splitlines()[-1])
    assert hooks["startup"] == PRODUCTION_STARTUP
    assert hooks["shutdown"] == PRODUCTION_SHUTDOWN


def test_no_hook_bypasses_the_lifespan():
    """With lifespan= set, a hook added through FastAPI's leftover on_event
    would silently never run. None is registered, and no module calls it."""
    from Orchestrator.app import app
    assert list(getattr(app.router, "on_startup", [])) == []
    assert list(getattr(app.router, "on_shutdown", [])) == []
    offenders = []
    pattern = re.compile(r"\.(on_event|add_event_handler)\(")
    for root in ("Orchestrator", "ToolVault"):
        for path in (REPO / root).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if "/tests/" in rel or "/venv/" in rel or rel.startswith("Orchestrator/tests"):
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{rel}:{n}: {line.strip()}")
    assert offenders == []


# Each startup hook that starts a long-lived loop, the function it starts it
# with, and the registry name it is tracked under. (The migration resume hook's
# task is registered by migrate._launch: test_long_lived_tasks_tracked.py.)
_LOOP_HOOKS = [
    ("Orchestrator.startup", "startup_memory_watchdog",
     "Orchestrator.memory_watchdog", "start_memory_watchdog", "memory-watchdog"),
    ("Orchestrator.startup", "startup_embeddings_watcher",
     "Orchestrator.embeddings.watcher", "start_watcher", "embeddings-watcher"),
    ("Orchestrator.startup", "startup_live_session_reaper",
     "Orchestrator.live_session_reaper", "start_reaper", "live-session-reaper"),
]


@pytest.mark.parametrize("hook_module,hook,start_module,start,name", _LOOP_HOOKS)
def test_loop_hooks_register_their_task(monkeypatch, hook_module, hook, start_module, start, name):
    import importlib
    ls = _lifespan()
    mod = importlib.import_module(start_module)
    hook_fn = getattr(importlib.import_module(hook_module), hook)

    async def scenario():
        started = asyncio.get_running_loop().create_task(asyncio.Event().wait())
        monkeypatch.setattr(mod, start, lambda *a, **k: started)
        try:
            await hook_fn()
            assert ls.APP_LIFESPAN.tasks.names().count(name) == 1
        finally:
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
        assert name not in ls.APP_LIFESPAN.tasks.names()

    asyncio.run(scenario())


def test_the_cu_reaper_and_storage_loops_are_registered(monkeypatch):
    ls = _lifespan()
    from Orchestrator import startup
    from Orchestrator.browser import display, native_stream

    class _Alloc:
        def reap_orphans(self):
            pass

    monkeypatch.setattr(display, "get_allocator", lambda: _Alloc())
    monkeypatch.setattr(native_stream, "reap_orphan_stream_procs", lambda: None)

    async def scenario():
        await startup._cu_display_reaper()
        await startup.startup_storage_maintenance()
        names = ls.APP_LIFESPAN.tasks.names()
        assert "cu-display-reaper" in names and "storage-maintenance" in names
        assert await ls.APP_LIFESPAN.tasks.cancel_all(timeout=1) >= 2

    asyncio.run(scenario())


def test_the_zellij_reaper_is_registered(monkeypatch):
    ls = _lifespan()
    from Orchestrator import cli_agent, startup
    from Orchestrator.cli_agent import reaper

    monkeypatch.setattr(cli_agent, "get_backend", lambda: "zellij")

    async def scenario():
        started = asyncio.get_running_loop().create_task(asyncio.Event().wait())
        monkeypatch.setattr(reaper, "start_zellij_reaper", lambda: started)
        await startup.startup_cli_agent_zellij_reaper()
        assert "zellij-reaper" in ls.APP_LIFESPAN.tasks.names()
        started.cancel()
        await asyncio.gather(started, return_exceptions=True)

    asyncio.run(scenario())


def test_the_agent_session_reapers_are_registered(monkeypatch):
    ls = _lifespan()
    from Orchestrator.routes import agent_routes, gemini_agent_routes

    async def scenario():
        await agent_routes.start_agent_session_reaper()
        await gemini_agent_routes.start_gemini_session_reaper()
        names = ls.APP_LIFESPAN.tasks.names()
        assert "agent-session-reaper" in names and "gemini-session-reaper" in names
        await ls.APP_LIFESPAN.tasks.cancel_all(timeout=1)

    asyncio.run(scenario())
