#!/usr/bin/env python3
from __future__ import annotations
from dataclasses import asdict, dataclass, field

"""
gemini_agent_routes.py - Gemini CLI agent, WebSocket, session management
Mirrors the Claude Code CLI integration for Gemini CLI
"""

# Standard library imports
import asyncio
import fcntl
import json
import os
import subprocess
import threading
import time
import uuid
from typing import Dict, Any, Optional, List

# External library imports
from fastapi import WebSocket, WebSocketDisconnect

# Local imports
from Orchestrator.agent_context import (
    compose_with_fossils,
    resolve_operator,
    retrieve_for_agent,
)
from Orchestrator.checkpoint import app
from Orchestrator.config import GEMINI_API_KEY, USERS_DEFAULT
from Orchestrator.utils.paths import blackbox_root
from Orchestrator.volume import now_utc_iso


async def _inject_fossil_context_gemini(
    websocket: WebSocket,
    operator_raw: str,
    user_text: str,
    log_prefix: str = "[GEMINI-AGENT]",
) -> tuple[str, dict]:
    """Run shared retrieval, emit provenance to client. Mirrors agent_routes._inject_fossil_context."""
    operator = resolve_operator(operator_raw, log_prefix)
    fossil_text, provenance = retrieve_for_agent(
        user_text=user_text,
        operator=operator,
        log_prefix=log_prefix,
    )
    try:
        await websocket.send_json({"type": "provenance", "data": provenance})
    except Exception:
        pass
    return fossil_text, provenance

# =============================================================================
# Session Model
# =============================================================================

@dataclass
class GeminiAgentSession:
    """Represents a Gemini CLI agent session."""
    session_id: str
    operator: str = ""
    process: Optional[subprocess.Popen] = None
    mode: str = "streaming"  # streaming or interactive
    working_directory: str = ""
    created_at: str = ""
    last_activity: str = ""
    status: str = "idle"  # idle, running, completed, error
    message_count: int = 0
    yolo_mode: bool = True  # Auto-approve all tool calls
    output_buffer: List[str] = field(default_factory=list)
    output_cursor: int = 0
    websocket_clients: List[WebSocket] = field(default_factory=list)
    background_reader: Optional[threading.Thread] = None
    apps: List[Dict] = field(default_factory=list)
    # Snapshot retrieval provenance from build_fossil_context (Plan Task 4)
    provenance: Dict[str, List[str]] = field(default_factory=dict)


# Global storage for Gemini agent sessions
GEMINI_AGENT_SESSIONS: Dict[str, GeminiAgentSession] = {}
gemini_agent_lock = threading.Lock()


# =============================================================================
# Process Creation
# =============================================================================

def get_gemini_path() -> str:
    """Get path to Gemini CLI executable."""
    # Check nvm-installed location first
    nvm_path = os.path.expanduser("~/.nvm/versions/node/v20.19.6/bin/gemini")
    if os.path.exists(nvm_path):
        return nvm_path

    # Try to find via which
    try:
        result = subprocess.run(["which", "gemini"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except:
        pass

    # Fallback to PATH
    return "gemini"


def create_gemini_streaming(
    working_dir: str,
    prompt: str,
    yolo_mode: bool = True,
    continue_session: bool = False,
    resume_session: str = None
) -> subprocess.Popen:
    """Create Gemini CLI process in streaming JSON mode.

    Args:
        working_dir: Working directory for Gemini CLI
        prompt: The prompt to send
        yolo_mode: If True, auto-approve all tool calls (-y flag)
        continue_session: If True, continue the conversation
        resume_session: Session index to resume (e.g., "latest" or number)

    Returns:
        subprocess.Popen with stdout pipe for reading JSON stream
    """
    gemini_path = get_gemini_path()

    # Build command arguments
    cmd = [gemini_path]

    # Output format for streaming
    cmd.extend(["--output-format", "stream-json"])

    # YOLO mode - auto-approve all tool calls
    if yolo_mode:
        cmd.append("-y")
    else:
        cmd.extend(["--approval-mode", "default"])

    # Session continuity
    if resume_session:
        cmd.extend(["--resume", str(resume_session)])
        print(f"[GEMINI-AGENT] Resuming session: {resume_session}")

    # Add the prompt as positional argument
    cmd.append(prompt)

    print(f"[GEMINI-AGENT] Command: {' '.join(cmd[:6])}... (yolo={yolo_mode})")

    # Environment with nvm
    env = os.environ.copy()
    nvm_dir = os.path.expanduser("~/.nvm")
    if os.path.exists(nvm_dir):
        node_path = os.path.expanduser("~/.nvm/versions/node/v20.19.6/bin")
        env["PATH"] = node_path + ":" + env.get("PATH", "")
        env["NVM_DIR"] = nvm_dir

    # Add API key (central config falls back from GEMINI_API_KEY to GOOGLE_API_KEY)
    env["GEMINI_API_KEY"] = GEMINI_API_KEY

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=working_dir,
        env=env,
        start_new_session=True
    )

    # Set stdout to non-blocking
    flags = fcntl.fcntl(process.stdout.fileno(), fcntl.F_GETFL)
    fcntl.fcntl(process.stdout.fileno(), fcntl.F_SETFL, flags | os.O_NONBLOCK)

    print(f"[GEMINI-AGENT] Created Gemini CLI process PID={process.pid} in {working_dir}")
    return process


# =============================================================================
# Background Reader
# =============================================================================

def background_process_reader(session: GeminiAgentSession):
    """Background thread that continuously reads from Gemini CLI process.
    Runs independently of WebSocket connections.
    """
    print(f"[GEMINI-AGENT] Background reader started for session {session.session_id}")

    line_buffer = ""

    try:
        while session.process and session.process.poll() is None:
            try:
                raw_data = session.process.stdout.read(4096)
                if raw_data:
                    line_buffer += raw_data.decode('utf-8', errors='replace')

                    # Process complete lines
                    while '\n' in line_buffer:
                        line, line_buffer = line_buffer.split('\n', 1)
                        line = line.strip()
                        if not line:
                            continue

                        with gemini_agent_lock:
                            session.output_buffer.append(line)
                            session.last_activity = now_utc_iso()

            except (BlockingIOError, IOError):
                pass
            except Exception as e:
                print(f"[GEMINI-AGENT] Background reader error: {e}")
                break

            time.sleep(0.02)

        # Process remaining data
        try:
            remaining = session.process.stdout.read()
            if remaining:
                line_buffer += remaining.decode('utf-8', errors='replace')
                for line in line_buffer.split('\n'):
                    if line.strip():
                        with gemini_agent_lock:
                            session.output_buffer.append(line.strip())
        except:
            pass

    except Exception as e:
        print(f"[GEMINI-AGENT] Background reader fatal error: {e}")
        import traceback
        traceback.print_exc()

    print(f"[GEMINI-AGENT] Background reader ended for session {session.session_id}")
    with gemini_agent_lock:
        session.status = "completed"


# =============================================================================
# Streaming Reader Task
# =============================================================================

async def streaming_reader_task(session: GeminiAgentSession, websocket: WebSocket):
    """Background task that broadcasts buffered output to WebSocket.
    Parses Gemini CLI stream-json events and forwards to frontend.
    """
    print(f"[GEMINI-AGENT] Streaming reader started for session {session.session_id}")

    try:
        while session.process and session.process.poll() is None:
            # Get new output from buffer
            with gemini_agent_lock:
                if session.output_cursor < len(session.output_buffer):
                    new_lines = session.output_buffer[session.output_cursor:]
                    session.output_cursor = len(session.output_buffer)
                else:
                    new_lines = []

            # Process new lines
            for line in new_lines:
                if not line.strip():
                    continue

                try:
                    event = json.loads(line)
                    event_type = event.get("type", "")
                    print(f"[GEMINI-AGENT] Event type: {event_type}")

                    # Handle different event types from Gemini CLI
                    # Skip user/prompt events - these echo the user's input
                    if event_type in ("user", "prompt", "input", "user_message"):
                        # Don't send user's input back - it's already shown
                        continue

                    if event_type == "message":
                        # Message event contains response text
                        # Check if it's from assistant, not user
                        role = event.get("role", "assistant")
                        if role == "user":
                            continue  # Skip user messages
                        content = event.get("content", "")
                        if content:
                            await websocket.send_json({"type": "content", "data": content})

                    elif event_type in ("tool_call", "tool_use"):
                        # Tool call/use event - handle both formats
                        # Gemini CLI uses: tool_name, parameters
                        # Claude uses: name, input
                        tool_name = event.get("tool_name") or event.get("name", "")
                        tool_input = event.get("parameters") or event.get("input", {})
                        if tool_name:
                            await websocket.send_json({
                                "type": "tool_use",
                                "data": {"name": tool_name, "input": tool_input}
                            })

                    elif event_type == "tool_result":
                        # Tool result - check multiple possible fields
                        result = event.get("result") or event.get("output") or event.get("content", "")
                        if result:
                            await websocket.send_json({"type": "tool_result", "data": result})

                    elif event_type == "thinking":
                        # Thinking/reasoning
                        thinking = event.get("content", "")
                        if thinking:
                            await websocket.send_json({"type": "thinking", "data": thinking})

                    elif event_type == "error":
                        # Error event
                        error = event.get("message", event.get("error", "Unknown error"))
                        await websocket.send_json({"type": "error", "data": error})

                    elif event_type == "done" or event_type == "end":
                        # Completion event
                        pass  # Will be handled by process exit

                    elif event_type == "init":
                        # Session initialization event from Gemini CLI
                        model = event.get("model", "unknown")
                        cli_session = event.get("session_id", "")
                        await websocket.send_json({
                            "type": "info",
                            "data": f"Gemini session started: {model}"
                        })
                        print(f"[GEMINI-AGENT] Init event - model: {model}, session: {cli_session[:8] if cli_session else 'N/A'}...")

                    elif event_type in ("system", "result", "progress", "status"):
                        # System/status events - log but don't necessarily display
                        msg = event.get("message") or event.get("content") or event.get("status", "")
                        if msg:
                            print(f"[GEMINI-AGENT] {event_type}: {msg}")

                    else:
                        # Unknown event type - try to extract useful data
                        if "text" in event:
                            await websocket.send_json({"type": "content", "data": event["text"]})
                        elif "content" in event:
                            await websocket.send_json({"type": "content", "data": event["content"]})
                        elif "message" in event:
                            await websocket.send_json({"type": "content", "data": str(event["message"])})
                        else:
                            # Send raw for debugging
                            print(f"[GEMINI-AGENT] Unknown event: {event}")

                except json.JSONDecodeError:
                    # Not JSON, send as raw output
                    await websocket.send_json({"type": "raw", "data": line})
                except Exception as e:
                    print(f"[GEMINI-AGENT] Error processing line: {e}")

            await asyncio.sleep(0.02)

        # Process completed - CRITICAL: Drain remaining buffer before marking complete
        # The background reader may have captured more output after the last loop iteration
        print(f"[GEMINI-AGENT] Process exited, draining remaining buffer...")

        # Give background reader a moment to capture any final output
        await asyncio.sleep(0.1)

        # Drain any remaining buffered output
        with gemini_agent_lock:
            if session.output_cursor < len(session.output_buffer):
                remaining_lines = session.output_buffer[session.output_cursor:]
                session.output_cursor = len(session.output_buffer)
            else:
                remaining_lines = []

        print(f"[GEMINI-AGENT] Found {len(remaining_lines)} remaining lines to process")

        # Process remaining lines (same logic as above)
        for line in remaining_lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                event_type = event.get("type", "")
                print(f"[GEMINI-AGENT] Final drain - Event type: {event_type}")

                if event_type in ("user", "prompt", "input", "user_message"):
                    continue

                if event_type == "message":
                    role = event.get("role", "assistant")
                    if role == "user":
                        continue
                    content = event.get("content", "")
                    if content:
                        await websocket.send_json({"type": "content", "data": content})

                elif event_type in ("tool_call", "tool_use"):
                    tool_name = event.get("tool_name") or event.get("name", "")
                    tool_input = event.get("parameters") or event.get("input", {})
                    if tool_name:
                        await websocket.send_json({
                            "type": "tool_use",
                            "data": {"name": tool_name, "input": tool_input}
                        })

                elif event_type == "tool_result":
                    result = event.get("result") or event.get("output") or event.get("content", "")
                    if result:
                        await websocket.send_json({"type": "tool_result", "data": result})

                elif event_type == "thinking":
                    thinking = event.get("content", "")
                    if thinking:
                        await websocket.send_json({"type": "thinking", "data": thinking})

                elif event_type == "result":
                    # Result event contains stats - send as info
                    stats = event.get("stats", {})
                    status = event.get("status", "")
                    if status == "success":
                        print(f"[GEMINI-AGENT] Result stats: {stats}")

                elif event_type == "error":
                    error = event.get("message", event.get("error", "Unknown error"))
                    await websocket.send_json({"type": "error", "data": error})

                else:
                    # Try to extract content from unknown events
                    if "text" in event:
                        await websocket.send_json({"type": "content", "data": event["text"]})
                    elif "content" in event and event.get("role") != "user":
                        await websocket.send_json({"type": "content", "data": event["content"]})

            except json.JSONDecodeError:
                await websocket.send_json({"type": "raw", "data": line})
            except Exception as e:
                print(f"[GEMINI-AGENT] Error processing remaining line: {e}")

        with gemini_agent_lock:
            session.status = "completed"
        await websocket.send_json({"type": "completed", "data": None})
        print(f"[GEMINI-AGENT] Session completed, sent completion signal")

    except Exception as e:
        print(f"[GEMINI-AGENT] Streaming reader error: {e}")
        import traceback
        traceback.print_exc()


# =============================================================================
# WebSocket Endpoint
# =============================================================================

@app.websocket("/ws/gemini-agent/{session_id}")
async def gemini_agent_websocket(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for Gemini CLI streaming.

    Message types:
    - prompt: Start new prompt
    - input: Send follow-up input
    - end_session: End current session
    - reconnect: Reconnect to existing session
    - ping: Keep-alive
    """
    print(f"[GEMINI-AGENT] WebSocket connection for session: {session_id}")
    await websocket.accept()

    reader_task = None
    session = None

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")
            print(f"[GEMINI-AGENT] Received: {msg_type}")

            if msg_type == "prompt":
                operator = data.get("operator", USERS_DEFAULT)
                default_dir = str(blackbox_root())
                working_dir = data.get("working_dir") or default_dir
                prompt_text = data.get("text", "")
                yolo_mode = data.get("yolo_mode", True)
                # Frontend tells us if user has a persisted session to resume
                frontend_wants_resume = data.get("resume_session", False)

                if not prompt_text:
                    await websocket.send_json({"type": "error", "data": "No prompt provided"})
                    continue

                # Plan Task 4: inject fossil retrieval at session start.
                # Always emit a provenance event so the Android UI has a
                # deterministic signal — even for unknown operators.
                fossil_text, provenance = await _inject_fossil_context_gemini(
                    websocket,
                    operator_raw=operator,
                    user_text=prompt_text,
                    log_prefix="[GEMINI-AGENT]",
                )

                # Get or create session
                with gemini_agent_lock:
                    session = GEMINI_AGENT_SESSIONS.get(session_id)

                # Wait for previous process to complete
                if session and session.process and session.process.poll() is None:
                    print(f"[GEMINI-AGENT] Waiting for previous process...")
                    for _ in range(50):
                        if session.process.poll() is not None:
                            break
                        await asyncio.sleep(0.1)
                    if session.process.poll() is None:
                        session.process.terminate()

                # Determine if we should continue/resume:
                # - Backend has existing session with messages, OR
                # - Frontend explicitly wants to resume (has persisted session)
                backend_has_session = session is not None and session.message_count > 0
                should_resume = backend_has_session or frontend_wants_resume

                print(f"[GEMINI-AGENT] Resume decision: backend_session={backend_has_session}, frontend_wants={frontend_wants_resume}, should_resume={should_resume}")

                # Plan Task 4: only prepend fossil context on brand-new sessions.
                # Resume/continue paths already carry prior conversation turns
                # via the CLI's own session flags.
                effective_prompt = prompt_text
                if not should_resume:
                    effective_prompt = compose_with_fossils(fossil_text, prompt_text)

                # Create streaming process
                process = create_gemini_streaming(
                    working_dir,
                    effective_prompt,
                    yolo_mode=yolo_mode,
                    continue_session=should_resume,
                    resume_session="latest" if should_resume else None
                )

                if session is None:
                    session = GeminiAgentSession(
                        session_id=session_id,
                        operator=operator,
                        process=process,
                        working_directory=working_dir,
                        created_at=now_utc_iso(),
                        last_activity=now_utc_iso(),
                        status="running",
                        message_count=1,
                        yolo_mode=yolo_mode,
                        provenance=provenance,
                    )
                else:
                    session.process = process
                    session.last_activity = now_utc_iso()
                    session.status = "running"
                    session.message_count += 1
                    session.yolo_mode = yolo_mode
                    session.provenance = provenance

                with gemini_agent_lock:
                    GEMINI_AGENT_SESSIONS[session_id] = session

                print(f"[GEMINI-AGENT] {'Resuming' if should_resume else 'Created new'} session (msg #{session.message_count})")

                # Start background reader
                if session.background_reader is None or not session.background_reader.is_alive():
                    session.background_reader = threading.Thread(
                        target=background_process_reader,
                        args=(session,),
                        daemon=True,
                        name=f"gemini-reader-{session_id[:8]}"
                    )
                    session.background_reader.start()

                # Add websocket to clients
                with gemini_agent_lock:
                    if websocket not in session.websocket_clients:
                        session.websocket_clients.append(websocket)

                # Start streaming reader
                if reader_task is not None and not reader_task.done():
                    reader_task.cancel()
                reader_task = asyncio.create_task(streaming_reader_task(session, websocket))

            elif msg_type == "input":
                # Handle follow-up input
                input_text = data.get("text", "").strip()
                if not input_text:
                    continue

                with gemini_agent_lock:
                    session = GEMINI_AGENT_SESSIONS.get(session_id)

                if session is None:
                    await websocket.send_json({"type": "error", "data": "No active session"})
                    continue

                # Wait for previous process
                if session.process and session.process.poll() is None:
                    for _ in range(50):
                        if session.process.poll() is not None:
                            break
                        await asyncio.sleep(0.1)
                    if session.process.poll() is None:
                        session.process.terminate()

                # Create continuation process
                process = create_gemini_streaming(
                    session.working_directory,
                    input_text,
                    yolo_mode=session.yolo_mode,
                    resume_session="latest"
                )

                session.process = process
                session.last_activity = now_utc_iso()
                session.status = "running"
                session.message_count += 1

                # Start background reader
                if session.background_reader is None or not session.background_reader.is_alive():
                    session.background_reader = threading.Thread(
                        target=background_process_reader,
                        args=(session,),
                        daemon=True
                    )
                    session.background_reader.start()

                # Start streaming reader
                if reader_task is not None and not reader_task.done():
                    reader_task.cancel()
                reader_task = asyncio.create_task(streaming_reader_task(session, websocket))

            elif msg_type == "end_session":
                with gemini_agent_lock:
                    session = GEMINI_AGENT_SESSIONS.get(session_id)
                    if session:
                        if session.process and session.process.poll() is None:
                            session.process.terminate()
                        session.status = "completed"
                        session.message_count = 0
                        del GEMINI_AGENT_SESSIONS[session_id]
                        session = None

                await websocket.send_json({"type": "session_ended", "data": "Session ended"})

            elif msg_type == "reconnect":
                operator = data.get("operator", USERS_DEFAULT)

                with gemini_agent_lock:
                    session = GEMINI_AGENT_SESSIONS.get(session_id)

                if session is None:
                    await websocket.send_json({"type": "error", "data": "No session found"})
                    continue

                with gemini_agent_lock:
                    if websocket not in session.websocket_clients:
                        session.websocket_clients.append(websocket)

                if session.process and session.process.poll() is None:
                    if reader_task is not None and not reader_task.done():
                        reader_task.cancel()
                    reader_task = asyncio.create_task(streaming_reader_task(session, websocket))

                    await websocket.send_json({
                        "type": "info",
                        "data": f"Reconnected to session (msg #{session.message_count})"
                    })
                else:
                    await websocket.send_json({"type": "completed", "data": None})

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        print(f"[GEMINI-AGENT] WebSocket disconnected: {session_id}")
        with gemini_agent_lock:
            session = GEMINI_AGENT_SESSIONS.get(session_id)
            if session and websocket in session.websocket_clients:
                session.websocket_clients.remove(websocket)
    except Exception as e:
        print(f"[GEMINI-AGENT] WebSocket error: {e}")
        import traceback
        traceback.print_exc()


# =============================================================================
# HTTP Endpoints
# =============================================================================

@app.get("/gemini-agent/status")
async def gemini_agent_status():
    """Get Gemini CLI agent status."""
    gemini_path = get_gemini_path()
    gemini_exists = os.path.exists(gemini_path) if gemini_path != "gemini" else False

    # Check if gemini is available via which
    if not gemini_exists:
        try:
            result = subprocess.run(["which", "gemini"], capture_output=True, text=True,
                                   env={**os.environ, "PATH": os.path.expanduser("~/.nvm/versions/node/v20.19.6/bin") + ":" + os.environ.get("PATH", "")})
            gemini_exists = result.returncode == 0
        except:
            pass

    return {
        "available": gemini_exists,
        "gemini_path": gemini_path,
        "active_sessions": len(GEMINI_AGENT_SESSIONS),
        "api_key_configured": bool(GEMINI_API_KEY)
    }


@app.get("/gemini-agent/session/{operator}")
async def get_gemini_session_for_operator(operator: str):
    """Get existing Gemini agent session for an operator."""
    with gemini_agent_lock:
        for session_id, session in GEMINI_AGENT_SESSIONS.items():
            if session.operator == operator and session.status != "completed":
                return {
                    "session": {
                        "session_id": session.session_id,
                        "status": session.status,
                        "working_directory": session.working_directory,
                        "created_at": session.created_at,
                        "last_activity": session.last_activity,
                        "message_count": session.message_count,
                        "yolo_mode": session.yolo_mode
                    },
                    "output_buffer": session.output_buffer
                }
    return {"session": None}


@app.delete("/gemini-agent/session/{session_id}")
async def terminate_gemini_session(session_id: str):
    """Terminate a Gemini agent session."""
    with gemini_agent_lock:
        session = GEMINI_AGENT_SESSIONS.get(session_id)
        if session:
            if session.process and session.process.poll() is None:
                session.process.terminate()
                try:
                    session.process.wait(timeout=5)
                except:
                    session.process.kill()

            del GEMINI_AGENT_SESSIONS[session_id]
            return {"status": "terminated"}

    return {"status": "not_found"}
