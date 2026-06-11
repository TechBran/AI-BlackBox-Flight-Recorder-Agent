"""Golden (characterization) test for the /browser/run USE_COMPUTER task path.

Pins the externally observable contract of `tasks.process_browser_use` /
`tasks.process_task` BEFORE the CU consolidation (Tasks 10-12) replaces the
legacy `browser/agent_loop.BrowserSession` with a headless runner. This test
must stay green across that refactor — it is the proof of behavioral
equivalence for every consumer of the task contract (Portal poller, the
`use_computer` ToolVault tool, the scheduler).

Contract pinned here (current code, verified by reading process_browser_use
and the process_task worker dispatch in tasks.py):
  - task.status == COMPLETED, task.progress == 100
  - task.result_url == result_data["final_screenshot"]
  - result_data keys: result_text, screenshots (list), final_screenshot,
    steps, tokens{input,output} — MERGED over the pre-existing result_data
    (the original "url" key survives)

Assertion blocks are split into CONTRACT (must survive Task 12 byte-unchanged)
and LEGACY SEAM PINS (tied to the BrowserSession implementation; Task 12
re-points the fixture seams and may relax those specific assertions).

Isolation (this is a live production box):
  - task_db is replaced with an in-memory fake (no Portal/tasks.db rows)
  - task_queue is replaced (nothing left for a worker to pick up)
  - BrowserSession.start/stop are stubbed (no Xvfb/Chrome)
  - _call_api returns a canned end_turn response (no Anthropic HTTP)
  - capture_screenshot/save_screenshot_to_uploads patched in the agent_loop
    namespace (no files written to Portal/uploads)
  - requests.post stubbed (the post-success auto-snapshot POST to /chat must
    never reach the live Orchestrator on :9091)
"""
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from Orchestrator import tasks as tasks_mod
from Orchestrator.browser import agent_loop
from Orchestrator.browser.agent_loop import BrowserSession
from Orchestrator.models import Task, TaskStatus, TaskType

GOLDEN_API_RESPONSE = {
    "content": [{"type": "text", "text": "Golden run complete"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 11, "output_tokens": 7},
}


def _tiny_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="PNG")
    return buf.getvalue()


class FakeTaskDB:
    """In-memory stand-in for models.task_db — production sqlite stays untouched."""

    def __init__(self):
        self.tasks = {}

    def save_task(self, task: Task):
        self.tasks[task.task_id] = task

    def get_task(self, task_id: str):
        return self.tasks.get(task_id)


@pytest.fixture
def golden_env(monkeypatch):
    """Mock every external seam of the task path; drive the REAL code in between."""
    # -- task DB / queue isolation -------------------------------------------
    fake_db = FakeTaskDB()
    monkeypatch.setattr(tasks_mod, "task_db", fake_db)
    monkeypatch.setattr(tasks_mod, "task_queue", [])

    # -- no real display / Chrome --------------------------------------------
    monkeypatch.setattr(BrowserSession, "start", lambda self, url="about:blank": True)
    monkeypatch.setattr(BrowserSession, "stop", lambda self: None)

    # -- Anthropic seam: loop terminates immediately with end_turn ------------
    api_calls = []

    async def fake_call_api(self, system, messages, tools):
        api_calls.append({"system": system, "messages": messages, "tools": tools})
        return dict(GOLDEN_API_RESPONSE)

    monkeypatch.setattr(BrowserSession, "_call_api", fake_call_api)

    # -- screenshot seams, patched where agent_loop LOOKS THEM UP -------------
    # Task 12 NOTE: when agent_loop.py is deleted, re-point these seams at the
    # replacement runner's module. The replacement path also keeps a persistent
    # per-operator CU session — reset/patch it between tests or screenshot
    # counters will leak across tests (all three use operator="system").
    monkeypatch.setattr(agent_loop, "capture_screenshot", lambda *a, **k: _tiny_png())

    saved_urls = []

    def fake_save(png_bytes, ident, step):
        # Signature-agnostic: legacy passes (task_id, step); the Task-12 runner
        # passes (f"cu_{operator}", session.screenshot_count).
        assert png_bytes.startswith(b"\x89PNG"), "capture seam must yield real PNG bytes"
        url = f"/ui/uploads/golden_{ident}_step{step:03d}.png"
        saved_urls.append(url)
        return url

    monkeypatch.setattr(agent_loop, "save_screenshot_to_uploads", fake_save)

    # -- no real pacing: the loop's page-load/UI-settle sleeps are pure waste
    # here (2s+ per test). wait_for() does not route through asyncio.sleep.
    async def _instant(_secs):
        return None

    monkeypatch.setattr(agent_loop.asyncio, "sleep", _instant)

    # -- auto-snapshot POST must never reach the live server ------------------
    chat_posts = []

    def fake_post(url, *args, **kwargs):
        chat_posts.append(url)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("requests.post", fake_post)

    return SimpleNamespace(
        db=fake_db, api_calls=api_calls, saved_urls=saved_urls, chat_posts=chat_posts
    )


def _make_task(**kwargs) -> Task:
    """Create the task the same way the /browser/run route does — via the real
    create_task() (persists PENDING + enqueues), against the fake DB."""
    return tasks_mod.create_task(
        TaskType.USE_COMPUTER,
        operator="system",
        prompt="Golden test prompt",
        **kwargs,
    )


def test_use_computer_golden_result_contract(golden_env):
    task = _make_task(result_data={"url": "https://example.com"})

    # create_task persisted PENDING into the (fake) DB
    assert golden_env.db.get_task(task.task_id).status == TaskStatus.PENDING

    tasks_mod.process_browser_use(task)

    final = golden_env.db.get_task(task.task_id)

    # --- CONTRACT (must survive Task 12 byte-unchanged) ----------------------
    assert final.status == TaskStatus.COMPLETED
    assert final.progress == 100
    assert final.error_message is None

    rd = final.result_data
    # The five contract keys
    assert rd["result_text"] == "Golden run complete"
    assert isinstance(rd["screenshots"], list) and len(rd["screenshots"]) >= 1
    assert rd["final_screenshot"] == rd["screenshots"][-1]
    assert rd["steps"] >= 1
    assert rd["tokens"] == {"input": 11, "output": 7}
    # result_url mirrors the final screenshot
    assert final.result_url == rd["final_screenshot"]
    # New keys are MERGED over the original result_data — "url" survives
    assert rd["url"] == "https://example.com"

    # --- LEGACY SEAM PINS (Task 12 re-points seams; may relax these) ---------
    # Single API round-trip → exactly the initial screenshot, one step
    assert len(golden_env.api_calls) == 1
    assert rd["steps"] == 1
    assert rd["screenshots"] == golden_env.saved_urls
    # No screenshot files were written for real (fake_save returned URLs only)
    assert golden_env.saved_urls == [f"/ui/uploads/golden_{task.task_id}_step000.png"]


def test_worker_dispatch_routes_use_computer(golden_env):
    """process_task (the real worker entry at tasks.py:~284) routes USE_COMPUTER
    through the browser path and lands on the same COMPLETED contract."""
    task = _make_task(result_data=None)

    tasks_mod.process_task(task)

    final = golden_env.db.get_task(task.task_id)
    assert final.status == TaskStatus.COMPLETED
    assert final.progress == 100
    rd = final.result_data
    assert set(rd) >= {"result_text", "screenshots", "final_screenshot", "steps", "tokens"}
    assert rd["result_text"] == "Golden run complete"
    assert rd["tokens"] == {"input": 11, "output": 7}


def test_legacy_browser_use_type_routes_same_path(golden_env):
    """BROWSER_USE (legacy enum kept for old DB rows) hits the same handler."""
    task = tasks_mod.create_task(
        TaskType.BROWSER_USE, operator="system", prompt="Golden legacy prompt"
    )

    tasks_mod.process_task(task)

    final = golden_env.db.get_task(task.task_id)
    assert final.status == TaskStatus.COMPLETED
    assert final.result_data["result_text"] == "Golden run complete"
