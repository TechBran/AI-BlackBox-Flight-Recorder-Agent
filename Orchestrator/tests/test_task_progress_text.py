"""G3-T11 (M3.1) — progress_text + device_id on the task record and endpoints.

Backend foundation for live task-queue streaming: the per-step CU narration
(and the CLI-agent stdout tail, and the video poll count) is folded into a
single bounded, poll-visible `progress_text` column that the Portal (T12) and
Android (T13) render on the task pill. This test suite pins:

  * the idempotent ADD COLUMN migration on a pre-existing tasks.db,
  * the lightweight single-column `append_task_progress` write path,
  * the CU producer (`_drain_and_fold` folds cu_step/cu_action into a line),
  * the two additive endpoint fields (`progress_text` + `device_id`).
"""
import asyncio
import sqlite3
from types import SimpleNamespace

from Orchestrator import tasks as tasks_mod
from Orchestrator.browser import headless as cu_headless
from Orchestrator.cli_agent.headless import _progress_line_from_tail
from Orchestrator.models import Task, TaskDatabase, TaskStatus, TaskType
from Orchestrator.routes import task_routes


def _columns(db_path: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("PRAGMA table_info(tasks)").fetchall()
    finally:
        conn.close()
    return {r[1] for r in rows}


def _mk(task_id, **kw) -> Task:
    now = "2026-07-10T00:00:00Z"
    base = dict(
        task_id=task_id,
        task_type=TaskType.USE_COMPUTER,
        status=TaskStatus.PROCESSING,
        created_at=now,
        updated_at=now,
        operator="system",
    )
    base.update(kw)
    return Task(**base)


# ---------------------------------------------------------------------------
# 1. Migration path — an existing DB WITHOUT the column must migrate cleanly.
# ---------------------------------------------------------------------------
def test_migration_adds_progress_text_to_existing_db(tmp_path):
    db_path = str(tmp_path / "old_tasks.db")

    # Build the OLD (pre-T11) schema directly — 14 columns, no progress_text.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, prompt TEXT,
            input_file TEXT, result_url TEXT, result_data TEXT, error_message TEXT,
            progress INTEGER DEFAULT 0, operator TEXT, image_data TEXT, google_video_uri TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO tasks (task_id, task_type, status, created_at, updated_at, progress) "
        "VALUES (?,?,?,?,?,?)",
        ("old-1", "use_computer", "completed",
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 100),
    )
    conn.commit()
    conn.close()

    assert "progress_text" not in _columns(db_path)  # absent before migration

    # Opening via TaskDatabase runs _init_db -> the idempotent ALTER guard.
    db = TaskDatabase(db_path)

    # Removing the migration guard makes THIS assertion fail (mutation-verify).
    assert "progress_text" in _columns(db_path)

    # An old NULL row reads back cleanly as None, not a crash.
    old = db.get_task("old-1")
    assert old is not None
    assert old.progress_text is None

    # Idempotent: re-opening the (now-migrated) DB does not raise.
    TaskDatabase(db_path)


def test_fresh_db_has_progress_text_column(tmp_path):
    db = TaskDatabase(str(tmp_path / "fresh.db"))
    assert "progress_text" in _columns(db.db_path)
    db.save_task(_mk("f1"))
    assert db.get_task("f1").progress_text is None


# ---------------------------------------------------------------------------
# 2. append_task_progress — lightweight single-column write, bounded, latest-line.
# ---------------------------------------------------------------------------
def test_append_task_progress_writes_bounds_and_preserves(tmp_path, monkeypatch):
    db = TaskDatabase(str(tmp_path / "t.db"))
    monkeypatch.setattr(tasks_mod, "task_db", db)

    db.save_task(_mk("a1", result_data={"device_id": "blackbox", "keep": "me"}))

    tasks_mod.append_task_progress("a1", "step 2/5 — scroll(down)")
    got = db.get_task("a1")
    assert got.progress_text == "step 2/5 — scroll(down)"
    # A single-column write must NOT clobber the rest of the row.
    assert got.result_data == {"device_id": "blackbox", "keep": "me"}
    assert got.status == TaskStatus.PROCESSING

    # Latest-line semantics: the second call REPLACES the first.
    tasks_mod.append_task_progress("a1", "step 3/5 — left_click([9,9])")
    assert db.get_task("a1").progress_text == "step 3/5 — left_click([9,9])"

    # Bounded — a huge line is clamped.
    tasks_mod.append_task_progress("a1", "x" * 50000)
    assert len(db.get_task("a1").progress_text) <= tasks_mod.PROGRESS_TEXT_MAX_CHARS

    # Empty / blank lines are ignored (progress_text keeps its last value).
    tasks_mod.append_task_progress("a1", "")
    tasks_mod.append_task_progress("a1", "   ")
    assert db.get_task("a1").progress_text is not None


def test_update_task_clamps_progress_text(tmp_path, monkeypatch):
    """The shared write choke point: the CLI path writes progress_text through
    update_task -> save_task (NOT update_progress_text), so the bound must ALSO
    hold in update_task's setattr loop — otherwise an untrusted raw-stdout line
    could bypass the clamp via that path or any future direct caller."""
    db = TaskDatabase(str(tmp_path / "t.db"))
    monkeypatch.setattr(tasks_mod, "task_db", db)
    db.save_task(_mk("c1"))
    tasks_mod.update_task("c1", progress_text="y" * 50000)
    got = db.get_task("c1")
    assert got.progress_text is not None
    assert len(got.progress_text) <= tasks_mod.PROGRESS_TEXT_MAX_CHARS


# ---------------------------------------------------------------------------
# 2b. CLI producer — _progress_line_from_tail: latest non-blank line, bounded
#     to the CANONICAL constant (not a decoupled literal).
# ---------------------------------------------------------------------------
def test_cli_progress_line_from_tail_latest_and_bounded():
    # Latest NON-BLANK line (trailing blanks/whitespace skipped).
    assert _progress_line_from_tail(
        "first line\n\n   \nlast meaningful line  ") == "last meaningful line"
    assert _progress_line_from_tail("") == ""
    assert _progress_line_from_tail("   \n\n\t") == ""

    # Bounded to PROGRESS_TEXT_MAX_CHARS, not a bare literal. A line longer than
    # the bound clamps to EXACTLY the canonical constant — reverting the fix to a
    # decoupled/different literal makes this fail (mutation-verify).
    long_line = "x" * (tasks_mod.PROGRESS_TEXT_MAX_CHARS + 50)
    out = _progress_line_from_tail("earlier line\n" + long_line)
    assert len(out) == tasks_mod.PROGRESS_TEXT_MAX_CHARS


def test_update_task_preserves_progress_text(tmp_path, monkeypatch):
    """A later whole-row update_task (e.g. progress=90 on completion) must NOT
    wipe a progress_text set mid-run by append_task_progress."""
    db = TaskDatabase(str(tmp_path / "t.db"))
    monkeypatch.setattr(tasks_mod, "task_db", db)
    db.save_task(_mk("u1"))
    tasks_mod.append_task_progress("u1", "step 7/15 — type(hello)")
    tasks_mod.update_task("u1", progress=90)
    got = db.get_task("u1")
    assert got.progress == 90
    assert got.progress_text == "step 7/15 — type(hello)"


# ---------------------------------------------------------------------------
# 3. CU producer — _drain_and_fold folds cu_step/cu_action into progress_text
#    MID-RUN (a DB side effect) without changing the returned contract dict.
# ---------------------------------------------------------------------------
def test_cu_drain_and_fold_populates_progress_text(tmp_path, monkeypatch):
    db = TaskDatabase(str(tmp_path / "t.db"))
    monkeypatch.setattr(tasks_mod, "task_db", db)
    db.save_task(_mk("cu-1", result_data={"device_id": "blackbox"}))

    async def _run():
        q = asyncio.Queue()
        for e in [
            {"type": "cu_step", "data": {"step": 1, "total": 10}},
            {"type": "cu_action",
             "data": {"action": "left_click",
                      "params": {"action": "left_click", "coordinate": [243, 118]},
                      "step": 1}},
            {"type": "done", "data": {"content": "done"}},
            None,
        ]:
            q.put_nowait(e)
        session = SimpleNamespace(event_queue=q, current_step=1)

        async def _noop():
            return None

        agent_task = asyncio.create_task(_noop())
        await agent_task
        return await cu_headless._drain_and_fold(session, agent_task, [], task_id="cu-1")

    result = asyncio.run(_run())

    # Returned contract dict is UNCHANGED in shape (progress_text is a side effect).
    assert set(result) == {"success", "result_text", "screenshots",
                           "final_screenshot", "steps", "tokens"}
    assert result["success"] is True

    # The DB side effect: progress_text is the latest human-readable step line.
    # Removing the append_task_progress call in _drain_and_fold fails THIS
    # assertion (mutation-verify).
    final = db.get_task("cu-1")
    assert final.progress_text
    assert "left_click" in final.progress_text
    assert "1/10" in final.progress_text


# ---------------------------------------------------------------------------
# 4. Endpoints — additive progress_text + device_id.
# ---------------------------------------------------------------------------
def test_tasks_list_includes_progress_text_and_device_id(tmp_path, monkeypatch):
    db = TaskDatabase(str(tmp_path / "t.db"))
    monkeypatch.setattr(task_routes, "task_db", db)

    db.save_task(_mk("p1",
                     result_data={"device_id": "pixel-9"},
                     progress_text="step 3/10 — left_click([1,2])"))
    db.save_task(_mk("p2", result_data=None, progress_text=None))

    resp = task_routes.list_tasks(all=True)
    by_id = {x["task_id"]: x for x in resp["tasks"]}

    assert by_id["p1"]["progress_text"] == "step 3/10 — left_click([1,2])"
    # Removing device_id from the endpoint fails THIS assertion (mutation-verify).
    assert by_id["p1"]["device_id"] == "pixel-9"
    # Default mirrors tasks.py — result_data.get("device_id") or "blackbox".
    assert by_id["p2"]["device_id"] == "blackbox"
    assert by_id["p2"]["progress_text"] is None
    # Existing fields still present (additive, not a rename).
    assert "status" in by_id["p1"] and "progress" in by_id["p1"]


def test_tasks_status_includes_progress_text(tmp_path, monkeypatch):
    db = TaskDatabase(str(tmp_path / "t.db"))
    monkeypatch.setattr(task_routes, "task_db", db)
    db.save_task(_mk("s1",
                     result_data={"device_id": "blackbox"},
                     progress_text="step 5/9 — type(hello)"))

    resp = task_routes.get_task_status("s1")
    assert resp["progress_text"] == "step 5/9 — type(hello)"
    # device_id is reachable via the already-returned result_data.
    assert resp["result_data"]["device_id"] == "blackbox"
