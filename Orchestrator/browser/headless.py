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
import contextlib

from Orchestrator.browser.config import (
    ANTHROPIC_BETA_HEADER, COMPUTER_TOOL_TYPE,
    DISPLAY_WIDTH, DISPLAY_HEIGHT, NATIVE_MODE, is_domain_allowed,
)
from Orchestrator.browser.dispatch import resolve_backend
from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop
# The Gemini/OpenAI CU drivers + the Gemini task-session factory. Imported at
# module level (not lazily) so tests can monkeypatch `headless.run_gemini_cu_loop`
# / `headless.run_openai_cu_loop` / `headless.gemini_create_task_session` the same
# way they already patch `headless.run_anthropic_cu_loop`. TRADE-OFF: this eager
# import couples the Google (genai) AND OpenAI SDKs to THIS runner's
# importability — a box missing either SDK can no longer import headless at all,
# so even Anthropic headless CU breaks (the chat path imports them lazily and
# stays resilient). Accepted for the monkeypatch seam; both SDKs ship on every
# box that runs any CU.
from Orchestrator.gemini_cu.agent_loop import run_gemini_cu_loop
from Orchestrator.gemini_cu.config import DEFAULT_CU_MODEL as GEMINI_CU_MODEL_DEFAULT
from Orchestrator.gemini_cu.session_manager import (
    create_task_session as gemini_create_task_session,
    destroy_task_session as gemini_destroy_task_session,
)
from Orchestrator.openai_cu.agent_loop import run_openai_cu_loop
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


# Backend -> the Orchestrator.config attribute name that gates it. STATIC (the
# key NAMES) so a CI test can assert this stays in lockstep with
# CU_MODEL_FILTERS at COMMIT time — a 4th CU family added to CU_MODEL_FILTERS
# without a matching entry here would otherwise only surface at RUNTIME. The
# key VALUES are read from this module's globals at CALL time (see run_cu_task),
# so a late-set / monkeypatched / wizard-pasted key still takes effect and the
# test monkeypatch seam (`headless.<KEY>`) is preserved. Keys mirror
# CU_MODEL_FILTERS (resolve_backend returns 'google', never 'gemini').
_BACKEND_KEY_NAMES = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google":    "GOOGLE_API_KEY",
    "openai":    "OPENAI_API_KEY",
}


async def _pump_generator(session, agen):
    """Bridge a YIELDING CU driver (Gemini/OpenAI) onto the PUSH-based
    session.event_queue that _drain_and_fold consumes.

    The Anthropic driver pushes its own events + None sentinel directly; the
    Gemini/OpenAI loops are async generators, so this forwards each yielded
    event to the queue and always terminates it with the None sentinel. It
    deliberately does NOT invent a `done` — the done-synthesis lives in the fold
    (a done-less iteration-capped run is a real case the fold handles).
    """
    try:
        async for event in agen:
            try:
                session.event_queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop if the consumer fell behind (mirrors driver emit)
    except asyncio.CancelledError:
        raise  # propagate E-stop / outer wait_for-timeout cancellation
    except Exception as e:
        try:
            session.event_queue.put_nowait({"type": "error", "data": str(e)})
        except asyncio.QueueFull:
            pass
    finally:
        # Sentinel MUST be delivered so the drain terminates; drain one item if
        # the queue is full (same guard the Anthropic driver uses).
        try:
            session.event_queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                session.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                session.event_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


async def _drain_and_fold(session, agent_task, screenshots) -> dict:
    """Drain session.event_queue to the None sentinel and fold events into the
    pinned task-result contract dict. Shared by all three backends.

    Two normalizations make it correct across drivers (the Anthropic-only
    original silently mis-recorded both for Gemini/OpenAI):

    * USAGE keys — the Anthropic driver emits {prompt_tokens, completion_tokens}
      (driver_anthropic.py:179-182); the Gemini/OpenAI loops emit
      session.total_tokens {input, output} (gemini_cu/agent_loop.py:641,
      openai_cu/agent_loop.py:572). Read BOTH, or non-Anthropic tokens record
      {0, 0}.
    * DONE on exhaustion — the Anthropic driver ALWAYS emits a final `done`
      (driver_anthropic.py:491); Gemini/OpenAI emit one only when the model
      stops calling tools, and on MAX_ITERATIONS exhaustion fall through to
      `yield usage` with NO done. When no done arrives on a clean exit,
      synthesize the result from accumulated `content` (mirrors the chat path's
      _gemini_cu_agent_loop) so an iteration-capped run reports success with
      real text instead of an empty failure.
    """
    result_text = ""
    content_accumulated = ""
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
                d = data or {}
                tokens["input"] += d.get("prompt_tokens", d.get("input", 0)) or 0
                tokens["output"] += d.get("completion_tokens", d.get("output", 0)) or 0
            elif etype == "content":
                # Anthropic streams str deltas; Gemini/OpenAI yield {"text": ...}.
                # Kept only as the done-less exhaustion fallback.
                if isinstance(data, dict):
                    content_accumulated += data.get("text", "") or ""
                elif isinstance(data, str):
                    content_accumulated += data
            elif etype == "done":
                done_seen = True
                result_text = (data or {}).get("content", "")
            elif etype == "error":
                # Anthropic error data is a str; Gemini/OpenAI wrap it {"message"}.
                if isinstance(data, dict):
                    error_msg = data.get("message") or str(data)
                else:
                    error_msg = data if isinstance(data, str) else str(data)
            elif etype == "cu_stopped":
                stopped_reason = (data or {}).get("reason", "stopped")
            # all other event types (thinking, content_start, cu_step,
            # cu_action, cu_bash_output, provenance, cu_safety, ...) are
            # streaming-UI concerns — ignored here

        try:
            await agent_task  # surface any unexpected driver exception
        except Exception as e:  # pragma: no cover — drivers catch internally
            error_msg = error_msg or str(e)
    finally:
        # Own the driver task: if THIS coroutine is cancelled (outer wait_for
        # timeout), don't rely on loop teardown to stop the driver — it would
        # keep clicking with no consumer. Await the cancellation so the task is
        # fully torn down (no "task was destroyed but it is pending" warning).
        if agent_task and not agent_task.done():
            agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await agent_task

    final_screenshot = screenshots[-1] if screenshots else None
    if error_msg:
        return {
            "success": False, "result_text": error_msg,
            "screenshots": screenshots, "final_screenshot": final_screenshot,
            "steps": session.current_step, "tokens": tokens,
        }
    if stopped_reason:
        # E-stop: a stopped task is FAILED, not a success.
        return {
            "success": False,
            "result_text": result_text or f"[Task stopped: {stopped_reason}]",
            "screenshots": screenshots, "final_screenshot": final_screenshot,
            "steps": session.current_step, "tokens": tokens,
        }
    # Clean exit. A `done` means the driver produced a final answer (Anthropic
    # always does; Gemini/OpenAI do when the model stops calling tools).
    # Otherwise it's an iteration-capped Gemini/OpenAI run — synthesize from
    # accumulated content so real work isn't lost as an empty failure.
    if done_seen:
        final_text = result_text
    elif content_accumulated:
        final_text = content_accumulated
    else:
        final_text = "(CU loop ended without a final response)"
    return {
        "success": done_seen or bool(content_accumulated),
        "result_text": final_text,
        "screenshots": screenshots,
        "final_screenshot": final_screenshot,
        "steps": session.current_step,
        "tokens": tokens,
    }


async def _run_gemini_cu_task(task_id: str, operator: str, prompt: str,
                              device_id: str, model: str,
                              system_prompt: str, url: str) -> dict:
    """Gemini CU headless dispatch.

    Uses the Gemini CU session type (NOT
    browser.session_manager.get_or_create_session), so it deliberately SKIPS the
    Anthropic single-local-display lock: the Gemini driver requires a
    GeminiCUSession (gemini_cu/agent_loop.py:398) built with a different signature,
    and passing the browser session would raise. The device->environment
    resolution is ported from stream_gemini_computer_use (chat_routes.py:4365-4382).

    ISOLATION: the headless task gets its OWN session via create_task_session, NOT
    the operator-cached CHAT session that get_or_create_session returns. Borrowing
    the chat session was wrong on three counts — a cache hit is returned as-is,
    never re-applying the requested device_id/environment (so a cached ANDROID
    chat turn would silently run a "blackbox" task against the phone); its
    non-empty conversation_history suppressed the driver's first-turn fossil
    retrieval (gemini_cu/agent_loop.py:417) AND contaminated the one-shot task
    with the user's prior chat. An owned session fixes all three (correct device
    at construction, fresh history, no contamination), needs no queue rebind (its
    queue is born on this worker loop), and its teardown drops ONLY itself —
    leaving the operator's chat session untouched.

    DISPLAY ARBITRATION: skipping the Anthropic single-display LOCK does not mean
    skipping conflict detection — for a desktop target we port the chat path's
    operator-scoped desktop-conflict guard (chat_routes.py:4383-4389) so a Gemini
    task cannot run while that operator's Anthropic/OpenAI CU task is driving the
    one physical display. (Matters once M1-T7 lets voice agents launch CU tasks.)
    """
    # ── Resolve device -> environment ──
    if device_id and device_id != "blackbox":
        from Orchestrator.device_registry import get_registry, DeviceProtocol
        device = get_registry().get_device(device_id)
        if device and device.protocol == DeviceProtocol.ADB:
            environment = "android"
            try:
                from Orchestrator.adb import get_adb_manager
                result = await get_adb_manager().ensure_connected(device_id)
                if not result.get("success"):
                    return _failure(f"Cannot connect to device: {result.get('error')}")
            except Exception as e:
                return _failure(f"ADB connection error: {e}")
        else:
            environment = "desktop"
    else:
        environment = "desktop"

    # ── Desktop-conflict guard: ATOMIC check-and-reserve (shared arbiter M1-T6) ──
    # For a DESKTOP target, atomically claim the local display (keyed by task_id)
    # so two concurrent CU launches — tasks.py runs each in its OWN OS thread —
    # can never both take it. Consults BOTH registries AND the reservation table.
    # Android targets never touch the local X server, so they neither claim nor
    # are blocked. Failure SHAPE unchanged (_failure dict — fire-and-forget).
    if environment == "desktop":
        from Orchestrator.browser.display_arbiter import try_claim
        owner = try_claim("gemini-task", operator, task_id)
        if owner is not None:
            return _failure(f"Cannot start Gemini CU — {owner.describe()}. Stop it first.")

    from Orchestrator.browser.display_arbiter import release_claim
    session = None
    try:
        # OWN, isolated session (see ISOLATION above) — never the operator-cached
        # chat session. Fresh: own queue, empty history, requested device/env.
        session = gemini_create_task_session(operator, device_id, environment)
        session.status = "starting"
        session.user_message = prompt

        screenshots: list = []
        agen = run_gemini_cu_loop(
            session, prompt, model or GEMINI_CU_MODEL_DEFAULT, system_prompt, url)
        session.agent_task = asyncio.create_task(_pump_generator(session, agen))
        print(f"[CU-HEADLESS] Gemini driver launched for task {task_id} "
              f"({operator}, env={environment})")
        return await _drain_and_fold(session, session.agent_task, screenshots)
    finally:
        # Release the display reservation (idempotent; a no-op for android, which
        # never claimed) and tear down ONLY this task's own session — never
        # _operator_sessions, so the operator's chat session survives. MUST be in
        # the finally so both fire on failure/cancellation too — a dropped task
        # leaves no running pump and no leaked reservation behind.
        release_claim(task_id)
        if session is not None:
            gemini_destroy_task_session(session)


async def run_cu_task(task_id: str, operator: str, prompt: str,
                      device_id: str = "blackbox", model: str = "",
                      system_prompt: str = None, url: str = None) -> dict:
    """Run one Computer Use task headlessly and return the result contract dict.

    Three-backend dispatch (T5): the backend is resolved from `model`, only
    THAT backend's API key is required, then the task is routed to its driver:

      * anthropic -> run_anthropic_cu_loop over the browser ComputerUseSession
        (unchanged; the driver pushes events to session.event_queue directly).
      * openai    -> run_openai_cu_loop over the SAME browser ComputerUseSession
        (documented compatible, openai_cu/agent_loop.py:300-301), bridged onto
        the queue by _pump_generator.
      * google    -> _run_gemini_cu_task: a GeminiCUSession via its own factory,
        which SKIPS the Anthropic single-display lock (see that helper).

    Every path folds through _drain_and_fold, which pins the result contract
    dict and normalizes the drivers' differing usage/done conventions.
    """
    # Resolve the backend FIRST, then require only THAT backend's key. Checking
    # ANTHROPIC_API_KEY before the backend was known killed a Gemini/OpenAI task
    # on a box with no Anthropic key (a fresh-customer box may carry only a
    # Google or OpenAI key) and, worse, blamed the wrong vendor.
    backend = resolve_backend(model)
    # Fail LOUD, never silent: an unmapped backend (a 4th CU_MODEL_FILTERS family
    # added without extending _BACKEND_KEY_NAMES) must NOT slip through ungated.
    if backend not in _BACKEND_KEY_NAMES:
        return _failure(f"No API-key gate configured for backend '{backend}'")
    # Read the key VALUE at CALL time from this module's globals (never a per-call
    # snapshot of import-time values) so a late-set / monkeypatched / wizard-pasted
    # key takes effect. The NAME lives in the module-level _BACKEND_KEY_NAMES
    # constant (see its comment) so a CI test can assert the map stays in lockstep
    # with CU_MODEL_FILTERS without freezing the values.
    key_name = _BACKEND_KEY_NAMES[backend]
    key_value = globals().get(key_name)
    if not key_value:
        return _failure(f"{key_name} not set — add it in the onboarding wizard")

    # ── Dispatch by backend (T5) ──
    # Gemini needs its OWN session type + factory and must NOT take the Anthropic
    # single-display lock, so it branches BEFORE any browser session is created.
    if backend == "google":
        return await _run_gemini_cu_task(
            task_id, operator, prompt, device_id, model, system_prompt, url)

    # anthropic + openai share the browser ComputerUseSession + its single-local-
    # display lock. Lazy chat_routes import (only the anthropic branch uses these
    # names; importing chat_routes at module scope would create an import cycle).
    from Orchestrator.routes.chat_routes import (
        COMPUTER_USE_SYSTEM_PROMPT, build_cu_context, _get_tools, _last_user_msg,
    )

    if backend == "openai" and device_id != "blackbox":
        return _failure("OpenAI CU supports the local desktop only for now")

    # ── Get/create persistent session ──
    # get_or_create_session is session lifecycle only (M1-T6): it no longer
    # arbitrates the display. It may still raise RuntimeError in tests that stub
    # it, so keep the guard — a fire-and-forget task prefers a FAILED dict to a
    # crash.
    try:
        session = get_or_create_session(operator, device_id=device_id)
    except RuntimeError as e:
        return _failure(str(e))

    from Orchestrator.browser.display_arbiter import release_claim, try_claim
    try:
        if session.status in ("running", "starting"):
            return _failure(
                f"Operator {operator} already has a running Computer Use task "
                f"(session {session.session_id[:8]}). Wait for it to finish."
            )

        session.touch()
        if session.device_id != device_id:
            print(f"[CU-HEADLESS] Switching device {session.device_id} -> {device_id} for {operator}")
            session.device_id = device_id

        # ── Claim the local display for THIS launch (M1-T6, per-launch key =
        #    task_id). Atomic across BOTH registries + reservations; released in
        #    the finally below. A remote (non-"blackbox") VNC target does not touch
        #    the local X server, so it neither claims nor is blocked. ──
        if session.device_id == "blackbox":
            owner = try_claim("browser", operator, task_id, session_id=session.session_id)
            if owner is not None:
                return _failure(f"Cannot start Computer Use — {owner.describe()}. Stop it first.")

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
        if backend == "openai":
            # One-shot isolation: a reused session may still carry a VALID
            # openai_previous_response_id from a prior CHAT turn — continuing from it
            # would splice this headless task into that conversation. Start fresh
            # (reset_task_state does not clear it; it is not part of task state).
            session.openai_previous_response_id = None

        screenshots = []

        if backend == "openai":
            # The OpenAI loop captures its own initial screenshot, builds its own
            # tool array, and carries history server-side via previous_response_id,
            # so the Anthropic-shaped preamble below is skipped. Bridge its yielded
            # events onto the queue the shared fold drains (URL navigation, if any,
            # was handled by ensure_browser above — mirrors the Anthropic path).
            #
            # Fossil context: unlike the Gemini driver (which retrieves internally),
            # the OpenAI loop does NO retrieval, so — exactly as the Anthropic path
            # and interactive OpenAI CU (chat_routes.py:4705-4709) do — build the
            # system prompt WITH fossils here, or headless OpenAI is the only CU
            # cohort running without memory.
            from Orchestrator.openai_cu.agent_loop import (
                _default_system_prompt as _openai_default_prompt,
            )
            oai_sys = system_prompt or _openai_default_prompt()
            try:
                cu_fossil_context, cu_provenance = build_cu_context(prompt, operator)
            except Exception as e:
                print(f"[CU-HEADLESS] build_cu_context failed (non-fatal): {e}")
                cu_fossil_context, cu_provenance = "", {}
            session.provenance = cu_provenance
            if cu_fossil_context:
                oai_sys += "\n\n" + cu_fossil_context
                print(f"[CU-HEADLESS] Injected {len(cu_fossil_context)} chars of fossil context (openai)")
            agen = run_openai_cu_loop(session, prompt, model or None, oai_sys, None)
            session.agent_task = asyncio.create_task(_pump_generator(session, agen))
            print(f"[CU-HEADLESS] OpenAI driver launched for task {task_id} ({operator})")
            return await _drain_and_fold(session, session.agent_task, screenshots)

        # ── Anthropic path (unchanged from the pre-T5 runner): build the initial
        #    screenshot / system prompt / history / tool array / headers, launch the
        #    driver (which pushes to session.event_queue itself), then fold. ──
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

        return await _drain_and_fold(session, session.agent_task, screenshots)
    finally:
        # Release THIS launch's display claim (per-launch key = task_id). Every
        # exit path passes through here (invariant 4); idempotent, so a no-op for
        # a remote target or a denied claim that never recorded task_id.
        release_claim(task_id)
