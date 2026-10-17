"""Per-turn tool outcome log: which tool calls in this chat turn failed.

A /chat turn can "complete" — the model writes a polite reply — while the tool
calls it made all failed (audit W15: a monthly calendar job logged success
although both its list_events calls returned invalid_grant). Whoever owns the
whole turn needs the tool outcomes, not just the reply text.

Request-scoped via a ContextVar, the same pattern as chat_routes'
_ORIGIN_DEVICE_ID: the /chat worker (tasks.process_chat_task) opens a fresh log
with begin() before provider dispatch, BlackBoxToolExecutor.execute — the one
choke point every chat-loop tool call goes through — appends each outcome with
record(), and the worker publishes the list as result_data["tool_outcomes"].
The call_* loops run each tool with asyncio.run on the worker thread, and the
new event loop's task inherits the thread's context, so it sees the same list.
With no log open (streaming routes, voice bridges, MCP) record() is a no-op.

The cron executor turns the log into a run verdict with verdict().
"""

import contextvars
from typing import Any, Dict, List, Optional

# A turn that calls more tools than this keeps its LAST entries: later calls
# decide whether an earlier failure was recovered.
MAX_ENTRIES = 100
# Enough to carry the error (the dead-grant message is ~200 chars), bounded so a
# tool that fails with a huge body cannot bloat result_data or cron history.
ERROR_CHARS = 300

_LOG: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = contextvars.ContextVar(
    "tool_outcomes", default=None)


def begin() -> List[Dict[str, Any]]:
    """Open a fresh log for this turn and return it.

    Unconditional, like stamp_origin_device: chat tasks run on reused pool
    threads, so a log left open by the previous task must never leak into this one.
    """
    log: List[Dict[str, Any]] = []
    _LOG.set(log)
    return log


def end() -> None:
    """Close the log (later tool calls on this thread record nothing)."""
    _LOG.set(None)


def record(tool_name: str, result: Any) -> None:
    """Append one tool call's outcome to the open log, if any."""
    log = _LOG.get()
    if log is None:
        return
    ok = bool(getattr(result, "success", False))
    entry: Dict[str, Any] = {"tool": str(tool_name), "success": ok}
    if not ok:
        entry["error"] = str(getattr(result, "result", "") or "")[:ERROR_CHARS]
    log.append(entry)
    if len(log) > MAX_ENTRIES:
        del log[0]


def verdict(outcomes: Any) -> Optional[Dict[str, Any]]:
    """The run verdict for a turn's outcomes, or None when nothing failed.

    A failed call counts only if no LATER call of the same tool succeeded (the
    model fixing its own arguments and retrying is not a failure). A tool that
    succeeded with nothing to report ("no events", "[]") is a success, so a
    quiet day never flags a run. Returns
    {"partial": bool, "failures": [entries], "total": n} where partial means at
    least one call succeeded alongside the unrecovered failures.
    """
    if not isinstance(outcomes, list):
        return None
    entries = [o for o in outcomes if isinstance(o, dict) and o.get("tool")]
    failures = []
    for i, o in enumerate(entries):
        if o.get("success"):
            continue
        if any(later.get("success") and later.get("tool") == o["tool"] for later in entries[i + 1:]):
            continue
        failures.append(o)
    if not failures:
        return None
    return {
        "partial": any(o.get("success") for o in entries),
        "failures": failures,
        "total": len(entries),
    }
