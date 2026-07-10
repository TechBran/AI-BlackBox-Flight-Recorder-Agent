"""G2-T8 (M2.0) — real task cancellation.

The safety gate that must land before any YOLO CLI-agent tool exists. Covers:
  * TaskStatus.CANCELLED exists and is DISTINCT from FAILED.
  * CANCELLED is STICKY in update_task — an in-flight worker completion can never
    flip a cancelled task back to completed/failed (the "UI lies" bug T8 kills).
  * cancel_task marks CANCELLED, is idempotent, and works with NO handle.
  * cancel_all loops the per-task path and marks CANCELLED (not FAILED).
  * process-group kill (CLI-agent-shaped handle) really kills the group — and the
    reverse-edit mutation (drop the killpg) makes the test fail.
  * a cancelled CU task does NOT auto-snapshot (no /chat/save mint).
  * request_stop cancels agent_task -> done-callback fires -> display claim
    released (same-loop AND cross-loop via call_soon_threadsafe).

Fully offline: no SQLite (in-memory fake db), no network, no real CU driver.
"""
import asyncio
import contextlib
import os
import signal
import subprocess
import threading
import time

import pytest

import Orchestrator.tasks as tasks_mod
from Orchestrator.models import Task, TaskStatus, TaskType
from Orchestrator.volume import now_utc_iso


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_task_db(monkeypatch):
    """In-memory task store so cancel/update paths never touch SQLite."""
    store = {}

    class FakeDB:
        def get_task(self, task_id):
            return store.get(task_id)

        def save_task(self, task):
            store[task.task_id] = task

        def get_all_tasks(self, operator=None):
            vals = list(store.values())
            if operator:
                vals = [t for t in vals if t.operator == operator]
            return vals

    monkeypatch.setattr(tasks_mod, "task_db", FakeDB())
    return store


@pytest.fixture(autouse=True)
def _main_loop_guard():
    """Test hygiene: this module drives asyncio (agent_task cancels, a TestClient
    E2E) which can leave the process's DEFAULT main-thread event loop closed.
    Other suite files still use the deprecated
    ``asyncio.get_event_loop().run_until_complete()`` pattern, which raises on a
    closed loop. Restore a fresh, OPEN default loop after each test so this
    module is a good citizen in a full-suite run."""
    yield
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Wipe the module-global cancel registry between tests."""
    tasks_mod._cancel_handles.clear()
    tasks_mod._cancel_requested.clear()
    yield
    tasks_mod._cancel_handles.clear()
    tasks_mod._cancel_requested.clear()


@pytest.fixture
def clean_arbiter():
    from Orchestrator.browser import display_arbiter as da
    with da._lock:
        da._reservations.clear()
    yield da
    with da._lock:
        da._reservations.clear()


def _seed(store, task_id="t-1", task_type=TaskType.IMAGE_GENERATION,
          status=TaskStatus.PROCESSING, operator="Brandon", prompt="a sunset"):
    now = now_utc_iso()
    task = Task(task_id=task_id, task_type=task_type, status=status,
                created_at=now, updated_at=now, operator=operator, prompt=prompt)
    store[task_id] = task
    return task


# ---------------------------------------------------------------------------
# 1. TaskStatus.CANCELLED
# ---------------------------------------------------------------------------
def test_task_status_has_cancelled():
    assert TaskStatus.CANCELLED.value == "cancelled"
    assert TaskStatus.CANCELLED != TaskStatus.FAILED


# ---------------------------------------------------------------------------
# 2. CANCELLED is sticky
# ---------------------------------------------------------------------------
def test_cancelled_is_sticky_against_completed(fake_task_db):
    _seed(fake_task_db, status=TaskStatus.CANCELLED)
    tasks_mod.update_task("t-1", status=TaskStatus.COMPLETED, progress=100)
    assert fake_task_db["t-1"].status == TaskStatus.CANCELLED


def test_cancelled_is_sticky_against_failed(fake_task_db):
    _seed(fake_task_db, status=TaskStatus.CANCELLED)
    tasks_mod.update_task("t-1", status=TaskStatus.FAILED, error_message="boom")
    assert fake_task_db["t-1"].status == TaskStatus.CANCELLED


def test_normal_completion_still_works(fake_task_db):
    """The sticky guard must not touch a non-cancelled task."""
    _seed(fake_task_db, status=TaskStatus.PROCESSING)
    tasks_mod.update_task("t-1", status=TaskStatus.COMPLETED, progress=100)
    assert fake_task_db["t-1"].status == TaskStatus.COMPLETED


def test_save_task_db_layer_stickiness(tmp_path):
    """A DIRECT save_task (the shape gemini_cu_routes._run_task uses, bypassing
    update_task) must NOT resurrect a CANCELLED row to completed. Uses the REAL
    TaskDatabase on a temp file."""
    from Orchestrator.models import TaskDatabase
    db = TaskDatabase(db_path=str(tmp_path / "t.db"))
    now = now_utc_iso()
    t = Task(task_id="g-1", task_type=TaskType.GEMINI_CU,
             status=TaskStatus.CANCELLED, created_at=now, updated_at=now,
             operator="Brandon")
    db.save_task(t)
    # Simulate _run_task's direct write of a terminal success AFTER the cancel.
    t.status = TaskStatus.COMPLETED
    db.save_task(t)
    assert db.get_task("g-1").status == TaskStatus.CANCELLED
    # A brand-new (non-cancelled) task still saves normally.
    t2 = Task(task_id="g-2", task_type=TaskType.CHAT, status=TaskStatus.COMPLETED,
              created_at=now, updated_at=now, operator="Brandon")
    db.save_task(t2)
    assert db.get_task("g-2").status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# 3. cancel_task
# ---------------------------------------------------------------------------
def test_cancel_task_marks_cancelled(fake_task_db):
    _seed(fake_task_db, task_type=TaskType.IMAGE_GENERATION)
    res = tasks_mod.cancel_task("t-1")
    assert res["cancelled"] is True
    assert res["status"] == "cancelled"
    assert res["task_id"] == "t-1"
    assert fake_task_db["t-1"].status == TaskStatus.CANCELLED


def test_cancel_task_idempotent(fake_task_db):
    _seed(fake_task_db)
    tasks_mod.cancel_task("t-1")
    res2 = tasks_mod.cancel_task("t-1")
    assert res2["status"] == "cancelled"
    assert res2["cancelled"] is True          # still reports cancelled
    assert fake_task_db["t-1"].status == TaskStatus.CANCELLED


def test_cancel_task_not_found(fake_task_db):
    res = tasks_mod.cancel_task("nope")
    assert res["cancelled"] is False
    assert res.get("not_found") is True


def test_cancel_task_handleless_still_cancels(fake_task_db):
    """A row with NO registered handle is still cancellable (reaper behavior)."""
    _seed(fake_task_db, task_type=TaskType.CHAT)
    assert "t-1" not in tasks_mod._cancel_handles
    res = tasks_mod.cancel_task("t-1")
    assert res["cancelled"] is True
    assert fake_task_db["t-1"].status == TaskStatus.CANCELLED


def test_cancel_task_sets_cooperative_flag(fake_task_db):
    _seed(fake_task_db, task_type=TaskType.VIDEO_GENERATION)
    tasks_mod.cancel_task("t-1")
    assert tasks_mod.is_cancel_requested("t-1") is True


def test_completed_task_not_recancelled(fake_task_db):
    _seed(fake_task_db, status=TaskStatus.COMPLETED)
    res = tasks_mod.cancel_task("t-1")
    assert res["cancelled"] is False          # it completed, not cancelled
    assert fake_task_db["t-1"].status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# 4. cancel_all
# ---------------------------------------------------------------------------
def test_cancel_all_marks_cancelled_not_failed(fake_task_db):
    _seed(fake_task_db, "a", status=TaskStatus.PENDING)
    _seed(fake_task_db, "b", status=TaskStatus.PROCESSING)
    _seed(fake_task_db, "c", status=TaskStatus.COMPLETED)
    res = tasks_mod.cancel_all_tasks()
    assert res["cancelled"] == 2
    assert fake_task_db["a"].status == TaskStatus.CANCELLED
    assert fake_task_db["b"].status == TaskStatus.CANCELLED
    assert fake_task_db["c"].status == TaskStatus.COMPLETED   # untouched


# ---------------------------------------------------------------------------
# 5. raise_if_cancelled — the cooperative check point
# ---------------------------------------------------------------------------
def test_raise_if_cancelled_raises_when_flagged():
    tasks_mod.request_cooperative_cancel("x")
    with pytest.raises(tasks_mod.TaskCancelled):
        tasks_mod.raise_if_cancelled("x")


def test_raise_if_cancelled_noop_when_not_flagged():
    tasks_mod.raise_if_cancelled("y")   # must not raise


# ---------------------------------------------------------------------------
# 6. process-group kill (CLI-agent-shaped handle) — MUTATION target (a)
# ---------------------------------------------------------------------------
def _spawn_group_sleep():
    """Spawn `sleep 300` in its OWN process group (start_new_session=True), the
    exact shape a YOLO CLI agent will be launched with. Return the Popen."""
    return subprocess.Popen(
        ["sleep", "300"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _group_alive(pgid):
    """Probe the process group with signal 0 (no signal, just an existence
    check). True if the group still exists."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_cancel_kills_process_group(fake_task_db):
    proc = _spawn_group_sleep()
    try:
        pgid = os.getpgid(proc.pid)
        assert _group_alive(pgid)
        _seed(fake_task_db, "p-1", task_type=TaskType.AGENT_CHAT)
        tasks_mod.register_cancel_handle("p-1", "process", pid=proc.pid)

        res = tasks_mod.cancel_task("p-1")
        assert res["cancelled"] is True
        assert fake_task_db["p-1"].status == TaskStatus.CANCELLED

        # The kill must actually TERMINATE the process. If the process-group kill
        # were removed (the mutation), `sleep 300` keeps running and wait() times
        # out -> TimeoutExpired -> this test fails. That is the mutation guard.
        proc.wait(timeout=5)
        assert proc.returncode is not None
        assert proc.returncode < 0, "process should have died from a signal"

        # Reaping the leader clears the zombie; then the group is truly gone.
        # (killpg(pgid, 0) reports a zombie as alive, so we must reap first.)
        assert not _group_alive(pgid), "process group survived cancel"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


def test_cancel_kills_child_of_process_group(fake_task_db):
    """Process-GROUP kill (not bare pid kill) is what makes an agent's CHILDREN
    die too. Spawn a shell that forks a long child; kill the group; assert the
    grandchild is reaped."""
    proc = subprocess.Popen(
        ["bash", "-c", "sleep 300 & echo $!; wait"],
        start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        child_pid = int(proc.stdout.readline().strip())
        pgid = os.getpgid(proc.pid)
        assert _group_alive(pgid)
        _seed(fake_task_db, "p-2", task_type=TaskType.AGENT_CHAT)
        tasks_mod.register_cancel_handle("p-2", "process", pid=proc.pid)

        tasks_mod.cancel_task("p-2")

        deadline = time.monotonic() + 5
        def _child_alive():
            try:
                os.kill(child_pid, 0)
                return True
            except ProcessLookupError:
                return False
        while _child_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _child_alive(), "child process survived the group kill"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


# ---------------------------------------------------------------------------
# 7. no-mint-on-cancel (CU) — MUTATION target (c)
# ---------------------------------------------------------------------------
def test_cancelled_cu_task_does_not_mint(fake_task_db, monkeypatch):
    """A cancelled CU task must NOT auto-snapshot — a killed YOLO agent's partial
    output never enters the immutable ledger (precedent: the Android error-mint
    bug)."""
    _seed(fake_task_db, "cu-1", task_type=TaskType.USE_COMPUTER,
          prompt="open a browser and do things")

    async def fake_run_cu_task(**kwargs):
        return {"success": True, "result_text": "did stuff", "screenshots": [],
                "final_screenshot": None, "steps": 3, "tokens": {}}

    import Orchestrator.browser.headless as headless_mod
    monkeypatch.setattr(headless_mod, "run_cu_task", fake_run_cu_task, raising=False)

    posted = []
    import requests as real_requests
    monkeypatch.setattr(real_requests, "post",
                        lambda *a, **k: posted.append((a, k)))

    # Cancel is requested BEFORE the driver returns.
    tasks_mod.request_cooperative_cancel("cu-1")

    tasks_mod.process_browser_use(fake_task_db["cu-1"])

    assert posted == [], "cancelled CU task auto-snapshotted (minted) — must not"
    assert fake_task_db["cu-1"].status == TaskStatus.CANCELLED


def _run_gemini_task(task_id, operator, stopped, loop_events, fake_db, monkeypatch):
    """Drive gemini_cu_routes._run_task with a mocked session + loop + snapshot.
    Returns the list of _snapshot_cu_result invocations (the mint boundary)."""
    from Orchestrator.routes import gemini_cu_routes as gcr

    class FakeSession:
        def __init__(self):
            self.session_id = "gsess-" + task_id
            self.stop_requested = False   # _run_task resets at entry anyway
            self.current_step = 2
            self.total_tokens = {"input": 0, "output": 0}

    monkeypatch.setattr(gcr, "get_or_create_session", lambda *a, **k: FakeSession())

    async def fake_loop(session, prompt, model, system_prompt, url):
        # A real cancel fires request_stop() DURING the run (AFTER _run_task's
        # entry reset), so set stop_requested here — the driver then breaks.
        if stopped:
            session.stop_requested = True
        for ev in loop_events:
            yield ev

    monkeypatch.setattr(gcr, "run_gemini_cu_loop", fake_loop)

    minted = []

    async def fake_snapshot(*a, **k):
        minted.append((a, k))

    monkeypatch.setattr(gcr, "_snapshot_cu_result", fake_snapshot)

    _seed(fake_db, task_id, task_type=TaskType.GEMINI_CU, operator=operator)
    fake_db[task_id].result_data = {}

    # environment="android" so no local display claim is taken (ADB device).
    asyncio.run(gcr._run_task(task_id, operator, "android", "android",
                              "do a thing", "gemini-x", None, None))
    return minted


def test_gemini_cancelled_task_does_not_mint(fake_task_db, monkeypatch):
    """A cancelled GEMINI_CU task must NOT auto-snapshot — the gemini driver
    exits cleanly via `break`, so the mint must be guarded on stop_requested."""
    minted = _run_gemini_task(
        "gcu-1", "Brandon", stopped=True,
        loop_events=[{"type": "cu_stopped", "data": {"step": 2}}],
        fake_db=fake_task_db, monkeypatch=monkeypatch)
    assert minted == [], "cancelled GEMINI_CU task auto-snapshotted — must not"
    assert fake_task_db["gcu-1"].status == TaskStatus.CANCELLED


def test_gemini_successful_task_still_mints(fake_task_db, monkeypatch):
    """Control: a NON-cancelled GEMINI_CU success still mints (proves the guard
    above isn't passing because the mint path is globally broken)."""
    minted = _run_gemini_task(
        "gcu-2", "Brandon", stopped=False,
        loop_events=[{"type": "done", "data": {"content": "did it"}}],
        fake_db=fake_task_db, monkeypatch=monkeypatch)
    assert len(minted) == 1, "a normal GEMINI_CU success must still auto-snapshot"
    assert fake_task_db["gcu-2"].status == TaskStatus.COMPLETED


def test_gemini_fresh_task_clears_stale_stop_flag(fake_task_db, monkeypatch):
    """A stale stop_requested left on the PERSISTENT per-operator session by a
    cancelled PRIOR task must NOT void the operator's NEXT task (G2-T8 Issue 1).
    _run_task resets stop_requested at entry, so task B runs and mints normally.
    Without the reset, B breaks at step 1 and the mint guard voids it."""
    from Orchestrator.routes import gemini_cu_routes as gcr

    class FakeSession:
        def __init__(self):
            self.session_id = "shared-op-sess"
            self.stop_requested = True   # leftover from cancelled task A
            self.current_step = 0
            self.total_tokens = {"input": 0, "output": 0}

    shared = FakeSession()  # SAME instance returned for the operator (persistent)
    monkeypatch.setattr(gcr, "get_or_create_session", lambda *a, **k: shared)

    async def fake_loop(session, prompt, model, system_prompt, url):
        yield {"type": "done", "data": {"content": "task B result"}}

    monkeypatch.setattr(gcr, "run_gemini_cu_loop", fake_loop)

    minted = []

    async def fake_snapshot(*a, **k):
        minted.append((a, k))

    monkeypatch.setattr(gcr, "_snapshot_cu_result", fake_snapshot)

    _seed(fake_task_db, "gcu-B", task_type=TaskType.GEMINI_CU, operator="Brandon")
    fake_task_db["gcu-B"].result_data = {}

    asyncio.run(gcr._run_task("gcu-B", "Brandon", "android", "android",
                              "task B", "gemini-x", None, None))

    assert len(minted) == 1, "task B (not cancelled) must run and mint"
    assert fake_task_db["gcu-B"].status == TaskStatus.COMPLETED
    assert shared.stop_requested is False, "task entry must clear the stale flag"


def test_successful_cu_task_still_mints(fake_task_db, monkeypatch):
    """Control: a NON-cancelled CU success still mints (proves the test above
    isn't passing because mint is globally broken)."""
    _seed(fake_task_db, "cu-2", task_type=TaskType.USE_COMPUTER, prompt="do x")

    async def fake_run_cu_task(**kwargs):
        return {"success": True, "result_text": "ok", "screenshots": ["/ui/uploads/s.png"],
                "final_screenshot": "/ui/uploads/s.png", "steps": 1, "tokens": {}}

    import Orchestrator.browser.headless as headless_mod
    monkeypatch.setattr(headless_mod, "run_cu_task", fake_run_cu_task, raising=False)

    posted = []

    class _Resp:
        def raise_for_status(self):
            pass
    import requests as real_requests
    monkeypatch.setattr(real_requests, "post",
                        lambda *a, **k: (posted.append((a, k)), _Resp())[1])

    tasks_mod.process_browser_use(fake_task_db["cu-2"])

    assert len(posted) == 1, "a normal CU success must still auto-snapshot"
    assert fake_task_db["cu-2"].status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# 8. request_stop -> cancel -> done-callback -> display released (item 6)
# ---------------------------------------------------------------------------
def test_request_stop_cancels_and_releases_claim_same_loop(clean_arbiter):
    """The documented mechanism: request_stop() cancels agent_task; the task
    completes; add_done_callback fires release_claim; the display is free."""
    from Orchestrator.browser.session_manager import ComputerUseSession
    da = clean_arbiter

    async def scenario():
        sess = ComputerUseSession(operator="op-same")
        sess.status = "running"
        sess.agent_task = asyncio.create_task(asyncio.sleep(100))
        claim_id = "claim-same"
        owner = da.try_claim("browser", "op-same", claim_id,
                             session_id=sess.session_id)
        assert owner is None                      # granted
        sess.agent_task.add_done_callback(
            lambda _t, c=claim_id: da.release_claim(c))
        await asyncio.sleep(0)                     # let the task start
        assert da.local_display_owner() is not None

        sess.request_stop()                        # <-- the hard cancel
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(sess.agent_task, timeout=3)
        await asyncio.sleep(0)                     # let done-callback run

        assert sess.agent_task.cancelled()
        assert da.local_display_owner() is None, "display claim leaked after stop"

    # Run on an explicit local loop (NOT asyncio.run) so we never reset the
    # main-thread event loop — other suite files use the deprecated
    # asyncio.get_event_loop().run_until_complete() pattern that a closed/None
    # main loop would break. request_stop() is still called from INSIDE this
    # running loop, so it exercises the same-loop (chat) cancel path.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_request_stop_cross_loop_cancels(clean_arbiter):
    """The task path runs agent_task on a WORKER thread's own asyncio loop, so a
    request_stop() from the API thread must cancel it via call_soon_threadsafe —
    a bare .cancel() from a foreign thread would not wake the sleeping loop."""
    from Orchestrator.browser.session_manager import ComputerUseSession
    sess = ComputerUseSession(operator="op-xloop")
    sess.status = "running"

    ready = threading.Event()
    outcome = {}

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            sess.agent_task = asyncio.create_task(asyncio.sleep(100))
            ready.set()
            try:
                await sess.agent_task
            except asyncio.CancelledError:
                outcome["cancelled"] = True

        try:
            loop.run_until_complete(main())
        finally:
            loop.close()

    t = threading.Thread(target=worker)
    t.start()
    assert ready.wait(3)
    time.sleep(0.1)                                # ensure the task is awaiting

    sess.request_stop()                            # from THIS (foreign) thread

    t.join(4)
    assert not t.is_alive(), "cross-loop cancel did not stop the worker task"
    assert outcome.get("cancelled") is True


def test_gemini_request_stop_same_loop_cancels(clean_arbiter):
    """Parity with the browser twin: request_stop() from WITHIN the driver's own
    loop (the chat path) cancels agent_task; the task completes; the done-callback
    releases the display claim. Also asserts stop_requested is set."""
    from Orchestrator.gemini_cu.session_manager import GeminiCUSession
    da = clean_arbiter

    async def scenario():
        sess = GeminiCUSession(operator="g-same", device_id="blackbox",
                               environment="desktop")
        sess.status = "running"
        sess.agent_task = asyncio.create_task(asyncio.sleep(100))
        claim_id = "g-claim-same"
        owner = da.try_claim("gemini-chat", "g-same", claim_id,
                             session_id=sess.session_id)
        assert owner is None
        sess.agent_task.add_done_callback(
            lambda _t, c=claim_id: da.release_claim(c))
        await asyncio.sleep(0)
        assert da.local_display_owner() is not None

        sess.request_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(sess.agent_task, timeout=3)
        await asyncio.sleep(0)

        assert sess.stop_requested is True
        assert sess.agent_task.cancelled()
        assert da.local_display_owner() is None, "display claim leaked after stop"

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_gemini_request_stop_cross_loop_cancels():
    """The gemini twin gets the identical cross-loop fix."""
    from Orchestrator.gemini_cu.session_manager import GeminiCUSession
    sess = GeminiCUSession(operator="g-xloop", device_id="blackbox",
                           environment="desktop")
    sess.status = "running"

    ready = threading.Event()
    outcome = {}

    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            sess.agent_task = asyncio.create_task(asyncio.sleep(100))
            ready.set()
            try:
                await sess.agent_task
            except asyncio.CancelledError:
                outcome["cancelled"] = True

        try:
            loop.run_until_complete(main())
        finally:
            loop.close()

    t = threading.Thread(target=worker)
    t.start()
    assert ready.wait(3)
    time.sleep(0.1)
    sess.request_stop()
    t.join(4)
    assert not t.is_alive()
    assert outcome.get("cancelled") is True


# ---------------------------------------------------------------------------
# 9. END-TO-END GATE — POST /tasks/{id}/cancel on a real process group
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(monkeypatch):
    """Real app so POST /tasks/{id}/cancel exercises the registered route."""
    import Orchestrator.app  # noqa: F401 — registers routes onto the shared app
    from Orchestrator.checkpoint import app
    from fastapi.testclient import TestClient

    store = {}

    class FakeDB:
        def get_task(self, task_id):
            return store.get(task_id)

        def save_task(self, task):
            store[task.task_id] = task

        def get_all_tasks(self, operator=None):
            return list(store.values())

    monkeypatch.setattr(tasks_mod, "task_db", FakeDB())
    c = TestClient(app)
    c._store = store
    return c


def test_e2e_cancel_endpoint_kills_group_marks_cancelled_no_mint(client, monkeypatch):
    """The gate: spawn a long-running process in its own group, cancel it via the
    HTTP endpoint, and assert (a) the group is gone, (b) status is cancelled,
    (c) nothing minted."""
    posted = []
    import requests as real_requests
    monkeypatch.setattr(real_requests, "post",
                        lambda *a, **k: posted.append((a, k)))

    proc = _spawn_group_sleep()
    try:
        pgid = os.getpgid(proc.pid)
        _seed(client._store, "e2e-1", task_type=TaskType.AGENT_CHAT)
        tasks_mod.register_cancel_handle("e2e-1", "process", pid=proc.pid)

        resp = client.post("/tasks/e2e-1/cancel")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cancelled"] is True
        assert body["status"] == "cancelled"

        # (b) status is cancelled
        assert client._store["e2e-1"].status == TaskStatus.CANCELLED
        # (a) the group is gone (reap the zombie leader first)
        proc.wait(timeout=5)
        assert proc.returncode is not None and proc.returncode < 0
        assert not _group_alive(pgid), "process group survived the endpoint cancel"
        # (c) nothing minted
        assert posted == [], "a cancelled task must not hit /chat/save"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


def test_e2e_cancel_unknown_task_404(client):
    resp = client.post("/tasks/does-not-exist/cancel")
    assert resp.status_code == 404
