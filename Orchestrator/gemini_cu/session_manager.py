"""
Gemini CU Session Manager — manages persistent sessions for Gemini Computer Use.
"""
import asyncio
import time
import uuid
from typing import Optional, Dict, List, Any

MAX_ITERATIONS = 50
SESSION_TIMEOUT = 300


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

        # Background task state
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.agent_task: Optional[asyncio.Task] = None
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
        self.stop_requested = True
        if self.agent_task and not self.agent_task.done():
            self.agent_task.cancel()

    def destroy(self):
        self.request_stop()
        self.conversation_history.clear()


# Session Store
_sessions: Dict[str, GeminiCUSession] = {}
_operator_sessions: Dict[str, str] = {}


def get_or_create_session(operator: str, device_id: str, environment: str,
                          session_id: Optional[str] = None) -> GeminiCUSession:
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
    # Release any display reservation held by this operator's Gemini chat lane
    # (M1-T6 leak backstop — the chat guard claims keyed by "gemini-chat:<op>").
    from Orchestrator.browser.display_arbiter import release_claim
    release_claim(f"gemini-chat:{operator}")


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


def destroy_task_session(session: GeminiCUSession):
    """Tear down a create_task_session() session: stop its agent task and drop it
    from _sessions. Never touches _operator_sessions, so the operator's chat
    session is left intact. Idempotent."""
    session.destroy()
    _sessions.pop(session.session_id, None)
