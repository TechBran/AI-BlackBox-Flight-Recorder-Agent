"""
Tests for M2.7 — cron_job_history tool + enriched search_cron_jobs render.

- cron_job_history wraps manager.get_job_history(job_id, limit), operator-
  ownership-scoped per M2.5 (generic "Job not found" for a non-owner). It
  surfaces past runs (run_at, result/error, duration) so the AI can answer
  "did the 7am job fail?".
- search_cron_jobs render now includes the per-job run-outcome fields
  (last_run_result / error_count / last_run_duration_ms) that already live on
  the job dicts — a render-only change.

NO delete_cron_job tool is created — delete is intentionally UI-only.
"""

import asyncio
import sqlite3

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager
from Orchestrator.toolvault.context import ToolContext
from ToolVault.tools.cron_job_history.executor import execute as history_execute
from ToolVault.tools.search_cron_jobs.executor import execute as search_execute


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_new_tools.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    mgr = CronJobManager()
    monkeypatch.setattr(manager_mod, "_manager_instance", mgr, raising=False)
    return mgr


def _run(coro):
    return asyncio.run(coro)


def _insert_history_row(mgr, job_id, *, result="success", error=None,
                        duration_ms=1234, run_at="2026-06-24T07:00:00+00:00"):
    conn = sqlite3.connect(mgr.db_path)
    try:
        conn.execute(
            """
            INSERT INTO cron_job_history
                (job_id, run_at, prompt, model, result, delivery_status,
                 duration_ms, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, run_at, "hi", "gemini", result, "snapshot",
             duration_ms, error),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# cron_job_history tool
# ---------------------------------------------------------------------------

def test_history_returns_rows_for_owner(temp_manager):
    job = temp_manager.create_job(
        name="morning", prompt="hi", schedule="0 7 * * *", operator="A"
    )
    _insert_history_row(temp_manager, job["id"], result="success",
                        duration_ms=4200)
    result = _run(
        history_execute({"job_id": job["id"]}, ToolContext(operator="A"))
    )
    assert result.success, result.result
    rows = result.data["history"]
    assert len(rows) == 1
    assert rows[0]["result"] == "success"
    assert rows[0]["run_at"] == "2026-06-24T07:00:00+00:00"
    assert rows[0]["duration_ms"] == 4200


def test_history_denied_for_other_operator(temp_manager):
    """A non-owner gets the generic 'Job not found' (no existence leak)."""
    job = temp_manager.create_job(
        name="b job", prompt="hi", schedule="0 7 * * *", operator="B"
    )
    _insert_history_row(temp_manager, job["id"])
    result = _run(
        history_execute({"job_id": job["id"]}, ToolContext(operator="A"))
    )
    assert not result.success
    assert result.result == "Job not found"


def test_history_system_may_read_any(temp_manager):
    job = temp_manager.create_job(
        name="b job", prompt="hi", schedule="0 7 * * *", operator="B"
    )
    _insert_history_row(temp_manager, job["id"])
    result = _run(
        history_execute({"job_id": job["id"]}, ToolContext(operator="system"))
    )
    assert result.success, result.result
    assert len(result.data["history"]) == 1


def test_history_missing_job_id_errors(temp_manager):
    result = _run(history_execute({}, ToolContext(operator="A")))
    assert not result.success
    assert "job_id is required" in result.result


def test_history_respects_limit(temp_manager):
    job = temp_manager.create_job(
        name="j", prompt="hi", schedule="0 7 * * *", operator="A"
    )
    for i in range(5):
        _insert_history_row(
            temp_manager, job["id"],
            run_at=f"2026-06-2{i}T07:00:00+00:00",
        )
    result = _run(
        history_execute(
            {"job_id": job["id"], "limit": 2}, ToolContext(operator="A")
        )
    )
    assert result.success, result.result
    assert len(result.data["history"]) == 2


# ---------------------------------------------------------------------------
# search_cron_jobs — run-outcome fields surfaced
# ---------------------------------------------------------------------------

def test_search_surfaces_run_outcome_fields(temp_manager):
    job = temp_manager.create_job(
        name="morning report", prompt="hi", schedule="0 7 * * *", operator="A"
    )
    # Simulate a completed run exactly as the manager writes it: last_run_at is
    # always set alongside last_run_result / last_run_duration_ms in the same
    # UPDATE, and error_count increments on failure.
    conn = sqlite3.connect(temp_manager.db_path)
    try:
        conn.execute(
            "UPDATE cron_jobs SET last_run_at = ?, last_run_result = ?, "
            "last_run_duration_ms = ?, error_count = ? WHERE id = ?",
            ("2026-06-24T07:00:00+00:00", "error", 8100, 3, job["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    result = _run(search_execute({}, ToolContext(operator="A")))
    assert result.success, result.result
    text = result.result
    # The render must now let the AI answer "did the 7am job fail?".
    assert "error" in text  # last_run_result
    assert "8100" in text or "8.1" in text  # duration surfaced
    assert "3" in text  # error_count surfaced
