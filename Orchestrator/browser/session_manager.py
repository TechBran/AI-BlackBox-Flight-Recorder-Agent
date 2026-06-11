"""
Computer Use Session Manager — persistent per-operator browser sessions.
Sessions survive across chat turns so the user can have a conversation
with the browser agent (e.g., "go to X" then "now click Y").

Background task support: the agent loop runs as an asyncio.Task that
survives client disconnection. Events are pushed to a queue; the SSE
generator reads from the queue.  If the client goes away the task
completes on its own and saves results to BlackBox history.

Session IDs: each session gets a UUID so the frontend can track and
reconnect to specific sessions. Prompt queuing lets users send new
prompts while a task is running. E-stop allows immediate cancellation.
"""
import asyncio
import time
import uuid
from typing import Dict, List, Optional

from Orchestrator.browser.config import (
    DISPLAY_WIDTH, DISPLAY_HEIGHT, SESSION_TIMEOUT, NATIVE_MODE
)
from Orchestrator.browser.display import ensure_display_running, get_display
from Orchestrator.browser.chrome import ChromeInstance
from Orchestrator.browser.actions import ActionExecutor


class ComputerUseSession:
    """A persistent browser session for Computer Use chat provider."""

    def __init__(self, operator: str, session_id: Optional[str] = None, device_id: str = "blackbox"):
        self.operator = operator
        self.session_id: str = session_id or str(uuid.uuid4())
        self.device_id: str = device_id
        self.created_at: float = time.time()
        self.chrome = ChromeInstance(operator=operator)
        self.actions = ActionExecutor()
        self.conversation_history: list = []  # Anthropic-format messages
        self.screenshot_count: int = 0
        self.total_tokens: Dict[str, int] = {"input": 0, "output": 0}
        self.last_activity: float = time.time()

        # ── Background task state ──
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.agent_task: Optional[asyncio.Task] = None
        self.status: str = "idle"           # idle | running | complete | error | stopped | queued
        self.final_response: str = ""
        self.final_thinking: str = ""
        self.cu_log: List[dict] = []
        self.user_message: str = ""
        self.error_message: str = ""
        self.current_step: int = 0
        self.total_steps: int = 15
        self.usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        self.provenance: Dict[str, list] = {}  # Fossil context provenance for snapshot tracing

        # ── E-Stop + Prompt Queue ──
        self.stop_requested: bool = False
        self.prompt_queue: List[str] = []
        self._pending_dequeue: Optional[str] = None  # Stashed next prompt for auto-dequeue

    def request_stop(self):
        """Request emergency stop of the running agent task."""
        self.stop_requested = True
        if self.agent_task and not self.agent_task.done():
            self.agent_task.cancel()

    def fresh_event_queue(self) -> asyncio.Queue:
        """Replace the event queue with a new one bound to the CALLING loop.

        asyncio.Queue binds to the event loop on first await. Sessions persist
        across turns AND across launch paths: the chat path runs on the server
        loop, the headless task path runs via asyncio.run() in a worker thread.
        Awaiting a queue bound to the other (possibly dead) loop raises
        "bound to a different event loop". EVERY launch site must call this
        right before starting its driver task. Paired call sites:
        browser/headless.py (run_cu_task), chat_routes.stream_computer_use,
        and chat_routes.stream_openai_computer_use.
        """
        self.event_queue = asyncio.Queue(maxsize=2000)
        return self.event_queue

    def enqueue_prompt(self, text: str) -> int:
        """Add a prompt to the queue. Returns queue position (1-based)."""
        self.prompt_queue.append(text)
        return len(self.prompt_queue)

    def dequeue_prompt(self) -> Optional[str]:
        """Pop the next prompt from the queue, or None if empty."""
        return self.prompt_queue.pop(0) if self.prompt_queue else None

    def reset_task_state(self):
        """Reset background task fields for a new turn.
        Preserves session_id, prompt_queue, and conversation_history.
        """
        # Drain any leftover events from previous run
        while not self.event_queue.empty():
            try:
                self.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.agent_task = None
        self.status = "idle"
        self.stop_requested = False
        self.final_response = ""
        self.final_thinking = ""
        self.cu_log = []
        self.user_message = ""
        self.error_message = ""
        self.current_step = 0
        self.total_steps = 15
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self._pending_dequeue = None

    def trim_history(self, max_messages: int = 200):
        """Cap conversation history to prevent token explosion.
        Keeps first 2 messages (system context) + most recent messages.
        """
        if len(self.conversation_history) > max_messages:
            self.conversation_history = (
                self.conversation_history[:2] + self.conversation_history[-(max_messages - 2):]
            )

    def is_alive(self) -> bool:
        """Check if the browser/desktop is available."""
        if NATIVE_MODE:
            return True  # Real desktop is always alive
        return self.chrome.is_running()

    def touch(self):
        """Update last activity timestamp."""
        self.last_activity = time.time()

    def is_expired(self, timeout: int = SESSION_TIMEOUT) -> bool:
        """Check if session has been inactive too long."""
        return (time.time() - self.last_activity) > timeout

    async def ensure_browser(self, url: str = "about:blank") -> bool:
        """Start display + Chrome if not already running.
        In native mode: real desktop is always running, no Chrome needed.
        """
        if NATIVE_MODE:
            # Real desktop is always available — nothing to start
            return True
        if not ensure_display_running():
            return False
        if not self.chrome.is_running():
            return self.chrome.start(url)
        return True

    def destroy(self):
        """Stop Chrome. Display persists for reuse. In native mode, nothing to stop."""
        if NATIVE_MODE:
            return
        try:
            self.chrome.stop()
        except Exception as e:
            print(f"[CU-SESSION] Error stopping Chrome for {self.operator}: {e}")


# ── Global session store ──
# Dual-dict: _sessions keys by session_id, _operator_sessions maps operator → session_id
_sessions: Dict[str, ComputerUseSession] = {}
_operator_sessions: Dict[str, str] = {}  # operator → session_id


def get_or_create_session(operator: str, session_id: Optional[str] = None, device_id: str = "blackbox") -> ComputerUseSession:
    """Get existing session or create new one.

    - If session_id provided and exists: return it (validate operator matches)
    - If session_id provided but not found: create new session with that ID
    - If no session_id: check operator's active session, else create new
    - Enforces single-display constraint (refuses if another operator has running task)
    - device_id: target device for screenshot/actions ("blackbox" = local)
    """
    global _sessions, _operator_sessions

    # ── Lookup by session_id if provided ──
    if session_id and session_id in _sessions:
        session = _sessions[session_id]
        if session.operator == operator:
            if session.is_alive() and not session.is_expired():
                session.touch()
                return session
            else:
                # Expired or dead — clean up and recreate
                _cleanup_session(session_id)
        else:
            # Operator mismatch — ignore the session_id, fall through
            print(f"[CU-SESSION] session_id {session_id[:8]} belongs to {session.operator}, not {operator}")

    # ── Lookup by operator ──
    if operator in _operator_sessions:
        existing_sid = _operator_sessions[operator]
        if existing_sid in _sessions:
            session = _sessions[existing_sid]
            if session.is_alive() and not session.is_expired():
                session.touch()
                return session
            else:
                _cleanup_session(existing_sid)

    # ── Enforce single-display constraint (local device only) ──
    for sid, s in list(_sessions.items()):
        if s.operator != operator:
            if s.status == "running":
                # Only block if BOTH sessions target the local device
                if s.device_id == "blackbox" and device_id == "blackbox":
                    print(f"[CU-SESSION] Cannot create local session for {operator} — {s.operator} has running local task")
                    raise RuntimeError(f"Cannot start session: {s.operator} has a running Computer Use task on the local display")
                # Remote devices are independent — allow concurrent sessions
                continue
            print(f"[CU-SESSION] Destroying {s.operator}'s idle session for new session: {operator}")
            _cleanup_session(sid)

    # ── Create fresh session ──
    session = ComputerUseSession(operator, session_id=session_id, device_id=device_id)
    _sessions[session.session_id] = session
    _operator_sessions[operator] = session.session_id
    print(f"[CU-SESSION] Created new session {session.session_id[:8]} for {operator}")
    return session


def get_session(operator: str, session_id: str = "") -> Optional[ComputerUseSession]:
    """Lookup helper: find session by session_id or operator. Returns None if not found."""
    if session_id and session_id in _sessions:
        s = _sessions[session_id]
        if s.operator == operator:
            return s
    if operator in _operator_sessions:
        sid = _operator_sessions[operator]
        if sid in _sessions:
            return _sessions[sid]
    return None


def get_operator_session(operator: str) -> Optional[ComputerUseSession]:
    """Get the active session for an operator, or None."""
    sid = _operator_sessions.get(operator)
    if sid and sid in _sessions:
        return _sessions[sid]
    return None


def _cleanup_session(session_id: str):
    """Internal: remove a session from both dicts and destroy it."""
    global _sessions, _operator_sessions
    if session_id in _sessions:
        session = _sessions[session_id]
        session.destroy()
        # Remove from operator map
        if _operator_sessions.get(session.operator) == session_id:
            del _operator_sessions[session.operator]
        del _sessions[session_id]


def destroy_session(operator: str):
    """Explicitly destroy an operator's session."""
    global _sessions, _operator_sessions
    sid = _operator_sessions.get(operator)
    if sid:
        _cleanup_session(sid)
        print(f"[CU-SESSION] Destroyed session for {operator}")


def cleanup_inactive_sessions(timeout: int = 600):
    """Remove sessions that have been inactive for too long.
    Skips sessions whose background agent task is still running.
    """
    global _sessions
    now = time.time()
    expired = [
        sid for sid, s in _sessions.items()
        if (now - s.last_activity) > timeout and s.status != "running"
    ]
    for sid in expired:
        op = _sessions[sid].operator if sid in _sessions else "unknown"
        print(f"[CU-SESSION] Cleaning up expired session {sid[:8]} for {op}")
        _cleanup_session(sid)
    # Also clean up old screenshot files
    cleanup_old_screenshots()


def cleanup_old_screenshots(uploads_dir: str = None, max_age_days: int = 7):
    """Remove old CU/browser screenshots from uploads directory."""
    import os
    import glob
    if uploads_dir is None:
        uploads_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "Portal", "uploads")
    if not os.path.isdir(uploads_dir):
        return
    now = time.time()
    max_age = max_age_days * 86400
    patterns = [os.path.join(uploads_dir, p) for p in ("browser_*.png", "cu_*.png")]
    removed = 0
    for pattern in patterns:
        for fpath in glob.glob(pattern):
            try:
                if now - os.path.getmtime(fpath) > max_age:
                    os.remove(fpath)
                    removed += 1
            except OSError:
                pass
    if removed:
        print(f"[CU-SESSION] Cleaned up {removed} old screenshots")


def strip_screenshots_from_history(history: list) -> list:
    """Replace base64 images in older messages with text placeholders.
    Keeps only the most recent user message's images intact.
    This prevents token explosion from accumulating screenshots.
    """
    if len(history) <= 2:
        return history

    stripped = []
    # Strip all but the last 2 messages (last assistant + last user with screenshot)
    for i, msg in enumerate(history):
        if i >= len(history) - 2:
            stripped.append(msg)
            continue

        role = msg.get("role", "")
        content = msg.get("content")

        if isinstance(content, list):
            new_content = []
            for block in content:
                if block.get("type") == "image":
                    new_content.append({
                        "type": "text",
                        "text": "[Previous screenshot omitted to save tokens]"
                    })
                elif block.get("type") == "tool_result":
                    # Strip images from tool results too
                    inner = block.get("content", [])
                    if isinstance(inner, list):
                        new_inner = []
                        for item in inner:
                            if item.get("type") == "image":
                                new_inner.append({
                                    "type": "text",
                                    "text": "[Screenshot omitted]"
                                })
                            else:
                                new_inner.append(item)
                        new_content.append({**block, "content": new_inner})
                    else:
                        new_content.append(block)
                else:
                    new_content.append(block)
            stripped.append({"role": role, "content": new_content})
        else:
            stripped.append(msg)

    return stripped
