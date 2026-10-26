"""Tasks that outlive the request or callback that started them are held by the
app's lifespan registry (Orchestrator/lifespan.py) and cancelled at shutdown.

The base code started each of these with a bare create_task: an inbound call's
whole handler (ARI StasisStart), an outbound call, an xAI phone call and its
finaliser, the FreeSWITCH event listener. The event loop holds tasks only
weakly, so any of them could be garbage-collected mid-call, and none was
stopped by the lifespan (audit O01-core-runtime-02). The migration and Ollama
pull tasks were already held by module globals; they now also stop with the
lifespan, before the subsystems they use.
"""
from __future__ import annotations

import asyncio

import pytest


def _names():
    from Orchestrator.lifespan import APP_LIFESPAN
    return APP_LIFESPAN.tasks.names()


async def _forever(*_a, **_k):
    await asyncio.Event().wait()


async def _settle_and_cancel(name):
    """The task under `name` is registered; cancel it and see it leave."""
    from Orchestrator.lifespan import APP_LIFESPAN
    await asyncio.sleep(0)
    assert name in APP_LIFESPAN.tasks.names()
    await APP_LIFESPAN.tasks.cancel_all(timeout=1)
    assert name not in APP_LIFESPAN.tasks.names()


def test_ari_event_callbacks_are_tracked():
    from Orchestrator.asterisk.client import AsteriskARIClient

    client = AsteriskARIClient("http://127.0.0.1:8088", "ws://127.0.0.1:8088/ari/events",
                               "user", "pass", "blackbox")
    client.on_stasis_start = _forever
    client.on_hangup = _forever

    async def scenario():
        await client._handle_event({"type": "StasisStart",
                                    "channel": {"id": "ch-1", "caller": {"number": "100"},
                                                "dialplan": {"exten": "200"}},
                                    "args": []})
        await client._handle_event({"type": "ChannelHangupRequest", "channel": {"id": "ch-1"}})
        await asyncio.sleep(0)
        names = _names()
        assert "ari-stasis-start" in names and "ari-hangup" in names
        await _settle_and_cancel("ari-stasis-start")

    asyncio.run(scenario())


def test_an_outbound_asterisk_call_is_tracked(monkeypatch):
    from Orchestrator.routes import asterisk_routes as ar
    from Orchestrator.asterisk import client as ari

    class _Connected:
        is_connected = True

    monkeypatch.setattr(ari, "get_ari_client", lambda: _Connected())
    monkeypatch.setattr(ar, "_handle_outbound_call", _forever)
    monkeypatch.setattr(ar, "PHONE_SESSIONS", {})

    async def scenario():
        result = await ar.asterisk_outbound_call(ar.AsteriskCallRequest(to="5550100123"))
        assert result["status"] == "initiated"
        await _settle_and_cancel("asterisk-outbound-call")

    asyncio.run(scenario())


def test_an_xai_phone_call_attach_is_tracked(monkeypatch):
    from Orchestrator.routes import xai_phone_routes as xr
    from Orchestrator.xai_phone import call_bridge

    monkeypatch.setattr(call_bridge, "attach_call", _forever)

    async def scenario():
        xr._spawn_attach("call-1", {"type": "realtime.call.incoming"})
        await _settle_and_cancel("xai-phone-attach")

    asyncio.run(scenario())


def test_an_embeddings_migration_is_tracked(monkeypatch):
    from Orchestrator.embeddings import migrate

    monkeypatch.setattr(migrate, "_run_engine", _forever)
    monkeypatch.setattr(migrate, "_JOB_TASK", None)

    async def scenario():
        task = migrate._launch("any-slug")
        assert task is migrate._JOB_TASK
        await _settle_and_cancel("embeddings-migration")

    asyncio.run(scenario())


def test_an_ollama_pull_is_tracked(monkeypatch):
    from Orchestrator.embeddings import ollama_io

    monkeypatch.setattr(ollama_io, "_stream_pull", _forever)
    monkeypatch.setattr(ollama_io, "_PULL", None)
    monkeypatch.setattr(ollama_io, "_PULL_TASK", None)

    async def scenario():
        await ollama_io.start_pull("qwen3-embedding:0.6b")
        await _settle_and_cancel("ollama-pull")

    asyncio.run(scenario())


def test_a_cron_catch_up_run_is_tracked(tmp_path, monkeypatch):
    """A catch-up run for a job missed while the box was down can take
    minutes; the lifespan cancels it before the scheduler stops (the next boot
    records the interrupted run as outcome-unknown)."""
    from Orchestrator.scheduler import manager as manager_mod

    monkeypatch.setattr(manager_mod, "DB_PATH", tmp_path / "cron.db")
    mgr = manager_mod.CronJobManager()
    monkeypatch.setattr(mgr, "_execute_job", _forever)

    async def scenario():
        mgr._enqueue_catchup("job-1")
        await _settle_and_cancel("cron-catchup")

    asyncio.run(scenario())


@pytest.mark.parametrize("module", [
    "Orchestrator.asterisk.client",
    "Orchestrator.routes.asterisk_routes",
    "Orchestrator.routes.xai_phone_routes",
    "Orchestrator.xai_phone.call_bridge",
    "Orchestrator.routes.phone_routes",
])
def test_no_untracked_call_task_is_left(module):
    """Every create_task in these call paths hands its task to someone: the
    registry, or a session/attribute that owns it (listener_task and friends)."""
    import importlib
    import inspect
    import re
    src = inspect.getsource(importlib.import_module(module))
    bare = [line.strip() for line in src.splitlines()
            if re.match(r"\s*asyncio\.(create_task|ensure_future)\(", line)]
    assert bare == [], f"{module} starts tasks nobody holds: {bare}"


# ── job tasks a request starts: cancelled before the subsystems they use stop ─

def test_a_cron_run_now_is_tracked(monkeypatch):
    """A manual run goes through the same _execute_job as a catch-up run and
    can take minutes; it stops with the lifespan before the scheduler does."""
    from Orchestrator.routes import cron_routes

    class _Manager:
        def get_job(self, job_id):
            return {"id": job_id}

        run_job_now = staticmethod(_forever)

    monkeypatch.setattr(cron_routes, "get_scheduler_manager", lambda: _Manager())

    async def scenario():
        response = await cron_routes.run_cron_job_now("job-1")
        assert response.status_code == 202
        await _settle_and_cancel("cron-run-now")

    asyncio.run(scenario())


@pytest.fixture
def gemini_cu(monkeypatch):
    """The Gemini CU router with a driver that runs until cancelled and a
    recorder in place of the task-row write."""
    from Orchestrator.routes import gemini_cu_routes as gr
    recorded = []
    monkeypatch.setattr(gr, "_run_task", _forever)
    monkeypatch.setattr(gr, "_record_task_failure",
                        lambda task_id, message, only_if_running=False: recorded.append(message))
    return gr, recorded


def test_a_gemini_cu_driver_on_the_server_loop_is_tracked(gemini_cu, monkeypatch):
    from Orchestrator.notifications import bridge
    gr, recorded = gemini_cu
    monkeypatch.setattr(bridge, "_MAIN_LOOP", None)

    async def scenario():
        gr.start_gemini_cu_task("rt1", "op", "pixel-9", "android", "go", "m")
        await _settle_and_cancel("gemini-cu:rt1")

    asyncio.run(scenario())
    # The cancel reaches the driver's done callback, which fails the row.
    assert recorded and "interrupted" in recorded[0]


def test_a_gemini_cu_driver_started_from_a_tool_thread_is_tracked(gemini_cu, monkeypatch):
    """The non-stream tool path starts the driver from asyncio.run on a worker
    thread; it runs on the server loop and is registered there."""
    import concurrent.futures
    import threading
    from Orchestrator.lifespan import APP_LIFESPAN
    from Orchestrator.notifications import bridge
    gr, recorded = gemini_cu
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(bridge, "_MAIN_LOOP", loop)
    try:
        async def tool_call():
            return gr.start_gemini_cu_task("rt2", "op", "pixel-9", "android", "go", "m")

        fut = asyncio.run(tool_call())
        for _ in range(100):
            if "gemini-cu:rt2" in APP_LIFESPAN.tasks.names():
                break
            threading.Event().wait(0.01)
        assert "gemini-cu:rt2" in APP_LIFESPAN.tasks.names()
        cancelled = asyncio.run_coroutine_threadsafe(
            APP_LIFESPAN.tasks.cancel_all(timeout=1), loop).result(timeout=5)
        assert cancelled == 1
        with pytest.raises(concurrent.futures.CancelledError):
            fut.result(timeout=5)
        assert recorded and "interrupted" in recorded[0]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_an_embeddings_warm_up_is_tracked(monkeypatch):
    from Orchestrator.routes import embeddings_routes as er

    monkeypatch.setattr(er, "EMBEDDING_MODELS", {"local-x": {"provider": "ollama"}})
    monkeypatch.setattr(er, "set_keep_alive", lambda slug, warm: "-1")
    monkeypatch.setattr(er, "_warmup_model", _forever)

    async def scenario():
        await er.embeddings_keep_alive(er.KeepAliveRequest(slug="local-x", warm=True))
        await _settle_and_cancel("embeddings-warmup")

    asyncio.run(scenario())


@pytest.mark.parametrize("module", [
    "Orchestrator.routes.cron_routes",
    "Orchestrator.routes.gemini_cu_routes",
    "Orchestrator.routes.embeddings_routes",
])
def test_no_untracked_job_task_is_left(module):
    import importlib
    import inspect
    import re
    src = inspect.getsource(importlib.import_module(module))
    bare = [line.strip() for line in src.splitlines()
            if re.search(r"(asyncio|here)\.(create_task|ensure_future)\(", line)
            and "track_task(" not in line]
    assert bare == [], f"{module} starts job tasks the lifespan does not hold: {bare}"
