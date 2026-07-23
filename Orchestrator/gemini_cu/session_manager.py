"""
Gemini CU Session Manager — manages persistent sessions for Gemini Computer Use.
"""
import asyncio
import time
import uuid
from typing import Optional, Dict, List, Any

MAX_ITERATIONS = 50
SESSION_TIMEOUT = 300


def _cancel_task_cross_loop(agent_task) -> None:
    """Cancel an asyncio.Task from ANY thread (G2-T8) — see the twin in
    Orchestrator/browser/session_manager.py for the full rationale.

    ``Task.cancel()`` must run on the task's own loop or it is not delivered
    until that loop next wakes. We dispatch onto ``task.get_loop()`` via
    ``call_soon_threadsafe`` unless already on that loop (same-loop callers
    cancel directly — byte-identical to the pre-T8 behavior)."""
    if agent_task is None or agent_task.done():
        return
    try:
        task_loop = agent_task.get_loop()
    except Exception:
        task_loop = None
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if task_loop is None or task_loop is running:
        agent_task.cancel()
        return
    try:
        task_loop.call_soon_threadsafe(agent_task.cancel)
    except RuntimeError:
        pass


class GeminiCUSession:
    """A persistent Gemini Computer Use session."""

    def __init__(self, operator: str, device_id: str, environment: str,
                 session_id: Optional[str] = None):
        self.session_id = session_id or str(uuid.uuid4())
        self.operator = operator
        self.device_id = device_id
        self.environment = environment  # "browser", "desktop", or "android"
        self.conversation_history: List[Any] = []
        self.screenshot_count: int = 0
        self.total_tokens: Dict[str, int] = {"input": 0, "output": 0}
        self.last_activity: float = time.time()

        # Per-session virtual display (M9). Virtual by default; native is opt-in.
        self.native_mode: bool = False
        self.display = None
        # Session-bound ActionExecutor once ensure_display() binds one — the
        # single input authority for this session's display (2026-07-23
        # display-coherence fix; the loop previously built bare executors that
        # inherited the box-global NATIVE_MODE and drove the real desktop).
        self.actions = None
        # Chrome on THIS session's display — started by ensure_display for the
        # "browser" environment / url tasks (a Chrome-less Xvfb has nothing for
        # the url preamble to type into; review find, 2026-07-23).
        self.chrome = None

        # Background task state
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.agent_task: Optional[asyncio.Task] = None
        # The task row this session is currently driving (M2 sid->task join).
        self.task_id: Optional[str] = None
        # Bounded live-narration tail for the live-view bubble (M4) — fed by
        # session_manager.fold_event_to_reasoning from the gemini loop.
        self.reasoning_tail: str = ""
        self.status: str = "idle"
        self.final_response: str = ""
        self.error_message: str = ""
        self.current_step: int = 0
        self.total_steps: int = MAX_ITERATIONS

        # E-Stop
        self.stop_requested: bool = False
        self.prompt_queue: List[str] = []

        # Fields for chat provider compat (mirrors ComputerUseSession)
        self.user_message: str = ""
        self.cu_log: List[dict] = []
        self.provenance: Dict[str, list] = {}
        self.usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}

    @property
    def display_number(self) -> int:
        from Orchestrator.browser.config import ACTIVE_DISPLAY
        return self.display.display_num if self.display is not None else ACTIVE_DISPLAY

    def ensure_display(self, start_url: str = None) -> bool:
        """Bind this session to its OWN virtual display (browser/desktop only).

        Mirrors ComputerUseSession.ensure_browser: allocate a per-session Xvfb
        at the Gemini native resolution (1440x900 via resolution_for_backend),
        bind a session-scoped ActionExecutor — gemini-999 coord space
        de-normalized against THIS display's resolution — and, for the
        "browser" environment or a url task, start Chrome ON that display (a
        Chrome-less Xvfb gives the url preamble nothing to type into).
        Historically Gemini sessions NEVER allocated (display stayed None →
        display_number fell back to the real desktop). Android targets and the
        explicit native opt-in are no-ops. Idempotent; touches the handle."""
        if self.native_mode or self.environment == "android":
            return True
        from Orchestrator.browser.display import get_allocator
        # Heal a stale handle after a TTL reap (allocator registration is the
        # truth) — mirrors ComputerUseSession.ensure_browser.
        if self.display is not None and get_allocator().get(self.session_id) is not self.display:
            print(f"[GEMINI CU] display {self.display.display} for "
                  f"{self.session_id[:8]} was reaped — reallocating")
            self.display = None
            self.actions = None
            if self.chrome is not None:
                try:
                    self.chrome.stop()
                except Exception:
                    pass
                self.chrome = None
        if self.display is None:
            try:
                self.display = get_allocator().allocate(
                    self.session_id, backend="gemini", operator=self.operator)
            except Exception as e:
                print(f"[GEMINI CU] display allocation failed for {self.operator}: {e}")
                return False
            from Orchestrator.browser.actions import ActionExecutor, COORD_SPACE_GEMINI
            self.actions = ActionExecutor(
                display_number=self.display.display_num,
                coord_space=COORD_SPACE_GEMINI, native_mode=False,
                resolution=(self.display.width, self.display.height))
        self.display.touch()
        if self.environment == "browser" or start_url:
            try:
                if self.chrome is None:
                    from Orchestrator.browser.chrome import ChromeInstance
                    self.chrome = ChromeInstance(operator=self.operator)
                if not self.chrome.is_running():
                    self.chrome.start(start_url or "about:blank", handle=self.display)
            except Exception as e:
                # Chrome trouble degrades to a desktop-only display, never a
                # dead session — the model can still use the app menu/panel.
                print(f"[GEMINI CU] Chrome start failed (non-fatal): {e}")
        return True

    def trim_history(self, max_messages: int = 200):
        """Cap conversation history to prevent token explosion."""
        if len(self.conversation_history) > max_messages:
            self.conversation_history = (
                self.conversation_history[:2] + self.conversation_history[-(max_messages - 2):]
            )

    def sync_usage(self):
        """Sync total_tokens (Gemini format) → usage (Anthropic format) for compat."""
        self.usage = {
            "prompt_tokens": self.total_tokens.get("input", 0),
            "completion_tokens": self.total_tokens.get("output", 0),
        }

    def touch(self):
        self.last_activity = time.time()

    def is_expired(self, timeout: int = SESSION_TIMEOUT) -> bool:
        return (time.time() - self.last_activity) > timeout

    def reset_task_state(self):
        self.status = "idle"
        self.final_response = ""
        self.error_message = ""
        self.current_step = 0
        self.stop_requested = False
        self.task_id = None
        self.reasoning_tail = ""
        while not self.event_queue.empty():
            try:
                self.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def enqueue_prompt(self, text: str) -> int:
        self.prompt_queue.append(text)
        return len(self.prompt_queue)

    def dequeue_prompt(self) -> Optional[str]:
        if self.prompt_queue:
            return self.prompt_queue.pop(0)
        return None

    def request_stop(self):
        # Cooperative flag (driver checks it at loop-top) + hard cancel of
        # agent_task on ITS OWN loop (cross-loop safe — G2-T8). NOTE: the
        # gemini /run task path (gemini_cu_routes._run_task) iterates the loop
        # generator directly and never sets agent_task, so there it is the
        # cooperative flag alone that stops the driver; the display claim is
        # released by _run_task's `finally: release_claim`.
        self.stop_requested = True
        _cancel_task_cross_loop(self.agent_task)

    def destroy(self):
        self.request_stop()
        if self.chrome is not None:
            try:
                self.chrome.stop()
            except Exception as e:
                print(f"[GEMINI CU] Chrome stop failed for {self.operator}: {e}")
            self.chrome = None
        if self.display is not None:
            from Orchestrator.browser.display import get_allocator
            try:
                get_allocator().release(self.session_id)
            except Exception as e:
                print(f"[GEMINI CU] display release failed for {self.operator}: {e}")
            self.display = None
            self.actions = None
        self.conversation_history.clear()


# Session Store
_sessions: Dict[str, GeminiCUSession] = {}
_operator_sessions: Dict[str, str] = {}


def get_or_create_session(operator: str, device_id: str, environment: str,
                          session_id: Optional[str] = None,
                          force_new: bool = False) -> GeminiCUSession:
    """force_new=True appends a brand-new session (D1 multi-desktop,
    2026-07-23) — the busy agent keeps its desktop; _operator_sessions is the
    MRU pointer only."""
    if not force_new:
        if session_id and session_id in _sessions:
            session = _sessions[session_id]
            if session.operator == operator and not session.is_expired():
                session.touch()
                return session

        if operator in _operator_sessions:
            sid = _operator_sessions[operator]
            if sid in _sessions:
                session = _sessions[sid]
                if not session.is_expired():
                    session.touch()
                    return session
                else:
                    session.destroy()
                    del _sessions[sid]

    session = GeminiCUSession(operator, device_id, environment, session_id)
    _sessions[session.session_id] = session
    _operator_sessions[operator] = session.session_id
    print(f"[GEMINI CU] Created session {session.session_id} for {operator} "
          f"targeting {device_id} ({environment})")
    return session


def get_session_by_id(session_id: str) -> Optional[GeminiCUSession]:
    """Operator-agnostic lookup for the live-view activity/stop endpoints (M4)."""
    return _sessions.get(session_id)


def get_session(operator: str) -> Optional[GeminiCUSession]:
    sid = _operator_sessions.get(operator)
    if sid and sid in _sessions:
        return _sessions[sid]
    return None


def destroy_session(operator: str):
    sid = _operator_sessions.pop(operator, None)
    if sid and sid in _sessions:
        _sessions[sid].destroy()
        del _sessions[sid]


def create_task_session(operator: str, device_id: str,
                        environment: str) -> GeminiCUSession:
    """Create an ISOLATED, one-shot Gemini CU session for a headless task.

    Registered in _sessions (by its own uuid) but deliberately NOT in
    _operator_sessions, so it never shadows, borrows, or clobbers the operator's
    interactive CHAT session (which get_or_create_session owns via
    _operator_sessions). The headless task path needs its own event_queue (its
    worker thread runs a distinct asyncio.run loop), a fresh conversation_history
    (so the driver's first-turn fossil retrieval fires and no chat context leaks
    in), and the EXACT requested device_id/environment (get_or_create_session
    returns a cache hit as-is and never re-applies them). Pair with
    destroy_task_session(). Additive: alters no existing function's behavior.
    """
    session = GeminiCUSession(operator, device_id, environment)
    _sessions[session.session_id] = session
    return session


def cleanup_expired_sessions() -> int:
    """Sweep expired, non-running Gemini sessions so their per-session displays
    free promptly. Without this, an expired (300s) chat session pinned one of
    the MAX_VIRTUAL_SESSIONS display slots for the full 1800s display TTL —
    three stale gemini chat turns could exhaust the allocator (review find,
    2026-07-23). Called from the startup TTL sweep alongside the browser
    session sweep. Returns the number of sessions destroyed."""
    removed = 0
    for sid, s in list(_sessions.items()):
        running = s.agent_task is not None and not s.agent_task.done()
        if s.is_expired() and not running and s.status != "running":
            print(f"[GEMINI CU] sweeping expired session {sid[:8]} ({s.operator})")
            s.destroy()
            _sessions.pop(sid, None)
            if _operator_sessions.get(s.operator) == sid:
                _operator_sessions.pop(s.operator, None)
            removed += 1
    return removed


def destroy_task_session(session: GeminiCUSession):
    """Tear down a create_task_session() session: stop its agent task and drop it
    from _sessions. Never touches _operator_sessions, so the operator's chat
    session is left intact. Idempotent."""
    session.destroy()
    _sessions.pop(session.session_id, None)
