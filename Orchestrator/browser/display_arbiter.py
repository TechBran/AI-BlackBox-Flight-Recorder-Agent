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

Before M1-T6 no single guard saw all three; this module is the ONE place that
consults BOTH registries so every guard can ask the same question — "who owns the
local display right now?" — and see all three.

WHY A LOCK AND A RESERVATION REGISTRY (M1-T6 review, Issue 1 — the BLOCKER)
--------------------------------------------------------------------------
A point-in-time *check* is not enough. CU tasks run in tasks.py's
``ThreadPoolExecutor`` — each task in its OWN OS thread via ``asyncio.run`` — with
no lock anywhere around the session registries. The session that will drive the
display is registered (and only becomes ``status == "running"``) AFTER the guard
checks. So two threads can both check an empty registry, both pass, and both
drive the one physical mouse. No choice of "busy" statuses closes that — the
window (ensure_browser + a 2s settle + driver launch) is *seconds*. M1-T7 (three
voice agents launching CU) makes concurrent launches routine.

The fix is an ATOMIC check-and-reserve under a process-wide lock: look at both
registries AND the reservation table, and — if free — record a reservation,
before releasing the lock. The contenders are OS threads, so the lock is a
``threading.RLock`` (reentrant: ``try_claim`` calls ``local_display_owner``,
both under the lock). A reservation makes a caller's intent visible to the next
caller in the gap between "decided to run" and "session visibly running".

This is arbitration, not a queue/priority/preemption (YAGNI): on conflict the
caller REFUSES. Each caller keeps its own failure shape (RuntimeError /
``_failure`` dict / SSE error event); the arbiter only reports.

LEAK SAFETY (the named primary hazard). A leaked reservation must never wedge the
display permanently. Three independent reclaim paths:
  1. Explicit ``release_claim`` in the reserving callers' ``finally`` (headless
     browser + Gemini task).
  2. ``release_claim`` on session destruction (browser ``_cleanup_session``,
     Gemini ``destroy_session``) — the persistent chat kinds.
  3. Lazy prune inside every locked read: a reservation is dropped once its own
     session is RUNNING (now visible via the registry scan — the reservation is
     redundant) or has ENDED (complete/error/stopped), and unconditionally after
     a TTL. A RUNNING task always remains visible via the status scan, so
     expiring a stale reservation can never drop protection for a live task — it
     only reclaims a leaked or never-launched claim.

IMPORT DIRECTION (deliberate, acyclic). This module imports the two
session-manager modules LAZILY (function-local). It is imported — also lazily —
by ``browser/session_manager`` (its lock), ``chat_routes`` (the Gemini chat
guard) and ``browser/headless`` (the Gemini task guard). Keeping our own imports
function-local guarantees no module-load cycle can form no matter who imports
whom (the repo already leans on this — see ``browser/headless.py`` and
``browser/dispatch.py``). Self-exclusion uses each manager's PUBLIC accessor
(``get_operator_session`` / ``get_session``) so no caller — and this module's
own lookups — ever reach into another module's private registry maps (Issue 3).
"""
import threading
import time
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple

# A session "holds" the local display only while actively driving it. Match the
# single status every pre-M1-T6 guard already keyed on ("running") so this
# unification changes WHICH states the status-scan blocks for NO one. The gap
# BEFORE "running" (idle→starting→running) is covered by reservations, not by
# widening this — see the module docstring.
_BUSY_STATUSES = ("running",)

# When a reservation's own session reaches one of these, the reservation is
# reclaimed by prune: "running" is now visible via the status scan (reservation
# redundant); the terminal three mean the task ended (display free). NOT
# "starting" (the scan does not count it, so the reservation must persist to keep
# protection) and NOT "idle" (pre-launch — protection still needed).
_RECLAIM_WHEN_OWN_STATUS_IN = frozenset({"running", "complete", "error", "stopped"})

# Backstop only. Longer than any real launch window; a live task stays visible
# via the status scan regardless, so this never shortens protection for a running
# task — it only bounds a leaked / never-launched reservation.
_RESERVATION_TTL = 120.0

# Process-wide, cross-THREAD (contenders are OS threads). Reentrant: try_claim
# holds it across a local_display_owner call.
_lock = threading.RLock()

# Test seam: if set, called inside try_claim's critical section AFTER the "is the
# display free?" check and BEFORE the reservation is recorded. Production leaves
# it None (a single is-None branch, no overhead). A concurrency test sets it to a
# stall so two threads deterministically overlap in exactly the window the lock
# must serialize (without it, the GIL hides the check→record race).
_before_record_hook = None


@dataclass(frozen=True)
class DisplayOwner:
    """Who currently holds (or has reserved) the local X display."""
    kind: str          # "browser" | "gemini-chat" | "gemini-task"
    operator: str
    session_id: str    # session id, or the reservation's claim id

    def describe(self) -> str:
        label = ("an Anthropic/OpenAI Computer Use task" if self.kind == "browser"
                 else "a Gemini Computer Use task")
        return (f"{label} is running on the local display "
                f"(operator {self.operator}, session {self.session_id[:8]})")


# claim_id -> (owner, monotonic_claim_time). claim_id is caller-chosen and stable
# for the life of the claim (browser: the session id; Gemini chat:
# "gemini-chat:<operator>"; Gemini task: the task id).
_reservations: Dict[str, Tuple[DisplayOwner, float]] = {}


def _browser_holds_local(session) -> bool:
    return (getattr(session, "device_id", None) == "blackbox"
            and getattr(session, "status", None) in _BUSY_STATUSES)


def _gemini_holds_local(session) -> bool:
    return (getattr(session, "environment", None) == "desktop"
            and getattr(session, "status", None) in _BUSY_STATUSES)


def _own_session_for(owner: DisplayOwner):
    """The requester's OWN persistent session (via PUBLIC accessors), used to
    reclaim a redundant/ended reservation. Gemini TASK claims have no persistent
    self and are reclaimed only by explicit release + TTL."""
    if owner.kind == "browser":
        from Orchestrator.browser import session_manager as bsm
        return bsm.get_operator_session(owner.operator)
    if owner.kind == "gemini-chat":
        from Orchestrator.gemini_cu import session_manager as gsm
        return gsm.get_session(owner.operator)
    return None


def _prune_locked() -> None:
    """Drop stale reservations. MUST be called under _lock."""
    now = time.monotonic()
    for claim_id, (owner, ts) in list(_reservations.items()):
        if (now - ts) > _RESERVATION_TTL:
            _reservations.pop(claim_id, None)
            continue
        sess = _own_session_for(owner)
        if sess is not None and getattr(sess, "status", None) in _RECLAIM_WHEN_OWN_STATUS_IN:
            _reservations.pop(claim_id, None)


def _own_same_kind_claim_ids(requester_kind: str, operator: str) -> FrozenSet[str]:
    """The claim/session id(s) the requester counts as "itself" — skipped so a
    caller never blocks on its own session (resume/reconnect). Same-KIND only: a
    browser request excludes its own browser session but NOT the operator's
    Gemini session (that is a real conflict), and vice versa. Uses PUBLIC
    accessors (Issue 3)."""
    ids = set()
    if requester_kind == "browser":
        from Orchestrator.browser import session_manager as bsm
        s = bsm.get_operator_session(operator)
        if s is not None:
            ids.add(s.session_id)
    elif requester_kind == "gemini-chat":
        from Orchestrator.gemini_cu import session_manager as gsm
        s = gsm.get_session(operator)
        if s is not None:
            ids.add(s.session_id)
        ids.add(f"gemini-chat:{operator}")  # this operator's own chat reservation
    return frozenset(ids)


def local_display_owner(
    exclude_session_ids: FrozenSet[str] = frozenset(),
) -> Optional[DisplayOwner]:
    """Return who holds/claims the LOCAL X display, consulting reservations AND
    both registries, or ``None`` if free. Read-only. ``exclude_session_ids`` are
    treated as "self" and skipped. Prunes stale reservations first."""
    with _lock:
        _prune_locked()
        # Reservations first — they cover the pre-"running" window.
        for claim_id, (owner, _ts) in _reservations.items():
            if claim_id in exclude_session_ids or owner.session_id in exclude_session_ids:
                continue
            return owner
        # Browser registry (Anthropic + OpenAI share ComputerUseSession).
        from Orchestrator.browser import session_manager as bsm
        for sid, s in list(bsm._sessions.items()):
            if sid in exclude_session_ids:
                continue
            if _browser_holds_local(s):
                return DisplayOwner("browser", getattr(s, "operator", "?"), sid)
        # Gemini registry: chat (in _operator_sessions) + one-shot task (uuid only).
        from Orchestrator.gemini_cu import session_manager as gsm
        chat_sids = set(gsm._operator_sessions.values())
        for sid, s in list(gsm._sessions.items()):
            if sid in exclude_session_ids:
                continue
            if _gemini_holds_local(s):
                kind = "gemini-chat" if sid in chat_sids else "gemini-task"
                return DisplayOwner(kind, getattr(s, "operator", "?"), sid)
        return None


def try_claim(requester_kind: str, operator: str, claim_id: str) -> Optional[DisplayOwner]:
    """ATOMIC check-and-reserve. Under the lock: if the local display is free
    (respecting same-kind self-exclusion), record a reservation for ``claim_id``
    and return ``None`` (claimed). Otherwise reserve nothing and return the
    current owner (denied). Pair every success with ``release_claim(claim_id)``
    (finally / on session destruction); prune + TTL are the leak backstop."""
    with _lock:
        exclude = set(_own_same_kind_claim_ids(requester_kind, operator))
        exclude.add(claim_id)
        owner = local_display_owner(exclude_session_ids=frozenset(exclude))
        if owner is not None:
            return owner
        if _before_record_hook is not None:
            _before_record_hook()  # test seam — see its definition above
        _reservations[claim_id] = (
            DisplayOwner(requester_kind, operator, claim_id), time.monotonic())
        return None


def release_claim(claim_id: str) -> None:
    """Drop a reservation. Idempotent; safe to call for an id that was never
    claimed (e.g. a remote request, or a claim that already TTL-expired)."""
    with _lock:
        _reservations.pop(claim_id, None)
