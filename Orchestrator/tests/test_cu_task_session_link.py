"""CU task ↔ session linkage (design 2026-07-23, M2 — the linchpin).

Task rows historically never recorded WHICH CU session a task drives, so
every Live button could only guess (first-streamable session — possibly the
wrong agent's desktop) and the live-view page could not join sid→task row
for narration. session_id follows the device_id real-column precedent
exactly: dataclass field, idempotent ADD COLUMN, save_task mirror from
result_data, get_task_list projection, /tasks/list top-level key.
"""
import inspect

import pytest

from Orchestrator.models import Task, TaskDatabase, TaskStatus, TaskType


def _mk_task(task_id: str = "t-sess-1", **kw) -> Task:
    return Task(task_id=task_id, task_type=TaskType.USE_COMPUTER,
                status=TaskStatus.PENDING, created_at="2026-07-23T00:00:00Z",
                updated_at="2026-07-23T00:00:00Z", **kw)


@pytest.fixture
def db(tmp_path):
    return TaskDatabase(db_path=str(tmp_path / "tasks.db"))


def test_session_id_round_trips_as_a_real_column(db):
    t = _mk_task()
    t.session_id = "sess-abc"
    db.save_task(t)
    assert db.get_task("t-sess-1").session_id == "sess-abc"
    rows = db.get_task_list()
    assert rows and rows[0]["session_id"] == "sess-abc"


def test_save_task_mirrors_session_id_from_result_data(db):
    """Writers that only set result_data['session_id'] (the historical CU
    writer style for device_id) still populate the column."""
    t = _mk_task("t-sess-2", result_data={"session_id": "sess-blob"})
    db.save_task(t)
    assert db.get_task("t-sess-2").session_id == "sess-blob"
    assert db.get_task_list()[0]["session_id"] == "sess-blob"


def test_tasks_list_route_projects_session_id():
    from Orchestrator.routes import task_routes
    src = inspect.getsource(task_routes)
    assert '"session_id": t["session_id"]' in src, \
        "/tasks/list must project session_id top-level (both frontends key on it)"


def test_cu_runner_publishes_the_session_link():
    """Both headless CU paths must stamp session.task_id AND publish
    session_id onto the task row at launch."""
    from Orchestrator.browser import headless
    src = inspect.getsource(headless)
    assert "_publish_session_link" in src
    for fn in (headless.run_cu_task, headless._run_gemini_cu_task):
        assert "_publish_session_link" in inspect.getsource(fn), \
            f"{fn.__name__} must publish the session link"


def test_publish_session_link_writes_row_and_stamps_session(monkeypatch, db):
    from Orchestrator.browser import headless
    from Orchestrator import tasks as tasks_mod
    monkeypatch.setattr(tasks_mod, "task_db", db)
    db.save_task(_mk_task("t-sess-3"))

    class _S:
        session_id = "sess-live"
        task_id = None
    s = _S()
    headless._publish_session_link("t-sess-3", s)
    assert s.task_id == "t-sess-3"
    row = db.get_task("t-sess-3")
    assert row.session_id == "sess-live"
    assert (row.result_data or {}).get("view_url") == "/cu/view/sess-live"


def test_reset_task_state_clears_the_task_link():
    from Orchestrator.browser.session_manager import ComputerUseSession
    s = ComputerUseSession("op")
    s.task_id = "t-old"
    s.reset_task_state()
    assert s.task_id is None


def test_run_cu_task_relinks_task_id_after_reset():
    """Regression (review 2026-07-23): run_cu_task publishes the sid->task link
    BEFORE reset_task_state, which clears session.task_id — so the anthropic/
    openai task path must RE-STAMP task_id after the reset or the live-view
    STOP falls to session_stop (skipping CANCELLED row hygiene) and the
    activity endpoint reports task_id=null. Source-level guard: the re-stamp
    must sit after the reset call in run_cu_task."""
    import inspect
    from Orchestrator.browser.headless import run_cu_task
    src = inspect.getsource(run_cu_task)
    reset_pos = src.index("reset_task_state()")
    relink_pos = src.index("session.task_id = task_id", reset_pos)
    assert relink_pos > reset_pos, \
        "run_cu_task must re-stamp session.task_id AFTER reset_task_state()"


def test_cu_session_stop_always_stops_the_resolved_session(monkeypatch):
    """Regression (review 2026-07-23): under multi-desktop the STOP button must
    request_stop() the session THIS sid resolved to, never rely solely on
    cancel_task (whose cu handle re-resolves via the operator MRU pointer and
    can hit a DIFFERENT desktop)."""
    from Orchestrator.routes import browser_routes
    from Orchestrator.browser import session_manager as bsm
    monkeypatch.setattr(bsm.ComputerUseSession, "destroy", lambda self: None)
    bsm._sessions.clear(); bsm._operator_sessions.clear()
    try:
        target = bsm.get_or_create_session("op")   # the watched desktop
        target.status = "running"
        target.task_id = "task-A"
        # A newer desktop is the operator MRU — cancel_task's re-resolve would
        # wrongly hit THIS one.
        newer = bsm.get_or_create_session("op", force_new=True)
        newer.status = "running"
        stopped = {}
        monkeypatch.setattr(bsm.ComputerUseSession, "request_stop",
                            lambda self: stopped.setdefault(self.session_id, True))

        class _Row:
            status = "processing"
        monkeypatch.setattr("Orchestrator.tasks.task_db",
                            type("D", (), {"get_task": staticmethod(lambda t: _Row())})())
        monkeypatch.setattr("Orchestrator.tasks.cancel_task", lambda t: {"success": True})
        browser_routes.cu_session_stop(target.session_id)
        assert stopped.get(target.session_id) is True   # the watched one stopped
        assert newer.session_id not in stopped           # not the MRU sibling
    finally:
        bsm._sessions.clear(); bsm._operator_sessions.clear()
