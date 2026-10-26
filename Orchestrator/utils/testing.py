"""BLACKBOX_TESTING: the switch the test suite sets so the app skips boot work
that reaches outside the test process.

The root conftest.py sets BLACKBOX_TESTING=1 before anything imports the app.
Route tests enter `with TestClient(app)`, which runs the app's lifespan
(Orchestrator/lifespan.py) and so its startup and shutdown hooks; registered
through boot_hook(), a hook that starts the cron scheduler or a reaper,
rewrites the Manifest, reaps processes, connects to Asterisk, runs sudo or
xdg-mime, or calls a provider is skipped there (audit O21-python-tests-02).
Only the exact value "1" counts, so a stray BLACKBOX_TESTING=true in a service
environment changes nothing.

Stdlib only.
"""
from __future__ import annotations

import functools
import inspect
import os

from Orchestrator.lifespan import lifespan_of

TESTING_ENV = "BLACKBOX_TESTING"

# Set on the wrapper boot_hook() registers, so a test can tell a gated hook
# from a plain one.
BOOT_HOOK_ATTR = "blackbox_boot_hook"


def testing_mode() -> bool:
    """True when the test suite is running this process."""
    return os.environ.get(TESTING_ENV, "").strip() == "1"


def boot_hook(app, event: str):
    """Register a startup, stopping or shutdown hook on `app`'s lifespan
    (lifespan.EVENTS), for a hook with side effects outside this process: the
    app does not run it while testing_mode() is on. A hook that stays inside
    the process registers with lifespan.lifespan_hook instead.

    The check runs when the hook fires, not at import. The decorated name stays
    the undecorated function, so a test can still call the hook directly to test
    its own logic.
    """
    def register(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def gated():
                if testing_mode():
                    return None
                return await fn()
        else:
            @functools.wraps(fn)
            def gated():
                if testing_mode():
                    return None
                return fn()
        setattr(gated, BOOT_HOOK_ATTR, True)
        lifespan_of(app).add(event, gated)
        return fn
    return register
