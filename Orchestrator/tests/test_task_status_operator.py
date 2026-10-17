"""GET /tasks/{task_id} returns the task's operator.

Audit 2026-10-09 O20-mcp-server-03: the MCP server's get_media ownership gate
reads ``operator`` from this route, which never returned it, so every remote
get_media(task_id=...) was denied (and a key that is silently absent can never
be enforced). The field is additive; /tasks/list already carries it.
"""

from Orchestrator.models import Task, TaskDatabase, TaskStatus, TaskType
from Orchestrator.routes import task_routes


def _task(task_id, operator):
    now = "2026-10-01T00:00:00Z"
    return Task(task_id=task_id, task_type=TaskType.IMAGE_GENERATION, status=TaskStatus.COMPLETED,
                created_at=now, updated_at=now, result_url="/ui/uploads/x.png",
                operator=operator)


def test_task_status_includes_operator(tmp_path, monkeypatch):
    db = TaskDatabase(str(tmp_path / "t.db"))
    monkeypatch.setattr(task_routes, "task_db", db)
    db.save_task(_task("owned", "alice"))
    db.save_task(_task("ownerless", None))

    assert task_routes.get_task_status("owned")["operator"] == "alice"
    resp = task_routes.get_task_status("ownerless")
    assert "operator" in resp and resp["operator"] is None
    # Additive: the existing contract is untouched.
    assert resp["result_url"] == "/ui/uploads/x.png"
