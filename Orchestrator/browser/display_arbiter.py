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

Before M1-T6 no single guard saw all three: the browser lock inspected only
browser ``_sessions``; the Gemini guards inspected only the browser session. This
module is the ONE place that consults BOTH registries, so every guard can ask the
same question — "who owns the local display right now?" — and see all three.

This is ARBITRATION, not a lock (YAGNI): a point-in-time report. Callers REFUSE
on conflict — there is no queue, wait, or preemption. Each caller keeps its own
failure shape (RuntimeError / ``_failure`` dict / SSE error event); the arbiter
only reports.

Import direction (deliberate, acyclic): this module imports the two
session-manager modules LAZILY (function-local, inside ``local_display_owner``).
It is imported — also lazily — BY ``browser/session_manager`` (its lock),
``chat_routes`` (the Gemini chat guard) and ``browser/headless`` (the Gemini task
guard). Keeping our own imports function-local guarantees no module-load cycle can
form no matter who imports whom, and matches the pattern the repo already leans on
for exactly this reason (see ``browser/headless.py`` and ``browser/dispatch.py``).
"""
from dataclasses import dataclass
from typing import FrozenSet, Optional

# A session "holds" the local display only while actively driving it. We match
# the single status every pre-M1-T6 guard already keyed on ("running"), so this
# unification changes WHICH states block for NO one. "starting" is intentionally
# NOT included: widening it would change browser-vs-browser behavior during the
# brief pre-run window, and TOCTOU races between two near-simultaneous launches
# are out of scope (there is no lock — YAGNI).
_BUSY_STATUSES = ("running",)


@dataclass(frozen=True)
class DisplayOwner:
    """Who currently holds the local X display."""
    kind: str          # "browser" | "gemini-chat" | "gemini-task"
    operator: str
    session_id: str

    def describe(self) -> str:
        label = ("an Anthropic/OpenAI Computer Use task" if self.kind == "browser"
                 else "a Gemini Computer Use task")
        return (f"{label} is running on the local display "
                f"(operator {self.operator}, session {self.session_id[:8]})")


def _browser_holds_local(session) -> bool:
    return (getattr(session, "device_id", None) == "blackbox"
            and getattr(session, "status", None) in _BUSY_STATUSES)


def _gemini_holds_local(session) -> bool:
    return (getattr(session, "environment", None) == "desktop"
            and getattr(session, "status", None) in _BUSY_STATUSES)


def local_display_owner(
    exclude_session_ids: FrozenSet[str] = frozenset(),
) -> Optional[DisplayOwner]:
    """Return the session currently holding the LOCAL X display, consulting BOTH
    the browser and Gemini registries, or ``None`` if the display is free.

    ``exclude_session_ids`` names the session(s) the CALLER considers "itself"
    (the session it is about to resume/use); they are skipped so a caller never
    blocks on its own session. This is what preserves "resume my session": the
    Gemini chat guard excludes the operator's own cached chat session id.

    A point-in-time snapshot — see the module docstring: arbitration, not a lock.
    """
    # ── Browser registry (Anthropic + OpenAI share ComputerUseSession) ──
    from Orchestrator.browser import session_manager as bsm
    for sid, s in list(bsm._sessions.items()):
        if sid in exclude_session_ids:
            continue
        if _browser_holds_local(s):
            return DisplayOwner("browser", getattr(s, "operator", "?"), sid)

    # ── Gemini registry: chat sessions (in _operator_sessions) + one-shot task
    #    sessions (uuid-keyed in _sessions only) ──
    from Orchestrator.gemini_cu import session_manager as gsm
    chat_sids = set(gsm._operator_sessions.values())
    for sid, s in list(gsm._sessions.items()):
        if sid in exclude_session_ids:
            continue
        if _gemini_holds_local(s):
            kind = "gemini-chat" if sid in chat_sids else "gemini-task"
            return DisplayOwner(kind, getattr(s, "operator", "?"), sid)

    return None
