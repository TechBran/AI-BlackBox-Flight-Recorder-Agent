"""Headless Computer Use runner — the task/scheduler entry into the ONE CU loop.

`run_cu_task` composes what `stream_computer_use` (chat_routes) does, minus
SSE: it builds the same system prompt / tools / headers, launches
`run_anthropic_cu_loop` against the persistent per-operator session, then
drains `session.event_queue` until the `None` sentinel and folds the events
into the task-result contract dict pinned by
Orchestrator/tests/test_cu_golden_browser_run.py:

    {success, result_text, screenshots, final_screenshot, steps,
     tokens: {input, output}}

Consumers: tasks.process_browser_use (the /browser/run + use_computer +
scheduler path). This replaced the legacy browser/agent_loop.BrowserSession.
"""
import asyncio

from Orchestrator.browser.config import (
    ANTHROPIC_BETA_HEADER, COMPUTER_TOOL_TYPE,
    DISPLAY_WIDTH, DISPLAY_HEIGHT, NATIVE_MODE, is_domain_allowed,
)
from Orchestrator.browser.dispatch import resolve_backend
from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop
from Orchestrator.browser.screenshot import (
    capture_screenshot, capture_remote_screenshot,
    screenshot_to_base64, save_screenshot_to_uploads,
)
from Orchestrator.browser.session_manager import (
    get_or_create_session, strip_screenshots_from_history,
)
# All three CU API keys are imported from Orchestrator.config — the SINGLE module
# the CU drivers themselves read, so the runner's key gate can never disagree with
# the driver it gates: gemini_cu/agent_loop.py does
# `genai.Client(api_key=GOOGLE_API_KEY)` and openai_cu/agent_loop.py does
# `AsyncOpenAI(api_key=OPENAI_API_KEY)`, both importing from here; browser.config
# only RE-EXPORTED ANTHROPIC_API_KEY from this same place. Sourcing all three from
# one module keeps them visibly parallel, so a test author patches the one obvious
# attribute (`headless.<KEY>`) instead of silently patching the wrong module.
from Orchestrator.config import (
    CU_MODEL_DEFAULT, ANTHROPIC_API_KEY, GOOGLE_API_KEY, OPENAI_API_KEY,
)


def _failure(message: str, screenshots=None, steps: int = 0, tokens=None) -> dict:
    screenshots = screenshots or []
    return {
        "success": False,
        "result_text": message,
        "screenshots": screenshots,
        "final_screenshot": screenshots[-1] if screenshots else None,
        "steps": steps,
        "tokens": tokens or {"input": 0, "output": 0},
    }


async def run_cu_task(task_id: str, operator: str, prompt: str,
                      device_id: str = "blackbox", model: str = "",
                      system_prompt: str = None, url: str = None) -> dict:
    """Run one Computer Use task headlessly and return the result contract dict.

    Anthropic-only EXECUTION for now: the legacy task path (BrowserSession) was
    Anthropic-only, and the chat path covers Gemini CU. Non-anthropic backends
    fail fast with a clear message instead of silently running the wrong driver
    (T5 lands the real three-backend dispatch and removes that guard). The
    API-key gate below is ALREADY backend-aware ahead of T5 — it requires only
    the resolved backend's key — so a Google-only / OpenAI-only box reaches the
    fail-fast guard rather than dying on a missing Anthropic key.
    """
    # Lazy chat_routes imports — same precedent as driver_anthropic's helper
    # imports: chat_routes is the (deferred-cleanup) home of these helpers and
    # importing it at module level would create an import cycle.
    from Orchestrator.routes.chat_routes import (
        COMPUTER_USE_SYSTEM_PROMPT, build_cu_context, _get_tools, _last_user_msg,
    )

    # Resolve the backend FIRST, then require only THAT backend's key. Checking
    # ANTHROPIC_API_KEY before the backend was known killed a Gemini/OpenAI task
    # on a box with no Anthropic key (a fresh-customer box may carry only a
    # Google or OpenAI key) and, worse, blamed the wrong vendor. Each backend's
    # key is the SAME value its driver uses (see the import note above).
    backend = resolve_backend(model)
    # Built PER CALL (never hoisted to module scope): the key VALUES must be read
    # at call time so late-set / monkeypatched keys take effect. A module-level
    # dict would freeze the import-time values, silently breaking every
    # monkeypatch-based test in test_cu_headless_runner.py and defeating a
    # runtime key paste from the onboarding wizard.
    backend_keys = {
        "anthropic": ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        "google":    ("GOOGLE_API_KEY", GOOGLE_API_KEY),
        "openai":    ("OPENAI_API_KEY", OPENAI_API_KEY),
    }
    # Fail LOUD, never silent: an unmapped backend (e.g. a 4th CU_MODEL_FILTERS
    # family added post-T5 without extending this map) must NOT slip through with
    # no key gate at all — that is the exact silent-gap class M1-T4 exists to
    # close, and it would contradict CU_MODEL_FILTERS' "fail loud, not silent"
    # principle (config.py). Today the backend!=anthropic guard below backstops
    # it, but T5 removes that guard — so this stays the honest gate.
    if backend not in backend_keys:
        return _failure(f"No API-key gate configured for backend '{backend}'")
    key_name, key_value = backend_keys[backend]
    if not key_value:
        return _failure(f"{key_name} not set — add it in the onboarding wizard")

    # Non-Anthropic backends still fail fast here (T5 lands the real three-backend
    # session dispatch and removes this guard). Ordering matters: with a valid
    # backend key present, a Gemini/OpenAI task now reaches THIS message instead
    # of the (wrong) missing-Anthropic-key message it used to hit.
    if backend != "anthropic":
        return _failure(
            f"Headless CU tasks support Anthropic models only (got '{model}' "
            f"-> backend '{backend}'). Use the chat Computer Use provider for "
            f"Gemini/OpenAI computer use."
        )

    # ── Get/create persistent session ──
    # The session manager enforces the single-display constraint: it raises
    # RuntimeError if another operator has a running local task. The task
    # path is fire-and-forget, so a clear FAILED task beats a crash.
    try:
        session = get_or_create_session(operator, device_id=device_id)
    except RuntimeError as e:
        return _failure(str(e))

    if session.status in ("running", "starting"):
        return _failure(
            f"Operator {operator} already has a running Computer Use task "
            f"(session {session.session_id[:8]}). Wait for it to finish."
        )

    session.touch()
    if session.device_id != device_id:
        print(f"[CU-HEADLESS] Switching device {session.device_id} -> {device_id} for {operator}")
        session.device_id = device_id

    # ── Ensure display/device is ready (mirrors stream_computer_use) ──
    if session.device_id == "blackbox":
        if url and not is_domain_allowed(url):
            return _failure(f"Domain blocked by security policy: {url}")
        if not await session.ensure_browser(url or "about:blank"):
            return _failure("Failed to start browser session")
        if not NATIVE_MODE:
            from Orchestrator.browser.display import get_display
            display = get_display()
            if not display.health_check():
                print("[CU-HEADLESS] Display health check failed, attempting restart...")
                display.stop()
                display.start()
                if not display.health_check():
                    return _failure("Display health check failed after restart. Cannot proceed.")
                print("[CU-HEADLESS] Display restarted successfully")
        if not session.conversation_history:
            await asyncio.sleep(2)  # small wait for Chrome if freshly started
    else:
        # Remote device — verify VNC reachability
        from Orchestrator.device_registry import get_registry
        from Orchestrator.remote_desktop import VNCClient
        device = get_registry().get_device(session.device_id)
        if not device:
            return _failure(f"Device not found: {session.device_id}")
        vnc = VNCClient(device.tailscale_ip, device.vnc_port, device.metadata.get("vnc_password"))
        if not await vnc.is_reachable():
            return _failure(
                f"Cannot reach VNC on {device.name} ({device.tailscale_ip}:{device.vnc_port})"
            )

    # ── Reset task state for this run ──
    session.reset_task_state()
    # Rebind the event queue to THIS event loop (worker thread's asyncio.run);
    # see ComputerUseSession.fresh_event_queue for the cross-loop rationale.
    session.fresh_event_queue()
    # A headless task is one-shot: stale prompts queued by an earlier CHAT
    # turn must not auto-dequeue into this task's result.
    session.prompt_queue.clear()
    session.status = "starting"
    session.user_message = prompt

    # ── Capture initial screenshot (same naming as the chat path) ──
    screenshots = []
    initial_b64 = None
    try:
        if session.device_id != "blackbox":
            initial_png = await capture_remote_screenshot(session.device_id)
        else:
            initial_png = capture_screenshot()
        initial_b64 = screenshot_to_base64(initial_png)
        session.screenshot_count += 1
        screenshots.append(
            save_screenshot_to_uploads(initial_png, f"cu_{operator}", session.screenshot_count)
        )
    except Exception as e:
        print(f"[CU-HEADLESS] Initial screenshot failed: {e}")

    # ── Build system prompt (exactly as stream_computer_use) ──
    sys_prompt = system_prompt or COMPUTER_USE_SYSTEM_PROMPT.format(operator=operator)

    if session.device_id != "blackbox":
        from Orchestrator.device_registry import get_registry
        device = get_registry().get_device(session.device_id)
        if device:
            device_context = (
                f"\n\n## TARGET DEVICE\n"
                f"You are controlling a REMOTE device over VNC:\n"
                f"- Device: {device.name} ({device.id})\n"
                f"- Type: {device.device_type.value}\n"
                f"- IP: {device.tailscale_ip}:{device.vnc_port}\n"
            )
            if device.description:
                device_context += f"- Description: {device.description}\n"
            device_context += (
                f"\nScreenshots come from this remote device. "
                f"Actions (clicks, typing, scrolling) are sent to this remote device. "
                f"This is NOT the local BlackBox machine.\n"
            )
            sys_prompt += device_context

    # Degrade gracefully: a fossil-retrieval failure must not strand the
    # session in "starting" (the task path has no interactive retry).
    try:
        cu_fossil_context, cu_provenance = build_cu_context(prompt, operator)
    except Exception as e:
        print(f"[CU-HEADLESS] build_cu_context failed (non-fatal): {e}")
        cu_fossil_context, cu_provenance = "", {}
    session.provenance = cu_provenance
    if cu_fossil_context:
        sys_prompt += "\n\n" + cu_fossil_context
        print(f"[CU-HEADLESS] Injected {len(cu_fossil_context)} chars of fossil context")

    # ── Build history ──
    user_content = [{"type": "text", "text": prompt}]
    if initial_b64:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": initial_b64}
        })
    session.trim_history()
    history = strip_screenshots_from_history(list(session.conversation_history))
    history.append({"role": "user", "content": user_content})

    # ── Tool array + headers (exactly as stream_computer_use) ──
    try:
        vault_tools = _get_tools("anthropic", _last_user_msg(history), group="chat_cu")
    except Exception as e:
        print(f"[CU-HEADLESS] ToolVault injection failed (non-fatal): {e}")
        vault_tools = []
    tools = [
        {"type": COMPUTER_TOOL_TYPE, "name": "computer",
         "display_width_px": DISPLAY_WIDTH, "display_height_px": DISPLAY_HEIGHT},
        {"type": "bash_20250124", "name": "bash"},
        {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
    ] + vault_tools

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": ANTHROPIC_BETA_HEADER,
        "content-type": "application/json",
    }

    # ── Launch the driver and drain its event queue ──
    session.agent_task = asyncio.create_task(
        run_anthropic_cu_loop(
            session, history, sys_prompt, tools, headers,
            model or CU_MODEL_DEFAULT, operator, prompt,
        )
    )
    print(f"[CU-HEADLESS] Driver launched for task {task_id} ({operator})")

    result_text = ""
    error_msg = None
    stopped_reason = None
    done_seen = False
    tokens = {"input": 0, "output": 0}

    try:
        while True:
            event = await session.event_queue.get()
            if event is None:
                break  # sentinel — driver finished
            etype = event.get("type")
            data = event.get("data")
            if etype == "cu_screenshot":
                ss_url = (data or {}).get("url")
                if ss_url:
                    screenshots.append(ss_url)
            elif etype == "usage":
                tokens["input"] += (data or {}).get("prompt_tokens", 0)
                tokens["output"] += (data or {}).get("completion_tokens", 0)
            elif etype == "done":
                done_seen = True
                result_text = (data or {}).get("content", "")
            elif etype == "error":
                error_msg = data if isinstance(data, str) else str(data)
            elif etype == "cu_stopped":
                stopped_reason = (data or {}).get("reason", "stopped")
            # all other event types (thinking, content, cu_action, cu_step,
            # cu_bash_output, ...) are streaming-UI concerns — ignored here

        try:
            await session.agent_task  # surface any unexpected driver exception
        except Exception as e:  # pragma: no cover — driver catches internally
            error_msg = error_msg or str(e)
    finally:
        # Own the driver task: if THIS coroutine is cancelled (outer wait_for
        # timeout), don't rely on loop teardown to stop the driver — it would
        # keep clicking with no consumer if a caller ever drives us on a
        # long-lived loop instead of asyncio.run().
        if session.agent_task and not session.agent_task.done():
            session.agent_task.cancel()

    final_screenshot = screenshots[-1] if screenshots else None
    if error_msg:
        return {
            "success": False, "result_text": error_msg,
            "screenshots": screenshots, "final_screenshot": final_screenshot,
            "steps": session.current_step, "tokens": tokens,
        }
    if stopped_reason:
        # E-stop: the driver emits cu_stopped (and may still emit a done with
        # the stop notice) — a stopped task is a FAILED task, not a success.
        return {
            "success": False,
            "result_text": result_text or f"[Task stopped: {stopped_reason}]",
            "screenshots": screenshots, "final_screenshot": final_screenshot,
            "steps": session.current_step, "tokens": tokens,
        }
    return {
        "success": done_seen,
        "result_text": result_text if done_seen else "(CU loop ended without a final response)",
        "screenshots": screenshots,
        "final_screenshot": final_screenshot,
        "steps": session.current_step,
        "tokens": tokens,
    }
