"""Anthropic Computer Use driver.

`run_anthropic_cu_loop` is the Anthropic CU background agent loop,
extracted verbatim from Orchestrator/routes/chat_routes.py
(formerly `_cu_agent_loop`).  It pushes events to session.event_queue
instead of yielding, so the loop survives client disconnection.
"""
import asyncio
import json


async def run_anthropic_cu_loop(session, history, system_prompt, tools, headers, model, operator, user_text):
    """Background agent loop for Computer Use.  Pushes events to
    session.event_queue instead of yielding, so the loop survives
    client disconnection.
    """
    import httpx
    from Orchestrator.browser.config import MAX_ITERATIONS
    from Orchestrator.browser.screenshot import (
        capture_remote_screenshot,
        screenshot_to_base64, save_screenshot_to_uploads
    )
    from Orchestrator.browser.actions import execute_remote_action
    # Names that previously resolved from chat_routes module scope.
    # Imported lazily (at call time) so this module never imports
    # chat_routes at module level (no import cycle).
    from Orchestrator.config import VOL_PATH
    from Orchestrator.fossils import format_snapshot_for_delivery, hybrid_retrieve
    from Orchestrator.models import TaskType
    from Orchestrator.image_providers import IMAGE_TOOL_PROVIDERS
    from Orchestrator.tasks import create_task, generate_prompt_slug
    from Orchestrator.utils.async_helpers import run_blocking
    from Orchestrator.volume import read_text_safe
    from Orchestrator.web_tools import perform_web_fetch
    from Orchestrator.routes.chat_routes import (
        _cu_safe_params, _cu_save_to_blackbox,
        execute_bash_command, execute_text_editor,
        execute_get_media, execute_list_media, execute_search_media,
    )

    async def _capture_ss():
        """Capture screenshot from local or remote device based on session.device_id."""
        if session.device_id != "blackbox":
            return await capture_remote_screenshot(session.device_id)
        return session.capture_screenshot_bytes()

    async def emit(evt):
        try:
            session.event_queue.put_nowait(evt)
        except asyncio.QueueFull:
            pass  # drop event if queue full (consumer gone for too long)

    result_text = ""
    try:
        session.status = "running"
        cu_max_steps = MAX_ITERATIONS
        session.total_steps = cu_max_steps
        consecutive_screenshot_failures = 0
        import time as _time
        _cu_wall_clock_start = _time.monotonic()
        _CU_MAX_WALL_CLOCK = 1800  # 30 minutes max

        for iteration in range(cu_max_steps):
            step = iteration + 1
            session.current_step = step

            # E-Stop check
            if session.stop_requested:
                await emit({"type": "cu_stopped", "data": {"step": step, "reason": "User requested stop"}})
                session.status = "stopped"
                result_text = f"[Task stopped by user at step {step}]"
                break

            # Wall-clock timeout check
            if _time.monotonic() - _cu_wall_clock_start > _CU_MAX_WALL_CLOCK:
                await emit({"type": "error", "data": f"Task exceeded {_CU_MAX_WALL_CLOCK}s wall-clock limit"})
                session.status = "error"
                break
            await emit({"type": "cu_step", "data": {"step": step, "total": cu_max_steps}})
            print(f"[CU-BG] Step {step}/{cu_max_steps} for {operator}")

            # ── 413 guard: re-budget screenshots EVERY iteration ──
            # The request re-sends the whole history, so without this a long run
            # accumulates one PNG per step and dies on Anthropic's per-request
            # caps ("request_too_large" — Brandon's "too much to do" error).
            # Stripping only at task-save (the old behavior) was the bug.
            from Orchestrator.browser.session_manager import budget_screenshots_in_history
            history = budget_screenshots_in_history(history)

            # ── Stream API call ──
            payload = {
                "model": model,
                "max_tokens": 128000,
                "system": system_prompt,
                "tools": tools,
                "messages": history,
                "stream": True
            }

            thinking_buffer = ""
            thinking_signature = ""
            content_buffer = ""
            current_block_type = None
            stop_reason = None
            assistant_content_blocks = []
            tool_uses_this_turn = []
            current_tool_use = None

            max_retries = 3
            api_success = False
            for attempt in range(max_retries):
                try:
                    async with httpx.AsyncClient(timeout=300) as client:
                        async with client.stream("POST", "https://api.anthropic.com/v1/messages", headers=headers, json=payload) as response:
                            if response.status_code == 200:
                                async for line in response.aiter_lines():
                                    if not line or not line.startswith("data: "):
                                        continue
                                    data_str = line[6:]
                                    try:
                                        event = json.loads(data_str)
                                        event_type = event.get("type")

                                        if event_type == "content_block_start":
                                            block = event.get("content_block", {})
                                            current_block_type = block.get("type")
                                            if current_block_type == "thinking":
                                                # Per-BLOCK reset: a response can carry
                                                # multiple thinking blocks; without this the
                                                # second block's history entry would embed the
                                                # first's text under the second's signature —
                                                # a signature-verification 400 on replay.
                                                thinking_buffer = ""
                                                thinking_signature = ""
                                                await emit({"type": "thinking_start", "data": ""})
                                            elif current_block_type == "redacted_thinking":
                                                # Arrives complete in the start event; must be
                                                # round-tripped verbatim like signed thinking.
                                                assistant_content_blocks.append({
                                                    "type": "redacted_thinking",
                                                    "data": block.get("data", ""),
                                                })
                                            elif current_block_type == "text":
                                                # Per-block reset (same class as the thinking
                                                # dup): a 2nd text block in one response would
                                                # otherwise re-embed the 1st's text.
                                                content_buffer = ""
                                                await emit({"type": "content_start", "data": ""})
                                            elif current_block_type == "tool_use":
                                                current_tool_use = {
                                                    "type": "tool_use",
                                                    "id": block.get("id", ""),
                                                    "name": block.get("name", ""),
                                                    "input": {},
                                                    "_input_json": ""
                                                }

                                        elif event_type == "content_block_delta":
                                            delta = event.get("delta", {})
                                            delta_type = delta.get("type")
                                            if delta_type == "thinking_delta":
                                                text = delta.get("thinking", "")
                                                if text:
                                                    thinking_buffer += text
                                                    await emit({"type": "thinking", "data": text})
                                            elif delta_type == "text_delta":
                                                text = delta.get("text", "")
                                                if text:
                                                    content_buffer += text
                                                    await emit({"type": "content", "data": text})
                                            elif delta_type == "input_json_delta":
                                                if current_tool_use is not None:
                                                    current_tool_use["_input_json"] += delta.get("partial_json", "")
                                            elif delta_type == "signature_delta":
                                                # The thinking block's replay credential. Models
                                                # with adaptive/interleaved thinking (sonnet-5 era)
                                                # emit thinking even when the request sets no
                                                # thinking param — replaying the block WITHOUT its
                                                # signature 400s the very next request
                                                # ("thinking.signature: Field required"; caught
                                                # live by the M0 click battery, 2026-07-23).
                                                thinking_signature += delta.get("signature", "")

                                        elif event_type == "content_block_stop":
                                            if current_block_type == "thinking":
                                                await emit({"type": "thinking_end", "data": ""})
                                                block_out = {
                                                    "type": "thinking", "thinking": thinking_buffer
                                                }
                                                if thinking_signature:
                                                    block_out["signature"] = thinking_signature
                                                    assistant_content_blocks.append(block_out)
                                                # An unsigned thinking block is NOT replayable —
                                                # the API rejects it outright, so drop it from
                                                # history (the thinking text still streamed to
                                                # the UI above).
                                            elif current_block_type == "text":
                                                assistant_content_blocks.append({
                                                    "type": "text", "text": content_buffer
                                                })
                                            elif current_block_type == "tool_use" and current_tool_use:
                                                try:
                                                    raw = current_tool_use["_input_json"]
                                                    current_tool_use["input"] = json.loads(raw) if raw else {}
                                                except json.JSONDecodeError:
                                                    current_tool_use["input"] = {}
                                                del current_tool_use["_input_json"]
                                                tool_uses_this_turn.append(current_tool_use)
                                                assistant_content_blocks.append(current_tool_use)
                                                current_tool_use = None
                                            current_block_type = None

                                        elif event_type == "message_delta":
                                            delta = event.get("delta", {})
                                            stop_reason = delta.get("stop_reason")
                                            usage = event.get("usage", {})
                                            if usage:
                                                session.total_tokens["input"] += usage.get("input_tokens", 0)
                                                session.total_tokens["output"] += usage.get("output_tokens", 0)
                                                session.usage["prompt_tokens"] += usage.get("input_tokens", 0)
                                                session.usage["completion_tokens"] += usage.get("output_tokens", 0)
                                                await emit({"type": "usage", "data": {
                                                    "prompt_tokens": usage.get("input_tokens", 0),
                                                    "completion_tokens": usage.get("output_tokens", 0)
                                                }})

                                    except json.JSONDecodeError:
                                        print(f"[CU-BG] Malformed SSE line (step {step}): {data_str[:200]}")
                                        continue
                                api_success = True
                                break  # Success — exit retry loop
                            elif response.status_code == 429:
                                retry_after = int(response.headers.get("retry-after", 30))
                                wait = min(retry_after, 60)
                                print(f"[CU-BG] Rate limited (429), waiting {wait}s (attempt {attempt+1}/{max_retries})")
                                await emit({"type": "cu_step", "data": {"step": step, "total": cu_max_steps,
                                            "message": f"Rate limited, retrying in {wait}s..."}})
                                await asyncio.sleep(wait)
                                continue
                            elif response.status_code >= 500:
                                wait = min(2 ** attempt, 30)
                                print(f"[CU-BG] Server error {response.status_code}, retry in {wait}s (attempt {attempt+1})")
                                await asyncio.sleep(wait)
                                continue
                            else:
                                # 4xx client error — don't retry
                                error_text = await response.aread()
                                error_msg = error_text.decode()[:500]
                                print(f"[CU-BG] API error {response.status_code}: {error_msg}")
                                await emit({"type": "error", "data": f"API error {response.status_code}: {error_msg}"})
                                session.status = "error"
                                session.error_message = error_msg
                                return
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    if attempt < max_retries - 1:
                        wait = min(2 ** attempt, 30)
                        print(f"[CU-BG] Connection error: {e}, retry in {wait}s (attempt {attempt+1})")
                        await asyncio.sleep(wait)
                    else:
                        await emit({"type": "error", "data": f"API connection failed after {max_retries} attempts: {e}"})
                        session.status = "error"
                        return
                except Exception as api_err:
                    await emit({"type": "error", "data": f"API call failed: {api_err}"})
                    session.status = "error"
                    session.error_message = str(api_err)
                    return
            else:
                if not api_success:
                    await emit({"type": "error", "data": "API request failed after all retries"})
                    session.status = "error"
                    return

            # ── Add assistant response to history ──
            history.append({"role": "assistant", "content": assistant_content_blocks})

            # ── If done (no tool use) ──
            print(f"[CU-BG] Step {step} stop_reason: {stop_reason}")
            if stop_reason != "tool_use":
                result_text = content_buffer
                break

            # ── Execute tool calls ──
            tool_results = []
            for tu in tool_uses_this_turn:
                tool_name = tu.get("name", "")
                tool_id = tu.get("id", "")
                tool_input = tu.get("input", {})

                # ─── Anthropic system tools ───
                if tool_name == "computer":
                    action = tool_input.get("action", "")
                    action_params = {k: v for k, v in tool_input.items() if k != "action"}
                    print(f"[CU-BG]   computer: {action} | {_cu_safe_params(tool_input)}")
                    session.cu_log.append({"type": "action", "action": action, "step": step})
                    await emit({"type": "cu_action", "data": {"action": action, "params": _cu_safe_params(tool_input), "step": step}})

                    if session.device_id != "blackbox":
                        await execute_remote_action(session.device_id, action, **action_params)
                    else:
                        session.actions.execute(action, **action_params)

                    if action not in ("screenshot", "wait", "zoom"):
                        await asyncio.sleep(0.5)

                    try:
                        png_bytes = await _capture_ss()
                        if action == "zoom":
                            region = tool_input.get("region")
                            if region and len(region) == 4:
                                import io
                                from PIL import Image
                                img = Image.open(io.BytesIO(png_bytes))
                                x0, y0, x1, y1 = [int(v) for v in region]
                                cropped = img.crop((x0, y0, x1, y1))
                                buf = io.BytesIO()
                                cropped.save(buf, format="PNG")
                                png_bytes = buf.getvalue()

                        png_b64 = screenshot_to_base64(png_bytes)
                        consecutive_screenshot_failures = 0
                        session.screenshot_count += 1
                        ss_url = save_screenshot_to_uploads(png_bytes, f"cu_{operator}", session.screenshot_count)
                        await emit({"type": "cu_screenshot", "data": {"url": ss_url, "step": step}})

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": [{"type": "image", "source": {
                                "type": "base64", "media_type": "image/png", "data": png_b64
                            }}]
                        })
                    except Exception as ss_err:
                        consecutive_screenshot_failures += 1
                        print(f"[CU-BG]   Screenshot failed ({consecutive_screenshot_failures} consecutive): {ss_err}")
                        await emit({"type": "cu_screenshot_error", "data": {"error": str(ss_err), "step": step,
                                    "consecutive_failures": consecutive_screenshot_failures}})
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": [{"type": "text", "text": f"Screenshot failed: {ss_err}"}],
                            "is_error": True
                        })
                        if consecutive_screenshot_failures >= 3:
                            await emit({"type": "error", "data": "Display appears non-functional (3 consecutive screenshot failures). Ending task."})
                            session.status = "error"
                            break

                elif tool_name == "bash":
                    command = tool_input.get("command", "")
                    print(f"[CU-BG]   bash: {command[:100]}")
                    result = await execute_bash_command(command)
                    session.cu_log.append({"type": "bash", "command": command[:200], "step": step})
                    await emit({"type": "cu_bash_output", "data": {
                        "command": command[:200], "output": result["output"][:2000],
                        "exit_code": result["exit_code"], "step": step
                    }})
                    output = result["output"] or "(no output)"
                    if len(output) > 10000:
                        output = output[:10000] + "\n... [OUTPUT TRUNCATED at 10KB — full output not shown]"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": output
                    })

                elif tool_name in ("str_replace_editor", "str_replace_based_edit_tool"):
                    cmd = tool_input.get("command", "")
                    path = tool_input.get("path", "")
                    print(f"[CU-BG]   editor: {cmd} {path}")
                    result = await execute_text_editor(
                        cmd, path,
                        file_text=tool_input.get("file_text"),
                        old_str=tool_input.get("old_str"),
                        new_str=tool_input.get("new_str"),
                        insert_line=tool_input.get("insert_line"),
                        view_range=tool_input.get("view_range")
                    )
                    output = result.get("output", "")
                    session.cu_log.append({"type": "file_edit", "command": cmd, "path": path, "step": step})
                    await emit({"type": "cu_file_edit", "data": {
                        "command": cmd, "path": path, "step": step,
                        "output": output[:500]
                    }})
                    if len(output) > 10000:
                        output = output[:10000] + "\n... [TRUNCATED]"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": output,
                        "is_error": result.get("error", False)
                    })

                # ─── BlackBox tools (reuse execution logic) ───
                elif tool_name == "web_fetch":
                    url = tool_input.get("url", "")
                    max_chars = tool_input.get("max_chars", 80000)
                    print(f"[CU-BG]   web_fetch: {url}")
                    fetch_result = await run_blocking(perform_web_fetch, url, max_chars)
                    tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": fetch_result})

                elif tool_name in IMAGE_TOOL_PROVIDERS:
                    provider = IMAGE_TOOL_PROVIDERS[tool_name]
                    prompt = tool_input.get("prompt", "")
                    num_images = tool_input.get("numberOfImages", 1)
                    task = create_task(TaskType.IMAGE_GENERATION, operator="system", prompt=prompt,
                                       result_data={"options": {
                                           "aspectRatio": tool_input.get("aspectRatio", "16:9"),
                                           "resolution": tool_input.get("resolution", "1K"),
                                           "numberOfImages": num_images,
                                           "provider": provider
                                       }})
                    slug = generate_prompt_slug(prompt)
                    predicted_url = f"/ui/uploads/{slug}_{task.task_id}.png"
                    result_msg = f"Image generation started for: {prompt[:100]}. URL: {predicted_url}"
                    tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": result_msg})
                    await emit({"type": "image_task", "data": {"task_id": task.task_id, "prompt": prompt, "count": num_images}})

                elif tool_name == "generate_video":
                    prompt = tool_input.get("prompt", "")
                    duration = tool_input.get("duration", 8)
                    task = create_task(TaskType.VIDEO_GENERATION, operator="system", prompt=prompt,
                                       result_data={"options": {
                                           "aspectRatio": tool_input.get("aspectRatio", "16:9"),
                                           "duration": duration,
                                           "resolution": tool_input.get("resolution", "720p")
                                       }})
                    slug = generate_prompt_slug(prompt)
                    predicted_url = f"/ui/uploads/{slug}_{task.task_id}.mp4"
                    result_msg = f"Video generation started for: {prompt[:100]}. URL: {predicted_url}"
                    tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": result_msg})
                    await emit({"type": "video_task", "data": {"task_id": task.task_id, "prompt": prompt, "duration": duration}})

                elif tool_name == "lyria_music":
                    prompt = tool_input.get("prompt", "")
                    sample_count = tool_input.get("sampleCount", 1)
                    task = create_task(TaskType.LYRIA_MUSIC, operator="system", prompt=prompt,
                                       result_data={"prompt": prompt, "operator": "system"})
                    result_msg = f"Music generation started for: {prompt[:100]}. 30-second track."
                    tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": result_msg})
                    await emit({"type": "music_task", "data": {"task_id": task.task_id, "prompt": prompt, "sample_count": sample_count}})

                elif tool_name == "get_media":
                    result = execute_get_media(url=tool_input.get("url", ""), task_id=tool_input.get("task_id", ""))
                    tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": json.dumps(result, indent=2)})

                elif tool_name == "list_media":
                    result = execute_list_media(media_type=tool_input.get("media_type"), limit=tool_input.get("limit", 20))
                    tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": json.dumps(result, indent=2)})

                elif tool_name == "search_media":
                    result = execute_search_media(query=tool_input.get("query", ""), media_type=tool_input.get("media_type"), limit=tool_input.get("limit", 10))
                    tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": json.dumps(result, indent=2)})

                elif tool_name == "search_snapshots":
                    query = tool_input.get("query", "")
                    k = min(tool_input.get("k", 3), 5)
                    if not query:
                        result_msg = "Error: No search query provided."
                    else:
                        try:
                            vol_txt = read_text_safe(VOL_PATH)
                            search_results = await run_blocking(hybrid_retrieve, vol_txt, query, k=k, operator=operator)
                            if not search_results:
                                result_msg = f"No snapshots found matching: {query}"
                            else:
                                output_parts = [f"Found {len(search_results)} relevant snapshot(s) for: {query}\n"]
                                for i, snap_text in enumerate(search_results, 1):
                                    if len(snap_text) > 10000:
                                        snap_text = snap_text[:3000] + "\n... [truncated]"
                                    output_parts.append(f"--- Result {i} ---\n{format_snapshot_for_delivery(snap_text)}")
                                result_msg = "\n\n".join(output_parts)
                        except Exception as e:
                            result_msg = f"Search failed: {str(e)}"
                    tool_results.append({"type": "tool_result", "tool_use_id": tool_id, "content": result_msg})

                elif tool_name in ("send_sms", "make_phone_call", "make_voice_call",
                                   "search_contacts", "save_contact",
                                   "create_cron_job", "edit_cron_job", "search_cron_jobs",
                                   "use_computer",
                                   "get_task_status", "get_snapshot", "list_recent_snapshots",
                                   "get_current_time",
                                   "analyze_image", "analyze_audio", "analyze_video",
                                   "speech_to_text", "text_to_speech", "list_tts_voices",
                                   "gemini_pro_tts", "extend_video",
                                   "list_devices", "control_android_device",
                                   "gmail_search", "gmail_read", "gmail_send", "gmail_reply", "gmail_labels"):
                    from Orchestrator.tools import BlackBoxToolExecutor
                    executor = BlackBoxToolExecutor(operator=operator)
                    tool_result = await executor.execute(tool_name, tool_input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": tool_result.result if hasattr(tool_result, 'result') else str(tool_result)
                    })

                else:
                    # Catch-all: route ANY other tool (incl. the per-provider web
                    # search tools and dynamically-injected ToolVault tools) through
                    # BlackBoxToolExecutor instead of reporting "Unknown tool".
                    from Orchestrator.tools import BlackBoxToolExecutor
                    executor = BlackBoxToolExecutor(operator=operator)
                    tool_result = await executor.execute(tool_name, tool_input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": tool_result.result if hasattr(tool_result, 'result') else str(tool_result)
                    })

            # Add tool results to history
            if tool_results:
                history.append({"role": "user", "content": tool_results})

            # Reset for next iteration
            assistant_content_blocks = []
            tool_uses_this_turn = []
            content_buffer = ""
            thinking_buffer = ""

        # ── Save conversation to session for persistence across turns ──
        if not result_text:
            result_text = content_buffer or "(Agent reached step limit without a final response)"
        session.conversation_history = history
        session.touch()
        session.final_response = result_text
        session.final_thinking = thinking_buffer if thinking_buffer else ""

        # Only mark "complete" if there are no queued prompts waiting.
        # If there ARE queued prompts, stay "running" to prevent the
        # background status poller from adding a duplicate result bubble.
        if not session.prompt_queue:
            session.status = "complete"

        await emit({"type": "done", "data": {"thinking": session.final_thinking, "content": result_text}})

        # Save to BlackBox history (auto-mint, checkpoint)
        await _cu_save_to_blackbox(operator, user_text, result_text, session)

    except asyncio.CancelledError:
        # E-stop cancellation — handle gracefully
        print(f"[CU-BG] Task cancelled (E-stop) for {operator} at step {session.current_step}")
        session.status = "stopped"
        session.final_response = result_text or f"[Task stopped at step {session.current_step}]"
        await emit({"type": "cu_stopped", "data": {"step": session.current_step, "reason": "Task cancelled"}})
    except Exception as e:
        import traceback
        traceback.print_exc()
        session.status = "error"
        session.error_message = str(e)
        await emit({"type": "error", "data": f"Computer Use error: {str(e)}"})
    finally:
        # ── Auto-dequeue: if queue has pending prompts and not stopped, loop continues ──
        if session.prompt_queue and not session.stop_requested:
            next_prompt = session.dequeue_prompt()
            remaining = len(session.prompt_queue)
            print(f"[CU-BG] Auto-dequeuing next prompt for {operator} ({remaining} remaining)")

            # Reset task state FIRST (drains stale events from queue),
            # then emit cu_queue_next so it's not lost by the drain.
            saved_history = session.conversation_history
            session.reset_task_state()
            session.conversation_history = saved_history
            session.status = "running"
            session.user_message = next_prompt

            # Emit AFTER drain — this event must reach the SSE consumer
            await emit({"type": "cu_queue_next", "data": {"prompt": next_prompt[:100], "remaining": remaining}})
            # Yield to event loop so the SSE consumer can pick up cu_queue_next
            # before the recursive call floods the queue with new events
            await asyncio.sleep(0)

            # Capture fresh screenshot for the new prompt
            try:
                from Orchestrator.browser.screenshot import (
                    capture_remote_screenshot,
                    screenshot_to_base64, save_screenshot_to_uploads
                )
                if session.device_id != "blackbox":
                    fresh_png = await capture_remote_screenshot(session.device_id)
                else:
                    fresh_png = session.capture_screenshot_bytes()
                fresh_b64 = screenshot_to_base64(fresh_png)
                session.screenshot_count += 1
            except Exception:
                fresh_b64 = None

            # Build new user message
            new_user_content = [{"type": "text", "text": next_prompt}]
            if fresh_b64:
                new_user_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": fresh_b64}
                })

            from Orchestrator.browser.session_manager import strip_screenshots_from_history
            new_history = strip_screenshots_from_history(list(saved_history))
            new_history.append({"role": "user", "content": new_user_content})

            # Recursively run the agent loop for the dequeued prompt
            await run_anthropic_cu_loop(session, new_history, system_prompt, tools, headers, model, operator, next_prompt)
            return  # The recursive call handles its own sentinel

        # Sentinel: tells the queue consumer the loop is done.
        # Must be delivered, so drain an item if queue is full.
        try:
            session.event_queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                session.event_queue.get_nowait()  # drain one
            except asyncio.QueueEmpty:
                pass
            try:
                session.event_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass  # should never happen after drain
