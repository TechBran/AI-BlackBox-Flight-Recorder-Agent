"""MN.7 — tasks.py:update_task notify wiring tests.

ONE edit at the update_task choke-point covers every async worker (image, video,
music, TTS, checkpoint, async-chat, CU). On a TERMINAL status (COMPLETED /
FAILED) it fires a fire-and-forget notify via the sync→async bridge. The bridge
is mocked here so the tests are fully offline and assert it is CALLED with the
right operator + category + a short title.

Invariants asserted:
  * COMPLETED on a media task → category="media".
  * COMPLETED on a non-media task (chat/checkpoint) → category="task".
  * FAILED → notify fired (terminal), category by task type.
  * Non-terminal updates (progress, PROCESSING) → NO notify.
  * operator in (None, "", "system") → SUPPRESSED (no spam from tool-generated
    / system-scope tasks nobody subscribed to as themselves).
  * A notify-bridge exception NEVER breaks update_task's own DB write.
"""

import pytest

import Orchestrator.tasks as tasks_mod
from Orchestrator.models import Task, TaskStatus, TaskType
from Orchestrator.volume import now_utc_iso


@pytest.fixture
def captured(monkeypatch):
    """Capture every notify_in_background call from update_task; no real bus."""
    calls = []

    def fake_bg(operator, title, body, category="general", **k):
        calls.append(
            {"operator": operator, "title": title, "body": body, "category": category}
        )

    monkeypatch.setattr(tasks_mod, "notify_in_background", fake_bg)
    return calls


@pytest.fixture
def fake_task_db(monkeypatch):
    """In-memory task store so update_task can fetch + save without SQLite."""
    store = {}

    class FakeDB:
        def get_task(self, task_id):
            return store.get(task_id)

        def save_task(self, task):
            store[task.task_id] = task

    db = FakeDB()
    monkeypatch.setattr(tasks_mod, "task_db", db)
    return store


def _seed(store, task_id="t-1", task_type=TaskType.IMAGE_GENERATION,
          operator="Brandon", prompt="a red sunset over mountains"):
    now = now_utc_iso()
    task = Task(
        task_id=task_id,
        task_type=task_type,
        status=TaskStatus.PROCESSING,
        created_at=now,
        updated_at=now,
        operator=operator,
        prompt=prompt,
    )
    store[task_id] = task
    return task


def test_completed_media_task_fires_media_notify(captured, fake_task_db):
    _seed(fake_task_db, task_type=TaskType.IMAGE_GENERATION, operator="Brandon")

    tasks_mod.update_task("t-1", status=TaskStatus.COMPLETED, progress=100)

    assert len(captured) == 1
    c = captured[0]
    assert c["operator"] == "Brandon"
    assert c["category"] == "media"
    assert c["title"]  # short, non-empty


def test_completed_chat_task_fires_task_notify(captured, fake_task_db):
    _seed(fake_task_db, task_type=TaskType.CHAT, operator="Casey", prompt="hi")

    tasks_mod.update_task("t-1", status=TaskStatus.COMPLETED)

    assert len(captured) == 1
    assert captured[0]["operator"] == "Casey"
    assert captured[0]["category"] == "task"


def test_failed_task_fires_notify(captured, fake_task_db):
    _seed(fake_task_db, task_type=TaskType.VIDEO_GENERATION, operator="Brandon")

    tasks_mod.update_task(
        "t-1", status=TaskStatus.FAILED, error_message="quota exhausted"
    )

    assert len(captured) == 1
    assert captured[0]["category"] == "media"
    # The body should carry a hint of the failure for the durable inbox.
    assert "quota" in captured[0]["body"].lower() or "fail" in captured[0]["title"].lower()


def test_progress_update_does_not_notify(captured, fake_task_db):
    _seed(fake_task_db, operator="Brandon")

    tasks_mod.update_task("t-1", progress=50)  # non-terminal

    assert captured == []


def test_processing_status_does_not_notify(captured, fake_task_db):
    _seed(fake_task_db, operator="Brandon")

    tasks_mod.update_task("t-1", status=TaskStatus.PROCESSING, progress=10)

    assert captured == []


@pytest.mark.parametrize("op", [None, "", "system"])
def test_system_scope_operator_suppressed(captured, fake_task_db, op):
    _seed(fake_task_db, operator=op)

    tasks_mod.update_task("t-1", status=TaskStatus.COMPLETED, progress=100)

    assert captured == []  # no spam for system-scope / unattributed tasks


def test_notify_exception_does_not_break_update_task(monkeypatch, fake_task_db):
    """A bridge that raises must NOT prevent update_task's DB write / return."""
    _seed(fake_task_db, operator="Brandon")

    def boom(*a, **k):
        raise RuntimeError("bridge exploded")

    monkeypatch.setattr(tasks_mod, "notify_in_background", boom)

    # Must complete cleanly and persist the terminal status.
    tasks_mod.update_task("t-1", status=TaskStatus.COMPLETED, progress=100)

    saved = fake_task_db["t-1"]
    assert saved.status == TaskStatus.COMPLETED
    assert saved.progress == 100


def test_unknown_task_id_no_notify_no_raise(captured, fake_task_db):
    """update_task on a missing id is a no-op (existing behavior) — no notify."""
    tasks_mod.update_task("does-not-exist", status=TaskStatus.COMPLETED)
    assert captured == []
