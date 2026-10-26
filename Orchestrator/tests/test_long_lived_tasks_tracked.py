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
