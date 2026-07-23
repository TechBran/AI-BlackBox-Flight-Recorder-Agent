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
from Orchestrator.browser.chrome import ChromeInstance
from Orchestrator.browser.actions import ActionExecutor


def _cancel_task_cross_loop(agent_task) -> None:
    """Cancel an asyncio.Task from ANY thread (G2-T8).

    ``Task.cancel()`` is not thread-safe: it must run on the task's own event
    loop or the cancellation is not delivered until that loop next wakes. CU
    task-path drivers run on a worker thread's ``asyncio.run`` loop while the
    E-stop request arrives on the API thread, so we dispatch the cancel onto the
    task's OWN loop (obtained from ``task.get_loop()``) via
    ``call_soon_threadsafe`` whenever the caller is not already on that loop.
    Same-loop callers cancel directly — byte-identical to the pre-T8 behavior.
    """
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
        # Loop already closed -> the task is finished; nothing to cancel.
        pass


class ComputerUseSession:
    """A persistent browser session for Computer Use chat provider."""

    def __init__(self, operator: str, session_id: Optional[str] = None, device_id: str = "blackbox"):
        self.operator = operator
        self.session_id: str = session_id or str(uuid.uuid4())
        self.device_id: str = device_id
        self.created_at: float = time.time()
        self.chrome = ChromeInstance(operator=operator)
        self.native_mode: bool = False       # virtual by default; native is opt-in (M9)
        self.display = None                  # DisplayHandle when virtual, else None
        self.actions = ActionExecutor()
        self.conversation_history: list = []  # Anthropic-format messages
        # Human-readable reason for the last ensure_browser failure — the chat
        # streams surface it so a cap-hit reads "all 3 desktops in use", never
        # a generic "Failed to start browser session" (D1 cap surfacing).
        self.last_error: str = ""
        self.screenshot_count: int = 0
        self.total_tokens: Dict[str, int] = {"input": 0, "output": 0}
        self.last_activity: float = time.time()

        # ── Background task state ──
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.agent_task: Optional[asyncio.Task] = None
        # The task row this session is currently driving (M2 sid->task join;
        # set by headless._publish_session_link, cleared per turn).
        self.task_id: Optional[str] = None
        # Bounded rolling tail of the model's live narration (thinking +
        # spoken responses + action lines) — what the live-view bubble renders
        # (M4). Fed by fold_event_to_reasoning from all three drivers; the
        # ONLY narration store reachable for chat-launched runs (no task row).
        self.reasoning_tail: str = ""
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

    @property
    def display_number(self) -> int:
        from Orchestrator.browser.config import ACTIVE_DISPLAY
        return self.display.display_num if self.display is not None else ACTIVE_DISPLAY

    def capture_screenshot_bytes(self) -> bytes:
        """Screenshot THIS session's surface. Branches on whether a virtual
        display is ALLOCATED (not on native_mode), so with display=None it is
        byte-identical to the legacy capture_screenshot() path — additive and
        behavior-preserving until 9.4 allocates a per-session display."""
        from Orchestrator.browser.screenshot import (
            capture_screenshot, capture_screenshot_display,
        )
        if self.display is not None:
            # Touch per capture: last_activity otherwise only moves at turn
            # START, so the TTL reaper (VIRTUAL_DISPLAY_TTL idle) could tear a
            # display down under a >30-min agent mid-run (review find,
            # 2026-07-23). A capture happens every step — the honest heartbeat.
            self.display.touch()
            # native=False pins the capture env to THIS handle's :N — without it
            # the box-global NATIVE_MODE short-circuit captured the real desktop
            # (the 2026-07-23 display-coherence fix).
            return capture_screenshot_display(self.display.display_num, native=False)
        return capture_screenshot()

    def request_stop(self):
        """Request emergency stop of the running agent task.

        Sets the cooperative ``stop_requested`` flag (the driver honors it at its
        next loop-top — the fast path for a driver between actions) AND cancels
        ``agent_task`` (the hard path — an await-wedged driver raises
        ``CancelledError`` at its await point, the task completes, and the
        launch-site done-callback / headless ``finally`` releases the display
        claim). An E-stop for a YOLO agent is instant by design — no grace delay.

        CROSS-LOOP SAFE (G2-T8): the CU *task* path runs ``agent_task`` on a
        worker thread's own ``asyncio.run`` loop, so a bare ``.cancel()`` from the
        API thread would not wake that sleeping loop (the cancellation would not
        be delivered until the loop next woke — up to SESSION_TIMEOUT). We cancel
        on the task's OWN loop via ``call_soon_threadsafe``. Same-loop callers
        (the chat CU path: handler and agent_task both on the main loop) are
        byte-identical to a direct ``.cancel()``.
        """
        self.stop_requested = True
        _cancel_task_cross_loop(self.agent_task)

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
        self.task_id = None
        self.reasoning_tail = ""
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

    async def ensure_browser(self, url: str = "about:blank", backend: str = "anthropic") -> bool:
        """Start this session's display + Chrome. Native: nothing to start."""
        if self.native_mode:
            return True
        from Orchestrator.browser.display import get_allocator
        # Heal a stale handle: the TTL reaper (or an explicit release) may have
        # torn our display down while this session object lived on — allocator
        # registration is the truth, not our cached reference. Without this the
        # session is unhealable: every capture/click hits a dead Xvfb forever
        # (review find, 2026-07-23).
        if self.display is not None and get_allocator().get(self.session_id) is not self.display:
            print(f"[CU-SESSION] display {self.display.display} for "
                  f"{self.session_id[:8]} was reaped — reallocating")
            self.display = None
            try:
                self.chrome.stop()
            except Exception:
                pass
        if self.display is None:
            try:
                self.display = get_allocator().allocate(
                    self.session_id, backend=backend, operator=self.operator)
            except Exception as e:
                print(f"[CU-SESSION] display allocation failed for {self.operator}: {e}")
                self.last_error = str(e)
                return False
            # Re-bind the input executor to THIS session's display, unscaled.
            from Orchestrator.browser.actions import (
                ActionExecutor, COORD_SPACE_GEMINI, COORD_SPACE_ANTHROPIC)
            coord = COORD_SPACE_GEMINI if backend in ("google", "gemini") else COORD_SPACE_ANTHROPIC
            self.actions = ActionExecutor(display_number=self.display.display_num,
                                          coord_space=coord, native_mode=False,
                                          resolution=(self.display.width,
                                                      self.display.height))
        self.last_error = ""
        self.display.touch()
        if not self.chrome.is_running():
            return self.chrome.start(url, handle=self.display)
        return True

    def destroy(self):
        """Release this session's virtual display + Chrome.

        Gates on resources actually HELD, never on the mutable per-turn
        native_mode flag: a session that allocated a display on a virtual turn
        and was later flipped native would otherwise leak its quartet + Chrome
        + slot forever (review find, 2026-07-23)."""
        try:
            self.chrome.stop()
        except Exception as e:
            print(f"[CU-SESSION] Error stopping Chrome for {self.operator}: {e}")
        if self.display is not None:
            from Orchestrator.browser.display import get_allocator
            get_allocator().release(self.session_id)
            self.display = None


# ── Global session store ──
# Dual-dict: _sessions keys by session_id, _operator_sessions maps operator → session_id
_sessions: Dict[str, ComputerUseSession] = {}
_operator_sessions: Dict[str, str] = {}  # operator → session_id


def get_or_create_session(operator: str, session_id: Optional[str] = None,
                          device_id: str = "blackbox",
                          force_new: bool = False) -> ComputerUseSession:
    """Get existing session or create new one.

    - force_new=True: skip every lookup and append a brand-new session (D1,
      2026-07-23 multi-desktop semantics — a busy agent keeps its desktop and
      the new prompt gets its own).
    - If session_id provided and exists: return it (validate operator matches)
    - If session_id provided but not found: create new session with that ID
    - If no session_id: check operator's CURRENT session (MRU pointer), else
      create new. _operator_sessions is an MRU pointer only — an operator may
      hold multiple live sessions; older ones stay reachable by explicit id.
    - device_id: target device for screenshot/actions ("blackbox" = local)

    Multi-desktop (2026-07-23): the old cross-operator reclaim loop (destroy
    every other operator's idle session on fresh create) is GONE — sessions
    are per-desktop now and only expiry/explicit close removes them.

    Does NOT arbitrate the local display (M1-T6): single-display arbitration is
    per-LAUNCH and lives at the launch sites (see the display_arbiter). This is
    session lifecycle only.
    """
    global _sessions, _operator_sessions

    if not force_new:
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

        # ── Lookup by operator (MRU pointer) ──
        if operator in _operator_sessions:
            existing_sid = _operator_sessions[operator]
            if existing_sid in _sessions:
                session = _sessions[existing_sid]
                if session.is_alive() and not session.is_expired():
                    session.touch()
                    return session
                else:
                    _cleanup_session(existing_sid)

    # ── Create fresh session (force_new never reuses the caller's session_id —
    #    that id names the BUSY session the prompt is escaping from) ──
    session = ComputerUseSession(operator,
                                 session_id=None if force_new else session_id,
                                 device_id=device_id)
    _sessions[session.session_id] = session
    _operator_sessions[operator] = session.session_id
    print(f"[CU-SESSION] Created new session {session.session_id[:8]} for {operator}"
          + (" (forced new desktop)" if force_new else ""))
    return session


def get_session_by_id(session_id: str) -> Optional[ComputerUseSession]:
    """Operator-agnostic lookup for the live-view activity/stop endpoints (M4)
    — the served page knows only its sid. Tailscale is the auth perimeter, as
    for every /cu/view surface."""
    return _sessions.get(session_id)


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
    """Internal: remove a session from both dicts and destroy it. When the
    removed session was the operator's MRU pointer, repair the pointer to
    their most-recent surviving session (multi-desktop 2026-07-23) — a dead
    key would orphan every older desktop from no-id lookups."""
    global _sessions, _operator_sessions
    if session_id in _sessions:
        session = _sessions[session_id]
        session.destroy()
        del _sessions[session_id]
        if _operator_sessions.get(session.operator) == session_id:
            survivors = [s for s in _sessions.values()
                         if s.operator == session.operator]
            if survivors:
                newest = max(survivors, key=lambda s: s.last_activity)
                _operator_sessions[session.operator] = newest.session_id
            else:
                del _operator_sessions[session.operator]


def destroy_session(operator: str):
    """Explicitly destroy an operator's session."""
    global _sessions, _operator_sessions
    sid = _operator_sessions.get(operator)
    if sid:
        _cleanup_session(sid)
        print(f"[CU-SESSION] Destroyed session for {operator}")


def destroy_session_by_id(session_id: str) -> bool:
    """Explicitly destroy a session by id (the manual /cu/session/{sid}/close
    path). Returns False when the id is unknown — the caller 404s."""
    if session_id not in _sessions:
        return False
    op = _sessions[session_id].operator
    _cleanup_session(session_id)
    print(f"[CU-SESSION] Closed session {session_id[:8]} for {op} (explicit)")
    return True


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


# M4: bound for the session-held narration tail the live-view bubble polls.
# Mirrors the task-row reasoning_text discipline (rolling tail, hard clamp) so
# the 2-3s activity poll ships a small payload.
REASONING_TAIL_MAX_CHARS = 8000


def fold_event_to_reasoning(session, evt: dict) -> None:
    """Fold ONE driver event into the session's bounded narration tail (M4).

    Shared by all three CU drivers so the live-view activity endpoint reads
    one store regardless of backend or launch path. Handles both payload
    shapes: Anthropic streams raw str deltas for thinking/content; the
    Gemini/OpenAI loops yield {"text": ..., "step": N} dicts. cu_action events
    become "→ action(...)" lines — the floor that keeps the bubble non-empty
    for terse models. Never raises (a narration hiccup must not kill a run).
    """
    try:
        etype = evt.get("type")
        data = evt.get("data")
        text = ""
        if etype in ("thinking", "content"):
            if isinstance(data, dict):
                text = data.get("text", "") or ""
            elif isinstance(data, str):
                text = data
        elif etype == "cu_action":
            d = data or {}
            action = d.get("action") or "action"
            params = d.get("params")
            inner = ""
            if isinstance(params, dict):
                inner = ", ".join(
                    str(v)[:60] for k, v in params.items() if k != "action")
            step = d.get("step")
            prefix = f"[step {step}] " if step is not None else ""
            text = f"\n{prefix}→ {action}({inner})\n"
        if not text:
            return
        tail = (getattr(session, "reasoning_tail", "") or "") + text
        session.reasoning_tail = tail[-REASONING_TAIL_MAX_CHARS:]
    except Exception:
        pass


def budget_screenshots_in_history(history: list, keep_images: int = 3) -> list:
    """Return a copy of an Anthropic-format history with only the most recent
    ``keep_images`` image blocks intact; every older screenshot becomes a
    one-line text placeholder in the same position (tool_use/tool_result
    pairing preserved). Non-mutating and idempotent.

    THE 413 guard (2026-07-23): the CU send loop re-sends the whole history
    every iteration, so without a per-turn budget a long run accumulates up to
    CU_MAX_ITERATIONS full-res PNGs and dies on Anthropic's per-request caps
    (~100 images / ~32MB — surfaced as "request_too_large" at step ~10 on a
    native 3440x1440 capture). Call this at the top of EVERY send.
    """
    placeholder_text = ("[Earlier screenshot elided to keep the request under "
                        "the API size caps]")

    def _count(msgs) -> int:
        n = 0
        for msg in msgs:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "image":
                    n += 1
                elif (block.get("type") == "tool_result"
                      and isinstance(block.get("content"), list)):
                    n += sum(1 for item in block["content"]
                             if item.get("type") == "image")
        return n

    total = _count(history)
    if total <= keep_images:
        return history

    cutoff = total - keep_images  # image ordinals 1..cutoff get elided
    seen = 0
    out = []
    for msg in history:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_content = []
        changed = False
        for block in content:
            if block.get("type") == "image":
                seen += 1
                if seen <= cutoff:
                    new_content.append({"type": "text", "text": placeholder_text})
                    changed = True
                else:
                    new_content.append(block)
            elif (block.get("type") == "tool_result"
                  and isinstance(block.get("content"), list)):
                inner_new = []
                inner_changed = False
                for item in block["content"]:
                    if item.get("type") == "image":
                        seen += 1
                        if seen <= cutoff:
                            inner_new.append({"type": "text",
                                              "text": placeholder_text})
                            inner_changed = True
                        else:
                            inner_new.append(item)
                    else:
                        inner_new.append(item)
                if inner_changed:
                    new_content.append({**block, "content": inner_new})
                    changed = True
                else:
                    new_content.append(block)
            else:
                new_content.append(block)
        out.append({**msg, "content": new_content} if changed else msg)
    return out


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
