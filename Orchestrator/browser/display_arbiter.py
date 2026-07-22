"""Shared display arbitration for computer-use (CU) sessions — M1-T6.

The BlackBox drives ONE physical local X display. Three kinds of CU session can
contend for it, spread across TWO registries:

  * browser ComputerUseSession   — Orchestrator/browser/session_manager.py
    (shared by the Anthropic AND OpenAI CU drivers). Targets the LOCAL display
    only when ``device_id == "blackbox"``; a non-"blackbox" id is a REMOTE VNC
    desktop and does NOT touch the local X server.
  * Gemini CHAT session          — Orchestrator/gemini_cu/session_manager.py,
    cached per operator (``_operator_sessions`` → ``_sessions``).
  * Gemini TASK session          — same module, registered in ``_sessions`` ONLY
    (its own uuid; never in ``_operator_sessions``) by ``create_task_session``.

Both Gemini kinds target the local display only when ``environment == "desktop"``;
an ``"android"`` environment is an ADB device and does NOT touch the local X
server.

CLAIMS ARE PER-LAUNCH, NOT PER-SESSION (M1-T6 quality review — C1/C2 root cause)
------------------------------------------------------------------------------
A *session* is long-lived, reused across turns, and shared between an operator's
chat and task lanes. A *launch* is the thing that actually grabs the mouse. Keying
a reservation to a session breaks on reuse (a reused session's claim was released
between turns, leaving the next launch window unguarded) and lets a refused launch
drop a concurrent live launch's claim. So a claim is keyed by a caller-supplied
**per-launch id** (a task id, or a fresh uuid per chat turn) — never a bare
session id. The session id is recorded inside the owner for reporting + prune
lookup only; it is never the key.

THE LOCK (Issue 1 — the original blocker). CU tasks run in tasks.py's
``ThreadPoolExecutor`` — each in its own OS thread via ``asyncio.run`` — with no
lock around the registries, and a session only becomes ``status == "running"``
seconds after the launch decision (ensure_browser + a 2s settle + driver launch).
So the check and the reservation must be atomic under a process-wide
``threading.RLock`` (reentrant: ``try_claim`` calls ``local_display_owner``,
both under the lock). This is arbitration, not a queue/priority/preemption
(YAGNI): on conflict the caller REFUSES. Each caller keeps its own failure shape
(RuntimeError / ``_failure`` dict / SSE error event); the arbiter only reports.

GUARANTEED RELEASE (invariants 3+4). Every launch site releases the exact claim
it made, on every exit path, from a ``finally`` — never another launch's claim
(per-launch keys make cross-release impossible), and never relying on prune as
the primary path. Prune is a leak backstop only:
  * a claim whose recorded session is RUNNING is redundant (the status scan now
    owns the display) or has ENDED (complete/error/stopped) — dropped;
  * a claim whose session never materialised (or is gone) is dropped after a TTL.
The state check runs BEFORE the TTL, so a claim whose session still exists and is
merely ``starting``/``idle`` is never TTL-dropped mid-launch (C4). A live task is
always visible via the status scan regardless of its reservation, so expiring a
stale reservation never removes protection from a running task.
NOTE: a ``gemini-task`` claim is keyed by a task id and records no resolvable
session id (its GeminiCUSession uuid differs), so ``_own_session_for`` returns
None for it and prune falls to the TTL branch — it therefore keeps a residual
``starting``-beyond-TTL gap, closed only once it reaches ``running``. Its explicit
``finally`` release (headless task path) is what actually bounds it; prune is not
relied on there.

FAIL-CLOSED, BY DESIGN. If a driver HANGS after ``status == "running"``, its
claim is held: prune never reclaims a ``running`` session, and the TTL only fires
once the session is gone. This is intended — never hand agent B the mouse while
agent A's driver is wedged mid-action.

HOW THE CLAIM IS RELEASED ON E-STOP (corrected G2-T8 — the prior text here was
WRONG and was the source of the error, not a record of it: it claimed cu-stop
"does not task.cancel()"; it always has). ``/chat/cu-stop`` →
``session.request_stop()`` sets the cooperative ``stop_requested`` flag (the
driver honors it at its next loop-top — the fast path for a driver between
actions) AND cancels ``agent_task``. A driver wedged in an ``await`` raises
``CancelledError`` at that await point, the task COMPLETES, and the launch-site
``add_done_callback`` / headless ``finally: release_claim`` frees the display —
so an await-wedged driver is NOT held forever. The E-stop is instant by design
(no cooperative grace delay) — a kill switch for a YOLO agent that waits is not a
kill switch. request_stop cancels on the task's OWN loop via
``call_soon_threadsafe`` (``session_manager._cancel_task_cross_loop``), so the
worker-thread CU TASK path (``asyncio.run`` on its own loop) is freed too, not
just the same-loop chat path.

The ONE genuinely unrecoverable case is a driver blocked in a SYNCHRONOUS,
non-awaiting call (e.g. a sync screenshot / sync HTTP with no timeout): the event
loop never regains control, so the cancellation is never delivered — and
``task.cancel()`` cannot free that either (it is a thread problem, not a task
one). No backstop at this layer would help it; a process restart is the only
recourse. That case, and only that case, still holds the claim.

IMPORT DIRECTION (deliberate, acyclic). This module (as of the per-launch
redesign) is NOT imported by ``browser/session_manager`` — the browser lock no
longer arbitrates. It is imported lazily by the SIX launch sites
(``browser/headless`` ×2, the three ``chat_routes`` CU streams, and
``gemini_cu_routes`` ×2), and it imports the two session-manager modules LAZILY
(function-local) in return. Function-local imports guarantee no module-load cycle
can form (the repo already leans on this — see ``browser/headless.py`` and
``browser/dispatch.py``).

Callers MUST NOT import the private ``_GEMINI_LOCAL_ENVIRONMENTS`` — use the
public ``is_local_environment`` predicate so the "which envs drive the local
display" answer lives in ONE place (guarded by the driver-drift test).
"""
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple

# A session "holds" the local display only while actively driving it — match the
# single status every pre-M1-T6 guard keyed on ("running"). The pre-"running"
# window (idle→starting→running) is covered by the per-launch reservation, not by
# widening this.
_BUSY_STATUSES = ("running",)

# When a reservation's recorded session reaches one of these, prune reclaims it:
# "running" is now visible via the status scan (reservation redundant); the
# terminal three mean the task ended (display free). NOT "starting" (the scan
# does not count it — the reservation must persist) and NOT "idle" (pre-launch).
_RECLAIM_WHEN_OWN_STATUS_IN = frozenset({"running", "complete", "error", "stopped"})

# Backstop only, for a claim whose session never materialised / is gone. A live
# task stays visible via the status scan regardless, so this never shortens
# protection for a running task.
_RESERVATION_TTL = 120.0

# Process-wide, cross-THREAD (contenders are OS threads). Reentrant: try_claim
# holds it across a local_display_owner call.
_lock = threading.RLock()

# Test seam: if set, called inside try_claim's critical section AFTER the free
# check and BEFORE the reservation is recorded. Production leaves it None. A
# concurrency test sets it to a stall so two threads deterministically overlap in
# exactly the window the lock must serialize.
_before_record_hook = None


@dataclass(frozen=True)
class DisplayOwner:
    """Who currently holds (or has reserved) the local X display."""
    kind: str          # "browser" | "gemini-chat" | "gemini-task"
    operator: str
    session_id: str    # recorded for reporting / prune lookup — NEVER the claim key

    def describe(self) -> str:
        label = ("an Anthropic/OpenAI Computer Use task" if self.kind == "browser"
                 else "a Gemini Computer Use task")
        return (f"{label} is running on the local display "
                f"(operator {self.operator}, session {self.session_id[:8]})")


# claim_id (per-launch, caller-chosen, unique) -> (owner, monotonic_claim_time).
_reservations: Dict[str, Tuple[DisplayOwner, float]] = {}


# The Gemini `environment` values that drive the LOCAL X display. MUST be a
# superset of the environments the Gemini driver actually captures/acts on
# locally — gemini_cu/agent_loop.py's _capture_screenshot and
# _execute_predefined_action both branch on `environment in ("browser",
# "desktop")` for local-display work ("android" is ADB, not local). There is no
# shared constant across the two modules, so a structural drift test
# (test_arbiter_local_env_superset_of_driver) asserts this stays a superset — a
# fourth local environment added to the driver fails that test at commit time
# instead of surfacing with a live mouse (M1-T6 rev2, Hole 2).
_GEMINI_LOCAL_ENVIRONMENTS = ("browser", "desktop")


def is_local_environment(env: Optional[str]) -> bool:
    """Public predicate: does this Gemini ``environment`` drive the local X
    display? The SINGLE source of truth for that question — every launch site
    gates its claim on this, so widening the set (when the driver-drift test
    fires) updates all of them at once. Never let a caller hardcode the tuple or
    import the private constant (that is the exact drift the drift test guards)."""
    return env in _GEMINI_LOCAL_ENVIRONMENTS


def _browser_holds_local(session) -> bool:
    # Only a NATIVE session contends for the one physical display (M9). A virtual
    # session drives its OWN per-session Xvfb (display_arbiter is the native-only
    # mutex), so it never registers as the local-display owner.
    return (getattr(session, "device_id", None) == "blackbox"
            and getattr(session, "native_mode", False)
            and getattr(session, "status", None) in _BUSY_STATUSES)


def _gemini_holds_local(session) -> bool:
    # Native-only (M9): a virtual Gemini session does not touch the physical display.
    return (is_local_environment(getattr(session, "environment", None))
            and getattr(session, "native_mode", False)
            and getattr(session, "status", None) in _BUSY_STATUSES)


def _own_session_for(owner: DisplayOwner):
    """Resolve the reservation's recorded session BY ITS ID (M2 — not by
    operator, which would return whatever is cached now rather than the reserved
    session). Looks in both registries. Returns None when the id resolves to no
    live session (a gemini-task claim keyed by a task id, a never-created session,
    or a destroyed one)."""
    sid = owner.session_id
    from Orchestrator.browser import session_manager as bsm
    s = bsm._sessions.get(sid)
    if s is not None:
        return s
    from Orchestrator.gemini_cu import session_manager as gsm
    return gsm._sessions.get(sid)


def _prune_locked() -> None:
    """Drop stale reservations. MUST be called under _lock. State check BEFORE the
    TTL (C4): a claim whose session still exists and is starting/idle is kept."""
    now = time.monotonic()
    for claim_id, (owner, ts) in list(_reservations.items()):
        sess = _own_session_for(owner)
        if sess is not None:
            if getattr(sess, "status", None) in _RECLAIM_WHEN_OWN_STATUS_IN:
                _reservations.pop(claim_id, None)      # redundant (scan owns) or ended
            # else: exists but starting/idle — KEEP (never TTL-drop a live launch)
        elif (now - ts) > _RESERVATION_TTL:
            _reservations.pop(claim_id, None)          # never materialised / gone


def local_display_owner(
    exclude_session_ids: FrozenSet[str] = frozenset(),
) -> Optional[DisplayOwner]:
    """Return who holds/claims the LOCAL X display, consulting reservations AND
    both registries, or ``None`` if free. Read-only. ``exclude_session_ids`` skips
    matching claim ids / recorded session ids (used by tests; the launch sites do
    not need self-exclusion because a claim is made only at an actual launch,
    after reconnect/queue is already handled). Prunes stale reservations first."""
    with _lock:
        _prune_locked()
        for claim_id, (owner, _ts) in _reservations.items():
            if claim_id in exclude_session_ids or owner.session_id in exclude_session_ids:
                continue
            return owner
        from Orchestrator.browser import session_manager as bsm
        for sid, s in list(bsm._sessions.items()):
            if sid in exclude_session_ids:
                continue
            if _browser_holds_local(s):
                return DisplayOwner("browser", getattr(s, "operator", "?"), sid)
        from Orchestrator.gemini_cu import session_manager as gsm
        chat_sids = set(gsm._operator_sessions.values())
        for sid, s in list(gsm._sessions.items()):
            if sid in exclude_session_ids:
                continue
            if _gemini_holds_local(s):
                kind = "gemini-chat" if sid in chat_sids else "gemini-task"
                return DisplayOwner(kind, getattr(s, "operator", "?"), sid)
        return None


def try_claim(requester_kind: str, operator: str, claim_id: str,
              session_id: str = "") -> Optional[DisplayOwner]:
    """ATOMIC check-and-reserve. Under the lock: if the local display is free,
    record a reservation under the per-launch ``claim_id`` and return ``None``
    (claimed); otherwise reserve nothing and return the current owner (denied).
    ``session_id`` is recorded in the owner for reporting / prune lookup — it is
    NOT the key. Pair every success with ``release_claim(claim_id)`` in a
    ``finally`` (or use ``claim_local_display``)."""
    with _lock:
        owner = local_display_owner()
        if owner is not None:
            return owner
        if _before_record_hook is not None:
            _before_record_hook()  # test seam — see its definition above
        _reservations[claim_id] = (
            DisplayOwner(requester_kind, operator, session_id or claim_id),
            time.monotonic())
        return None


def release_claim(claim_id: str) -> None:
    """Drop a reservation. Idempotent; a no-op for an id that was never claimed
    (a denied launch, a remote/android launch that never claimed, or one already
    reclaimed by prune)."""
    with _lock:
        _reservations.pop(claim_id, None)


@contextmanager
def claim_local_display(requester_kind: str, operator: str, claim_id: str,
                        session_id: str = ""):
    """Context manager wrapping try_claim/release_claim so a launch site gets
    guaranteed release (invariant 4) and can only release its OWN claim
    (invariant 3) by construction. Yields the current owner: ``None`` means
    GRANTED (proceed); a ``DisplayOwner`` means DENIED (the body must not drive).
    Only a granted claim is released on exit."""
    owner = try_claim(requester_kind, operator, claim_id, session_id=session_id)
    try:
        yield owner
    finally:
        if owner is None:
            release_claim(claim_id)
