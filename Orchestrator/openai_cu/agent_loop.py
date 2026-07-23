"""
OpenAI Computer Use Agent Loop — Responses API with the built-in `computer`
tool on gpt-5.5 (the dedicated, access-gated computer-use-preview model is
deprecated; legacy support retained behind COMPUTER_USE_TOOL_TYPE).

Loop shape (mirrors run_gemini_cu_loop's event contract):
    response = client.responses.create(
        model="gpt-5.5",
        tools=[{"type": "computer"}],   # bare — no display/environment fields
        input=[...],
        reasoning={"summary": "concise"},
        truncation="auto",
    )
    -> each computer_call carries an `actions` ARRAY (gpt-5.5 batches; the
       legacy preview model sent a single `action`) — execute the batch in
       order via session.actions (the SESSION-BOUND ActionExecutor that
       ensure_browser re-binds to this session's own virtual display,
       session_manager.py — anthropic-1280 coordinate space: coordinates
       follow the pixel space of the 1280x720 screenshots we send, the same
       space Anthropic uses)
    -> reply with ONE computer_call_output per call_id carrying a fresh
       {"type": "computer_screenshot", "image_url": "data:image/png;base64,...",
        "detail": "original"}
       plus previous_response_id=response.id for continuity.

Key differences from Anthropic/Gemini:
- previous_response_id carries the whole conversation server-side, so each
  follow-up request only contains the computer_call_output items (no local
  history replay; reasoning items are implicitly chained too).
- pending_safety_checks on a computer_call must be echoed back as
  acknowledged_safety_checks on its computer_call_output. Agent mode
  auto-acknowledges (no human in the loop), mirroring Gemini's behavior, and
  surfaces a cu_safety event so the UI can show what was acknowledged.
- Actions: click, double_click, scroll, type, keypress, wait, screenshot,
  drag, move.
- Multi-turn chat continuity persists on the session as
  session.openai_previous_response_id.
"""
import asyncio
import time
from datetime import datetime
from typing import AsyncGenerator, Optional

from openai import AsyncOpenAI

from Orchestrator.browser.screenshot import (
    resize_screenshot, screenshot_to_base64,
    save_screenshot_to_uploads,
)
from Orchestrator.config import OPENAI_API_KEY
from Orchestrator.openai_cu.config import (
    OPENAI_CU_MODEL_DEFAULT, OPENAI_CU_WIDTH, OPENAI_CU_HEIGHT,
    OPENAI_CU_ENVIRONMENT, COMPUTER_USE_TOOL_TYPE,
    MAX_ITERATIONS, MAX_WALL_CLOCK,
)


# OpenAI CUA key names (upper-cased for lookup) -> xdotool combo tokens.
# Modifiers lowercase, special keys in xdotool keysym form; single letters
# are lowercased; anything else passes through as-is (F1..F12 etc.).
_OPENAI_KEY_MAP = {
    "CTRL": "ctrl", "CONTROL": "ctrl",
    "SHIFT": "shift",
    "ALT": "alt", "OPTION": "alt",
    "CMD": "super", "SUPER": "super", "META": "super", "WIN": "super",
    "ENTER": "Return", "RETURN": "Return",
    "SPACE": "space",
    "BACKSPACE": "BackSpace",
    "TAB": "Tab",
    "ESC": "Escape", "ESCAPE": "Escape",
    "DELETE": "Delete", "DEL": "Delete",
    "INSERT": "Insert",
    "HOME": "Home", "END": "End",
    "PAGEUP": "Prior", "PAGEDOWN": "Next",
    "ARROWUP": "Up", "ARROWDOWN": "Down",
    "ARROWLEFT": "Left", "ARROWRIGHT": "Right",
    "UP": "Up", "DOWN": "Down", "LEFT": "Left", "RIGHT": "Right",
}


def _map_openai_keys(keys: list) -> str:
    """Translate OpenAI keypress key names to an xdotool combo ("ctrl+a")."""
    mapped = []
    for k in keys or []:
        k = str(k).strip()
        upper = k.upper()
        if upper in _OPENAI_KEY_MAP:
            mapped.append(_OPENAI_KEY_MAP[upper])
        elif len(k) == 1:
            mapped.append(k.lower())
        else:
            mapped.append(k)  # pass-through (F5, etc.)
    return "+".join(mapped)


def _scroll_direction_amount(scroll_x: int, scroll_y: int) -> tuple:
    """Map OpenAI's pixel-delta scroll to ActionExecutor (direction, ticks).

    Heuristic: ~40px per wheel notch (browsers report ~40-120px per tick, we
    take the conservative end), minimum 1 tick so a small delta still scrolls.
    Vertical wins when both axes are present; (0,0) degenerates to one tick
    down.
    """
    if scroll_y:
        direction = "down" if scroll_y > 0 else "up"
        dominant = scroll_y
    elif scroll_x:
        direction = "right" if scroll_x > 0 else "left"
        dominant = scroll_x
    else:
        return ("down", 1)
    return (direction, max(1, abs(int(dominant)) // 40 or 1))


def _pt(p) -> tuple:
    """Extract (x, y) from a drag-path point (dict or SDK object)."""
    if isinstance(p, dict):
        return int(p.get("x", 0)), int(p.get("y", 0))
    return int(getattr(p, "x", 0)), int(getattr(p, "y", 0))


def _action_params(action) -> dict:
    """Flatten an SDK action object into a JSON-safe params dict for events."""
    if hasattr(action, "model_dump"):
        try:
            d = action.model_dump()
        except Exception:
            d = dict(vars(action))
    elif isinstance(action, dict):
        d = dict(action)
    else:
        d = dict(vars(action))
    d.pop("type", None)
    return {k: v for k, v in d.items()
            if isinstance(v, (int, float, str, bool, list)) or v is None}


# OpenAI click button -> ActionExecutor action ("wheel" is the middle button)
_BUTTON_MAP = {
    "left": "left_click",
    "right": "right_click",
    "wheel": "middle_click",
    "middle": "middle_click",
}


async def _execute_openai_action(action, executor) -> dict:
    """Execute one OpenAI computer_call action via the CALLER-SUPPLIED executor.

    The caller passes session.actions — the session-bound ActionExecutor that
    ensure_browser re-binds to this session's own virtual display
    (session_manager.py:214-219), exactly like the Anthropic path. D4 bug fix
    (2026-07-23 design): constructing a fresh default ActionExecutor() here
    targeted the GLOBAL display, which no virtual CU session is on.
    Coordinates are pixels in the declared 1280x720 display — the
    anthropic-1280 coordinate space the session executor is bound with.
    Executor calls run in a thread (blocking xdotool/ydotool subprocesses).
    """
    a_type = getattr(action, "type", None)

    # No-input actions first — no executor needed.
    if a_type == "screenshot":
        # Fresh screenshot goes back with the computer_call_output anyway.
        return {"success": True, "message": "screenshot returned with call output"}
    if a_type == "wait":
        await asyncio.sleep(1)
        return {"success": True, "message": "waited 1s"}

    if a_type == "click":
        x, y = int(getattr(action, "x", 0)), int(getattr(action, "y", 0))
        button = getattr(action, "button", "left") or "left"
        exec_action = _BUTTON_MAP.get(button, "left_click")
        return await asyncio.to_thread(
            executor.execute, exec_action, coordinate=[x, y])

    if a_type == "double_click":
        x, y = int(getattr(action, "x", 0)), int(getattr(action, "y", 0))
        return await asyncio.to_thread(
            executor.execute, "double_click", coordinate=[x, y])

    if a_type == "scroll":
        x, y = int(getattr(action, "x", 0)), int(getattr(action, "y", 0))
        direction, amount = _scroll_direction_amount(
            getattr(action, "scroll_x", 0) or 0,
            getattr(action, "scroll_y", 0) or 0)
        return await asyncio.to_thread(
            executor.execute, "scroll", coordinate=[x, y],
            direction=direction, amount=amount)

    if a_type == "type":
        return await asyncio.to_thread(
            executor.execute, "type", text=getattr(action, "text", "") or "")

    if a_type == "keypress":
        combo = _map_openai_keys(getattr(action, "keys", None) or [])
        return await asyncio.to_thread(executor.execute, "key", text=combo)

    if a_type == "drag":
        path = getattr(action, "path", None) or []
        if len(path) < 2:
            return {"success": False, "error": "drag requires a path of >= 2 points"}
        sx, sy = _pt(path[0])
        ex, ey = _pt(path[-1])
        return await asyncio.to_thread(
            executor.execute, "left_click_drag",
            start_coordinate=[sx, sy], coordinate=[ex, ey])

    if a_type == "move":
        x, y = int(getattr(action, "x", 0)), int(getattr(action, "y", 0))
        return await asyncio.to_thread(
            executor.execute, "mouse_move", coordinate=[x, y])

    return {"success": False, "error": f"Unknown OpenAI CU action: {a_type}"}


async def _capture_cu_screenshot(session) -> bytes:
    """Native-res capture resized to the declared 1280x720 display.

    Routes through the session's capture seam so a per-session virtual display
    (when allocated) is captured; with display=None it is the legacy path.
    capture_screenshot already resizes to the CU 1280x720 in native mode, so
    the explicit resize is a no-op safeguard for other capture paths (also a
    no-op for a virtual 1280x720 display).
    to_thread: capture is a blocking subprocess + PIL resize — keep the event
    loop free during a CU step (Task-12 amendment pattern).
    """
    png = await asyncio.to_thread(session.capture_screenshot_bytes)
    return await asyncio.to_thread(
        resize_screenshot, png, OPENAI_CU_WIDTH, OPENAI_CU_HEIGHT)


def _save_screenshot(png_bytes: bytes, session) -> str:
    session.screenshot_count += 1
    return save_screenshot_to_uploads(
        png_bytes, f"cu_{session.operator}", session.screenshot_count)


def _default_system_prompt() -> str:
    """Browser-variant default (Gemini's, minus the 0-999 coordinate text —
    OpenAI uses pixel coordinates in the declared display)."""
    base = (
        "You are a Computer Use agent controlling a web browser. "
        "You can see the screen through screenshots and interact via click, "
        "type, scroll, and keyboard actions. Complete the user's task step by "
        "step, verifying each action's result in the next screenshot. "
        "If a page is loading, use the wait action before retrying."
    )
    return base + f"\n\nCurrent date/time: {datetime.now().isoformat(timespec='seconds')}"


def _collect_text(response) -> tuple:
    """Pull text from response.output.

    Returns (all_texts, message_texts): all_texts includes reasoning
    summaries (streamed as content events); message_texts is assistant
    message text only (the final answer for the done event).
    """
    all_texts, message_texts = [], []
    for item in getattr(response, "output", None) or []:
        i_type = getattr(item, "type", None)
        if i_type == "reasoning":
            for s in getattr(item, "summary", None) or []:
                t = getattr(s, "text", None)
                if t:
                    all_texts.append(t)
        elif i_type == "message":
            for c in getattr(item, "content", None) or []:
                t = getattr(c, "text", None)
                if t:
                    all_texts.append(t)
                    message_texts.append(t)
    return all_texts, message_texts


def _safety_check_dict(check) -> dict:
    return {
        "id": getattr(check, "id", None),
        "code": getattr(check, "code", None),
        "message": getattr(check, "message", None),
    }


def _is_stale_continuity_error(e: Exception) -> bool:
    """True when responses.create failed because previous_response_id is
    unusable: either poisoned (a prior turn died between create and the
    computer_call_output answer -> "No tool output found for computer
    call...") or expired/missing (404 — OpenAI retains stored responses
    ~30 days). Both are unrecoverable for that id; the only fix is a fresh
    conversation."""
    msg = str(e)
    if "No tool output found" in msg:
        return True
    if getattr(e, "status_code", None) == 404:
        return True
    return "previous response" in msg.lower() and "not found" in msg.lower()


async def run_openai_cu_loop(
    session,
    prompt: str,
    model: Optional[str] = None,
    system_prompt: Optional[str] = None,
    url: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Run the OpenAI CUA agent loop on the local desktop.

    `session` is a browser/session_manager ComputerUseSession (same one the
    Anthropic path uses). Yields the shared CU event vocabulary:
    cu_step, cu_action, cu_screenshot, cu_safety, cu_stopped, content,
    done, error, usage.
    """
    if not OPENAI_API_KEY:
        yield {"type": "error", "data": {"message": "OPENAI_API_KEY not set"}}
        return

    model = model or OPENAI_CU_MODEL_DEFAULT
    start_time = time.time()
    session.status = "running"
    session.current_step = 0

    # Multi-turn continuity across chat turns: previous_response_id chains
    # the server-side conversation. None on a fresh session.
    previous_response_id = getattr(session, "openai_previous_response_id", None)

    # C1 (poisoned continuity): mid-turn, session.openai_previous_response_id
    # points at a response whose computer_calls have NOT been answered yet.
    # If this turn exits abnormally (E-stop, wall-clock timeout, iteration
    # exhaustion, API error), continuing from that id 400s forever ("No tool
    # output found for computer call..."). So on every abnormal exit we
    # restore the id captured at TURN START: a clean prior turn remains
    # continuable, and on a fresh session it clears to None. Post-E-stop
    # turns deliberately lose this turn's continuity.
    turn_start_response_id = previous_response_id

    def _restore_turn_start_id():
        session.openai_previous_response_id = turn_start_response_id

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    if COMPUTER_USE_TOOL_TYPE == "computer":
        # Current contract (gpt-5.5): bare tool, no display/environment —
        # coordinates follow the pixel space of the screenshots we send.
        tools = [{"type": "computer"}]
    else:
        # Legacy deprecated computer-use-preview tool (access-gated orgs).
        tools = [{
            "type": COMPUTER_USE_TOOL_TYPE,
            "display_width": OPENAI_CU_WIDTH,
            "display_height": OPENAI_CU_HEIGHT,
            "environment": OPENAI_CU_ENVIRONMENT,
        }]

    # ── Optional URL navigation before the first screenshot ──
    # SESSION executor only (both callers pass url=None today — ensure_browser
    # handles navigation — but this branch previously built a bare
    # ActionExecutor() that was never even imported: a NameError landmine that,
    # import-fixed, would have typed onto the box-global display instead of the
    # session's (review find, 2026-07-23)).
    if url and getattr(session, "actions", None) is not None:
        executor = session.actions
        await asyncio.to_thread(executor.execute, "key", text="ctrl+l")
        await asyncio.sleep(0.2)
        await asyncio.to_thread(executor.execute, "type", text=url)
        await asyncio.sleep(0.1)
        await asyncio.to_thread(executor.execute, "key", text="Return")
        await asyncio.sleep(2)

    # ── Initial screenshot ──
    try:
        screenshot_bytes = await _capture_cu_screenshot(session)
    except Exception as e:
        yield {"type": "error", "data": {"message": f"Failed to capture initial screenshot: {e}"}}
        session.status = "error"
        return
    yield {"type": "cu_screenshot",
           "data": {"url": _save_screenshot(screenshot_bytes, session), "step": 0}}

    # ── First request input: instructions (developer role) + user turn ──
    # The Responses API CUA takes instructions via input messages; with
    # previous_response_id continuity the developer message from the first
    # turn persists server-side, so only inject it on a fresh conversation.
    input_items = []
    user_content = [{"type": "input_text", "text": prompt}]
    if not previous_response_id:
        input_items.append({
            "role": "developer",
            "content": [{"type": "input_text",
                         "text": system_prompt or _default_system_prompt()}],
        })
    elif system_prompt:
        # I1: on continuation turns the first turn's developer message
        # persists server-side, but the PER-TURN fossil context built by
        # stream_openai_computer_use for this prompt would otherwise never
        # reach the model. Append it as an extra input_text part on the user
        # message (user message items accept multiple input_text parts).
        user_content.append({"type": "input_text",
                             "text": f"[Context refresh]\n{system_prompt}"})
    user_content.append({
        "type": "input_image",
        "image_url": f"data:image/png;base64,{screenshot_to_base64(screenshot_bytes)}",
    })
    input_items.append({"role": "user", "content": user_content})

    # ── Agent loop ──
    for step in range(1, MAX_ITERATIONS + 1):
        if session.stop_requested:
            _restore_turn_start_id()  # abnormal exit (C1)
            yield {"type": "cu_stopped", "data": {"step": step}}
            break

        if time.time() - start_time > MAX_WALL_CLOCK:
            _restore_turn_start_id()  # abnormal exit (C1)
            yield {"type": "error", "data": {"message": "Wall clock timeout (30 min)"}}
            break

        session.current_step = step
        yield {"type": "cu_step", "data": {"step": step, "total": MAX_ITERATIONS}}

        kwargs = dict(
            model=model,
            tools=tools,
            input=input_items,
            reasoning={"summary": "concise"},
            truncation="auto",
        )
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id

        try:
            response = await client.responses.create(**kwargs)
        except Exception as e:
            response = None
            if kwargs.get("previous_response_id") and _is_stale_continuity_error(e):
                # Belt-and-braces (C1): the chained id is poisoned (a dead
                # turn left unanswered computer_calls) or expired (404).
                # Retry ONCE as a fresh conversation: developer message
                # re-included, current screenshot, no previous_response_id.
                print(f"[OPENAI CU] Stale previous_response_id "
                      f"({previous_response_id}); retrying fresh: {e}")
                previous_response_id = None
                turn_start_response_id = None  # the poisoned id is unrecoverable
                session.openai_previous_response_id = None
                input_items = [
                    {"role": "developer",
                     "content": [{"type": "input_text",
                                  "text": system_prompt or _default_system_prompt()}]},
                    {"role": "user",
                     "content": [
                         {"type": "input_text", "text": prompt},
                         {"type": "input_image",
                          "image_url": f"data:image/png;base64,{screenshot_to_base64(screenshot_bytes)}"},
                     ]},
                ]
                kwargs = dict(kwargs, input=input_items)
                kwargs.pop("previous_response_id", None)
                try:
                    response = await client.responses.create(**kwargs)
                except Exception as retry_err:
                    e = retry_err
            if response is None:
                print(f"[OPENAI CU] Step {step}: API ERROR: {e}")
                _restore_turn_start_id()  # abnormal exit (C1)
                msg = f"OpenAI API error: {e}"
                # model_not_found = the ORG lacks access to this model id
                # (e.g. the deprecated, tier-gated computer-use-preview, or a
                # gpt-5.5 rollout the account hasn't reached). Not a bad id.
                if "model_not_found" in str(e):
                    msg = (
                        f"Your OpenAI account does not have access to "
                        f"'{model}'. Check your account tier at "
                        f"platform.openai.com (Settings → Limits) or request "
                        f"access. Anthropic and Gemini CU models work with a "
                        f"standard API key; use one of those meanwhile."
                    )
                yield {"type": "error", "data": {"message": msg}}
                session.status = "error"
                return

        previous_response_id = response.id
        session.openai_previous_response_id = response.id

        # ── Token accounting ──
        usage = getattr(response, "usage", None)
        if usage:
            session.total_tokens["input"] += getattr(usage, "input_tokens", 0) or 0
            session.total_tokens["output"] += getattr(usage, "output_tokens", 0) or 0

        # ── Surface reasoning summaries / assistant text ──
        texts, message_texts = _collect_text(response)
        if texts:
            yield {"type": "content", "data": {"text": "\n".join(texts), "step": step}}

        computer_calls = [item for item in (getattr(response, "output", None) or [])
                          if getattr(item, "type", None) == "computer_call"]

        # ── No computer calls -> task complete ──
        if not computer_calls:
            session.final_response = ("\n".join(message_texts)
                                      or "\n".join(texts) or "Task completed.")
            yield {"type": "done", "data": {"content": session.final_response}}
            break

        # ── Execute calls, reply with computer_call_output items ──
        input_items = []
        for call in computer_calls:
            # gpt-5.5 batches multiple actions into one computer_call via the
            # `actions` array; the deprecated preview model sent a single
            # `action`. Execute the batch in order, then answer the call_id
            # with ONE screenshot output.
            batch = list(getattr(call, "actions", None) or [])
            single = getattr(call, "action", None)
            if not batch and single is not None:
                batch = [single]

            for action in batch:
                a_type = getattr(action, "type", "unknown")
                yield {"type": "cu_action",
                       "data": {"action": a_type, "params": _action_params(action),
                                "step": step}}

                result = await _execute_openai_action(action, session.actions)
                if not result.get("success", True):
                    print(f"[OPENAI CU] Action {a_type} failed: {result}")
                await asyncio.sleep(0.5)

            output_item = {
                "type": "computer_call_output",
                "call_id": getattr(call, "call_id", None),
            }

            pending = getattr(call, "pending_safety_checks", None) or []
            if pending:
                checks = [_safety_check_dict(c) for c in pending]
                # Agent mode: auto-acknowledge (no human in the loop),
                # mirroring Gemini's safety_acknowledgement behavior.
                output_item["acknowledged_safety_checks"] = checks
                yield {"type": "cu_safety", "data": {"checks": checks, "step": step}}

            # C2: computer_call_output.output MUST be a computer_screenshot —
            # the API has no text variant, so a text fallback would 400 and
            # kill the loop. On capture failure: retry once; if that fails,
            # reuse the last good screenshot (shape-valid — the model sees an
            # unchanged screen and will re-screenshot); if no screenshot has
            # ever succeeded, abort the turn cleanly.
            new_shot = None
            try:
                new_shot = await _capture_cu_screenshot(session)
            except Exception as e:
                print(f"[OPENAI CU] Post-action screenshot failed ({e}); retrying once")
                try:
                    new_shot = await _capture_cu_screenshot(session)
                except Exception as retry_err:
                    print(f"[OPENAI CU] Screenshot retry failed ({retry_err}); "
                          f"reusing last good screenshot")

            if new_shot is not None:
                screenshot_bytes = new_shot
                yield {"type": "cu_screenshot",
                       "data": {"url": _save_screenshot(screenshot_bytes, session),
                                "step": step}}
            elif screenshot_bytes is None:
                # Defensive: normally unreachable (the initial-capture guard
                # returns before the loop), but never send an invalid shape.
                _restore_turn_start_id()  # abnormal exit (C1)
                yield {"type": "error",
                       "data": {"message": "Screen capture is failing and no "
                                           "previous screenshot exists; aborting turn."}}
                session.status = "error"
                return

            output_item["output"] = {
                "type": "computer_screenshot",
                "image_url": f"data:image/png;base64,{screenshot_to_base64(screenshot_bytes)}",
                # Full-resolution interpretation — docs: "prefer detail:
                # 'original' on screenshot inputs" for computer use accuracy.
                "detail": "original",
            }
            input_items.append(output_item)
    else:
        # for-else: MAX_ITERATIONS exhausted without a clean done/stop break —
        # the last response's computer_calls were never answered (C1).
        _restore_turn_start_id()

    session.status = "complete"
    yield {"type": "usage", "data": session.total_tokens}
