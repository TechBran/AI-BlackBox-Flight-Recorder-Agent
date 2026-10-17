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

Each entry is {tool, success, key[, incidental][, error]}: ``key`` fingerprints
the call's arguments (a retry of the same call vs other work), ``incidental``
marks tool discovery and the clock. The cron executor turns the log into a run
verdict with verdict().
"""

import contextvars
import hashlib
import json
from typing import Any, Dict, List, Optional

# A turn that calls more tools than this keeps its LAST entries: later calls
# decide whether an earlier failure was recovered.
MAX_ENTRIES = 100
# Enough to carry the error (the dead-grant message is ~200 chars), bounded so a
# tool that fails with a huge body cannot bloat result_data or cron history.
ERROR_CHARS = 300

# Calls whose success says nothing about whether the job's work got done: tool
# discovery (the toolvault meta-tool's read-only actions) and the clock. They
# stay in the log, but a turn whose only successes are these did not partly
# work — it failed.
_INCIDENTAL_TOOLS = frozenset({"get_current_time"})
_DISCOVERY_ACTIONS = frozenset({"search", "read", "list"})

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


def _fingerprint(tool_input: Any) -> str:
    """A short stable key for a call's arguments: same arguments, same key.

    Lets verdict() tell a plain retry of the SAME call from a call that did
    other work (chunk 2/3 of an SMS broadcast after chunk 1/3 failed).
    """
    try:
        canonical = json.dumps(tool_input, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = repr(tool_input)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


def _is_incidental(tool_name: str, tool_input: Any) -> bool:
    """True for discovery and clock calls (see _INCIDENTAL_TOOLS)."""
    if tool_name in _INCIDENTAL_TOOLS:
        return True
    if tool_name == "toolvault" and isinstance(tool_input, dict):
        return str(tool_input.get("action", "")).strip().lower() in _DISCOVERY_ACTIONS
    return False


def record(tool_name: str, result: Any, tool_input: Any = None) -> None:
    """Append one tool call's outcome (and its argument key) to the open log, if any."""
    log = _LOG.get()
    if log is None:
        return
    ok = bool(getattr(result, "success", False))
    name = str(tool_name)
    entry: Dict[str, Any] = {"tool": name, "success": ok, "key": _fingerprint(tool_input)}
    if _is_incidental(name, tool_input):
        entry["incidental"] = True
    if not ok:
        entry["error"] = str(getattr(result, "result", "") or "")[:ERROR_CHARS]
    log.append(entry)
    if len(log) > MAX_ENTRIES:
        del log[0]


def verdict(outcomes: Any) -> Optional[Dict[str, Any]]:
    """The run verdict for a turn's outcomes, or None when nothing failed.

    A failed call is recovered only by a LATER successful call of the same tool
    with the same arguments (a plain retry). A later success with other
    arguments may be the model correcting its own arguments, or may be other
    work (chunk 2/3 of a broadcast whose chunk 1/3 failed): the failure stays,
    marked ``retried`` so the summary can say so — except for discovery and
    clock calls, which have no side effects, so any later success of the same
    one recovers them. A tool that succeeded with nothing to report ("no
    events", "[]") is a success, so a quiet day never flags a run. Returns
    {"partial": bool, "failures": [entries], "total": n} where partial means at
    least one call that did real work (not discovery or the clock) succeeded
    alongside the unrecovered failures.
    """
    if not isinstance(outcomes, list):
        return None
    entries = [o for o in outcomes if isinstance(o, dict) and o.get("tool")]
    failures = []
    for i, o in enumerate(entries):
        if o.get("success"):
            continue
        later_ok = [later for later in entries[i + 1:]
                    if later.get("success") and later.get("tool") == o["tool"]]
        # A plain retry needs a known key on both sides: an entry without one
        # (unknown arguments) is never assumed to be the same call.
        key = o.get("key")
        if key is not None and any(later.get("key") == key for later in later_ok):
            continue
        if o.get("incidental") and later_ok:
            continue  # a corrected discovery/clock call: no side effects, nothing undone
        failures.append(dict(o, retried=True) if later_ok else o)
    if not failures:
        return None
    return {
        "partial": any(o.get("success") and not o.get("incidental") for o in entries),
        "failures": failures,
        "total": len(entries),
    }
