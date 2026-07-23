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
