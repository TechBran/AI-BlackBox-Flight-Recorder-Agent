"""The app's one lifespan, and the registry of the long-lived tasks it stops.

The app used to register its startup and shutdown work as 33 `@app.on_event`
hooks, run in whatever order the imports happened to leave, and the loops
those hooks started were never stopped (audit O01-core-runtime-02). Starlette
1.0 removed on_event. Every hook now registers here, through lifespan_hook()
or, for one with side effects outside this process, boot_hook()
(Orchestrator/utils/testing.py, which the test suite skips), and the app runs
them all from one lifespan context manager: checkpoint.py builds the app with
FastAPI(lifespan=APP_LIFESPAN).

Order. Hooks run in registration order, which is import order:
Orchestrator.app imports checkpoint, then startup (its hooks, top to bottom),
then the route modules (tts_routes, agent_routes, gemini_agent_routes, in
app.py's order). Several hooks depend on it and say so in their docstrings
(the index check before the embeddings transcode, the migration resume before
the embeddings watcher, the Asterisk bring-up before SMS);
Orchestrator/tests/test_app_lifespan.py pins the whole sequence.

Startup runs each startup hook in turn, a sync one inline on the loop as
on_event ran them (nothing is served until startup returns). A hook that
raises fails the startup, as before, after the shutdown half below has stopped
whatever the earlier hooks started.

Shutdown runs in three phases. First the "stopping" hooks, which stop new
work from starting (the background task worker taking queued tasks:
startup.shutdown_task_worker). Then it cancels every task in the registry and
waits up to TASK_CANCEL_TIMEOUT_S for them, so a task's own cleanup (a call
handler hanging up its channel) still finds the subsystems it uses. Then it
runs each shutdown hook in turn. In both hook phases an async hook is bounded
by HOOK_TIMEOUT_S, and a hook that raises or hangs is logged and the rest
still run (under on_event the first failure skipped every later hook).
uvicorn only starts the lifespan shutdown after its connection drain
(--timeout-graceful-shutdown), so the worker can still start a task during
the drain. The unit's TimeoutStopSec covers the drain plus
shutdown_budget_s() (Orchestrator/tests/test_app_lifespan.py checks it).

The registry is app.state.background_tasks (APP_LIFESPAN.tasks; track_task()
reaches it from modules that do not import the app). It keeps a strong
reference to each task until the task ends: the event loop holds tasks only
weakly, so a task nobody references can be garbage-collected mid-run. Register
a task that outlives the hook or request that started it (a maintenance loop,
a call handler, a migration). A task scoped to one request or socket belongs
to that handler, and a subsystem with its own stop (the cron scheduler, the
SMS AMI client, the ARI client) stops its own tasks in its shutdown hook. The
in-app update drive (update_routes._spawn) is deliberately not registered:
cancelling it does not stop the git or pip process it is waiting on, so it
would only skip the update's later phases sooner than the process exit does.

Stdlib only.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("blackbox.lifespan")

EVENTS = ("startup", "stopping", "shutdown")
# How long shutdown waits for the cancelled tasks, then for each async
# stopping and shutdown hook. The unit's TimeoutStopSec has to cover these
# (shutdown_budget_s) plus uvicorn's connection drain before them
# (--timeout-graceful-shutdown), or systemd SIGKILLs the last hooks.
TASK_CANCEL_TIMEOUT_S = 5.0
HOOK_TIMEOUT_S = 10.0


def hook_name(fn: Callable) -> str:
    """module.qualname of a hook, seen through boot_hook's wrapper."""
    fn = getattr(fn, "__wrapped__", fn)
    return f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', repr(fn))}"


class TaskRegistry:
    """Long-lived asyncio tasks, held until they end and cancelled at shutdown."""

    def __init__(self) -> None:
        self._tasks: Dict[asyncio.Future, str] = {}

    def track(self, task: Optional[asyncio.Future], name: Optional[str] = None):
        """Hold `task` until it ends; returns it. None passes through (a start
        function that had nothing to start). Call from the task's own loop."""
        if task is None:
            return None
        if not isinstance(task, asyncio.Future):
            raise TypeError(f"track() takes an asyncio task, not {type(task).__name__}")
        if not task.done():
            if name is None:
                name = task.get_name() if isinstance(task, asyncio.Task) else repr(task)
            self._tasks[task] = name
            task.add_done_callback(self._forget)
        return task

    def _forget(self, task: asyncio.Future) -> None:
        self._tasks.pop(task, None)

    def names(self) -> List[str]:
        """The names of the tasks still running, in the order they were added."""
        # list() copies in one step: a task of a worker thread's loop may be
        # added or forgotten concurrently.
        return [name for task, name in list(self._tasks.items()) if not task.done()]

    def __len__(self) -> int:
        return len(self.names())

    async def cancel_all(self, timeout: float = TASK_CANCEL_TIMEOUT_S) -> int:
        """Cancel every task of the running loop and wait up to `timeout` for
        them to end. Returns how many were cancelled. A task of another loop
        (a test client session that has since closed) is that loop's to stop."""
        loop = asyncio.get_running_loop()
        current = asyncio.current_task()
        tasks = [t for t in list(self._tasks)
                 if not t.done() and t is not current and t.get_loop() is loop]
        if not tasks:
            return 0
        names = {t: self._tasks.get(t, "?") for t in tasks}
        for task in tasks:
            task.cancel()
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            logger.warning("[LIFESPAN] background task %s did not stop within %.0fs",
                           names[task], timeout)
        logger.info("[LIFESPAN] cancelled %d background task(s): %s",
                    len(tasks), ", ".join(sorted(names.values())))
        return len(tasks)


class AppLifespan:
    """The startup and shutdown hooks of one app, run as its lifespan."""

    def __init__(self) -> None:
        self._hooks: Dict[str, List[Callable[[], Any]]] = {event: [] for event in EVENTS}
        self.tasks = TaskRegistry()

    def add(self, event: str, fn: Callable[[], Any]) -> None:
        """Register `fn` (sync or async, no arguments) to run at `event`."""
        if event not in EVENTS:
            raise ValueError(f"unknown lifespan event {event!r}; expected one of {EVENTS}")
        self._hooks[event].append(fn)

    def hooks(self, event: str) -> List[Callable[[], Any]]:
        """The hooks registered for `event`, in the order they run."""
        return list(self._hooks[event])

    def shutdown_budget_s(self) -> float:
        """The longest the shutdown half can take when every async hook runs
        into its timeout. (A sync hook runs inline and is not bounded; keep
        those quick.)"""
        hooks = self.hooks("stopping") + self.hooks("shutdown")
        async_hooks = sum(1 for h in hooks if inspect.iscoroutinefunction(h))
        return TASK_CANCEL_TIMEOUT_S + async_hooks * HOOK_TIMEOUT_S

    def bind(self, app) -> None:
        """Make this the lifespan that lifespan_of(app) returns, and put its
        task registry on app.state. For an app built with FastAPI(lifespan=self)."""
        app.state.lifespan = self
        app.state.background_tasks = self.tasks

    def attach(self, app) -> None:
        """Run this lifespan inside the one `app` already has: for an app built
        without lifespan=, whose default lifespan is kept around it."""
        outer = app.router.lifespan_context

        @asynccontextmanager
        async def composed(app_):
            async with outer(app_) as state:
                async with self(app_):
                    yield state

        app.router.lifespan_context = composed
        self.bind(app)

    @asynccontextmanager
    async def __call__(self, app):
        try:
            await self._startup()
        except Exception:
            logger.error("[LIFESPAN] startup failed; stopping what had started")
            await self._shutdown()
            raise
        try:
            yield
        finally:
            await self._shutdown()

    async def _startup(self) -> None:
        for hook in self.hooks("startup"):
            result = hook()
            if inspect.isawaitable(result):
                await result

    async def _shutdown(self) -> None:
        await self._run_stop_hooks("stopping")
        await self.tasks.cancel_all(TASK_CANCEL_TIMEOUT_S)
        await self._run_stop_hooks("shutdown")

    async def _run_stop_hooks(self, event: str) -> None:
        for hook in self.hooks(event):
            try:
                result = hook()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, HOOK_TIMEOUT_S)
            except TimeoutError:
                logger.error("[LIFESPAN] %s hook %s did not finish within %.0fs; "
                             "running the rest", event, hook_name(hook), HOOK_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — one failed stop must not skip the others.
                logger.exception("[LIFESPAN] %s hook %s failed; running the rest",
                                 event, hook_name(hook))


# The lifespan of the app this process serves (checkpoint.py binds it).
APP_LIFESPAN = AppLifespan()


def lifespan_of(app) -> AppLifespan:
    """The AppLifespan that runs `app`'s hooks. An app that has none yet (one a
    test builds with a bare FastAPI()) gets one attached."""
    lifespan = getattr(app.state, "lifespan", None)
    if isinstance(lifespan, AppLifespan):
        return lifespan
    lifespan = AppLifespan()
    lifespan.attach(app)
    return lifespan


def lifespan_hook(app, event: str):
    """Decorator: register a startup, stopping or shutdown hook on `app`'s
    lifespan, for work that stays inside this process (capturing the loop,
    stopping an in-process worker), so the test suite runs it too. A hook with
    side effects outside the process registers with boot_hook instead."""
    def register(fn):
        lifespan_of(app).add(event, fn)
        return fn
    return register


def track_task(task: Optional[asyncio.Future], name: Optional[str] = None):
    """Register a long-lived task with the app's registry (cancelled at
    shutdown); returns it. For modules that do not import the app."""
    return APP_LIFESPAN.tasks.track(task, name)
