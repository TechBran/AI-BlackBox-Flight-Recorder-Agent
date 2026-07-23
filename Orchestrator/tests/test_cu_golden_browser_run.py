"""Golden (characterization) test for the /browser/run USE_COMPUTER task path.

Pins the externally observable contract of `tasks.process_browser_use` /
`tasks.process_task`. Written BEFORE the CU consolidation (Tasks 10-12);
Task 12 replaced the legacy `browser/agent_loop.BrowserSession` with the
headless runner (`browser/headless.run_cu_task`) and re-pointed ONLY the
fixture seams below — the CONTRACT assertion blocks are byte-unchanged.
This test is the proof of behavioral equivalence for every consumer of the
task contract (Portal poller, the `use_computer` ToolVault tool, the
scheduler).

Contract pinned here:
  - task.status == COMPLETED, task.progress == 100
  - task.result_url == result_data["final_screenshot"]
  - result_data keys: result_text, screenshots (list), final_screenshot,
    steps, tokens{input,output} — MERGED over the pre-existing result_data
    (the original "url" key survives)

Assertion blocks are split into CONTRACT (survived Task 12 byte-unchanged)
and SEAM PINS (tied to the current headless-runner implementation).

Isolation (this is a live production box):
  - task_db is replaced with an in-memory fake (no Portal/tasks.db rows)
  - the persistent per-operator CU session is destroyed between tests
    (screenshot counters / token totals would otherwise leak — all three
    tests use operator="system")
  - ComputerUseSession.ensure_browser/destroy stubbed (no Xvfb/Chrome)
  - httpx is replaced in sys.modules with a fake whose SSE stream returns a
    canned end_turn response (no Anthropic HTTP)
  - capture_screenshot/save_screenshot_to_uploads patched in the headless
    namespace (no files written to Portal/uploads)
  - chat_routes seams (_get_tools, build_cu_context, _cu_save_to_blackbox)
    stubbed — no ToolVault embedding, fossil retrieval, or real auto-mint
  - requests.post stubbed (the post-success auto-snapshot POST to /chat/save
    must never reach the live Orchestrator on :9091)
"""
import io
import json
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from Orchestrator import tasks as tasks_mod
from Orchestrator.browser import headless
from Orchestrator.browser.session_manager import ComputerUseSession, destroy_session
from Orchestrator.models import Task, TaskStatus, TaskType
from Orchestrator.routes import chat_routes

GOLDEN_API_RESPONSE = {
    "content": [{"type": "text", "text": "Golden run complete"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 11, "output_tokens": 7},
}


def _golden_sse_lines():
    """The Anthropic streaming-SSE rendering of GOLDEN_API_RESPONSE."""
    events = [
        {"type": "content_block_start", "content_block": {"type": "text"}},
        {"type": "content_block_delta",
         "delta": {"type": "text_delta",
                   "text": GOLDEN_API_RESPONSE["content"][0]["text"]}},
        {"type": "content_block_stop"},
        {"type": "message_delta",
         "delta": {"stop_reason": GOLDEN_API_RESPONSE["stop_reason"]},
         "usage": GOLDEN_API_RESPONSE["usage"]},
    ]
    return ["data: " + json.dumps(e) for e in events]


class _FakeHTTPX:
    """Stand-in for the httpx module: the driver's only external API seam.
    `client.stream(...)` records the call and replays the golden SSE body."""

    class TimeoutException(Exception):
        pass

    class ConnectError(Exception):
        pass

    def __init__(self, api_calls: list):
        self._api_calls = api_calls

    def AsyncClient(self, **kwargs):
        api_calls = self._api_calls

        class _Resp:
            status_code = 200

            async def aiter_lines(self):
                for line in _golden_sse_lines():
                    yield line

            async def aread(self):
                return b""

        class _StreamCtx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *exc):
                return False

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, api_url, headers=None, json=None):
                api_calls.append({"method": method, "url": api_url,
                                  "headers": headers, "payload": json})
                return _StreamCtx()

        return _Client()


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

    # -- per-operator session isolation: fresh CU session for each test -------
    monkeypatch.setattr(ComputerUseSession, "destroy", lambda self: None)
    destroy_session("system")

    # -- no real display / Chrome (M9: a virtual launch allocates a per-session
    #    display; the mock stands in a fake handle so the runner's allocation
    #    check passes and per-display capture routes through) -------------------
    class _FakeHandle:
        display_num = 100

        def get_env(self):
            return {"DISPLAY": ":100"}

        def touch(self):
            pass

    async def _ensure_browser(self, url="about:blank", backend="anthropic"):
        self.display = _FakeHandle()
        return True

    monkeypatch.setattr(ComputerUseSession, "ensure_browser", _ensure_browser)
    monkeypatch.setattr(headless, "NATIVE_MODE", True)  # skip Xvfb health check
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        "Orchestrator.browser.screenshot.capture_screenshot_display",
        lambda n, native=None: _tiny_png())
    monkeypatch.setattr(
        "Orchestrator.browser.screenshot.capture_screenshot",
        lambda *a, **k: _tiny_png())

    # -- Anthropic seam: fake httpx, loop terminates with end_turn -------------
    api_calls = []
    monkeypatch.setitem(sys.modules, "httpx", _FakeHTTPX(api_calls))

    # -- screenshot seams, patched where headless LOOKS THEM UP ---------------
    monkeypatch.setattr(headless, "capture_screenshot", lambda *a, **k: _tiny_png())

    saved_urls = []

    def fake_save(png_bytes, ident, step):
        # Signature-agnostic: the runner passes (f"cu_{operator}",
        # session.screenshot_count).
        assert png_bytes.startswith(b"\x89PNG"), "capture seam must yield real PNG bytes"
        url = f"/ui/uploads/golden_{ident}_step{step:03d}.png"
        saved_urls.append(url)
        return url

    monkeypatch.setattr(headless, "save_screenshot_to_uploads", fake_save)

    # -- chat_routes seams (resolved lazily by runner/driver at call time):
    #    no ToolVault embedding, no fossil retrieval, no real BlackBox mint
    monkeypatch.setattr(chat_routes, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(chat_routes, "build_cu_context", lambda *a, **k: ("", {}))

    async def _no_save(*a, **k):
        return None

    monkeypatch.setattr(chat_routes, "_cu_save_to_blackbox", _no_save)

    # -- no real pacing: the runner's Chrome-settle sleep is pure waste here.
    # wait_for() does not route through asyncio.sleep.
    async def _instant(_secs):
        return None

    monkeypatch.setattr(headless.asyncio, "sleep", _instant)

    # -- auto-snapshot POST must never reach the live server ------------------
    chat_posts = []

    def fake_post(url, *args, **kwargs):
        chat_posts.append(url)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None)

    monkeypatch.setattr("requests.post", fake_post)

    yield SimpleNamespace(
        db=fake_db, api_calls=api_calls, saved_urls=saved_urls, chat_posts=chat_posts
    )

    destroy_session("system")


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

    # --- SEAM PINS (Task 12: re-pointed to the headless runner) --------------
    # Single API round-trip → exactly the initial screenshot, one step
    assert len(golden_env.api_calls) == 1
    assert rd["steps"] == 1
    assert rd["screenshots"] == golden_env.saved_urls
    # Initial screenshot only, chat-path naming: cu_{operator} + session count
    assert golden_env.saved_urls == ["/ui/uploads/golden_cu_system_step001.png"]
    # The auto-snapshot went to /chat/save (direct persistence), not /chat
    assert golden_env.chat_posts == ["http://localhost:9091/chat/save"]


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
