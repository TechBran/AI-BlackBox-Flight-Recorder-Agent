"""GET /tasks/list filters in SQL, with an optional status and limit filter.

Audit 2026-10-09 rank 67 (P01-chat-core-web-21): every open Portal tab and the
Android app poll /tasks/list every 2 s, and the route read EVERY task row of
the box's whole history into Python on each poll, then threw almost all of them
away (the default view is only active tasks plus the ones that ended in the
last 10 minutes). The default filter now runs in the query, a caller can ask
for exact statuses (?status=pending,processing) and cap the rows (?limit=N),
and total_tasks is a COUNT, not len(all rows).
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from Orchestrator.models import Task, TaskDatabase, TaskStatus, TaskType
from Orchestrator.routes import task_routes


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.now(timezone.utc)


def _task(task_id, status, *, minutes_ago=0, created_minutes_ago=None, operator="alice",
          updated_format=_iso):
    updated = NOW - timedelta(minutes=minutes_ago)
    created = NOW - timedelta(minutes=minutes_ago if created_minutes_ago is None else created_minutes_ago)
    return Task(task_id=task_id, task_type=TaskType.IMAGE_GENERATION, status=status,
                created_at=_iso(created), updated_at=updated_format(updated),
                prompt=f"prompt {task_id}", operator=operator)


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = TaskDatabase(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(task_routes, "task_db", database)
    database.save_task(_task("pending-new", TaskStatus.PENDING, minutes_ago=1))
    database.save_task(_task("processing-old", TaskStatus.PROCESSING, minutes_ago=600))
    database.save_task(_task("done-recent", TaskStatus.COMPLETED, minutes_ago=3))
    database.save_task(_task("failed-recent", TaskStatus.FAILED, minutes_ago=4))
    database.save_task(_task("cancelled-recent", TaskStatus.CANCELLED, minutes_ago=5))
    database.save_task(_task("done-old", TaskStatus.COMPLETED, minutes_ago=90))
    database.save_task(_task("failed-old", TaskStatus.FAILED, minutes_ago=3000))
    # A legacy row written with isoformat() (+00:00, microseconds): still recent.
    database.save_task(_task("done-recent-iso", TaskStatus.COMPLETED, minutes_ago=2,
                             updated_format=lambda dt: dt.isoformat()))
    database.save_task(_task("bob-pending", TaskStatus.PENDING, minutes_ago=1, operator="bob"))
    return database


def _ids(resp):
    return [t["task_id"] for t in resp["tasks"]]


def test_default_view_is_active_plus_recently_ended(db):
    resp = task_routes.list_tasks()
    assert set(_ids(resp)) == {"pending-new", "processing-old", "done-recent", "failed-recent",
                               "cancelled-recent", "done-recent-iso", "bob-pending"}
    assert resp["total_tasks"] == 9
    assert resp["filtered_count"] == 7


def test_the_default_filter_runs_in_the_query(db, monkeypatch):
    """The route must not pull the whole history into Python on every poll."""
    rows_returned = []
    real = db.get_task_list

    def spy(*args, **kwargs):
        rows = real(*args, **kwargs)
        rows_returned.append(len(rows))
        return rows

    monkeypatch.setattr(db, "get_task_list", spy)
    task_routes.list_tasks()
    assert rows_returned == [7], "old terminal rows were fetched and filtered in Python"


def test_status_filter_selects_exact_statuses(db):
    resp = task_routes.list_tasks(status="pending,processing")
    assert set(_ids(resp)) == {"pending-new", "processing-old", "bob-pending"}
    resp = task_routes.list_tasks(status="failed")
    assert set(_ids(resp)) == {"failed-recent", "failed-old"}, "an explicit status has no time window"


def test_unknown_status_is_refused(db):
    with pytest.raises(HTTPException) as exc:
        task_routes.list_tasks(status="pending,exploded")
    assert exc.value.status_code == 400


def test_limit_keeps_the_newest_rows(db):
    resp = task_routes.list_tasks(all=True, limit=3)
    assert len(resp["tasks"]) == 3
    created = [t["created_at"] for t in resp["tasks"]]
    assert created == sorted(created, reverse=True)
    assert resp["total_tasks"] == 9
    for bad in (0, -1, 100000):
        with pytest.raises(HTTPException) as exc:
            task_routes.list_tasks(limit=bad)
        assert exc.value.status_code == 400


def test_operator_and_all_still_work(db):
    assert set(_ids(task_routes.list_tasks(operator="bob"))) == {"bob-pending"}
    assert task_routes.list_tasks(operator="bob")["total_tasks"] == 1
    assert len(task_routes.list_tasks(all=True)["tasks"]) == 9


def test_payload_shape_is_unchanged(db):
    task = task_routes.list_tasks(status="pending")["tasks"][0]
    assert set(task) == {"task_id", "task_type", "status", "progress", "created_at", "updated_at",
                         "result_url", "operator", "prompt", "progress_text", "device_id",
                         "reasoning_text", "session_id"}
    assert task["device_id"] == "blackbox"
