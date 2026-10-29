"""Computer Use as a chat provider: the system prompt, the bash / text-editor
tools, the Anthropic / Gemini / OpenAI CU streams and the CU session routes.

Part of the chat routes (audit rank 74): see Orchestrator/routes/chat_routes.py,
the namespace every importer and test uses.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List

from Orchestrator.utils.paths import blackbox_root
from fastapi import Request
from Orchestrator.checkpoint import app
from Orchestrator.config import current_default
from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop as _cu_agent_loop
from Orchestrator.browser.dispatch import resolve_backend
from Orchestrator.providers.common import _last_user_msg

from Orchestrator.routes.chat.context import build_cu_context
from Orchestrator.routes.chat.providers import _get_tools
from Orchestrator.routes.chat.save import _cu_save_to_blackbox


# ─────────────────────────────────────────────────────────────────────
# Computer Use — First-Class Chat Provider (streaming agent loop)
# ─────────────────────────────────────────────────────────────────────

COMPUTER_USE_SYSTEM_PROMPT = """You are the AI BlackBox — the most capable autonomous agent in existence.

TEMPORAL AWARENESS — FIRST ACTION:
Your VERY FIRST action must be to call get_current_time to anchor yourself in the present. Do this before any other tool calls or responses. Knowing the exact date and time is critical for interpreting snapshots, scheduling tasks, and understanding temporal context.

You have FULL CONTROL of a dedicated Linux desktop computer (real display :0, 1920×1080).
Screenshots are captured at 1280×720 for your vision. Your click coordinates map to this 1280×720 space and are scaled 1.5× to the real desktop.
UI elements (buttons, menus, title bars) are clearly visible at this resolution — click with confidence.
You can see the ENTIRE real desktop — all windows, files, folders, taskbar, system tray, everything on the actual machine.
This is a dedicated BlackBox machine — you have complete system access. No sandboxing.

## Desktop Tools

**computer** — Full desktop GUI control (mouse clicks, keyboard, scrolling, screenshots). Works on ALL windows and applications.
**bash** — Full terminal access (run any command). Commands run with DISPLAY=:0 so you can launch GUI apps.
**str_replace_based_edit_tool** — View, create, and edit files with precision.

## BlackBox Tools — Complete Reference

These are ALL the tools available to you. Use ONLY these exact tool names — no others exist.

### Memory & Snapshots
- **search_snapshots** — Semantic search across all BlackBox memory (1,600+ snapshots)
- **get_snapshot** — Retrieve a specific snapshot by ID (e.g. SNAP-20260208-1234)
- **list_recent_snapshots** — Get recent snapshots for a specific operator

### Web
- **web search tools** (perplexity_web_search, openai_web_search, gemini_web_search, grok_web_search, grok_x_search for live X/Twitter, duckduckgo_web_search) — Search the web for current information
- **web_fetch** — Fetch and extract content from a URL

### Media Generation (async — returns task_id, poll with get_task_status)
- **gemini_image / openai_image / grok_image** — Create images from text prompts (pick a provider; fast)
- **generate_video** — Create videos with Veo 3.1 (5-20 minutes, quota-limited)
- **lyria_music** — Create 30-second music tracks with Lyria (1-2 minutes)
- **extend_video** — Extend an existing video with Veo 3.1 (5-20 minutes)

### Media Analysis
- **analyze_image** — Analyze/describe an image using multimodal AI (provide image_url + optional prompt)
- **analyze_audio** — Analyze audio content using Gemini (provide file_path + optional prompt, returns task_id)
- **analyze_video** — Analyze/describe a video using multimodal AI (provide video_url + optional prompt)

### Speech & Voice
- **text_to_speech** — Convert text to speech with any voice id from list_tts_voices (omit voice for the box default)
- **speech_to_text** — Transcribe audio to text using OpenAI Whisper (provide audio_path)
- **gemini_pro_tts** — High-quality speech with 30 Gemini Pro voices (returns task_id)
- **list_tts_voices** — List this box's TTS voices by provider (OpenAI, Gemini, ElevenLabs, local), with availability and the default

### Media Library
- **get_media** — Get a specific media item by URL or task_id
- **list_media** — List generated media (filter by type)
- **search_media** — Search media by prompt/description

### Task Status
- **get_task_status** — Check completion of ANY async task (image, video, music, audio analysis, TTS). ALWAYS poll after generating.

### Communication
- **send_sms** — Send an SMS text message
- **make_phone_call** — Start a phone call (AI voice conversation)
- **make_voice_call** — Make a voice call to deliver a pre-written message
- **search_contacts** — Look up contacts by name/phone/tag
- **save_contact** — Save or update a contact

### Scheduling
- **create_cron_job** — Create a scheduled recurring task
- **edit_cron_job** — Modify an existing scheduled task
- **search_cron_jobs** — Find scheduled tasks by name/prompt

### Automation & Device Control
- **use_computer** — Control the computer using an AI agent (browse web, use apps, run commands — returns task_id)
- **list_devices** — List all devices on the Tailscale mesh network (phones, computers, etc.)
- **control_android_device** — Control an Android device via Gemini CU (tap, type, swipe, open apps — returns task_id)

### Gmail
- **gmail_search** — Search or list recent emails (Gmail search syntax)
- **gmail_read** — Read full email content by message ID
- **gmail_send** — Compose and send a new email
- **gmail_reply** — Reply to an existing email thread
- **gmail_labels** — List labels or modify messages (mark_read, archive, star, etc)

### Utility
- **get_current_time** — Get current date/time with timezone

## Knowledge Hierarchy

When answering questions, prioritize sources in this order:
1. **SNAPSHOTS (Primary Truth)**: Search snapshots FIRST. They contain all past work, decisions, and operator preferences.
2. **WEB SEARCH (Live Data)**: Use the web search tools (perplexity_web_search, grok_web_search, grok_x_search for live X/Twitter, etc.) for current information and documentation.
3. **TRAINING DATA (Fallback)**: Only rely on built-in knowledge when snapshots and web don't have the answer.

## Media Rules

- **Images**: Generate instinctively when helpful. Imagen is fast (seconds) with separate quota.
- **Videos**: Only generate when explicitly requested. Veo 3.1 has strict hourly/daily quotas (3-5 max per session). On 429 rate limit, pivot to images.
- **Music**: Only generate when explicitly requested. Avoid artist/style references (copyright filter). Retry with generic descriptions on failure.
- **Analysis**: Use analyze_image/analyze_audio/analyze_video to examine existing media content.
- **TTS**: Use text_to_speech for quick speech (any voice from list_tts_voices), gemini_pro_tts for premium quality (async).
- After generating, ALWAYS use get_task_status to poll completion before referencing the result.
- Display media URLs on their own line so they render inline:
  /ui/uploads/sunset-mountains_abc123.png  (renders as image)
  /ui/uploads/racing-car_def456.mp4  (renders as video player)

## File Generation

Create downloadable files with artifact blocks:
  Format: [ARTIFACT:filename.ext:type]\\ncontent\\n[/ARTIFACT]
  Types: text, pdf, csv, docx (NOT for images/videos/music — those use generation tools)

## Methodology

1. **Observe** — Take a screenshot to see the full desktop state
2. **Plan** — Decide the most efficient action
3. **Act** — Execute one action at a time
4. **Verify** — Screenshot to confirm the result
5. **Iterate** — Repeat until the task is complete

## Guidelines

- You can see the ENTIRE display — all windows, desktop, taskbars, everything
- Be methodical and precise with mouse clicks — verify coordinates from screenshots
- Use keyboard shortcuts when efficient (Ctrl+L for address bar, Ctrl+A to select all, Alt+Tab to switch windows, etc.)
- For text entry, use the `type` action with the text to type
- You can launch any application via bash: `xterm &`, `nautilus &`, `eog image.png &`, `firefox &`, `code &`, etc.
- **Window management**: Alt+Tab switches windows. Click title bars to drag/move.
- When browsing, wait for pages to load before acting
- **Scrolling**: Each amount unit ≈ 100px. Use amount=3 for a small scroll (~300px), amount=5-7 for about one screen, amount=10+ for big jumps. Page_Down/Page_Up/space keys also work. Move mouse to content area first.
- Always verify your work by observing the result
- Summarize what you accomplished when done

## Memory & Context — YOUR MOST IMPORTANT CAPABILITY

The BlackBox contains 1,600+ snapshots — your complete memory of every past session, decision, preference, and interaction with this user. **Search snapshots FIRST before answering questions about past work, preferences, or context.** Don't guess or hallucinate history — the answers are in the snapshots. Call search_snapshots proactively and often. Search multiple times with different queries if needed. The snapshots below are recent context, but always search for deeper history.

## Scheduled Tasks (Cron Jobs)

You can schedule ANY model including 'computer-use' (yourself) for recurring desktop automation.
Computer-use cron jobs share the single desktop display (:0) and execute sequentially — space schedules apart.

## Important

CRITICAL: Do NOT generate snapshot format markers like '=== START SNAPSHOT ===', 'VOLUME TRACKER', 'GAUGES', 'Raw Session Log', etc. in your responses. These corrupt the BlackBox index.

## Operator

You are serving operator: {operator}
"""


# How long the CU bash step keeps collecting output after the shell exits. An
# app the command launched in the background (`xterm &`) inherits the output
# pipes and holds them open for as long as it runs, so the step ends at the
# shell's exit, not at end-of-file.
_BASH_DRAIN_GRACE_S = 0.3
# Bytes kept per stream: the 10 KB character cap needs at most four bytes per
# character. The rest is read and dropped, so memory stays bounded.
_BASH_CAPTURE_BYTES = 4 * 10000 + 4
# Pipes still held by launched apps, kept open (and drained) until they exit.
_bash_drains: set = set()
# Output a launched app may write after its step before its pipes are closed
# (it then gets SIGPIPE): draining runs on the server's loop, and an app that
# floods its output (`yes &`) must not keep the loop busy forever.
_BASH_DETACHED_DRAIN_BYTES = 64 * 1024 * 1024


class _BashStepProtocol(asyncio.SubprocessProtocol):
    """Collects a CU bash step's output and reports the shell's own exit.

    asyncio's Process.wait() returns only once every pipe is closed, which a
    launched app holding them never does; process_exited fires at the exit."""

    def __init__(self, loop):
        self.out, self.err = bytearray(), bytearray()
        self.exited = loop.create_future()
        self.pipes_closed = loop.create_future()
        self._open_pipes = {1, 2}
        self._transport = None
        self.detached_bytes = None  # counted once the step has returned

    def connection_made(self, transport):
        self._transport = transport

    def pipe_data_received(self, fd, data):
        if self.detached_bytes is not None:
            self.detached_bytes += len(data)
            if self.detached_bytes > _BASH_DETACHED_DRAIN_BYTES and self._transport is not None:
                self._transport.close()
            return
        buf = self.out if fd == 1 else self.err
        room = _BASH_CAPTURE_BYTES - len(buf)
        if room > 0:
            buf += data[:room]

    def pipe_connection_lost(self, fd, exc):
        self._open_pipes.discard(fd)
        if not self._open_pipes and not self.pipes_closed.done():
            self.pipes_closed.set_result(None)

    def process_exited(self):
        if not self.exited.done():
            self.exited.set_result(None)


async def execute_bash_command(command: str, timeout: int = 120) -> dict:
    """Execute a bash command and return output. Caps output at 10KB.

    The CU driver awaits this on the server's event loop, where the old
    subprocess.run froze every stream and voice call for up to ``timeout``
    (audit O02-chat-routes-A-02). It is an asyncio subprocess now, started in
    its own session so it leads a process group. The step ends when the shell
    exits: an app the command put in the background (`xterm &`, as the CU
    prompt suggests) keeps running, and its output pipes stay open and drained,
    so it never dies of SIGPIPE. If the shell itself is still running at the
    timeout or on cancellation (the E-stop), the whole group is killed, so a
    hung command cannot leave its children behind.
    """
    import subprocess
    from Orchestrator.browser.config import ACTIVE_DISPLAY, NATIVE_MODE, get_native_env
    from Orchestrator.cli_agent.env_scrub import scrubbed_child_env
    transport = protocol = None
    try:
        # Scrubbed env + ensure DISPLAY is set for GUI commands. This is the CU
        # driver's model-driven `bash` tool (every Anthropic CU run offers it),
        # so it must not hand the server's credentials to a prompt-injected
        # agent (audit O14-cli-agents-devices-02).
        env = scrubbed_child_env()
        if NATIVE_MODE:
            env.update(get_native_env())
        else:
            env["DISPLAY"] = f":{ACTIVE_DISPLAY}"
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.subprocess_shell(
            lambda: _BashStepProtocol(loop), command,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(blackbox_root()), env=env, start_new_session=True,
        )
        try:
            await asyncio.wait_for(asyncio.shield(protocol.exited), timeout)
        except asyncio.TimeoutError:
            await _kill_process_group(transport.get_pid(), protocol.exited)
            return {"output": f"Command timed out after {timeout}s", "exit_code": -1}
        # The shell has exited, so everything its foreground commands wrote is
        # already in the pipes.
        await asyncio.wait({protocol.pipes_closed}, timeout=_BASH_DRAIN_GRACE_S)
        output = _decode_command_output(bytes(protocol.out))
        stderr = _decode_command_output(bytes(protocol.err))
        combined = output + ("\n" + stderr if stderr else "")
        # Cap output to prevent token explosion
        if len(combined) > 10000:
            combined = combined[:10000] + "\n... [OUTPUT TRUNCATED at 10KB]"
        return {"output": combined, "exit_code": transport.get_returncode()}
    except asyncio.CancelledError:
        if transport is not None and transport.get_returncode() is None:
            await _kill_process_group(transport.get_pid(), protocol.exited)
        raise
    except Exception as e:
        return {"output": f"Error executing command: {e}", "exit_code": -1}
    finally:
        if transport is not None:
            _close_after_drain(transport, protocol)


def _close_after_drain(transport, protocol) -> None:
    """Close the step's transport once its pipes are closed. A launched app may
    hold them for hours, so a task waits for that and closes it."""
    if protocol.pipes_closed.done():
        transport.close()
        return
    protocol.detached_bytes = 0
    task = asyncio.ensure_future(_drain_until_closed(transport, protocol))
    _bash_drains.add(task)
    task.add_done_callback(_bash_drains.discard)


async def _drain_until_closed(transport, protocol) -> None:
    try:
        await protocol.pipes_closed
    finally:
        # Also reached when the loop shuts down (a headless CU run ends): close
        # the pipes with the loop rather than leave the transport to the GC.
        # The shell has exited or been killed, so close() kills nothing.
        transport.close()


def _decode_command_output(data: bytes) -> str:
    """Bytes to text the way subprocess's text=True gave it (universal newlines),
    without failing on bytes that are not UTF-8."""
    text = (data or b"").decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


async def _kill_process_group(pid, exited) -> None:
    """SIGKILL the whole process group ``pid`` leads (start_new_session) and
    wait for the shell's exit, so neither a hung command nor a child it started
    outlives it."""
    import os
    import signal
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(asyncio.shield(exited), 5)
    except asyncio.TimeoutError:
        pass


async def execute_text_editor(command: str, path: str, **params) -> dict:
    """Execute str_replace_editor commands (view, create, str_replace, insert).

    The file reads and writes run in a worker thread: the CU driver awaits this
    on the server's event loop."""
    return await asyncio.to_thread(_execute_text_editor_sync, command, path, **params)


def _execute_text_editor_sync(command: str, path: str, **params) -> dict:
    from pathlib import Path as P
    try:
        if command == "view":
            file_path = P(path)
            if not file_path.exists():
                return {"output": f"File not found: {path}", "error": True}
            content = file_path.read_text(errors="replace")
            view_range = params.get("view_range")
            if view_range and isinstance(view_range, list) and len(view_range) == 2:
                lines = content.split("\n")
                start = max(0, view_range[0] - 1)
                end = min(len(lines), view_range[1])
                numbered = [f"{i+start+1}\t{line}" for i, line in enumerate(lines[start:end])]
                return {"output": "\n".join(numbered)}
            else:
                lines = content.split("\n")
                numbered = [f"{i+1}\t{line}" for i, line in enumerate(lines)]
                return {"output": "\n".join(numbered)}

        elif command == "create":
            file_content = params.get("file_text", "")
            file_path = P(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(file_content)
            return {"output": f"File created: {path}"}

        elif command == "str_replace":
            old_str = params.get("old_str", "")
            new_str = params.get("new_str", "")
            file_path = P(path)
            if not file_path.exists():
                return {"output": f"File not found: {path}", "error": True}
            content = file_path.read_text(errors="replace")
            if old_str not in content:
                return {"output": f"String not found in {path}", "error": True}
            count = content.count(old_str)
            if count > 1:
                return {"output": f"Multiple matches ({count}) found. Be more specific.", "error": True}
            content = content.replace(old_str, new_str, 1)
            file_path.write_text(content)
            return {"output": f"Replacement made in {path}"}

        elif command == "insert":
            insert_line = params.get("insert_line", 0)
            new_str = params.get("new_str", "")
            file_path = P(path)
            if not file_path.exists():
                return {"output": f"File not found: {path}", "error": True}
            lines = file_path.read_text(errors="replace").split("\n")
            lines.insert(insert_line, new_str)
            file_path.write_text("\n".join(lines))
            return {"output": f"Inserted at line {insert_line} in {path}"}

        else:
            return {"output": f"Unknown command: {command}", "error": True}
    except Exception as e:
        return {"output": f"Editor error: {e}", "error": True}


async def stream_computer_use(messages: List[Dict], model: str, operator: str, session_id: str = None, device_id: str = "blackbox", native_mode: bool = False):
    """Launch the CU agent loop as a background asyncio.Task and consume
    events from its queue.  The task keeps running even if the SSE client
    disconnects.  Reconnecting yields buffered / live events from the
    same queue.

    Yields SSE events:
      cu_screenshot, cu_action, cu_bash_output, cu_file_edit, cu_step,
      thinking, content, usage, done, error
    """
    from Orchestrator.browser.config import (
        ANTHROPIC_API_KEY as CU_API_KEY, ANTHROPIC_API_URL, MAX_ITERATIONS
    )
    from Orchestrator.browser.driver_anthropic import build_anthropic_cu_request
    from Orchestrator.browser.screenshot import (
        capture_screenshot, capture_remote_screenshot,
        screenshot_to_base64, save_screenshot_to_uploads
    )
    from Orchestrator.browser.session_manager import (
        get_or_create_session, strip_screenshots_from_history
    )
    import uuid
    from Orchestrator.browser.display_arbiter import try_claim, release_claim

    # Per-LAUNCH display claim id (M1-T6). Claimed only if THIS turn launches a
    # driver (new-message branch below); released in the consumer-loop finally.
    _display_claim_id = str(uuid.uuid4())

    if not CU_API_KEY:
        yield {"type": "error", "data": "ANTHROPIC_API_KEY not set"}
        return

    # ── Get/create persistent session (lifecycle only — no display arbitration
    #    here; that is per-launch, at the driver launch below). A lookup tears
    #    down an expired session (Chrome + display quartet): in a thread. ──
    session = await asyncio.to_thread(
        get_or_create_session, operator, session_id=session_id, device_id=device_id)
    session.touch()

    # ── On-the-fly device switching ──
    if session.device_id != device_id:
        print(f"[CU-STREAM] Switching device {session.device_id} -> {device_id} for {operator}")
        session.device_id = device_id

    # ── Emit session ID + device immediately so frontend can track it ──
    yield {"type": "cu_session", "data": {"session_id": session.session_id, "device_id": session.device_id}}

    # ── Extract incoming user text early for comparison ──
    incoming_user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                incoming_user_text = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            else:
                incoming_user_text = content
            break

    # ── D1 multi-desktop (2026-07-23): a NEW prompt while this session is busy
    #    appends a BRAND-NEW desktop — the running agent keeps working on its
    #    own display; you return to it via the live-view rail. Replaces the old
    #    enqueue-into-the-busy-agent behavior. Reconnect/same-message flows
    #    still take the queue/reconnect branch below. ──
    if (session.status in ("running", "starting")
            and incoming_user_text
            and incoming_user_text != "[reconnect]"
            and incoming_user_text != session.user_message):
        session = await asyncio.to_thread(
            get_or_create_session, operator, device_id=device_id, force_new=True)
        print(f"[CU-STREAM] Busy agent -> new desktop {session.session_id[:8]} for {operator}")
        # Re-emit so the frontend repoints its stored session id at the NEW desktop.
        yield {"type": "cu_session", "data": {"session_id": session.session_id,
                                              "device_id": session.device_id,
                                              "new_desktop": True}}

    # ── Queue detection: reconnect/same-message paths for a busy session ──
    if session.status in ("running", "starting") and (not session.agent_task or not session.agent_task.done() or session.status == "starting"):
        if incoming_user_text and incoming_user_text != "[reconnect]" and incoming_user_text != session.user_message:
            # New prompt while task is running/starting → queue it
            position = session.enqueue_prompt(incoming_user_text)
            print(f"[CU-STREAM] Queued prompt #{position} for {operator}: {incoming_user_text[:60]}")
            yield {"type": "cu_queued", "data": {"position": position, "prompt": incoming_user_text[:100]}}
            return
        if session.status == "running":
            # Reconnect — fall through to queue consumer
            print(f"[CU-STREAM] Reconnecting to running task for {operator} (step {session.current_step}/{session.total_steps})")
            yield {"type": "cu_step", "data": {"step": session.current_step, "total": session.total_steps, "reconnect": True}}
            # Fall through to queue consumer below
        else:
            # "starting" status with same message — task is still initializing, wait
            print(f"[CU-STREAM] Task still starting for {operator}, reconnect later")
            yield {"type": "cu_step", "data": {"step": 0, "total": 0, "reconnect": True}}
    elif session.status == "running":
        # Task somehow died but status stuck — allow new request
        session.reset_task_state()
        yield {"type": "error", "data": "Previous task ended unexpectedly. Please retry."}
        return
    elif session.status == "complete" and session.final_response and incoming_user_text == session.user_message:
        # Same message as completed task — client reconnecting to get missed result
        print(f"[CU-STREAM] Returning completed result for {operator} (reconnect)")
        yield {"type": "done", "data": {"thinking": session.final_thinking, "content": session.final_response}}
        return
    else:
        # New message or fresh session — reset and process
        if session.status == "complete":
            print(f"[CU-STREAM] New message for {operator}, resetting completed session")
            session.reset_task_state()

        # Ensure display/device is ready.
        # VIRTUAL (default): allocate this session's own per-session display.
        # NATIVE opt-in: use the shared physical display (arbiter-guarded below).
        if session.device_id == "blackbox":
            session.native_mode = bool(native_mode)
            # Local device — start display + Chrome
            if not await session.ensure_browser("about:blank", backend="anthropic"):
                yield {"type": "error", "data": session.last_error or "Failed to start browser session"}
                return

            if not session.native_mode and (session.display is None
                                            or not session.display.get_env()):
                yield {"type": "error", "data": "Virtual display allocation failed"}
                return

            # Small wait for Chrome if freshly started
            if not session.conversation_history:
                await asyncio.sleep(2)
        else:
            # Remote device — verify VNC reachability
            from Orchestrator.device_registry import get_registry
            from Orchestrator.remote_desktop import VNCClient
            device = get_registry().get_device(session.device_id)
            if not device:
                yield {"type": "error", "data": f"Device not found: {session.device_id}"}
                return
            vnc = VNCClient(device.tailscale_ip, device.vnc_port, device.metadata.get("vnc_password"))
            if not await vnc.is_reachable():
                yield {"type": "error", "data": f"Cannot reach VNC on {device.name} ({device.tailscale_ip}:{device.vnc_port})"}
                return
            print(f"[CU-STREAM] Remote device {session.device_id} VNC reachable")

        # ── Use the user's latest message (extracted above) ──
        user_text = incoming_user_text
        if not user_text:
            yield {"type": "error", "data": "No user message found"}
            return

        # Reset task state for new turn and mark as starting to prevent races
        session.reset_task_state()
        # Rebind the event queue to the server loop: a headless task run
        # (browser/headless.run_cu_task via asyncio.run in a worker thread) may
        # have left it bound to its now-closed loop. See fresh_event_queue.
        session.fresh_event_queue()
        session.status = "starting"  # Prevents concurrent requests from also entering new-message path
        session.user_message = user_text

        # ── Capture initial screenshot ──
        # (capture and save are blocking subprocess/disk work: off the loop)
        initial_b64 = None
        try:
            if session.device_id != "blackbox":
                initial_png = await capture_remote_screenshot(session.device_id)
            else:
                initial_png = await asyncio.to_thread(session.capture_screenshot_bytes)
            initial_b64 = screenshot_to_base64(initial_png)
            session.screenshot_count += 1
            screenshot_url = await asyncio.to_thread(
                save_screenshot_to_uploads, initial_png, f"cu_{operator}", session.screenshot_count)
            yield {"type": "cu_screenshot", "data": {"url": screenshot_url, "step": 0}}
        except Exception as e:
            print(f"[CU-STREAM] Initial screenshot failed: {e}")

        # ── Build messages ──
        system_prompt = COMPUTER_USE_SYSTEM_PROMPT.format(operator=operator)

        # ── Inject remote device context into system prompt ──
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
                system_prompt += device_context

        # Enrich with fossil context (reduced counts for CU token budget)
        cu_fossil_context, cu_provenance = await asyncio.to_thread(build_cu_context, user_text, operator)
        session.provenance = cu_provenance
        if cu_fossil_context:
            system_prompt += "\n\n" + cu_fossil_context
            print(f"[CU-STREAM] Injected {len(cu_fossil_context)} chars of fossil context")

        user_content = [{"type": "text", "text": user_text}]
        if initial_b64:
            user_content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": initial_b64}
            })
        session.trim_history()
        history = strip_screenshots_from_history(list(session.conversation_history))
        history.append({"role": "user", "content": user_content})

        # ── Tool array + headers: the computer tool entry and beta header are
        #    per model (5.5+ = toolset, no beta) ──
        tools, headers = build_anthropic_cu_request(
            model, CU_API_KEY, await asyncio.to_thread(_get_tools, "anthropic", _last_user_msg(history), group="chat_cu"))

        # ── Claim the shared physical display ONLY for a NATIVE launch (M9). A
        #    virtual launch drives its own per-session display and never contends,
        #    so concurrent virtual agents are allowed (D14 — a badge, not a lock).
        #    Released via the driver's done-callback below. ──
        if session.device_id == "blackbox" and native_mode:
            owner = try_claim("browser", operator, _display_claim_id,
                              session_id=session.session_id)
            if owner is not None:
                yield {"type": "error", "data": f"Cannot start Computer Use — {owner.describe()}. Stop it first."}
                return

        # ── Launch background task ──
        session.agent_task = asyncio.create_task(
            _cu_agent_loop(session, history, system_prompt, tools, headers, model, operator, user_text)
        )
        # Release the display claim on the DRIVER TASK's lifecycle, not this
        # consumer generator's (M1-T6 rev2, Hole 3): the driver survives client
        # disconnect by design, so a consumer-finally release would drop a live
        # claim during the pre-"running" launch window. This callback fires only
        # when the driver actually finishes (incl. its internal auto-dequeue),
        # or on E-stop cancellation.
        session.agent_task.add_done_callback(
            lambda _t, _cid=_display_claim_id: release_claim(_cid))
        print(f"[CU-STREAM] Background task launched for {operator}")

    # ── Queue consumer loop — yields events as SSE ──
    try:
        while True:
            try:
                event = await asyncio.wait_for(session.event_queue.get(), timeout=120)
            except asyncio.TimeoutError:
                # Heartbeat to keep the SSE connection alive
                yield {"type": "heartbeat", "data": {"step": session.current_step, "total": session.total_steps}}
                continue

            if event is None:
                # Sentinel — task is done
                break

            yield event
    except (asyncio.CancelledError, GeneratorExit):
        # Client disconnected — task keeps running in background (and still owns
        # its display claim until IT finishes — see the done-callback above).
        print(f"[CU-STREAM] Client disconnected for {operator}, task continues in background")


# =============================================================================
# Gemini Computer Use — Chat Provider Streaming
# =============================================================================

def _detect_device_switch(user_text: str, operator: str):
    """Detect if user wants to switch to a specific device from their message."""
    try:
        from Orchestrator.device_registry import get_registry
        registry = get_registry()
        all_devices = registry.get_all_devices()
    except Exception:
        return None

    text_lower = user_text.lower()

    # Check for explicit device mentions (name or ID)
    for device in all_devices:
        name_lower = device.name.lower()
        if name_lower in text_lower or device.id in text_lower:
            return device.id

    # Generic terms
    if any(term in text_lower for term in ("my phone", "android", "mobile", "cell phone")):
        for device in all_devices:
            if (device.device_type.value == "android" and
                    device.status.value == "online"):
                return device.id

    if any(term in text_lower for term in ("desktop", "computer", "linux", "local machine")):
        return "blackbox"

    return None


async def stream_gemini_computer_use(messages: List[Dict], model: str,
                                      operator: str,
                                      session_id: str = None,
                                      device_id: str = "blackbox",
                                      native_mode: bool = False):
    """Launch the Gemini CU agent loop as a background asyncio.Task and consume
    events from its queue. Mirrors stream_computer_use() for identical UX.

    Yields SSE events:
      cu_screenshot, cu_action, cu_step, cu_session, cu_stopped,
      cu_queued, cu_queue_next, content, usage, done, error, heartbeat
    """
    import uuid
    from Orchestrator.gemini_cu import (
        get_or_create_session as gemini_get_or_create,
        run_gemini_cu_loop,
    )
    from Orchestrator.gemini_cu.config import DEFAULT_CU_MODEL
    from Orchestrator.config import GOOGLE_API_KEY
    from Orchestrator.browser.display_arbiter import (
        try_claim, release_claim, is_local_environment,
    )

    # Per-LAUNCH display claim id (M1-T6). A claim is made only if THIS turn
    # actually launches a driver (below); released in the consumer-loop finally.
    _display_claim_id = str(uuid.uuid4())

    if not GOOGLE_API_KEY:
        yield {"type": "error", "data": "GOOGLE_API_KEY not set"}
        return

    if not model:
        model = DEFAULT_CU_MODEL

    # ── Resolve device → environment ──
    from Orchestrator.device_registry import get_registry, DeviceProtocol
    registry = get_registry()
    device = registry.get_device(device_id)
    if device and device.protocol == DeviceProtocol.ADB:
        environment = "android"
        # Ensure ADB connection
        try:
            from Orchestrator.adb import get_adb_manager
            result = await get_adb_manager().ensure_connected(device_id)
            if not result.get("success"):
                yield {"type": "error", "data": f"Cannot connect to device: {result.get('error')}"}
                return
        except Exception as e:
            yield {"type": "error", "data": f"ADB connection error: {str(e)}"}
            return
    else:
        environment = "desktop"

    # Display arbitration is per-LAUNCH (M1-T6): the claim is made right before
    # the driver is launched (new-message branch below), not here — so reconnect/
    # queue turns never claim and can never self-block, and there is no guard-level
    # claim to leak on an early return (review C3).

    # ── Get/create persistent session (a lookup tears down an expired one:
    #    in a worker thread) ──
    session = await asyncio.to_thread(
        gemini_get_or_create, operator, device_id, environment, session_id)
    session.touch()

    # ── Emit session ID ──
    yield {"type": "cu_session", "data": {"session_id": session.session_id}}

    # ── Extract user text ──
    incoming_user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                incoming_user_text = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            else:
                incoming_user_text = content
            break

    # ── D1 multi-desktop (2026-07-23): busy agent + new prompt -> brand-new
    #    desktop (gemini manager twin); the running agent keeps its display. ──
    if (session.status in ("running", "starting")
            and incoming_user_text
            and incoming_user_text != "[reconnect]"
            and incoming_user_text != session.user_message):
        session = await asyncio.to_thread(
            gemini_get_or_create, operator, session.device_id,
            session.environment, force_new=True)
        print(f"[GEMINI-CU-STREAM] Busy agent -> new desktop {session.session_id[:8]} for {operator}")
        yield {"type": "cu_session", "data": {"session_id": session.session_id,
                                              "new_desktop": True}}

    # ── Queue/reconnect detection (same pattern as Anthropic CU) ──
    if session.status in ("running", "starting") and (not session.agent_task or not session.agent_task.done() or session.status == "starting"):
        if incoming_user_text and incoming_user_text != "[reconnect]" and incoming_user_text != session.user_message:
            position = session.enqueue_prompt(incoming_user_text)
            print(f"[GEMINI-CU-STREAM] Queued prompt #{position} for {operator}: {incoming_user_text[:60]}")
            yield {"type": "cu_queued", "data": {"position": position, "prompt": incoming_user_text[:100]}}
            return
        if session.status == "running":
            print(f"[GEMINI-CU-STREAM] Reconnecting to running task for {operator} (step {session.current_step}/{session.total_steps})")
            yield {"type": "cu_step", "data": {"step": session.current_step, "total": session.total_steps, "reconnect": True}}
        else:
            print(f"[GEMINI-CU-STREAM] Task still starting for {operator}")
            yield {"type": "cu_step", "data": {"step": 0, "total": 0, "reconnect": True}}
    elif session.status == "running":
        session.reset_task_state()
        yield {"type": "error", "data": "Previous task ended unexpectedly. Please retry."}
        return
    elif session.status == "complete" and session.final_response and incoming_user_text == session.user_message:
        print(f"[GEMINI-CU-STREAM] Returning completed result for {operator} (reconnect)")
        yield {"type": "done", "data": {"thinking": "", "content": session.final_response}}
        return
    else:
        if session.status == "complete":
            print(f"[GEMINI-CU-STREAM] New message for {operator}, resetting completed session")
            session.reset_task_state()

        user_text = incoming_user_text
        if not user_text:
            yield {"type": "error", "data": "No user message found"}
            return

        # ── Device switch detection ──
        detected_device = _detect_device_switch(user_text, operator)
        if detected_device and detected_device != session.device_id:
            print(f"[GEMINI-CU-STREAM] Device switch: {session.device_id} → {detected_device}")
            from Orchestrator.gemini_cu import destroy_session as gemini_destroy
            await asyncio.to_thread(gemini_destroy, operator)
            new_device = registry.get_device(detected_device)
            new_env = "android" if new_device and new_device.protocol == DeviceProtocol.ADB else "desktop"
            if new_env == "android":
                try:
                    from Orchestrator.adb import get_adb_manager
                    result = await get_adb_manager().ensure_connected(detected_device)
                    if not result.get("success"):
                        yield {"type": "error", "data": f"Cannot connect to {detected_device}: {result.get('error')}"}
                        return
                except Exception as e:
                    yield {"type": "error", "data": f"ADB error: {str(e)}"}
                    return
            session = await asyncio.to_thread(
                gemini_get_or_create, operator, detected_device, new_env)
            session.touch()
            yield {"type": "cu_session", "data": {"session_id": session.session_id, "device_switch": detected_device}}

        session.reset_task_state()
        # NATIVE opt-in contends for the shared display; virtual (default) does not.
        session.native_mode = bool(native_mode and is_local_environment(session.environment))
        session.status = "starting"
        session.user_message = user_text

        # ── Build system prompt with fossil context ──
        system_prompt = session._default_prompt if hasattr(session, '_default_prompt') else None
        if not system_prompt:
            from Orchestrator.gemini_cu.agent_loop import _default_system_prompt
            system_prompt = _default_system_prompt(session)

        cu_fossil_context, cu_provenance = await asyncio.to_thread(build_cu_context, user_text, operator)
        session.provenance = cu_provenance
        if cu_fossil_context:
            # HARD cap for gemini CU (the chat-path half of the MS02 chess-game
            # 400, 2026-07-23): this injection once reached 311,810 chars (~78K
            # tokens) against the preview model's 131,072-token input limit
            # shared with per-step screenshots. Twin of the agent_loop's
            # retrieve_for_agent max_chars=20_000.
            if len(cu_fossil_context) > 20_000:
                print(f"[GEMINI-CU-STREAM] fossil context {len(cu_fossil_context)} chars"
                      f" > 20000 budget — truncating")
                cu_fossil_context = (cu_fossil_context[:20_000]
                                     + "\n[fossil context truncated to fit the "
                                       "model's input window]")
            system_prompt += "\n\n" + cu_fossil_context
            print(f"[GEMINI-CU-STREAM] Injected {len(cu_fossil_context)} chars of fossil context")

        # ── Claim the shared physical display ONLY for a NATIVE local launch (M9).
        #    A virtual launch drives its own display and never contends; android is
        #    never local so it never claims/blocks. Released via the driver's
        #    done-callback below. ──
        if native_mode and is_local_environment(session.environment):
            owner = try_claim("gemini-chat", operator, _display_claim_id,
                              session_id=session.session_id)
            if owner is not None:
                yield {"type": "error", "data": f"Cannot start Gemini CU — {owner.describe()}. Stop it first."}
                return

        # ── Launch background task ──
        session.agent_task = asyncio.create_task(
            _gemini_cu_agent_loop(session, operator, user_text, model, system_prompt)
        )
        # Release on the DRIVER TASK's lifecycle, not this consumer generator's
        # (M1-T6 rev2, Hole 3): the task survives client disconnect, so this
        # callback fires only when the driver actually finishes (incl. its
        # auto-dequeue) or on E-stop.
        session.agent_task.add_done_callback(
            lambda _t, _cid=_display_claim_id: release_claim(_cid))
        print(f"[GEMINI-CU-STREAM] Background task launched for {operator} on {session.device_id} ({session.environment})")

    # ── Queue consumer loop ──
    try:
        while True:
            try:
                event = await asyncio.wait_for(session.event_queue.get(), timeout=120)
            except asyncio.TimeoutError:
                yield {"type": "heartbeat", "data": {"step": session.current_step, "total": session.total_steps}}
                continue

            if event is None:
                break

            yield event
    except (asyncio.CancelledError, GeneratorExit):
        # Task keeps running in background and still owns its claim until IT
        # finishes (see the done-callback above).
        print(f"[GEMINI-CU-STREAM] Client disconnected for {operator}, task continues in background")


async def _gemini_cu_agent_loop(session, operator: str, user_text: str,
                                 model: str, system_prompt: str):
    """Background agent loop for Gemini CU. Pushes events to
    session.event_queue, same pattern as _cu_agent_loop().
    """
    from Orchestrator.gemini_cu import run_gemini_cu_loop

    async def emit(evt):
        try:
            session.event_queue.put_nowait(evt)
        except asyncio.QueueFull:
            pass

    result_text = ""
    try:
        session.status = "running"
        print(f"[GEMINI-CU-BG] Starting agent loop for {operator}: model={model}, env={session.environment}, prompt={user_text[:80]}")

        async for event in run_gemini_cu_loop(session, user_text, model, system_prompt):
            event_type = event.get("type")

            if event_type == "cu_step":
                session.current_step = event["data"]["step"]
                session.total_steps = event["data"]["total"]
            elif event_type == "content":
                result_text += event["data"].get("text", "")
            elif event_type == "done":
                result_text = event["data"].get("content", result_text)
            elif event_type == "error":
                session.status = "error"
                session.error_message = event["data"].get("message", str(event["data"]))

            await emit(event)

        # Save conversation and sync usage
        session.final_response = result_text or "(Agent completed without text response)"
        session.sync_usage()

        if not session.prompt_queue:
            session.status = "complete"

        await emit({"type": "done", "data": {"thinking": "", "content": session.final_response}})

        # Save to BlackBox history
        await _cu_save_to_blackbox(operator, user_text, result_text, session)

    except asyncio.CancelledError:
        print(f"[GEMINI-CU-BG] Task cancelled (E-stop) for {operator} at step {session.current_step}")
        session.status = "stopped"
        session.final_response = result_text or f"[Task stopped at step {session.current_step}]"
        await emit({"type": "cu_stopped", "data": {"step": session.current_step, "reason": "Task cancelled"}})
    except Exception as e:
        import traceback
        traceback.print_exc()
        session.status = "error"
        session.error_message = str(e)
        await emit({"type": "error", "data": f"Gemini CU error: {str(e)}"})
    finally:
        # ── Auto-dequeue ──
        if session.prompt_queue and not session.stop_requested:
            next_prompt = session.dequeue_prompt()
            remaining = len(session.prompt_queue)
            print(f"[GEMINI-CU-BG] Auto-dequeuing next prompt for {operator} ({remaining} remaining)")

            saved_history = session.conversation_history
            session.reset_task_state()
            session.conversation_history = saved_history
            session.status = "running"
            session.user_message = next_prompt

            await emit({"type": "cu_queue_next", "data": {"prompt": next_prompt[:100], "remaining": remaining}})
            await asyncio.sleep(0)

            # Build system prompt for next turn
            from Orchestrator.gemini_cu.agent_loop import _default_system_prompt
            next_system = _default_system_prompt(session)
            cu_fossil_context, cu_provenance = await asyncio.to_thread(build_cu_context, next_prompt, operator)
            session.provenance = cu_provenance
            if cu_fossil_context:
                next_system += "\n\n" + cu_fossil_context

            await _gemini_cu_agent_loop(session, operator, next_prompt, model, next_system)
            return  # Recursive call handles its own sentinel

        # Sentinel
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


# =============================================================================
# OpenAI Computer Use — Chat Provider Streaming
# =============================================================================

async def stream_openai_computer_use(messages: List[Dict], model: str,
                                      operator: str,
                                      session_id: str = None,
                                      device_id: str = "blackbox",
                                      native_mode: bool = False):
    """Launch the OpenAI CUA agent loop as a background asyncio.Task and
    consume events from its queue. Mirrors stream_gemini_computer_use for
    identical UX, but uses the browser/session_manager ComputerUseSession
    (same one the Anthropic path uses — so /chat/cu-status and /chat/cu-stop
    work for it automatically).

    Yields SSE events:
      cu_session, cu_screenshot, cu_action, cu_step, cu_safety, cu_stopped,
      cu_queued, cu_queue_next, content, usage, done, error, heartbeat
    """
    import uuid
    from Orchestrator.browser.session_manager import get_or_create_session
    from Orchestrator.browser.display_arbiter import try_claim, release_claim
    from Orchestrator.config import OPENAI_API_KEY
    from Orchestrator.openai_cu.config import OPENAI_CU_MODEL_DEFAULT

    # Per-LAUNCH display claim id (M1-T6). Claimed only if THIS turn launches a
    # driver (new-message branch below); released in the consumer-loop finally.
    _display_claim_id = str(uuid.uuid4())

    if not OPENAI_API_KEY:
        yield {"type": "error", "data": "OPENAI_API_KEY not set"}
        return

    if not model:
        model = OPENAI_CU_MODEL_DEFAULT

    # ── Local desktop only for now ──
    if device_id != "blackbox":
        yield {"type": "error", "data": "OpenAI CU supports the local desktop only for now"}
        return

    # ── Desktop conflict guard (mirror of Gemini's Anthropic-session check) ──
    try:
        from Orchestrator.gemini_cu.session_manager import get_session as gemini_get_session
        gemini_session = gemini_get_session(operator)
        if gemini_session and gemini_session.status == "running":
            yield {"type": "error", "data": "Cannot start OpenAI CU — Gemini CU task running on this desktop. Stop it first."}
            return
    except Exception:
        pass

    # ── Get/create persistent session (shared with the Anthropic path; lifecycle
    #    only — display arbitration is per-launch, at the driver launch below).
    #    A lookup tears down an expired session: in a worker thread. ──
    session = await asyncio.to_thread(
        get_or_create_session, operator, session_id=session_id, device_id=device_id)
    session.touch()

    # ── Emit session ID ──
    yield {"type": "cu_session", "data": {"session_id": session.session_id, "device_id": session.device_id}}

    # ── Extract user text ──
    incoming_user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                incoming_user_text = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            else:
                incoming_user_text = content
            break

    # ── D1 multi-desktop (2026-07-23): busy agent + new prompt -> brand-new
    #    desktop; the running agent keeps its own display. ──
    if (session.status in ("running", "starting")
            and incoming_user_text
            and incoming_user_text != "[reconnect]"
            and incoming_user_text != session.user_message):
        session = await asyncio.to_thread(
            get_or_create_session, operator, device_id=device_id, force_new=True)
        print(f"[OPENAI-CU-STREAM] Busy agent -> new desktop {session.session_id[:8]} for {operator}")
        yield {"type": "cu_session", "data": {"session_id": session.session_id,
                                              "device_id": session.device_id,
                                              "new_desktop": True}}

    # ── Queue/reconnect detection (same pattern as Anthropic/Gemini CU) ──
    if session.status in ("running", "starting") and (not session.agent_task or not session.agent_task.done() or session.status == "starting"):
        if incoming_user_text and incoming_user_text != "[reconnect]" and incoming_user_text != session.user_message:
            position = session.enqueue_prompt(incoming_user_text)
            print(f"[OPENAI-CU-STREAM] Queued prompt #{position} for {operator}: {incoming_user_text[:60]}")
            yield {"type": "cu_queued", "data": {"position": position, "prompt": incoming_user_text[:100]}}
            return
        if session.status == "running":
            print(f"[OPENAI-CU-STREAM] Reconnecting to running task for {operator} (step {session.current_step}/{session.total_steps})")
            yield {"type": "cu_step", "data": {"step": session.current_step, "total": session.total_steps, "reconnect": True}}
        else:
            print(f"[OPENAI-CU-STREAM] Task still starting for {operator}")
            yield {"type": "cu_step", "data": {"step": 0, "total": 0, "reconnect": True}}
    elif session.status == "running":
        session.reset_task_state()
        yield {"type": "error", "data": "Previous task ended unexpectedly. Please retry."}
        return
    elif session.status == "complete" and session.final_response and incoming_user_text == session.user_message:
        print(f"[OPENAI-CU-STREAM] Returning completed result for {operator} (reconnect)")
        yield {"type": "done", "data": {"thinking": "", "content": session.final_response}}
        return
    else:
        if session.status == "complete":
            print(f"[OPENAI-CU-STREAM] New message for {operator}, resetting completed session")
            session.reset_task_state()

        user_text = incoming_user_text
        if not user_text:
            yield {"type": "error", "data": "No user message found"}
            return

        session.reset_task_state()
        # Rebind the event queue to the server loop (see fresh_event_queue —
        # a headless run may have left it bound to a dead loop).
        session.fresh_event_queue()
        # NATIVE opt-in contends for the shared display; virtual (default) allocates
        # this session's own display.
        session.native_mode = bool(native_mode)
        session.status = "starting"
        session.user_message = user_text

        # ── Ensure this session's display + Chrome are up (OpenAI CU captures via
        #    session.capture_screenshot_bytes(), which needs the per-session
        #    display). Virtual (default) allocates; native uses the shared display. ──
        if not await session.ensure_browser("about:blank", backend="openai"):
            yield {"type": "error", "data": session.last_error or "Failed to start browser session"}
            return
        if not session.native_mode and (session.display is None
                                        or not session.display.get_env()):
            yield {"type": "error", "data": "Virtual display allocation failed"}
            return

        # ── Build system prompt with fossil context ──
        from Orchestrator.openai_cu.agent_loop import _default_system_prompt
        system_prompt = _default_system_prompt()

        cu_fossil_context, cu_provenance = await asyncio.to_thread(build_cu_context, user_text, operator)
        session.provenance = cu_provenance
        if cu_fossil_context:
            system_prompt += "\n\n" + cu_fossil_context
            print(f"[OPENAI-CU-STREAM] Injected {len(cu_fossil_context)} chars of fossil context")

        # ── Claim the shared physical display ONLY for a NATIVE launch (M9). A
        #    virtual launch drives its own per-session display and never contends. ──
        if session.device_id == "blackbox" and native_mode:
            owner = try_claim("browser", operator, _display_claim_id,
                              session_id=session.session_id)
            if owner is not None:
                yield {"type": "error", "data": f"Cannot start OpenAI CU — {owner.describe()}. Stop it first."}
                return

        # ── Launch background task ──
        session.agent_task = asyncio.create_task(
            _openai_cu_agent_loop(session, operator, user_text, model, system_prompt)
        )
        # Release on the DRIVER TASK's lifecycle, not this consumer generator's
        # (M1-T6 rev2, Hole 3): the task survives client disconnect, so this
        # callback fires only when the driver actually finishes (incl. its
        # auto-dequeue) or on E-stop.
        session.agent_task.add_done_callback(
            lambda _t, _cid=_display_claim_id: release_claim(_cid))
        print(f"[OPENAI-CU-STREAM] Background task launched for {operator} (local desktop)")

    # ── Queue consumer loop ──
    try:
        while True:
            try:
                event = await asyncio.wait_for(session.event_queue.get(), timeout=120)
            except asyncio.TimeoutError:
                yield {"type": "heartbeat", "data": {"step": session.current_step, "total": session.total_steps}}
                continue

            if event is None:
                break

            yield event
    except (asyncio.CancelledError, GeneratorExit):
        # Task keeps running in background and still owns its claim until IT
        # finishes (see the done-callback above).
        print(f"[OPENAI-CU-STREAM] Client disconnected for {operator}, task continues in background")


async def _openai_cu_agent_loop(session, operator: str, user_text: str,
                                 model: str, system_prompt: str):
    """Background agent loop for OpenAI CU. Pushes events to
    session.event_queue, same pattern as _cu_agent_loop()/_gemini_cu_agent_loop().
    """
    from Orchestrator.openai_cu import run_openai_cu_loop

    async def emit(evt):
        try:
            session.event_queue.put_nowait(evt)
        except asyncio.QueueFull:
            pass

    result_text = ""
    try:
        session.status = "running"
        print(f"[OPENAI-CU-BG] Starting agent loop for {operator}: model={model}, prompt={user_text[:80]}")

        async for event in run_openai_cu_loop(session, user_text, model, system_prompt):
            event_type = event.get("type")

            if event_type == "cu_step":
                session.current_step = event["data"]["step"]
                session.total_steps = event["data"]["total"]
            elif event_type == "content":
                result_text += event["data"].get("text", "")
            elif event_type == "done":
                result_text = event["data"].get("content", result_text)
            elif event_type == "error":
                session.status = "error"
                session.error_message = event["data"].get("message", str(event["data"]))

            await emit(event)

        # Save result and sync usage (browser-session shape)
        session.final_response = result_text or "(Agent completed without text response)"
        session.usage = {
            "prompt_tokens": session.total_tokens.get("input", 0),
            "completion_tokens": session.total_tokens.get("output", 0),
        }

        if not session.prompt_queue:
            session.status = "complete"

        await emit({"type": "done", "data": {"thinking": "", "content": session.final_response}})

        # Save to BlackBox history
        await _cu_save_to_blackbox(operator, user_text, result_text, session)

    except asyncio.CancelledError:
        print(f"[OPENAI-CU-BG] Task cancelled (E-stop) for {operator} at step {session.current_step}")
        # C1: cancellation can land between responses.create and the
        # computer_call_output answer — the stored previous_response_id would
        # then 400 every future turn ("No tool output found..."). The wrapper
        # doesn't know the turn-start id, so clear it: the cancelled turn
        # loses chat continuity (acceptable trade for a working session).
        session.openai_previous_response_id = None
        session.status = "stopped"
        session.final_response = result_text or f"[Task stopped at step {session.current_step}]"
        await emit({"type": "cu_stopped", "data": {"step": session.current_step, "reason": "Task cancelled"}})
    except Exception as e:
        import traceback
        traceback.print_exc()
        # C1: same poisoned-continuity guard as the CancelledError path —
        # the driver may have died between create and answer.
        session.openai_previous_response_id = None
        session.status = "error"
        session.error_message = str(e)
        await emit({"type": "error", "data": f"OpenAI CU error: {str(e)}"})
    finally:
        # ── Auto-dequeue ──
        if session.prompt_queue and not session.stop_requested:
            next_prompt = session.dequeue_prompt()
            remaining = len(session.prompt_queue)
            print(f"[OPENAI-CU-BG] Auto-dequeuing next prompt for {operator} ({remaining} remaining)")

            saved_history = session.conversation_history
            session.reset_task_state()
            session.conversation_history = saved_history
            session.status = "running"
            session.user_message = next_prompt

            await emit({"type": "cu_queue_next", "data": {"prompt": next_prompt[:100], "remaining": remaining}})
            await asyncio.sleep(0)

            # Build system prompt for next turn (continuity flows through
            # session.openai_previous_response_id inside the driver)
            from Orchestrator.openai_cu.agent_loop import _default_system_prompt
            next_system = _default_system_prompt()
            cu_fossil_context, cu_provenance = await asyncio.to_thread(build_cu_context, next_prompt, operator)
            session.provenance = cu_provenance
            if cu_fossil_context:
                next_system += "\n\n" + cu_fossil_context

            await _openai_cu_agent_loop(session, operator, next_prompt, model, next_system)
            return  # Recursive call handles its own sentinel

        # Sentinel
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


def _cu_safe_params(data: dict) -> str:
    """Format CU params for logging/display without flooding."""
    safe = {}
    for k, v in data.items():
        if k == "action":
            continue
        if k == "text" and isinstance(v, str) and len(v) > 50:
            safe[k] = v[:50] + "..."
        elif k == "region" and isinstance(v, list):
            safe[k] = v
        elif isinstance(v, (int, float)):
            safe[k] = v
        elif isinstance(v, list) and len(v) <= 4:
            safe[k] = v
        else:
            safe[k] = str(v)[:80] if isinstance(v, str) else v
    return str(safe) if safe else ""


# -----------------------------------------------------------------------------
# Computer Use Background Status
# -----------------------------------------------------------------------------
def resolve_cu_session(operator: str, session_id: str = "", model: str = ""):
    """Shared CU session resolver for /chat/cu-status, /chat/cu-stop, and task
    cancellation (tasks.cancel_task routes CU cancels through this).

    Routes the backend decision through ``resolve_backend`` — it must NEVER
    string-sniff model ids (test_cu_catalog.py:269 pins this). Anthropic AND
    OpenAI share the browser session manager; Google uses the gemini one. Returns
    the resolved ComputerUseSession/GeminiCUSession, or None. ``session_id`` is
    honored by both lookups; empty falls back to the operator's session."""
    session = None
    if session_id:
        # A task-scoped Gemini session lives only in the gemini store, never
        # behind the operator's MRU pointer: resolve it by id first, or a cancel
        # of that task would stop the operator's chat session instead.
        from Orchestrator.gemini_cu.session_manager import get_session_by_id as gemini_by_id
        by_id = gemini_by_id(session_id)
        if by_id is not None and by_id.operator == operator:
            return by_id
    if model and resolve_backend(model) == "google":
        from Orchestrator.gemini_cu.session_manager import get_session as gemini_get_session
        session = gemini_get_session(operator)
    if not session:
        from Orchestrator.browser.session_manager import get_session as browser_get_session
        session = browser_get_session(operator, session_id)
    if not session:
        try:
            from Orchestrator.gemini_cu.session_manager import get_session as gemini_get_session
            session = gemini_get_session(operator)
        except Exception:
            pass
    return session


@app.get("/chat/cu-status")
async def cu_status(request: Request):
    """Return the status of the current Computer Use background task.

    ``session_id`` (optional, multi-desktop 2026-07-23) reports THAT session
    instead of the operator's MRU one — with multiple live desktops the MRU
    fallback can name the wrong agent."""
    operator = request.query_params.get("operator", current_default())
    cu_model = request.query_params.get("model", "")
    session_id = request.query_params.get("session_id", "")

    session = resolve_cu_session(operator, session_id, cu_model)
    if not session:
        return {"status": "idle", "step": 0, "total": 0}

    return {
        "status": session.status,
        "session_id": session.session_id,
        "step": session.current_step,
        "total": session.total_steps,
        "final_response": session.final_response if session.status == "complete" else "",
        "cu_log": getattr(session, 'cu_log', []) if session.status in ("complete", "running") else [],
        "user_message": getattr(session, 'user_message', ''),
        "error_message": session.error_message if session.status == "error" else None,
        "usage": getattr(session, 'usage', {}),
        "queue_length": len(session.prompt_queue)
    }


@app.post("/chat/cu-stop")
async def cu_stop(request: Request):
    """Emergency stop for a running Computer Use task."""
    body = await request.json()
    operator = body.get("operator", "")
    session_id = body.get("session_id", "")
    cu_model = body.get("model", "")

    session = resolve_cu_session(operator, session_id, cu_model)

    if not session or session.status != "running":
        return {"success": False, "error": "No running CU task"}

    session.request_stop()
    print(f"[CU-STOP] Stop requested for {operator} session {session.session_id[:8]}")
    return {"success": True, "message": f"Stop requested for session {session.session_id}"}


@app.post("/chat/cu-new-session")
async def cu_new_session(request: Request):
    """Clear the current CU session and start fresh."""
    body = await request.json()
    operator = body.get("operator", "")

    # Destroy sessions from BOTH managers to ensure clean state
    try:
        from Orchestrator.browser.session_manager import get_operator_session, destroy_session
        old_session = get_operator_session(operator)
        if old_session:
            if old_session.status == "running":
                old_session.request_stop()
                await asyncio.sleep(0.5)
            # Chrome + display quartet teardown waits for seconds: in a thread.
            await asyncio.to_thread(destroy_session, operator)
            print(f"[CU-NEW] Destroyed Anthropic CU session for {operator}")
    except Exception:
        pass

    try:
        from Orchestrator.gemini_cu.session_manager import get_session as gemini_get, destroy_session as gemini_destroy
        gemini_session = gemini_get(operator)
        if gemini_session:
            if gemini_session.status == "running":
                gemini_session.request_stop()
                await asyncio.sleep(0.5)
            await asyncio.to_thread(gemini_destroy, operator)
            print(f"[CU-NEW] Destroyed Gemini CU session for {operator}")
    except Exception:
        pass

    return {"success": True, "message": "Session cleared"}
