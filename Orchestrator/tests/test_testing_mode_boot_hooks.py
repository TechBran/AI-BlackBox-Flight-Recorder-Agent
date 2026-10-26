"""BLACKBOX_TESTING=1 keeps the production lifespan's side effects out of tests.

Route tests enter `with TestClient(app)`, which runs every startup hook. On the
real app those hooks start the cron scheduler (and its catch-up runs against the
live service), the zellij and CU-display reapers, the embeddings watcher, the
Asterisk/AMI/SMS bring-up, sudo drift-heals and an index rebuild (audit
O21-python-tests-02). The suite sets BLACKBOX_TESTING=1 (root conftest.py) and
the app honours it at startup; a hook is still callable directly, so the tests
of a hook's own logic keep working.
"""
import asyncio
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _testing():
    # Imported per test so the real-app tripwire test below still runs (and
    # fails for the right reason) on a tree without the helper.
    from Orchestrator.utils import testing
    return testing


def _mini_app():
    testing = _testing()
    app = FastAPI()
    calls = []

    @testing.boot_hook(app, "startup")
    def sync_hook():
        calls.append("sync")

    @testing.boot_hook(app, "startup")
    async def async_hook():
        calls.append("async")

    @testing.boot_hook(app, "shutdown")
    async def stop_hook():
        calls.append("stop")

    return app, calls, (sync_hook, async_hook, stop_hook)


def test_boot_hooks_are_skipped_under_testing(monkeypatch):
    monkeypatch.setenv("BLACKBOX_TESTING", "1")
    app, calls, _ = _mini_app()
    with TestClient(app):
        pass
    assert calls == []


@pytest.mark.parametrize("value", [None, "", "0", "true"])
def test_boot_hooks_run_when_not_testing(monkeypatch, value):
    # Only the exact value "1" (what the suite sets) switches the hooks off, so
    # a stray BLACKBOX_TESTING=true in a service environment changes nothing.
    if value is None:
        monkeypatch.delenv("BLACKBOX_TESTING", raising=False)
    else:
        monkeypatch.setenv("BLACKBOX_TESTING", value)
    app, calls, _ = _mini_app()
    with TestClient(app):
        assert calls == ["sync", "async"]
    assert calls == ["sync", "async", "stop"]


def test_boot_hook_returns_the_undecorated_function(monkeypatch):
    monkeypatch.setenv("BLACKBOX_TESTING", "1")
    _, calls, (sync_hook, async_hook, _) = _mini_app()
    sync_hook()
    asyncio.run(async_hook())
    assert calls == ["sync", "async"]


def test_suite_runs_with_testing_mode_on():
    # The env guard itself: the root conftest sets it before any app import.
    assert _testing().testing_mode() is True


def _startup_handlers(app):
    # The app's lifespan hooks, plus anything left on FastAPI's on_event lists
    # (which a lifespan= app never runs: test_app_lifespan.py keeps them empty).
    from Orchestrator.lifespan import lifespan_of
    lifespan = lifespan_of(app)
    return [*lifespan.hooks("startup"), *lifespan.hooks("shutdown"),
            *getattr(app.router, "on_startup", []), *getattr(app.router, "on_shutdown", [])]


def test_production_app_lifespan_runs_no_side_effect_hook(monkeypatch, tmp_path):
    """Hostile check on the REAL app: every dependency the first startup hook
    reaches is a tripwire, so an ungated hook fails the lifespan before it can
    touch the box (the base code failed here)."""
    from Orchestrator.app import app
    import Orchestrator.startup as st

    def tripwire(*_a, **_k):
        raise AssertionError("a side-effecting startup hook ran under BLACKBOX_TESTING=1")

    index = tmp_path / "snapshot_index.json"
    index.write_text("{}")
    monkeypatch.setattr(st, "SNAPSHOT_INDEX", index)
    for name in ("read_schema_version", "rebuild_snapshot_index", "load_snapshot_index",
                 "load_operator_state", "get_scheduler_manager"):
        monkeypatch.setattr(st, name, tripwire)
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings", return_value={}):
        with TestClient(app) as client:
            assert client.get("/toolvault/health").status_code == 200


# The app hooks that only capture or stop state inside this process, and so
# register as plain lifespan hooks (lifespan_hook). Every other startup/shutdown
# hook, in any module, must go through boot_hook.
_IN_PROCESS_HOOKS = {
    "Orchestrator.routes.tts_routes._tts_queue_capture_loop",
    "Orchestrator.routes.tts_routes._tts_queue_shutdown",
}


def _ungated_hooks(app):
    testing = _testing()
    ungated = []
    for handler in _startup_handlers(app):
        if getattr(handler, testing.BOOT_HOOK_ATTR, False):
            continue
        fn = getattr(handler, "__wrapped__", handler)
        name = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', repr(fn))}"
        if name not in _IN_PROCESS_HOOKS:
            ungated.append(name)
    return ungated


def test_every_app_hook_is_gated():
    """A hook added later to the app, from startup.py or any route module, must
    use boot_hook (or be listed above as in-process only), or the suite starts
    running it again."""
    from Orchestrator.app import app
    testing = _testing()

    assert _ungated_hooks(app) == []
    gated = [h for h in _startup_handlers(app) if getattr(h, testing.BOOT_HOOK_ATTR, False)]
    assert len(gated) >= 20  # every startup.py hook


def test_the_hook_check_sees_a_plain_hook_from_any_module():
    from Orchestrator.lifespan import lifespan_hook
    app = FastAPI()

    async def reaper():  # this module, not startup.py: still caught
        pass

    lifespan_hook(app, "startup")(reaper)
    assert _ungated_hooks(app) == [f"{__name__}.test_the_hook_check_sees_a_plain_hook_from_any_module.<locals>.reaper"]
