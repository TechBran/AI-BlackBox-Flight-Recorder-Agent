"""A FAILED/timed-out CU task must FAIL LOUDLY into the ledger (2026-07-11):
mint a snapshot carrying the agent's full narration up to the failure
(reasoning_text from the task row) and ENDING with the exact terminal error —
so the turn is never lost — while a CANCELLED task still never mints (G2-T8).
"""
import ast
import types
from pathlib import Path

import requests

import Orchestrator.tasks as tasks_mod
from Orchestrator.models import TaskType, TaskStatus

ROOT = Path(__file__).resolve().parents[2]


def _task(task_id="cu-fail-1", prompt="book a flight to Tokyo"):
    return types.SimpleNamespace(
        task_id=task_id, prompt=prompt, operator="Brandon",
        task_type=TaskType.USE_COMPUTER, status=TaskStatus.PROCESSING,
        result_data={},
    )


def _capture_posts(monkeypatch):
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append({"url": url, "json": json})
        return types.SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(requests, "post", fake_post)
    return posts


def test_failure_mints_transcript_and_error(monkeypatch):
    posts = _capture_posts(monkeypatch)
    monkeypatch.setattr(tasks_mod, "is_cancel_requested", lambda tid: False)
    row = types.SimpleNamespace(
        reasoning_text="[step 1] I'll open the airline site\n  → left_click([243, 118])\n"
                       "[step 2] typing Tokyo\n  → type(Tokyo)",
        progress_text="step 2/150 — type(Tokyo)",
    )
    monkeypatch.setattr(tasks_mod.task_db, "get_task", lambda tid: row)

    tasks_mod._mint_cu_failure_snapshot(
        _task(), "Brandon",
        "OpenAI API error: Error code: 429 - insufficient_quota")

    assert len(posts) == 1
    body = posts[0]["json"]["assistant_response"]
    assert posts[0]["json"]["operator"] == "Brandon"
    # LOUD failure framing — retrieval must never read this as a success
    assert "TASK FAILED" in body and "incomplete" in body
    # the narration up to the failure is preserved...
    assert "I'll open the airline site" in body and "type(Tokyo)" in body
    # ...and the exact terminal error ends the record
    assert "429" in body and "insufficient_quota" in body
    assert body.rstrip().endswith("insufficient_quota")


def test_cancelled_task_never_mints(monkeypatch):
    """G2-T8 stays intact: an operator kill is a decision, not a failure —
    no snapshot even if a failure path is reached during the cancel race."""
    posts = _capture_posts(monkeypatch)
    monkeypatch.setattr(tasks_mod, "is_cancel_requested", lambda tid: True)

    tasks_mod._mint_cu_failure_snapshot(_task(), "Brandon", "timed out")

    assert posts == []


def test_mint_failure_never_masks_the_real_error(monkeypatch):
    """A /chat/save hiccup is swallowed — the CU failure handling must finish."""
    monkeypatch.setattr(tasks_mod, "is_cancel_requested", lambda tid: False)
    monkeypatch.setattr(tasks_mod.task_db, "get_task",
                        lambda tid: types.SimpleNamespace(
                            reasoning_text="x", progress_text="step 1/150"))

    def boom(*a, **k):
        raise RuntimeError("orchestrator offline")
    monkeypatch.setattr(requests, "post", boom)

    # Must not raise.
    tasks_mod._mint_cu_failure_snapshot(_task(), "Brandon", "some error")


def test_all_three_failure_paths_call_the_mint():
    """Structural gate (AST): the soft-fail branch, the TimeoutError handler,
    and the generic-Exception handler inside process_browser_use must EACH call
    _mint_cu_failure_snapshot — a refactor that drops one silently loses turns
    again."""
    src = (ROOT / "Orchestrator" / "tasks.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "process_browser_use")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_mint_cu_failure_snapshot"]
    assert len(calls) == 3, (
        f"process_browser_use must mint the failure snapshot on ALL THREE "
        f"failure paths (soft-fail / TimeoutError / Exception); found "
        f"{len(calls)} call(s)"
    )
