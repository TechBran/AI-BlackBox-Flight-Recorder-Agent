#!/usr/bin/env python3
"""
voice_ws_shared.py - Cross-route helpers shared by the three realtime voice
WebSocket bridges (OpenAI realtime_routes, xAI grok_live_routes, Google
gemini_live_routes).

P1b hardening (2026-07-11 voice-agent upgrade pass, workstream 6):
- Tool-dispatch exception -> error payload back to the model, so a raised
  executor NEVER dangles a function call id (previously: silent dead turn,
  the model waits forever on a tool result and the user hears dead air).
- save_voice_transcript(): transcript persistence via POST /chat/save
  (direct persistence + turns_threshold=1 auto-mint) instead of POST /chat
  (full LLM round-trip, ~400x more expensive — CLAUDE.md anti-pattern),
  write-ahead through the retry spool (Orchestrator/voice_transcript_spool.py)
  so a failed save is retried instead of lost (audit rank 51).

Keep this module LIGHT (json/aiohttp/starlette only): it is imported by all
three voice routes and, through them, the phone bridge chain.
"""

import asyncio
import json
import os
import time

import aiohttp
from typing import Callable, Dict, Optional, Set

from starlette.websockets import WebSocketState


# =============================================================================
# Session ownership across client re-attaches (audit rank 50)
# =============================================================================
# All three clients re-attach to the SAME session id after a network blip
# (Portal reconnectToExistingSession, Android VoiceClient's resume loop), within
# the reaper's grace window, and the route reuses the session object. Before
# this, the connect branch inherited the previous handler's teardown flags
# (intentional_disconnect=True: keepalive dead on tick one, every upstream close
# treated as deliberate, goAway ignored; a used-up reconnect budget), and an
# older handler whose finally ran AFTER the re-attach closed the newer handler's
# upstream and listener (O09-live-voice-providers-01).

def claim_session(session) -> int:
    """Stamp a new attach generation on `session` and return it.

    Called by each /ws/<provider> handler right after it takes the session.
    A handler, or a detached reconnect it spawned, whose generation is no
    longer current has been superseded by a newer attach of the same session
    id and must leave the upstream, listener, keepalive and conversation to
    the newer owner.
    """
    session.generation = getattr(session, "generation", 0) + 1
    return session.generation


def superseded(session, generation: int) -> bool:
    """True when a newer attach owns `session` than the one stamped `generation`."""
    return getattr(session, "generation", 0) != generation


def _caller_cancelled() -> bool:
    """True when the running task itself has a cancellation pending (3.11+)."""
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)
    return bool(task is not None and cancelling is not None and cancelling())


async def cancel_task(task, *, teardown: bool = False) -> None:
    """Cancel `task` if it is still running and wait for it to finish.

    The task's own error or cancellation never reaches the caller. A
    cancellation of the CALLER while it waits does, so a listener or a handler
    that is itself being cancelled stops; swallowed, a listener cancelled while
    it waited on a tool kept reading frames and held its own retirement up
    forever. ``teardown=True`` is for the teardown paths, which must run to the
    end (the transcript save comes after): they absorb a cancellation of the
    caller, as the routes' finally blocks always did. A task cannot await
    itself: called from inside `task` this only requests the cancellation.
    """
    if task is None:
        return
    if not task.done():
        task.cancel()
    if task is asyncio.current_task():
        return
    try:
        await task
    except asyncio.CancelledError:
        if _caller_cancelled():
            if not teardown:
                raise
            current = asyncio.current_task()
            if hasattr(current, "uncancel"):
                current.uncancel()
    except Exception:  # noqa: BLE001 — the task's own error ends with the task
        pass


def spawn_reconnect(session, coro) -> "asyncio.Task":
    """Run a reconnect coroutine detached and tracked on the session, so a
    re-attach or a teardown cancels it outright instead of trusting it to
    notice, after its backoff sleep, that the session moved on.

    EVERY spawned reconnect is tracked (``session.reconnect_tasks``), not just
    the latest: the listener's close handler and the keepalive can both spawn
    one in the same tick, before either has set is_reconnecting, and the one
    that ends up dialing is then the first, not the last.
    """
    task = asyncio.create_task(coro)
    tasks = getattr(session, "reconnect_tasks", None)
    if tasks is None:
        tasks = session.reconnect_tasks = set()
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def retire_upstream(session, ws_attr: str, *, teardown: bool = False) -> None:
    """Stop everything a previous connect left live on `session`.

    Cancels the pending reconnects, the keepalive loop, the upstream listener
    and the in-flight tool calls (their call ids belong to this upstream; no
    later socket can be answered for them), then closes the upstream socket
    named by `ws_attr` (``gemini_ws`` / ``grok_ws`` / ``openai_ws``).
    Listener before socket: left running it would observe the close and start
    a reconnect. A no-op on a fresh session.

    What gets retired is what is live on ENTRY: the cancellations below await,
    and a re-attach that connects meanwhile installs its own upstream,
    listener and keepalive, which are not this call's to stop. A field is
    cleared only while it still holds what was retired. ``teardown`` as for
    cancel_task.
    """
    current = asyncio.current_task()
    reconnects = [t for t in getattr(session, "reconnect_tasks", ()) if t is not current]
    keepalive = getattr(session, "keepalive_task", None)
    listener = getattr(session, "listener_task", None)
    tools = list((getattr(session, "tool_tasks", None) or {}).keys())
    ws = getattr(session, ws_attr, None)

    for task in reconnects:
        await cancel_task(task, teardown=teardown)
    await cancel_task(keepalive, teardown=teardown)
    if getattr(session, "keepalive_task", None) is keepalive:
        session.keepalive_task = None
    await cancel_task(listener, teardown=teardown)
    if getattr(session, "listener_task", None) is listener:
        session.listener_task = None
    for task in tools:
        await cancel_task(task, teardown=teardown)
    if ws is not None:
        await close_upstream(session, ws_attr, ws)


async def close_upstream(session, ws_attr: str, ws) -> None:
    """Close `ws`, and clear ``session.<ws_attr>`` only while it still holds `ws`.

    A reconnect or teardown closes the socket IT knows about: the field may
    already be a newer attach's upstream by the time the close returns.
    """
    try:
        await ws.close()
    except Exception:
        pass
    if getattr(session, ws_attr, None) is ws:
        setattr(session, ws_attr, None)


async def adopt_upstream(session, ws_attr: str, ws, generation: int, log_prefix: str) -> bool:
    """Install a freshly dialed upstream as ``session.<ws_attr>``, unless the
    attach `generation` that dialed it was superseded during the dial: then
    the socket is closed unused and False returned, and the newer attach's
    upstream stays in place (a detached reconnect's dial used to overwrite it).
    """
    if superseded(session, generation):
        print(f"{log_prefix} Session {getattr(session, 'session_id', '')} was re-attached "
              f"during the dial; closing the superseded upstream")
        try:
            await ws.close()
        except Exception:
            pass
        return False
    setattr(session, ws_attr, ws)
    return True


def reset_for_connect(session) -> None:
    """Fresh connection state for a session object that may be a re-attach."""
    session.intentional_disconnect = False
    session.is_reconnecting = False
    session.reconnect_count = 0
    session.status = "connecting"


async def end_portal_session(session, generation: int, keepalive_task, *, ws_attr: str,
                             save, log_prefix: str, flush=None) -> None:
    """Teardown when a /ws/<provider> handler's client WS ends.

    Only the CURRENT owner tears the session down, and only while it stays
    the owner. Teardown awaits (retiring the upstream, then a transcript save
    that mints inline and takes seconds) and the clients re-attach a second
    after a drop, so ownership is checked again after every await. Once a
    re-attach has claimed the session, its new client socket, upstream,
    buffers and status are the newer handler's, and so is the conversation,
    which it saves at its own teardown. The tail used to run unchecked after
    the save, leaving the re-attached session deaf (portal_ws None), marked
    disconnected and reapable while its upstream ran (O09-live-voice-providers-01).

    ``save(session)`` persists the transcript; ``flush(session)``, when given,
    moves a turn still in its buffers into the conversation first.
    """
    from Orchestrator.live_session_reaper import release_payload
    from Orchestrator.volume import now_utc_iso

    sid = getattr(session, "session_id", "")

    def _handed_over(stage: str) -> bool:
        if not superseded(session, generation):
            return False
        print(f"{log_prefix} Session {sid} was re-attached {stage}; "
              f"it stays with the newer attach")
        return True

    if superseded(session, generation):
        await cancel_task(keepalive_task, teardown=True)
        print(f"{log_prefix} Superseded handler for {sid} closed; "
              f"the session stays with the newer attach")
        return

    # Mark the session terminal FIRST so anything still running for it bails
    # instead of re-dialing and flipping status back to "connected", which
    # would resurrect a clientless session the reaper can never evict (it only
    # reaps status=="disconnected"). A re-attach resets it at its connect.
    session.intentional_disconnect = True

    # Cleanup: the pending reconnects, the keepalive, the CURRENT listener
    # (after a reconnect a different task than the one this handler spawned)
    # and the upstream. Stopped before the save, so no turn lands after it.
    await retire_upstream(session, ws_attr, teardown=True)
    await cancel_task(keepalive_task, teardown=True)
    if _handed_over("while its upstream was retired"):
        return

    if flush is not None:
        flush(session)
    if session.conversation:
        await save(session)
    if _handed_over("while its transcript was saved"):
        return

    session.portal_ws = None
    session.status = "disconnected"
    session.last_activity = now_utc_iso()  # start reaper grace clock (live_session_reaper)
    release_payload(session)               # free audio/transcript buffers; conversation already saved
    print(f"{log_prefix} Session {sid} cleaned up")


# =============================================================================
# Voice tool calls run off the listener (audit rank 51)
# =============================================================================
# A tool call used to run INLINE in the upstream listener, so nothing was read
# from the provider while it ran: control_phone's 300 s budget, a CU or CLI
# task, a phone call or a slow web_fetch held the socket unread, and under the
# old 60 s stale timer the reconnect cancelled the tool mid-call so the function
# call never got an answer (O08-stt-realtime-02, O09-live-voice-providers-05).
# Each call now runs as a task tracked on its session.

# Ceiling for ONE voice tool call, above control_phone's default 300 s blocking
# budget so a tool that finishes by its own rules is never cut short; a wedged
# one is answered with an error instead of holding the session's tool lane
# forever. Read at call time.
VOICE_TOOL_DEADLINE_S = 330.0


def _tool_lock(session) -> asyncio.Lock:
    lock = getattr(session, "tool_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        session.tool_lock = lock
    return lock


def dispatch_voice_tool(session, execute, deliver, fail, *, label: str, call_id: str = "",
                        deadline: Optional[float] = None) -> "asyncio.Task":
    """Run one tool call as a task tracked on `session`, off the listener.

    ``execute()`` produces the result text and runs under the deadline
    (VOICE_TOOL_DEADLINE_S unless given); ``deliver(result)`` answers the model
    and the client; ``fail(exc)`` answers with an error payload when execute
    raises or overruns, so no call id is ever left dangling. A session's calls
    run one at a time in arrival order (asyncio.Lock is FIFO), which keeps the
    old inline ordering of their results and of the response.create after each.
    The task sits in ``session.tool_tasks`` (keyed to `call_id`) until it
    ends; teardown cancels it (cancel_voice_tools / retire_upstream).
    """
    limit = VOICE_TOOL_DEADLINE_S if deadline is None else deadline

    async def _run():
        async with _tool_lock(session):
            try:
                result = await asyncio.wait_for(execute(), timeout=limit)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                print(f"[VOICE-TOOLS] {label} gave no result within {limit:.0f}s — "
                      f"answering the model with an error")
                await _quietly(fail, TimeoutError(f"no result within {limit:.0f} seconds"))
                return
            except Exception as exc:
                print(f"[VOICE-TOOLS] {label} failed: {type(exc).__name__}: {exc}")
                await _quietly(fail, exc)
                return
            await _quietly(deliver, result)

    task = asyncio.create_task(_run(), name=f"voice-tool {label}")
    tasks = session.tool_tasks
    tasks[task] = call_id
    task.add_done_callback(lambda t: tasks.pop(t, None))
    return task


async def _quietly(fn, arg) -> None:
    try:
        await fn(arg)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[VOICE-TOOLS] could not deliver a tool answer: {type(e).__name__}: {e}")


async def cancel_voice_tools(session, call_ids=None, *, wait: bool = True,
                             teardown: bool = False) -> None:
    """Cancel the in-flight tool calls of `session` (only those whose provider
    call id is in `call_ids`, when given) and, with `wait`, wait for them.

    The upstream listener passes ``wait=False``: it must go on reading, and a
    tool's unwind (stopping a CU or phone task) is not something to block
    the provider's socket on. ``teardown`` as for cancel_task.
    """
    for task, call_id in list((getattr(session, "tool_tasks", None) or {}).items()):
        if call_ids is None or call_id in call_ids:
            if wait:
                await cancel_task(task, teardown=teardown)
            elif not task.done():
                task.cancel()


def voice_debug_enabled() -> bool:
    """VOICE_DEBUG=1|true|yes|on opts into full session-config dumps in the logs.

    Off by default: a realtime session.update carries the operator's injected
    snapshot memory, custom role and every tool schema (audit
    O09-live-voice-providers-12). Read per call, so no import-time freeze.
    """
    return os.getenv("VOICE_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def tool_error_text(name: str, exc: BaseException) -> str:
    """Error payload returned to the model in place of a tool result."""
    return (
        f"Tool '{name}' failed: {type(exc).__name__}: {exc}. "
        "Briefly tell the user the action failed, then continue the conversation."
    )


async def _safe_portal_send(websocket, data: dict) -> bool:
    """Best-effort JSON send to the client WS; never raises.

    Local copy of the routes' _safe_ws_send — importing it from a route module
    here would create an import cycle (routes import this module).
    """
    try:
        if websocket and hasattr(websocket, "application_state") \
                and websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_json(data)
            return True
    except Exception:
        pass
    return False


async def send_openai_style_tool_error(upstream_ws, portal_ws,
                                       event: Dict, exc: BaseException) -> bool:
    """Answer a dangling OpenAI-schema function call with an error payload.

    Used by BOTH the OpenAI Realtime and xAI Grok routes (identical wire
    format): sends conversation.item.create/function_call_output for the
    event's call_id followed by response.create, and notifies the portal
    with an error-flagged tool_result. No-op (returns False) when the event
    is not a function-call event. Never raises.
    """
    if not event or event.get("type") != "response.function_call_arguments.done":
        return False

    call_id = event.get("call_id", "")
    name = event.get("name", "")
    result = tool_error_text(name, exc)

    sent = False
    if upstream_ws:
        try:
            await upstream_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                },
            }))
            await upstream_ws.send(json.dumps({"type": "response.create"}))
            sent = True
        except Exception as send_err:
            print(f"[VOICE-SHARED] Could not deliver tool error for '{name}' "
                  f"(call_id={call_id}): {send_err}")

    await _safe_portal_send(portal_ws, {
        "type": "tool_result",
        "data": {"name": name, "result_length": len(result), "error": True},
    })
    return sent


async def send_gemini_tool_error(gemini_ws, portal_ws, event: Dict,
                                 exc: BaseException,
                                 answered_ids: Optional[Set[str]] = None) -> bool:
    """Answer dangling Gemini functionCalls with error functionResponses.

    A Gemini toolCall event carries a LIST of functionCalls; a raise mid-loop
    dangles every not-yet-answered id. `answered_ids` (recorded by the dispatch
    loop) prevents double-answering ids that already got a real response.
    No-op (returns False) when the event has no toolCall or nothing is
    unanswered. Never raises.
    """
    tool_call = (event or {}).get("toolCall")
    if not tool_call:
        return False

    answered = answered_ids or set()
    pending = [fc for fc in tool_call.get("functionCalls", [])
               if fc.get("id", "") not in answered]
    if not pending:
        return False

    responses = [{
        "id": fc.get("id", ""),
        "name": fc.get("name", ""),
        "response": {"result": tool_error_text(fc.get("name", ""), exc)},
    } for fc in pending]

    sent = False
    if gemini_ws:
        try:
            await gemini_ws.send(json.dumps(
                {"toolResponse": {"functionResponses": responses}}))
            sent = True
        except Exception as send_err:
            print(f"[VOICE-SHARED] Could not deliver Gemini tool error "
                  f"({len(responses)} call(s)): {send_err}")

    for fc in pending:
        await _safe_portal_send(portal_ws, {
            "type": "tool_result",
            "data": {"name": fc.get("name", ""), "result_length": 0, "error": True},
        })
    return sent


def _chat_save_url() -> str:
    from Orchestrator.config import orchestrator_url
    return orchestrator_url("/chat/save")


# Transcript ids whose POST is in flight in this process. The spool retry skips
# them, so a save and its retry can never both reach /chat/save.
_SAVES_IN_FLIGHT: Set[str] = set()


async def _post_chat_save(body: dict, log_prefix: str) -> Optional[int]:
    """POST /chat/save; returns the HTTP status, or None when no response came."""
    try:
        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(
                _chat_save_url(),
                json=body,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    reply = await resp.json()
                    print(f"{log_prefix} Transcript saved via /chat/save "
                          f"(minted={reply.get('minted')}, snap_id={reply.get('snap_id')})")
                else:
                    error = await resp.text()
                    print(f"{log_prefix} /chat/save failed: {resp.status} - {error[:200]}")
                return resp.status
    except Exception as e:
        print(f"{log_prefix} /chat/save error: {e}")
        return None


def conversation_releaser(session) -> Callable[[], None]:
    """A one-shot that drops exactly the turns a save is persisting.

    Built when the transcript is (the turns in ``session.conversation`` right
    now); calling it deletes those and keeps any turn appended while the save
    ran. Idempotent. Pass it to save_voice_transcript as ``on_safe`` and call
    it again on a True result.
    """
    included = len(session.conversation)
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            del session.conversation[:included]
    return release


async def save_voice_transcript(operator: str, user_message: str,
                                session_summary: str, model_label: str,
                                log_prefix: str, session_id: str = "",
                                on_safe: Optional[Callable[[], None]] = None) -> bool:
    """Persist a voice-session transcript via POST /chat/save.

    Direct persistence: the backend's turns_threshold=1 auto-mint fires
    perform_mint() inline (embedding included), so the snapshot is searchable
    when the 200 returns. NEVER POST /chat here (full LLM round-trip) and
    NEVER call /mint afterward (duplicate snapshot).

    Write-ahead: the transcript is written to the retry spool
    (voice_transcript_spool, keyed by `session_id`) BEFORE the POST and taken
    out after the 200. A failed, timed-out or cancelled save, or a crash in the
    middle of one, leaves it spooled, and the live-session reaper retries it on
    its sweeps and right after startup. A failed save used to be "kept for a
    later retry" that nothing ran (O09-live-voice-providers-13).

    Returns True when the transcript is safe — in the ledger, or durably queued
    for it — and False only when the save failed AND the spool could not be
    written. Callers clear their conversation buffer only on True: a False
    keeps it in memory for the next teardown path or the reaper. ``on_safe``
    runs the moment the transcript is safe, which is before the POST once the
    spool is written: a save cancelled mid-POST (the phone bridge's 10 s
    timeout) has still handed the turns to the spool, and a caller that kept
    them would post them again on its next save.
    """
    from Orchestrator import voice_transcript_spool as spool

    record = spool.new_record(session_id=session_id, operator=operator,
                              user_message=user_message, session_summary=session_summary,
                              model_label=model_label, log_prefix=log_prefix)
    def _safe() -> None:
        if on_safe is not None:
            try:
                on_safe()
            except Exception as e:
                print(f"{log_prefix} on_safe callback failed: {e}")

    spooled = False
    try:
        await asyncio.to_thread(spool.write, record)
        spooled = True
    except Exception as e:
        print(f"{log_prefix} could not spool the transcript for retry: {e}")
    if spooled:
        _safe()

    _SAVES_IN_FLIGHT.add(record["transcript_id"])
    try:
        status = await _post_chat_save(spool.chat_save_body(record), log_prefix)
    finally:
        _SAVES_IN_FLIGHT.discard(record["transcript_id"])

    if status == 200:
        if spooled:
            try:
                await asyncio.to_thread(spool.remove, record)
            except Exception as e:
                # The retry finds the marker in the ledger and drops it then.
                print(f"{log_prefix} saved, but the retry copy could not be removed: {e}")
        else:
            _safe()
        return True
    if spooled:
        print(f"{log_prefix} transcript kept in the retry spool "
              f"({spool.record_path(record).name}); the live-session reaper retries it")
        return True
    return False


async def retry_spooled_transcripts(now: Optional[float] = None, *,
                                    ignore_schedule: bool = False) -> Dict[str, int]:
    """Retry every spooled transcript that is due (live-session reaper, startup).

    Per entry: skip it while its own save is still in flight in this process;
    drop it when the ledger already holds its marker (a save whose 200 never
    arrived may have been minted all the same); otherwise POST it again. A 200
    removes it; a 4xx other than 408/429 is a refusal no retry can fix and
    parks it (kept on disk as *.rejected.json); anything else backs off
    (1 min, doubling to 1 h). `ignore_schedule` (the startup pass) retries
    entries whose backoff has not run out: the save they waited on died with
    the old process.
    """
    from Orchestrator import voice_transcript_spool as spool

    now = time.time() if now is None else now
    counts = {"saved": 0, "already_saved": 0, "failed": 0, "rejected": 0}
    records = await asyncio.to_thread(lambda: list(spool.pending()))
    for record in records:
        transcript_id = record["transcript_id"]
        if transcript_id in _SAVES_IN_FLIGHT:
            continue
        if not ignore_schedule and float(record.get("next_attempt_at") or 0) > now:
            continue
        prefix = record.get("log_prefix") or "[VOICE-SPOOL]"
        _SAVES_IN_FLIGHT.add(transcript_id)
        try:
            if await asyncio.to_thread(spool.ledger_has, record):
                await asyncio.to_thread(spool.remove, record)
                counts["already_saved"] += 1
                print(f"{prefix} spooled transcript of {record.get('session_id')} is already "
                      f"in the ledger; dropped the retry copy")
                continue
            status = await _post_chat_save(spool.chat_save_body(record), prefix)
            if status == 200:
                await asyncio.to_thread(spool.remove, record)
                counts["saved"] += 1
            elif status is not None and 400 <= status < 500 and status not in (408, 429):
                parked = await asyncio.to_thread(spool.park_rejected, record)
                counts["rejected"] += 1
                print(f"{prefix} /chat/save refused the spooled transcript of "
                      f"{record.get('session_id')} ({status}); parked as {parked.name}, "
                      f"not retried")
            else:
                spool.mark_failed(record, now)
                await asyncio.to_thread(spool.write, record)
                counts["failed"] += 1
        except Exception as e:
            print(f"[VOICE-SPOOL] retry of transcript {transcript_id} failed: {e}")
        finally:
            _SAVES_IN_FLIGHT.discard(transcript_id)
    return counts
