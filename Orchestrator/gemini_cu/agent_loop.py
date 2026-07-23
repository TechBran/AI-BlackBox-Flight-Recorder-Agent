"""
Gemini Computer Use Agent Loop.

Implements the screenshot -> Gemini API -> action -> screenshot cycle
for both browser and Android targets.
"""
import asyncio
import json
import time
import base64
import os
from datetime import datetime
from typing import Optional, Dict, Any, List, AsyncGenerator

from google import genai
from google.genai import types

from Orchestrator.browser.actions import ActionExecutor, COORD_SPACE_GEMINI

from Orchestrator.gemini_cu.config import (
    DEFAULT_CU_MODEL, MAX_ITERATIONS, MAX_WALL_CLOCK,
    BROWSER_ONLY_FUNCTIONS
)
from Orchestrator.gemini_cu.session_manager import GeminiCUSession
from Orchestrator.config import GOOGLE_API_KEY
from Orchestrator.agent_context import (
    append_fossils_to_system,
    resolve_operator,
    retrieve_for_agent,
)


# Predefined CU function names from Google's API
PREDEFINED_CU_FUNCTIONS = {
    "click_at", "hover_at", "type_text_at", "key_combination",
    "scroll_at", "scroll_document", "navigate", "open_web_browser",
    "go_back", "go_forward", "search", "wait_5_seconds", "drag_and_drop"
}

# Custom Android functions
CUSTOM_ANDROID_FUNCTIONS = {
    "open_app", "long_press_at", "go_home", "go_back_android",
    "scroll_down", "scroll_up"
}


def _map_gemini_keys(keys: str) -> str:
    """Map Gemini CU key names to xdotool format.
    Gemini sends: "Control+A", "Enter", "Meta+Shift+T", "Escape"
    xdotool expects: "ctrl+a", "Return", "super+shift+t", "Escape"
    """
    # Map modifier and special key names
    key_map = {
        "Control": "ctrl",
        "Meta": "super",
        "Alt": "alt",
        "Shift": "shift",
        "Enter": "Return",
        "Backspace": "BackSpace",
        "Delete": "Delete",
        "Escape": "Escape",
        "Tab": "Tab",
        "Space": "space",
        "ArrowUp": "Up",
        "ArrowDown": "Down",
        "ArrowLeft": "Left",
        "ArrowRight": "Right",
        "PageUp": "Prior",
        "PageDown": "Next",
        "Home": "Home",
        "End": "End",
    }
    parts = keys.split("+")
    mapped = []
    for p in parts:
        p_stripped = p.strip()
        if p_stripped in key_map:
            mapped.append(key_map[p_stripped])
        elif len(p_stripped) == 1:
            # Single character — lowercase for xdotool
            mapped.append(p_stripped.lower())
        else:
            # Pass through as-is (F1, F2, etc.)
            mapped.append(p_stripped)
    return "+".join(mapped)


def _build_tools(environment: str) -> list:
    """Build the tool configuration for Gemini CU."""
    tools = []
    if environment in ("browser", "desktop"):
        tools.append(types.Tool(
            computer_use=types.ComputerUse(
                environment=types.Environment.ENVIRONMENT_BROWSER
            )
        ))
    elif environment == "android":
        cu_tool = types.Tool(
            computer_use=types.ComputerUse(
                environment=types.Environment.ENVIRONMENT_BROWSER,
                excluded_predefined_functions=BROWSER_ONLY_FUNCTIONS
            )
        )
        tools.append(cu_tool)

        # Add custom Android functions
        android_fns = _get_android_function_declarations()
        tools.append(types.Tool(function_declarations=android_fns))
    return tools


def _get_android_function_declarations() -> list:
    """Build custom function declarations for Android CU."""
    return [
        types.FunctionDeclaration(
            name="open_app",
            description="Opens an Android app by package name.",
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {"type": "string",
                                 "description": "App package name or friendly name"}
                },
                "required": ["app_name"]
            }
        ),
        types.FunctionDeclaration(
            name="long_press_at",
            description="Long press at a coordinate on the Android screen.",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"}
                },
                "required": ["x", "y"]
            }
        ),
        types.FunctionDeclaration(
            name="go_home",
            description="Navigate to the Android home screen.",
            parameters={"type": "object", "properties": {}}
        ),
        types.FunctionDeclaration(
            name="go_back_android",
            description="Press the Android back button.",
            parameters={"type": "object", "properties": {}}
        ),
        types.FunctionDeclaration(
            name="scroll_down",
            description="Scroll down on the Android screen.",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"}
                }
            }
        ),
        types.FunctionDeclaration(
            name="scroll_up",
            description="Scroll up on the Android screen.",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate (0-999)"},
                    "y": {"type": "integer", "description": "Y coordinate (0-999)"}
                }
            }
        ),
    ]


async def _capture_screenshot(session: GeminiCUSession) -> bytes:
    """Capture a screenshot from the target device.
    For desktop/browser: the NATIVE frame, unresized. For android: ADB as-is.

    Deliberately NO resize (click-accuracy investigation 2026-07-11): the old
    stretch of the 1280x720 (16:9) display into 1440x900 (16:10) showed the
    model an 11%-vertically-distorted, LANCZOS-upscale-blurred frame on every
    grounding decision. Google's RECOMMENDED_RESOLUTION (1440x900) describes
    the ENVIRONMENT size in their Playwright example, not a stretch target;
    the model accepts any frame size and returns 0-999 coords normalized to
    what it SAW, so a full-frame native image round-trips through to_native
    exactly. Sharp + undistorted beats "documented size".
    """
    if session.environment in ("browser", "desktop"):
        from Orchestrator.browser.screenshot import capture_screenshot_display
        # Touch per capture — the honest activity heartbeat, so the display TTL
        # reaper never tears the Xvfb down under a long-running agent.
        if session.display is not None:
            session.display.touch()
        # to_thread: capture is a blocking subprocess — keep the event loop
        # free for other Orchestrator requests during a CU step. With a
        # per-session display allocated, native=False pins the capture to the
        # session's :N (the box-global NATIVE_MODE must not stomp it); with
        # display=None (native opt-in) the legacy env decision applies.
        return await asyncio.to_thread(
            capture_screenshot_display, session.display_number,
            native=False if session.display is not None else None)
    elif session.environment == "android":
        from Orchestrator.adb.commands import ADBCommands
        cmds = ADBCommands(session.device_id)
        await cmds.detect_screen_size()
        return await cmds.screenshot()
    else:
        raise ValueError(f"Unknown environment: {session.environment}")


async def _execute_predefined_action(session: GeminiCUSession,
                                      action_name: str, args: dict) -> dict:
    """Execute a predefined Gemini CU action."""
    if session.environment in ("browser", "desktop"):
        # All desktop input routes through the SESSION-BOUND executor: it
        # de-normalizes 0-999 coords against the session display's own
        # resolution and targets that display's :N. NEVER fall back to a bare
        # executor for a virtual session — if the session was destroyed
        # mid-step, a bare executor would inherit the box-global NATIVE_MODE
        # and land the rest of the step on the operator's REAL desktop
        # (review find, 2026-07-23). Only the explicit native opt-in may
        # construct a native executor.
        executor = getattr(session, "actions", None)
        if executor is None:
            if getattr(session, "native_mode", False):
                executor = ActionExecutor(coord_space=COORD_SPACE_GEMINI,
                                          native_mode=True)
            else:
                return {"success": False,
                        "message": "Session display is gone (destroyed mid-step)"}

        if action_name == "click_at":
            x, y = args.get("x", 0), args.get("y", 0)
            print(f"[GEMINI CU] click_at: ({x},{y}) [gemini 0-999] → native {executor.to_native(x, y)}")
            # to_thread on every executor.execute: the executor runs blocking
            # subprocesses (ydotool/xdotool) + sync sleep jitter — don't stall
            # the event loop for 0.5-2s per action.
            return await asyncio.to_thread(
                executor.execute, "left_click", coordinate=[x, y])
        elif action_name == "type_text_at":
            x, y = args.get("x", 0), args.get("y", 0)
            await asyncio.to_thread(
                executor.execute, "left_click", coordinate=[x, y])
            await asyncio.sleep(0.2)
            if args.get("clear_before_typing", False):
                await asyncio.to_thread(executor.execute, "key", text="ctrl+a")
                await asyncio.sleep(0.1)
            await asyncio.to_thread(
                executor.execute, "type", text=args.get("text", ""))
            return {"success": True, "message": f"Typed at ({x},{y})"}
        elif action_name == "hover_at":
            x, y = args.get("x", 0), args.get("y", 0)
            return await asyncio.to_thread(
                executor.execute, "mouse_move", coordinate=[x, y])
        elif action_name == "key_combination":
            keys = args.get("keys", "")
            # Gemini sends "Control+A"; the executor's key action expects
            # xdotool-style combos ("ctrl+a")
            keys = _map_gemini_keys(keys)
            return await asyncio.to_thread(executor.execute, "key", text=keys)
        elif action_name == "scroll_at":
            x, y = args.get("x", 0), args.get("y", 0)
            direction = args.get("direction", "down")
            magnitude = args.get("magnitude", 3)
            return await asyncio.to_thread(
                executor.execute,
                "scroll", coordinate=[x, y],
                direction=direction, amount=max(1, int(magnitude)))
        elif action_name == "scroll_document":
            direction = args.get("direction", "down")
            # Screen center in gemini 0-999 space
            return await asyncio.to_thread(
                executor.execute,
                "scroll", coordinate=[500, 500],
                direction=direction, amount=5)
        elif action_name == "navigate":
            url = args.get("url", "")
            await asyncio.to_thread(executor.execute, "key", text="ctrl+l")
            await asyncio.sleep(0.2)
            await asyncio.to_thread(executor.execute, "type", text=url)
            await asyncio.sleep(0.1)
            await asyncio.to_thread(executor.execute, "key", text="Return")
            return {"success": True, "action": "navigate", "url": url}
        elif action_name == "wait_5_seconds":
            await asyncio.sleep(5)
            return {"success": True, "action": "wait"}
        elif action_name == "drag_and_drop":
            sx, sy = args.get("x", 0), args.get("y", 0)
            dx, dy = args.get("destination_x", 0), args.get("destination_y", 0)
            return await asyncio.to_thread(
                executor.execute,
                "left_click_drag",
                start_coordinate=[sx, sy], coordinate=[dx, dy])
        elif action_name == "open_web_browser":
            # The model's native recovery action on a browserless desktop —
            # previously fell through to "Unknown browser action" (review find,
            # 2026-07-23). Spawn/focus Chrome ON this session's display.
            try:
                if getattr(session, "chrome", None) is None:
                    from Orchestrator.browser.chrome import ChromeInstance
                    session.chrome = ChromeInstance(operator=session.operator)
                if not session.chrome.is_running():
                    started = await asyncio.to_thread(
                        session.chrome.start, "about:blank", session.display)
                    if not started:
                        return {"success": False,
                                "error": "Browser failed to start"}
                    await asyncio.sleep(2)
                return {"success": True, "action": "open_web_browser"}
            except Exception as e:
                return {"success": False, "error": f"open_web_browser failed: {e}"}
        elif action_name == "go_back":
            return await asyncio.to_thread(executor.execute, "key", text="alt+Left")
        elif action_name == "go_forward":
            return await asyncio.to_thread(executor.execute, "key", text="alt+Right")
        elif action_name == "search":
            # Focus the address bar — typing there searches by default.
            await asyncio.to_thread(executor.execute, "key", text="ctrl+l")
            return {"success": True, "action": "search",
                    "message": "Address bar focused — type the search query"}
        else:
            return {"success": False, "error": f"Unknown browser action: {action_name}"}

    elif session.environment == "android":
        from Orchestrator.adb.commands import ADBCommands
        cmds = ADBCommands(session.device_id)
        await cmds.detect_screen_size()

        if action_name == "click_at":
            return await cmds.tap(args.get("x", 500), args.get("y", 500))
        elif action_name == "type_text_at":
            await cmds.tap(args.get("x", 500), args.get("y", 500))
            await asyncio.sleep(0.3)
            return await cmds.type_text(args.get("text", ""))
        elif action_name == "hover_at":
            return {"success": True, "action": "hover (no-op on Android)"}
        elif action_name == "key_combination":
            return await cmds.key_event(args.get("keys", ""))
        elif action_name == "scroll_at":
            # Browser convention (matches how the model was trained):
            # "down" = scroll page down = see more content below = finger swipes UP
            # "up"   = scroll page up   = see content above     = finger swipes DOWN
            direction = args.get("direction", "down")
            if direction == "down":
                return await cmds.scroll_down(args.get("x", 500), args.get("y", 500))
            elif direction == "up":
                return await cmds.scroll_up(args.get("x", 500), args.get("y", 500))
            elif direction == "left":
                return await cmds.swipe(
                    args.get("x", 500), args.get("y", 500),
                    max(0, args.get("x", 500) - 300), args.get("y", 500))
            elif direction == "right":
                return await cmds.swipe(
                    args.get("x", 500), args.get("y", 500),
                    min(999, args.get("x", 500) + 300), args.get("y", 500))
            else:
                return await cmds.scroll_down(args.get("x", 500), args.get("y", 500))
        elif action_name == "wait_5_seconds":
            await asyncio.sleep(5)
            return {"success": True, "action": "wait"}
        else:
            return {"success": False, "error": f"Unknown android action: {action_name}"}

    return {"success": False, "error": "Unknown environment"}


async def _execute_custom_function(session: GeminiCUSession,
                                    func_name: str, args: dict) -> dict:
    """Execute a custom Android function."""
    from Orchestrator.adb.commands import ADBCommands
    cmds = ADBCommands(session.device_id)
    await cmds.detect_screen_size()

    if func_name == "open_app":
        return await cmds.open_app(args.get("app_name", ""))
    elif func_name == "long_press_at":
        return await cmds.long_press(args.get("x", 500), args.get("y", 500))
    elif func_name == "go_home":
        return await cmds.go_home()
    elif func_name == "go_back_android":
        return await cmds.go_back()
    elif func_name == "scroll_down":
        return await cmds.scroll_down(args.get("x", 500), args.get("y", 500))
    elif func_name == "scroll_up":
        return await cmds.scroll_up(args.get("x", 500), args.get("y", 500))
    else:
        return {"success": False, "error": f"Unknown custom function: {func_name}"}


def _save_screenshot(png_bytes: bytes, session: GeminiCUSession) -> str:
    """Save screenshot to uploads directory and return URL path."""
    uploads_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "Portal", "uploads"
    )
    os.makedirs(uploads_dir, exist_ok=True)
    filename = f"gemini_cu_{session.operator}_{session.screenshot_count:03d}.png"
    filepath = os.path.join(uploads_dir, filename)
    with open(filepath, "wb") as f:
        f.write(png_bytes)
    session.screenshot_count += 1
    return f"/ui/uploads/{filename}"


def _default_system_prompt(session: GeminiCUSession) -> str:
    """Generate a default system prompt based on environment."""
    if session.environment == "android":
        base = (
            "You are a Computer Use agent controlling an Android device via touch input. "
            "You can see the screen through screenshots and interact via tap, swipe, "
            "type, and other actions.\n\n"
            "SCROLL CONVENTIONS (browser-style, mapped to touch for you):\n"
            "- scroll_at direction='down' = scroll page down (see more content below)\n"
            "- scroll_at direction='up' = scroll page up (see content above)\n"
            "- To open the Android app drawer from the home screen: use scroll_at with "
            "direction='down' starting from the bottom of the screen (y=800-900)\n\n"
            "TIPS:\n"
            "- Use the custom functions (open_app, go_home, go_back_android) for "
            "Android-specific actions.\n"
            "- If a scroll doesn't seem to work, try starting from a different y position.\n"
            "- Use the type tool instead of the onscreen keyboard when possible.\n"
            "- Complete the user's task step by step, taking a new screenshot after each action."
        )
    elif session.environment == "desktop":
        base = (
            "You are the AI BlackBox — a Computer Use agent controlling a Linux desktop. "
            "You can see the screen through screenshots and interact via click, type, "
            "scroll, and navigation actions.\n\n"
            "The desktop is a real Linux machine with a taskbar at the bottom "
            "(app launcher buttons on its left) and a right-click Applications "
            "menu on the desktop background (Terminal, File Manager, Web "
            "Browser). No browser is running until you open one — use the "
            "open_web_browser action, a taskbar launcher, or the right-click "
            "menu before navigating to any website.\n\n"
            "Your coordinates are normalized 0-999. (0,0) = top-left, (999,999) = bottom-right.\n\n"
            "Click in the CENTER of UI elements for best accuracy. "
            "Locate every target VISUALLY in the CURRENT screenshot before each click — "
            "never reuse coordinate numbers you computed on an earlier turn. "
            "If a click did not have the intended effect, do NOT repeat the same "
            "coordinates: re-examine the new screenshot, find the target again, and "
            "derive fresh coordinates (your previous estimate was likely off).\n\n"
            "For targets that are CELLS in a grid, board, table, or calendar: do NOT "
            "compute a cell's position by arithmetic offsets from another cell — grids "
            "may be flipped, mirrored, or unevenly sized. Visually locate EACH cell "
            "directly, and when the interface prints coordinate labels (file/rank "
            "letters, row/column headers), READ those labels to identify the target "
            "before clicking.\n\n"
            "Complete the user's task step by step, taking a screenshot after each action "
            "to verify the result. If a page is loading, use wait_5_seconds before retrying."
        )
    else:
        base = (
            "You are a Computer Use agent controlling a web browser. "
            "You can see the screen through screenshots and interact via click, type, "
            "scroll, and navigation actions. "
            "Locate every target VISUALLY in the CURRENT screenshot before each click — "
            "never reuse coordinate numbers from an earlier turn, and after a click "
            "that did not have the intended effect, derive FRESH coordinates from the "
            "new screenshot instead of repeating the same numbers. "
            "Complete the user's task step by step. "
            "If a page is loading, use wait_5_seconds before retrying."
        )
    return base + f"\n\nCurrent date/time: {datetime.now().isoformat(timespec='seconds')}"


async def run_gemini_cu_loop(
    session: GeminiCUSession,
    prompt: str,
    model_name: str = DEFAULT_CU_MODEL,
    system_prompt: Optional[str] = None,
    url: Optional[str] = None
) -> AsyncGenerator[dict, None]:
    """
    Run the Gemini Computer Use agent loop.
    Yields SSE-style event dicts as the loop progresses.
    """
    start_time = time.time()
    session.status = "running"
    session.current_step = 0

    # Per-session display (2026-07-23 coherence fix): bind/allocate BEFORE the
    # first capture so screenshots, clicks, and the live view all target this
    # session's own :N — never the operator's real desktop. One choke point
    # covers every launch path (chat stream, headless task, /run route).
    # start_url threads through so a url task gets Chrome ON this display.
    if not session.ensure_display(start_url=url):
        session.status = "error"
        yield {"type": "error", "data": {"message": "Virtual display allocation failed"}}
        return

    tools = _build_tools(session.environment)
    print(f"[GEMINI CU] run_gemini_cu_loop started: env={session.environment}, model={model_name}, tools={len(tools)}")

    # Plan Task 4: inject fossil retrieval at agent-loop start.
    # Only on the first turn of a session — subsequent turns reuse
    # session.conversation_history and re-injecting would balloon the prompt.
    is_first_turn = not getattr(session, "conversation_history", None)
    operator = resolve_operator(session.operator, "[GEMINI-CU]")
    fossil_text, provenance = retrieve_for_agent(
        user_text=prompt,
        operator=operator,
        log_prefix="[GEMINI-CU]",
    )
    # Always stash provenance on the session and yield it to the consumer
    # (REST `/run` ignores it; SSE `/stream` forwards it; chat-provider
    # consumers in chat_routes already read session.provenance).
    session.provenance = provenance
    yield {"type": "provenance", "data": provenance}

    base_system = system_prompt or _default_system_prompt(session)
    if is_first_turn:
        composed_system = append_fossils_to_system(base_system, fossil_text)
    else:
        composed_system = base_system

    config = types.GenerateContentConfig(
        tools=tools,
        system_instruction=composed_system,
    )

    client = genai.Client(api_key=GOOGLE_API_KEY)

    # Capture initial screenshot
    print(f"[GEMINI CU] Capturing initial screenshot...")
    try:
        screenshot_bytes = await _capture_screenshot(session)
        print(f"[GEMINI CU] Screenshot captured: {len(screenshot_bytes)} bytes")
    except Exception as e:
        print(f"[GEMINI CU] Screenshot FAILED: {e}")
        yield {"type": "error", "data": {"message": f"Failed to capture initial screenshot: {e}"}}
        session.status = "error"
        return

    screenshot_url = _save_screenshot(screenshot_bytes, session)
    print(f"[GEMINI CU] Screenshot saved: {screenshot_url}")
    yield {"type": "cu_screenshot", "data": {"url": screenshot_url, "step": 0}}

    # Build initial content — reuse conversation history for multi-turn
    if session.conversation_history:
        contents = list(session.conversation_history)
        contents.append(types.Content(role="user", parts=[
            types.Part.from_text(text=prompt),
            types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
        ]))
    else:
        contents = [
            types.Content(role="user", parts=[
                types.Part.from_text(text=prompt),
                types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
            ])
        ]

    # Navigate to URL if browser/desktop mode. Session executor ONLY — no
    # bare-executor fallback (it would land keys on the real desktop); with no
    # executor, skip: ensure_display's Chrome start already navigated there.
    if url and session.environment in ("browser", "desktop") \
            and getattr(session, "actions", None) is not None:
        executor = session.actions
        await asyncio.to_thread(executor.execute, "key", text="ctrl+l")
        await asyncio.sleep(0.2)
        await asyncio.to_thread(executor.execute, "type", text=url)
        await asyncio.sleep(0.1)
        await asyncio.to_thread(executor.execute, "key", text="Return")
        await asyncio.sleep(2)
        screenshot_bytes = await _capture_screenshot(session)
        screenshot_url = _save_screenshot(screenshot_bytes, session)
        yield {"type": "cu_screenshot", "data": {"url": screenshot_url, "step": 0}}
        contents[0] = types.Content(role="user", parts=[
            types.Part.from_text(text=prompt),
            types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
        ])

    # Agent Loop
    for step in range(1, MAX_ITERATIONS + 1):
        if session.stop_requested:
            yield {"type": "cu_stopped", "data": {"step": step}}
            break

        elapsed = time.time() - start_time
        if elapsed > MAX_WALL_CLOCK:
            yield {"type": "error", "data": {"message": "Wall clock timeout (30 min)"}}
            break

        session.current_step = step
        yield {"type": "cu_step", "data": {"step": step, "total": MAX_ITERATIONS}}

        # Call Gemini API
        print(f"[GEMINI CU] Step {step}: calling API with {len(contents)} content blocks, system_instruction={len(str(config.system_instruction or ''))} chars")
        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            print(f"[GEMINI CU] Step {step}: API response received, candidates={len(response.candidates) if response.candidates else 0}")
        except Exception as e:
            print(f"[GEMINI CU] Step {step}: API ERROR: {e}")
            yield {"type": "error", "data": {"message": f"Gemini API error: {e}"}}
            session.status = "error"
            return

        # Track tokens
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            session.total_tokens["input"] += (
                getattr(response.usage_metadata, "prompt_token_count", 0) or 0)
            session.total_tokens["output"] += (
                getattr(response.usage_metadata, "candidates_token_count", 0) or 0)

        if not response.candidates:
            yield {"type": "error", "data": {"message": "No response candidates from Gemini"}}
            break

        candidate = response.candidates[0]
        content = candidate.content
        contents.append(content)

        # Debug: log what the model returned
        part_types = []
        for p in content.parts:
            if hasattr(p, "function_call") and p.function_call:
                part_types.append(f"fn:{p.function_call.name}")
            elif hasattr(p, "text") and p.text:
                part_types.append(f"text({len(p.text)}ch)")
            else:
                part_types.append("other")
        print(f"[GEMINI CU] Step {step} response: {part_types}")

        # Process response parts
        function_calls = []
        text_parts = []
        for part in content.parts:
            if hasattr(part, "function_call") and part.function_call:
                function_calls.append(part.function_call)
            elif hasattr(part, "text") and part.text:
                text_parts.append(part.text)

        if text_parts:
            yield {"type": "content", "data": {"text": "\n".join(text_parts), "step": step}}

        # If no function calls, task is complete
        if not function_calls:
            session.final_response = "\n".join(text_parts) if text_parts else "Task completed."
            yield {"type": "done", "data": {"content": session.final_response}}
            break

        # Execute function calls
        function_response_parts = []
        for fc in function_calls:
            fname = fc.name
            fargs = dict(fc.args) if fc.args else {}
            print(f"[GEMINI CU] Action: {fname} | Raw args: {fargs}")

            yield {"type": "cu_action", "data": {"action": fname, "params": fargs, "step": step}}

            # Build response data dict — always include url (required by API)
            response_data = {"url": f"{session.environment}://{session.device_id}"}
            if "safety_decision" in fargs:
                # Auto-acknowledge safety decisions (agent mode, no human in loop)
                response_data["safety_acknowledgement"] = "true"
                yield {"type": "cu_safety", "data": {"decision": fargs["safety_decision"], "step": step}}

            # Handle built-in get_screenshot (just return current screen)
            if fname == "get_screenshot":
                try:
                    screenshot_bytes = await _capture_screenshot(session)
                    screenshot_url = _save_screenshot(screenshot_bytes, session)
                    yield {"type": "cu_screenshot", "data": {"url": screenshot_url, "step": step}}
                    response_data["result"] = "screenshot captured"
                    function_response_parts.append(
                        types.Part.from_function_response(
                            name=fname, response=response_data
                        )
                    )
                    function_response_parts.append(
                        types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
                    )
                except Exception as e:
                    response_data["error"] = str(e)
                    function_response_parts.append(
                        types.Part.from_function_response(
                            name=fname, response=response_data
                        )
                    )
                continue

            # Execute predefined or custom actions
            if fname in PREDEFINED_CU_FUNCTIONS:
                result = await _execute_predefined_action(session, fname, fargs)
            elif fname in CUSTOM_ANDROID_FUNCTIONS:
                result = await _execute_custom_function(session, fname, fargs)
            else:
                result = {"success": False, "error": f"Unknown function: {fname}"}

            await asyncio.sleep(0.5)

            # Capture new screenshot after action
            try:
                screenshot_bytes = await _capture_screenshot(session)
                screenshot_url = _save_screenshot(screenshot_bytes, session)
                yield {"type": "cu_screenshot", "data": {"url": screenshot_url, "step": step}}

                response_data["result"] = json.dumps(result)
                function_response_parts.append(
                    types.Part.from_function_response(
                        name=fname, response=response_data
                    )
                )
                function_response_parts.append(
                    types.Part.from_bytes(data=screenshot_bytes, mime_type="image/png")
                )
            except Exception as e:
                response_data["error"] = str(e)
                function_response_parts.append(
                    types.Part.from_function_response(
                        name=fname, response=response_data
                    )
                )

        contents.append(types.Content(role="user", parts=function_response_parts))

    session.status = "complete"
    session.conversation_history = contents
    yield {"type": "usage", "data": session.total_tokens}
