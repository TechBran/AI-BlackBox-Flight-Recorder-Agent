"""Shutdown stops the background task worker before anything else stops.

Until uvicorn owned SIGTERM the process hard-exited, so nothing stopped the
worker thread. Once shutdown really ran, the worker kept taking queued tasks
(media, chat, CU, CLI agents) and starting them while the shutdown hooks tore
down the SMS, Asterisk and scheduler subsystems those tasks use (audit
X05-architecture-04: "set worker_running=False and join with a timeout").
The lifespan's first shutdown phase, "stopping", now does that.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.models as models_module
import Orchestrator.tasks as tasks_mod
from Orchestrator.models import TaskStatus, TaskType


@pytest.fixture
def fake_task_db(monkeypatch):
    store = {}

    class FakeDB:
        def get_task(self, task_id):
            return store.get(task_id)

        def save_task(self, task):
            store[task.task_id] = task

        def get_all_tasks(self, operator=None):
            return list(store.values())

    monkeypatch.setattr(tasks_mod, "task_db", FakeDB())
    return store


@pytest.fixture
def worker_env(fake_task_db, monkeypatch):
    """A clean queue, no worker running, and a process_task that records each
    task it starts and holds it until `release` is set."""
    if models_module.worker_running:
        pytest.skip("a background worker is already running in this process")
    with tasks_mod.task_lock:
        tasks_mod.task_queue.clear()
    started = []
    release = threading.Event()
    first_started = threading.Event()

    def fake_process_task(task):
        started.append(task.task_id)
        first_started.set()
        release.wait(timeout=15)

    monkeypatch.setattr(tasks_mod, "process_task", fake_process_task)
    env = {"started": started, "release": release, "first_started": first_started}
    yield env
    release.set()
    models_module.worker_running = False
    thread = getattr(tasks_mod, "_worker_thread", None)
    if thread is not None:
        thread.join(5)
    monkeypatch.setattr(tasks_mod, "_worker_thread", None, raising=False)
    with tasks_mod.task_lock:
        tasks_mod.task_queue.clear()


def test_no_queued_task_starts_once_the_worker_is_stopped(worker_env):
    first = tasks_mod.create_task(TaskType.IMAGE_GENERATION, operator="system", prompt="a")
    thread = tasks_mod.start_background_worker()
    assert worker_env["first_started"].wait(10), "the worker never started the first task"

    running = tasks_mod.stop_background_worker(timeout=5)

    assert not thread.is_alive(), "the worker loop did not end"
    assert models_module.worker_running is False
    # The task already running is reported (it ends with the process), not killed.
    assert running == [first.task_id]

    later = tasks_mod.create_task(TaskType.IMAGE_GENERATION, operator="system", prompt="b")
    time.sleep(1.2)  # more than two passes of the worker's 0.5 s loop
    assert worker_env["started"] == [first.task_id]
    assert tasks_mod.task_db.get_task(later.task_id).status == TaskStatus.PENDING
    with tasks_mod.task_lock:
        assert later.task_id in tasks_mod.task_queue


def test_a_stop_landing_mid_pass_starts_nothing_and_loses_nothing(worker_env, monkeypatch):
    """The worker picks a task, then shutdown stops it before the submit: the
    task must neither start nor drop out of the queue."""
    real_select = tasks_mod._select_runnable
    picked = {}

    def select_then_stop(candidates, active_types):
        chosen = real_select(candidates, active_types)
        if chosen is not None and not picked:
            picked["id"] = chosen
            models_module.worker_running = False   # shutdown lands right here
        return chosen

    monkeypatch.setattr(tasks_mod, "_select_runnable", select_then_stop)
    queued = tasks_mod.create_task(TaskType.IMAGE_GENERATION, operator="system", prompt="a")
    thread = tasks_mod.start_background_worker()
    thread.join(5)

    assert not thread.is_alive()
    assert picked.get("id") == queued.task_id
    assert worker_env["started"] == []
    with tasks_mod.task_lock:
        assert queued.task_id in tasks_mod.task_queue


def test_stopping_is_a_no_op_when_the_app_never_started_the_worker(monkeypatch):
    """Under the test suite the boot hook that starts the worker is skipped; the
    stop hook then must not touch a worker a test runs on its own."""
    monkeypatch.setattr(tasks_mod, "_worker_thread", None, raising=False)
    monkeypatch.setattr(models_module, "worker_running", True)
    assert tasks_mod.stop_background_worker(timeout=0.1) == []
    assert models_module.worker_running is True


def test_starting_twice_keeps_one_worker(worker_env):
    first = tasks_mod.start_background_worker()
    assert tasks_mod.start_background_worker() is first
    tasks_mod.stop_background_worker(timeout=5)
    assert not first.is_alive()


# ── the lifespan's "stopping" phase ──────────────────────────────────────────

def test_stopping_hooks_run_before_tasks_are_cancelled_and_shutdown_hooks_run(monkeypatch):
    monkeypatch.delenv("BLACKBOX_TESTING", raising=False)
    from Orchestrator import lifespan as ls
    app = FastAPI()
    calls = []
    seen = {}

    @ls.lifespan_hook(app, "startup")
    async def start_loop():
        seen["task"] = app.state.background_tasks.track(
            asyncio.get_running_loop().create_task(asyncio.Event().wait()), "loop")

    @ls.lifespan_hook(app, "stopping")
    async def stop_intake():
        calls.append("stopping")
        seen["running_when_stopping"] = not seen["task"].done()

    @ls.lifespan_hook(app, "stopping")
    def stop_raises():
        calls.append("stopping-2")
        raise RuntimeError("worker already gone")

    @ls.lifespan_hook(app, "shutdown")
    async def stop_subsystem():
        calls.append("shutdown")
        seen["cancelled_by_shutdown_hook"] = seen["task"].done()

    with TestClient(app):
        pass
    assert calls == ["stopping", "stopping-2", "shutdown"]
    assert seen["running_when_stopping"] is True
    assert seen["cancelled_by_shutdown_hook"] is True


def test_the_app_stops_the_worker_in_its_stopping_phase():
    from Orchestrator import startup
    from Orchestrator.lifespan import hook_name, lifespan_of
    names = [hook_name(h) for h in lifespan_of(startup.app).hooks("stopping")]
    assert names == ["Orchestrator.startup.shutdown_task_worker"]


def test_the_stop_hook_runs_the_stop_off_the_event_loop(monkeypatch):
    from Orchestrator import startup
    seen = {}

    def fake_stop(timeout=None):
        seen["thread"] = threading.current_thread()
        return ["t-1"]

    monkeypatch.setattr(tasks_mod, "stop_background_worker", fake_stop)
    asyncio.run(startup.shutdown_task_worker())
    assert seen["thread"] is not threading.main_thread()
